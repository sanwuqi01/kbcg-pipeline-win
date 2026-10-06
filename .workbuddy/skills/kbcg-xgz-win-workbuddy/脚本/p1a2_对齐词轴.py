#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工序①a2 对齐词轴（2026-08-06 接入 Qwen3 ForcedAligner · 词时间从"whisper 推断"升级为"帧级强制对齐"）
用法：p1a2_对齐词轴.py <工作目录>
跑在 mlx-qwen3-asr 的独立 venv 下（见 shebang），不是 mlx-whisper 的。

═══ 为什么有这个模块（三类系统性病灶，2026-08-05/06 C2637 实证）═══
① 段首词轴外残声：whisper 漏转的「呃/嗯」贴在人声块头，p1 段头取块起点，
   残声原样带进成片（全片扫出 74 处候选）。whisper 词轴里根本没有这个音，
   谁也内收不到它。强制对齐后首词 ws=真实起音（实测「然」16.84 vs whisper 16.60，
   多裹的正是漏转的「呃」16.63-16.84），p1 改「段头=max(块起,首词ws)」自然排除。
② fix 补字挂邻词时间：keep.json 补的字（如漏转的「呢」）在 whisper 词轴上没有时间，
   p1 整并到首词后时间还是邻词的——字幕音画错位。对齐器把补字后的文本重新贴回
   音频，补字拿到真实时间（实测「呢」17.48-17.88）。
③ 词轴插值偏 ±10 帧：whisper 块内词时间是连续分配的推断值，p3 声学吸附只能救
   切点附近。对齐后全部词时间帧级可信。

═══ 契约（下游全链依赖，改前必读）═══
· 词的下标 i、文本 t、顺序【绝对不变】——keep.json 87 条 drop / 62+ 条 fix 全按
  下标引用，下标漂移 = 全毁。本脚本只改时间字段。
· 更新 ws/we（首字 start / 末字 end）。vs/ve 同步 = ws/we：对齐时间本身就是
  真实发音时间，p1a 那套「块首锚块起」的校正（治 whisper 块首虚早 0.2s）在
  对齐时间上不但不需要，还会把块头残声重新裹回首词（病灶①复活）。
  下游 p1 粗定位 / segments words s,e / p3 分卡 / p1b A0 读的都是 vs/ve，
  只改 ws/we 不改 vs/ve = 对齐白做。
· drop 的词绝不参与对齐：其中可能正是 ASR 幻觉，把不存在的字送进 aligner 会扭曲
  两侧保留词。drop 保留 p1a 原始时间并标记 alignment_skipped=drop。
· fix 先应用再对齐：等长逐字替换 / 不等长整并到首词（与 p1_建段.py 同一套规则，
  两处必须同步改）；整并的首词按替换后文本长度消耗对齐字符，区间内其余词
  消耗 0 个字 → 零时长空字词（ws==we，挂在前词末尾）。
· blk 按新词时间与 voice_blocks 最大重叠重判；orphan/weak 保持原值
  （幻觉词判定是 p1a 用声学证据做的，对齐器对不存在的音也会硬塞时间，不可信）。
· 其余顶层字段（fps/voice_blocks/voiced_spans/pause_tier1/2 …）原样保留。

═══ 断言（任一违反即报错退出，不写文件）═══
· 逐窗：对齐输出字符拼接（去空白）必须逐字等于窗文本
· 逐窗：窗内词新旧 ws 偏移中位 ≤0.5s（防整窗错位——错位是系统性的，中位必炸）
· 全片：词时间单调不减；每词 we≥ws（空字词 we==ws 容忍）
炸了先缩窗（240→120s）自动重试一轮，仍炸则非零退出交人处理，不许硬写。
"""
import sys, os, json, re, shutil, wave, hashlib
import numpy as np

SR = 16000
WIN_MAX_DEFAULT = 240.0        # 单窗上限（模型上限 5min，留余量）
WIN_MAX_RETRY = 120.0          # 断言炸掉后的缩窗重试值
SPARSE_GAP_MAX = 15.0          # 无显式 drop 时，两段保留文本相隔更久也必须拆任务
POST_VAD_ENDPOINT_MOVE_MAX = 0.65
# DSCF2925 用户补回的岛首“既”：旧 ASR 早 0.548s，强制对齐+独立
# VAD 同时落在 164.608s，用户 FCP 刀口为 164.531s。这是旧词轴错误被
# 两套声学证据纠正，不是整窗错位。预钳制端点不再提前退出：整窗错位
# 由中位偏移硬闸阻断，单个端点必须先进入独立 VAD，随后再以 0.65s 验收。

WORK = sys.argv[1].rstrip('/')

track = json.load(open(f'{WORK}/word_track.json'))
words = track['words']
kp = json.load(open(f'{WORK}/keep.json'))
cfg = json.load(open(f'{WORK}/config.json'))
VB = track['voice_blocks']

# ---- 音频：只认 p1a 与素材指纹绑定的 _vad16k.wav ----
def _sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        left = limit
        while True:
            size = 1 << 20 if left is None else min(1 << 20, left)
            if size <= 0:
                break
            b = f.read(size)
            if not b:
                break
            h.update(b)
            if left is not None:
                left -= len(b)
    return h.hexdigest()


def _file_identity(path):
    path = os.path.realpath(path)
    st = os.stat(path)
    return {
        'path': path,
        'bytes': st.st_size,
        'mtime_ns': st.st_mtime_ns,
        'sha256_head64m': _sha256(path, 64 << 20),
    }


apath = f'{WORK}/_vad16k.wav'
meta_path = f'{WORK}/_vad16k.meta.json'
if not os.path.isfile(apath) or not os.path.isfile(meta_path):
    sys.exit('⛔ p1a2: 缺 _vad16k.wav 或指纹侧车；必须先重跑 p1a，不许猜 audio.wav')
meta = json.load(open(meta_path))
source_identity = _file_identity(cfg['proxy'])
track_cache = track.get('audio_cache') or {}
if meta.get('source') != source_identity or track_cache.get('source') != source_identity:
    sys.exit('⛔ p1a2: 音频缓存绑定的素材与 config.proxy 不一致；请重跑 p1a')
wav_hash = _sha256(apath)
if meta.get('wav_sha256') != wav_hash or track_cache.get('wav_sha256') != wav_hash:
    sys.exit('⛔ p1a2: _vad16k.wav 内容与词轴/侧车指纹不一致；拒绝用陈旧音频对齐')
with wave.open(apath, 'rb') as wv:
    assert wv.getframerate() == SR and wv.getnchannels() == 1, \
        f'{apath} 不是 16k 单声道（{wv.getframerate()}Hz/{wv.getnchannels()}ch）'
    AUDIO = np.frombuffer(wv.readframes(wv.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
DUR = len(AUDIO) / SR

# ---- 1) 应用 fix 得到每词对齐文本（规则与 p1_建段.py 的 fix 段逐字同源）----
align_txt = [w['t'] for w in words]
for it in kp.get('fix', []):
    i0, i1, new = it[0], it[1], it[2]
    old = ''.join(words[i]['t'] for i in range(i0, i1 + 1))
    if len(new) == len(old):                    # 等长：逐字替换
        k = 0
        for i in range(i0, i1 + 1):
            L = len(words[i]['t'])
            align_txt[i] = new[k:k + L]; k += L
    else:                                        # 不等长：整并到首词，其余置空
        align_txt[i0] = new
        for i in range(i0 + 1, i1 + 1):
            align_txt[i] = ''

# 对齐器要无标点纯文本：每词剥掉非声学字符（标点/空白），只留 CJK/拉丁/数字。
# 剥的是【对齐消耗文本】，词轴里的显示文本 t 一个字不动。
_KEEP_CH = re.compile(r'[0-9A-Za-z㐀-䶿一-鿿぀-ヿ]+')
cons_txt = [''.join(_KEEP_CH.findall(t)) for t in align_txt]

# 内容决策明确 drop 的词可能是 ASR 幻觉，禁止把其文本硬塞进真实音频。
drop_indices = set()
for item in kp.get('drop', []):
    i0, i1 = item[0], item[1]
    if not (0 <= i0 <= i1 < len(words)):
        sys.exit(f'⛔ p1a2: keep.drop 越界 [{i0}, {i1}] / {len(words)} 词')
    drop_indices.update(range(i0, i1 + 1))
for i in drop_indices:
    cons_txt[i] = ''

# ---- 2) VAD 分窗：窗边界落在块间静音处（后半程里挑最大间隙）----
def make_windows(win_max):
    wins, cur = [], 0.0
    while True:
        if DUR - cur <= win_max:
            wins.append((cur, DUR)); break
        cands = []                              # (间隙长, 间隙中点)
        for i in range(len(VB) - 1):
            g0, g1 = VB[i][1], VB[i + 1][0]
            mid = (g0 + g1) / 2
            if mid <= cur + 1.0:
                continue
            if mid > cur + win_max:
                break
            cands.append((g1 - g0, mid))
        if not cands:
            sys.exit(f'⛔ p1a2: [{cur:.0f}s, {cur+win_max:.0f}s] 内找不到任何块间静音可切窗')
        tail = [c for c in cands if c[1] > cur + win_max * 0.5]
        best = max(tail if tail else cands, key=lambda c: c[0])
        wins.append((cur, best[1])); cur = best[1]
    return wins

def assign_words(wins):
    """按旧词时间把词【按序连续】分配进窗，返回每窗 (i0, i1)（含端点）"""
    spans, wi = [], 0
    for k, (lo, hi) in enumerate(wins):
        i0 = wi
        while wi < len(words):
            mid = (words[wi]['vs'] + words[wi]['ve']) / 2
            if mid <= hi or k == len(wins) - 1:
                wi += 1
            else:
                break
        spans.append((i0, wi - 1))
    assert wi == len(words), f'分窗漏词：{wi}/{len(words)}'
    return spans


def make_alignment_jobs(wins, spans):
    """把全片 VAD 窗裁成只围绕保留文本的对齐任务。

    keep.drop 可能删掉一句里的口头语，也可能删掉整段乃至数十分钟。ForcedAligner
    假设任务音频和任务文本大体同源，所以任何显式 drop 边界都必须拆任务；否则
    被删声音仍在音频里，会挤压两侧时码。任务音频严格取首尾保留词的原 ws/we，
    不向被删语音扩上下文。无显式 drop 但声学间隙过大时也拆开。
    """
    jobs = []
    for window_index, ((base_lo, base_hi), (span_i0, span_i1)) in enumerate(zip(wins, spans)):
        if span_i1 < span_i0:
            continue
        active = [i for i in range(span_i0, span_i1 + 1) if cons_txt[i]]
        if not active:
            continue
        groups = [[active[0]]]
        for i in active[1:]:
            prev = groups[-1][-1]
            crosses_drop = any(x in drop_indices for x in range(prev + 1, i))
            if crosses_drop or words[i]['ws'] - words[prev]['we'] > SPARSE_GAP_MAX:
                groups.append([i])
            else:
                groups[-1].append(i)
        for group in groups:
            i0, i1 = group[0], group[-1]
            crossed = [i for i in range(i0, i1 + 1) if i in drop_indices]
            if crossed:
                raise AssertionError(
                    f'窗{window_index} 保留词[{i0}-{i1}] 内部仍含 drop {crossed[:12]}；'
                    '显式 drop 边界必须拆成独立对齐任务'
                )
            lo = max(base_lo, words[i0]['ws'])
            hi = min(base_hi, words[i1]['we'])
            if hi <= lo:
                raise AssertionError(
                    f'窗{window_index} 保留词[{i0}-{i1}] 裁窗非法：{lo:.3f}-{hi:.3f}s'
                )
            jobs.append((window_index, lo, hi, i0, i1))
    return jobs

# ---- 3/4) 逐窗对齐 + 字→词回填 ----
def run_pass(win_max, aligner):
    wins = make_windows(win_max)
    spans = assign_words(wins)
    jobs = make_alignment_jobs(wins, spans)
    print(f'对齐: {len(wins)} 个全片窗 → {len(jobs)} 个保留文本任务（≤{win_max:.0f}s）'
          ' · 模型 Qwen3-ForcedAligner-0.6B')
    new_ws = [None] * len(words)
    new_we = [None] * len(words)
    new_chars = [None] * len(words)
    # 不进 aligner 的词也必须拿到确定结果：drop 保留原时间；纯标点/整并余词先给
    # 零长锚点，若位于对齐任务内会在逐词回填时改挂到前一保留词末尾。
    for i in range(len(words)):
        if i in drop_indices:
            new_ws[i], new_we[i] = words[i]['ws'], words[i]['we']
            new_chars[i] = []
        elif not cons_txt[i]:
            anchor = round(words[i]['vs'], 3)
            new_ws[i] = new_we[i] = anchor
            new_chars[i] = []
    for job_index, (window_index, lo, hi, i0, i1) in enumerate(jobs):
        text = ''.join(cons_txt[i] for i in range(i0, i1 + 1))
        assert text, f'任务{job_index} 不应为空文本'
        seg = AUDIO[int(lo * SR):int(hi * SR)]
        # Qwen 的 CJK 会天然逐字 tokenize，但连续拉丁/数字会合成一个 word-level 单元。
        # 显式按字符加空格，强制所有语种字符都各自产生真实时间戳；空格不参与 got 对账。
        aws = aligner.align(seg, ' '.join(text), 'Chinese')
        # 对齐单元 → 字符级时间流。多字符单元只有整体边界，内部没有真实字时；
        # 旧实现曾在线性均摊后冒充“精确字级”，现一律失败，禁止隐形插值回退。
        chars, ct = [], []
        for aw in aws:
            t_ = ''.join(aw.text.split())
            if not t_:
                continue
            if len(t_) != 1:
                raise AssertionError(
                    f'任务{job_index}/原窗{window_index} 对齐器返回多字符单元「{t_}」'
                    f'[{aw.start_time:.3f}-{aw.end_time:.3f}]；'
                    '内部没有真实字级边界，禁止线性均摊。请切换/配置逐字符输出的 aligner'
                )
            chars.append(t_)
            ct.append((aw.start_time, aw.end_time))
        got = ''.join(chars)
        if got != text:
            # 报错前给足上下文：第一处不一致的前后 20 字
            p = next((x for x in range(min(len(got), len(text))) if got[x] != text[x]),
                     min(len(got), len(text)))
            raise AssertionError(
                f'任务{job_index}/原窗{window_index} [{lo:.0f}-{hi:.0f}s] '
                f'对齐字数/文本不符：输出 {len(got)} vs 输入 {len(text)}\n'
                f'   首异 @{p}：输出「{got[max(0,p-20):p+20]}」\n'
                f'            输入「{text[max(0,p-20):p+20]}」')
        # 逐词消耗字符
        pos, prev_end = 0, lo
        moves = []
        for i in range(i0, i1 + 1):
            n = len(cons_txt[i])
            if i in drop_indices:
                # 不参与 aligner，也不让它占据/推动保留词的字符流。
                new_ws[i], new_we[i] = words[i]['ws'], words[i]['we']
                new_chars[i] = []
                continue
            if n == 0:                          # 空字词（fix 整并余词/纯标点）：挂前词末尾
                new_ws[i] = new_we[i] = round(prev_end, 3)
                new_chars[i] = []
                continue
            s_ = lo + ct[pos][0]
            e_ = lo + ct[pos + n - 1][1]
            new_ws[i], new_we[i] = round(s_, 3), round(e_, 3)
            new_chars[i] = [
                {'c': ch, 's': round(lo + ct[pos + j][0], 3),
                 'e': round(lo + ct[pos + j][1], 3)}
                for j, ch in enumerate(cons_txt[i])
            ]
            prev_end = e_
            pos += n
            moves.append(abs(s_ - words[i]['ws']))
        assert pos == len(text), f'任务{job_index}/原窗{window_index} 回填消耗不平：{pos}/{len(text)}'
        moves.sort()
        med = moves[len(moves) // 2] if moves else 0.0
        if med > 0.5:
            raise AssertionError(
                f'任务{job_index}/原窗{window_index} [{lo:.0f}-{hi:.0f}s] '
                f'新旧 ws 中位偏移 {med:.2f}s > 0.5s（疑窗错位）'
            )
        first_active = next(i for i in range(i0, i1 + 1) if cons_txt[i])
        last_active = next(i for i in range(i1, i0 - 1, -1) if cons_txt[i])
        head_move = abs(new_ws[first_active] - words[first_active]['ws'])
        tail_move = abs(new_we[last_active] - words[last_active]['we'])
        print(f'   任务{job_index}/原窗{window_index}: [{lo:7.1f}-{hi:7.1f}s] '
              f'词[{i0}-{i1}] {len(text)} 字 · ws 中位移 {med*1000:.0f}ms'
              f' · 端点 {head_move*1000:.0f}/{tail_move*1000:.0f}ms')
    return new_ws, new_we, new_chars, jobs

# ---- 对齐器实例化（平台分派）----
# macOS 原路径：mlx_qwen3_asr.ForcedAligner —— Apple Metal 专属，Windows 无任何轮子。
# Windows 路径：脚本_win/aligner_win.py 的 Qwen3ForcedAligner，用**同款权重**
#   Qwen3-ForcedAligner-0.6B（官方 PyTorch/Transformers 实现）复刻同一个 align() 契约：
#   align(audio16k_float32, ' '.join(text), 'Chinese') → 每项单字符 {text, start_time, end_time}。
# 两者在小字符级时间的产出上已做 D2 闸门验收（四项判定 + silero-vad 声学旁证，
# 见 03_Windows迁移分析/02_实测记录.md 的 D2 节），故此处只做分派、不改任何对齐语义。
try:
    from mlx_qwen3_asr import ForcedAligner
except ImportError:
    _SCRIPTS_WIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '脚本_win')
    if os.path.isdir(_SCRIPTS_WIN) and _SCRIPTS_WIN not in sys.path:
        sys.path.insert(0, _SCRIPTS_WIN)
    from aligner_win import Qwen3ForcedAligner as ForcedAligner
al = ForcedAligner()
try:
    new_ws, new_we, new_chars, alignment_jobs = run_pass(WIN_MAX_DEFAULT, al)
except AssertionError as e:
    print(f'⚠ 240s 窗对齐断言失败，缩窗 {WIN_MAX_RETRY:.0f}s 重试一轮：\n   {e}')
    new_ws, new_we, new_chars, alignment_jobs = run_pass(WIN_MAX_RETRY, al)  # 再炸就非零退出

# ---- 4b) VAD 主块钳制（2026-08-06 C2637 首跑实证，缺它 p1b 炸 11 处 A1/A3）----
# 对齐器的系统性短板：把词头/词尾的静音、呼吸裹进词区间——实测「怎么」标 1.84s
# 横跨 0.80s 的块间隙（真实发音只在前块内），「案」尾多裹 0.55s 静音跨进下一块。
# 后果：① 词骑在块间隙上，停顿候选发现不到间隙，段内残留 0.5~0.8s 死静音（A3）；
#       ② 词头标进块前静音，段头跟着提前，带静音进成片（A1）。
# 修法与旧架构同源：p1a 的 vs/ve 本来就锚块边界（块首词 vs=块起、块末词 ve=块止）。
# 规则：词的时间不许越出它的【主块】（重叠最大的 VAD 块）——
#   ws=max(ws, 主块起)：只钳「早于主块起」的越界；残声治理靠 ws【晚于】块起，不受影响。
#   we=min(we, 主块止)：块外弱音拖尾按块止裁，与旧架构块末词 ve=块止 完全同口径。
# 与任何主块都不相交的词（纯弱音/幻觉）保持对齐时间原样——内容红线，A1/A2 对
# 纯弱音段本来就有「弱音段」豁免口径。
n_ch = n_ct = 0
for i in range(len(words)):
    if not cons_txt[i] or new_we[i] <= new_ws[i]:
        continue
    s_, e_ = new_ws[i], new_we[i]
    best, bx, by = 0.0, None, None
    for x, y in VB:
        ov = min(y, e_) - max(x, s_)
        if ov > best:
            best, bx, by = ov, x, y
    if bx is None:
        continue
    if s_ < bx - 1e-6:
        new_ws[i] = round(bx, 3); n_ch += 1
    if e_ > by + 1e-6:
        new_we[i] = round(by, 3); n_ct += 1
    if new_we[i] < new_ws[i]:
        new_we[i] = new_ws[i]
print(f'      VAD 主块钳制: 头 {n_ch} / 尾 {n_ct}（对齐器裹进词区间的块外静音已裁）')

# 钳制是对齐后的第二次时间变换；不能只在钳制前查端点。
# 逐岛再验一次，防止少数首尾词被大幅内收却被全片中位数掩盖。
for job_index, (window_index, lo, hi, i0, i1) in enumerate(alignment_jobs):
    first_active = next(i for i in range(i0, i1 + 1) if cons_txt[i])
    last_active = next(i for i in range(i1, i0 - 1, -1) if cons_txt[i])
    head_move = abs(new_ws[first_active] - words[first_active]['ws'])
    tail_move = abs(new_we[last_active] - words[last_active]['we'])
    if head_move > POST_VAD_ENDPOINT_MOVE_MAX or tail_move > POST_VAD_ENDPOINT_MOVE_MAX:
        raise AssertionError(
            f'任务{job_index}/原窗{window_index} VAD 钳制后端点偏移过大：'
            f'首词 {head_move:.2f}s / 尾词 {tail_move:.2f}s'
            f'（VAD 钳制后阈值 {POST_VAD_ENDPOINT_MOVE_MAX:.2f}s）'
        )

# 字级时间同步钳制到最终词边界。p3 会直接消费它，不再整段线性猜时间。
for i, rows in enumerate(new_chars):
    if rows is None:
        continue
    prev = new_ws[i]
    for row in rows:
        row['s'] = round(min(max(row['s'], prev, new_ws[i]), new_we[i]), 3)
        row['e'] = round(min(max(row['e'], row['s']), new_we[i]), 3)
        prev = row['e']

# ---- 5) 全片合理性断言 ----
assert all(x is not None for x in new_ws), 'p1a2: 有词没拿到对齐时间'
assert all(x is not None for x in new_chars), 'p1a2: 有词没拿到字级对齐时间'
_aligned_indices = [i for i in range(len(words)) if i not in drop_indices and cons_txt[i]]
if not _aligned_indices:
    sys.exit('⛔ p1a2: drop/fix 后没有任何可强制对齐的保留文本')
for a, b in zip(_aligned_indices, _aligned_indices[1:]):
    assert new_ws[b] >= new_ws[a] - 1e-6, \
        f'词时间非单调：词{a}({new_ws[a]}) → 词{b}({new_ws[b]})'
    assert new_ws[b] >= new_we[a] - 0.005, \
        f'相邻词时间重叠：词{a}({new_ws[a]}-{new_we[a]}) → 词{b}({new_ws[b]}-{new_we[b]})'
for i in range(len(words)):
    assert new_we[i] >= new_ws[i], f'词{i} we<ws：{new_ws[i]}→{new_we[i]}'
    assert ''.join(x['c'] for x in new_chars[i]) == cons_txt[i], \
        f'词{i} 字级文本与对齐文本不符'
    assert all(new_ws[i] <= x['s'] <= x['e'] <= new_we[i] for x in new_chars[i]), \
        f'词{i} 字级时间越出词边界'
_mv = sorted(abs(new_ws[i] - words[i]['ws']) for i in range(len(words)) if cons_txt[i])
med_all, p90_all = _mv[len(_mv) // 2], _mv[int(len(_mv) * 0.9)]
assert med_all <= 0.5, f'全片新旧 ws 中位偏移 {med_all:.2f}s > 0.5s'

# ---- 6) 回写：ws/we 与 vs/ve 同步为对齐时间；blk 重判；orphan/weak 保持 ----
def blk_overlap(s_, e_):
    best, bi = 0.0, -1
    for k, (x, y) in enumerate(VB):
        ov = min(y, e_) - max(x, s_)
        if ov > best:
            best, bi = ov, k
    return bi

n_reblk = 0
for i, w in enumerate(words):
    if i in drop_indices:
        # drop 不写入任何强制对齐推断；保留 p1a 的原始声学字段供留删审计。
        w['align_text'] = ''
        w['char_times'] = []
        w['alignment_skipped'] = 'drop'
        continue
    w['ws'], w['we'] = new_ws[i], new_we[i]
    w['vs'], w['ve'] = new_ws[i], new_we[i]
    w['align_text'] = cons_txt[i]
    w['char_times'] = new_chars[i]
    w.pop('alignment_skipped', None)
    nb = blk_overlap(new_ws[i], new_we[i]) if new_we[i] > new_ws[i] else blk_overlap(new_ws[i] - 0.02, new_ws[i] + 0.02)
    if nb != w.get('blk', -1):
        w['blk'] = nb; n_reblk += 1

# 词轴外残声必须成为后续显式裁决项，不能只在终端打印后丢失。
resid = []
for k, (x, y) in enumerate(VB):
    first = next((i for i in range(len(words))
                  if words[i].get('blk') == k and cons_txt[i]), None)
    if first is not None and new_ws[first] - x >= 0.12:
        resid.append({
            'residual_id': f'R{k:04d}',
            'block_index': k,
            'start': round(float(x), 6),
            'end': round(float(new_ws[first]), 6),
            'duration': round(float(new_ws[first] - x), 6),
            'next_word_index': first,
            'next_word_text': words[first]['t'],
        })
track['residual_candidates'] = resid

# 备份原 whisper 词轴（输入若已是对齐版——重复跑 p1a2——不覆盖既有备份）
bak = f'{WORK}/word_track.whisper备份.json'
if track.get('aligner') is None:
    shutil.copy(f'{WORK}/word_track.json', bak)
track['aligner'] = 'Qwen/Qwen3-ForcedAligner-0.6B'
track['char_timing'] = 'forced-aligner-char@1'
json.dump(track, open(f'{WORK}/word_track.json', 'w'), ensure_ascii=False, indent=1)

# ---- 7) 统计 ----
print(f'词轴对齐: {len(words)} 词 · ws 移动 中位 {med_all*1000:.0f}ms / p90 {p90_all*1000:.0f}ms'
      f' · blk 重判 {n_reblk} · 备份 {os.path.basename(bak)}')
fixw = []
for it in kp.get('fix', []):
    i0, i1, new = it[0], it[1], it[2]
    old_len = sum(len(words[i]['t']) for i in range(i0, i1 + 1))
    if len(new) != old_len:
        fixw.append(f'词{i0}「{new}」{new_we[i0]-new_ws[i0]:.2f}s [{new_ws[i0]:.2f}-{new_we[i0]:.2f}]')
if fixw:
    print('      fix 整并/补字词新时长: ' + ' · '.join(fixw[:8]))
print(f'      块头词轴外残声候选 {len(resid)} 处（块起→首词真实起音 ≥0.12s，'
      f'已写入 word_track.residual_candidates，必须逐项裁决）')
for item in resid[:6]:
    print(f'         块{item["block_index"]} @{item["start"]:.2f}s '
          f'首词「{item["next_word_text"]}」起音 {item["end"]:.2f}s'
          f'（残声 {item["duration"]*1000:.0f}ms）')
