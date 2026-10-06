#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工序①a 建词轴 word-track（2026-07-28 第一性原理重构 · 唯一真相源）
用法：p1a_建词轴.py <工作目录>

═══ 为什么有这个模块（架构级理由，改之前必读）═══
旧架构有三套互相独立的边界系统：EDL(秒级粗判) / VAD(帧级能量) / 分卡(语义)，
靠「词吸附·弱声母锚·砍缝残词回收·行内词永不丢·尾部词锚·闸门词证据降级」六个
补丁互相对齐。补丁在缝合裂缝，而裂缝是架构造出来的——
2026-07-28「用上AI反而更忙」帧级比对实证：段头系统性早 0.208s（中位 +12.5 帧，
56% 的段偏差 ≥12 帧），而闸门 100% 全绿，因为它检查的是三套边界【互相自洽】，
不是跟【真实人声】一致。补丁自证清白的闭环。

第一性原理：真相只有一个——【这个人在什么时刻说了哪个字】。
段、字幕都必须是这一条真相的投影，而不是三次独立判断再想办法对齐。
本模块产出的 word_track.json 就是那条唯一真相，此后全链只读它。

═══ 时间语义（三套时间戳，别混）═══
ws/we : whisper 原始词时间戳。块内连续可信，但【块首词的 ws 系统性虚早 ~0.2s】
        （mlx 把静音尾巴算进了下一个词的起点）——绝不可直接当段头用。
vs/ve : 校正后的真实发音时间。块首词 vs = 人声块起点，块末词 ve = 人声块终点，
        块内其余词沿用 whisper 值。段边界只许用 vs/ve。
blk   : 所属人声块 id；blk=-1 表示该词落在 VAD 判定的无声区
        （轻声/弱音，内容红线仍必须保留——三轮进化的成果，不许退回）。

注：段边界最终还要在 p1 里做局部精修（与 p1b 验收器同参数），
本模块的 vs/ve 是粗定位 + 块归属，用来判断「哪里是气口、哪些词连成一句」。
"""
import sys, os, json, glob, subprocess, wave, hashlib
import numpy as np, torch

WORK = sys.argv[1]
cfg = json.load(open(f'{WORK}/config.json'))
SR = 16000


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


# ---- 转写唯一绑定：多份 words 绝不猜一份 ----
word_files = sorted(
    f for f in glob.glob(f'{WORK}/*_words.json')
    if 'aligned' not in os.path.basename(f)
)
if len(word_files) != 1:
    names = '、'.join(os.path.basename(f) for f in word_files) or '（无）'
    sys.exit(
        f'⛔ p1a: 必须且只能有 1 份 *_words.json，实际 {len(word_files)} 份：{names}\n'
        '   拒绝用 glob 顺序猜转写。请删除/移走陈旧转写后重跑。'
    )
wf = word_files[0]
words_identity = _file_identity(wf)

# ---- 音频准备：缓存必须与当前素材指纹一致 ----
wav16 = f'{WORK}/_vad16k.wav'
wav_meta = f'{WORK}/_vad16k.meta.json'
media_path = os.path.realpath(cfg['proxy'])
if not os.path.isfile(media_path):
    sys.exit(f'⛔ p1a: config.proxy 不存在：{media_path}')
source_identity = _file_identity(media_path)

cache_ok = False
if os.path.isfile(wav16) and os.path.isfile(wav_meta):
    try:
        old_meta = json.load(open(wav_meta))
        cache_ok = (
            old_meta.get('source') == source_identity
            and old_meta.get('wav_sha256') == _sha256(wav16)
        )
    except Exception:
        cache_ok = False

if not cache_ok:
    subprocess.run(['ffmpeg', '-y', '-i', cfg['proxy'], '-vn', '-ac', '1', '-ar', str(SR),
                    wav16, '-loglevel', 'error'], check=True)
    json.dump({
        'schema': 'vad16k-cache@1',
        'source': source_identity,
        'wav_sha256': _sha256(wav16),
    }, open(wav_meta, 'w'), ensure_ascii=False, indent=1)
else:
    print(f'      音频缓存命中：{os.path.basename(media_path)} 指纹一致')

def read_audio(path):
    with wave.open(path, 'rb') as w:
        assert w.getframerate() == SR and w.getnchannels() == 1
        raw = w.readframes(w.getnframes())
    return torch.from_numpy(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0)

from silero_vad import load_silero_vad, get_speech_timestamps
model = load_silero_vad()
audio = read_audio(wav16)

# ---- 人声块：主检测 + 弱音补检并回 ----
# ⚠ VAD 参数必须与 p1 局部精修 / p1b 验收器【逐字相同】（.35/60/120/0）。
# 2026-07-28 血的教训：旧版这里用 min_silence=350ms + pad=30ms，导致
#   ① 0.12~0.35s 的停顿全被埋进块内，而 whisper 给块内相邻词的时间戳是【连续的】
#      （前词 end == 后词 start），gap 恒为 0 —— 停顿候选根本无法被发现，
#      实测段内残留 25 处无声窗共 10.3s（终版只剩 3 处），这就是成片比作者长 10.7s 的全部原因；
#   ② p1b 的 A3（段内气口检测）被「空窗中点被词覆盖则豁免」无条件放行——
#      因为词时间戳连续，任何空窗中点都被词覆盖，A3 等于从来没生效过。
# 块划分必须足够细，真实停顿才会暴露成 gap；切不切交给 expression_review@2 逐项功能听审，不在这里预判。
ts = get_speech_timestamps(audio, model, sampling_rate=SR, threshold=0.35,
                           min_speech_duration_ms=60, min_silence_duration_ms=120,
                           speech_pad_ms=0)
VOICE = [(t['start']/SR, t['end']/SR) for t in ts]
# 弱音补检：衰减拖尾/轻声 0.35 阈值检不出（AI编程样本 7 处 4.83s 被作者全部加回，
# 这种尾音是他 jump cut 的节奏，必须留）。只并回块，不反向改写段边界。
model.reset_states()
ts_w = get_speech_timestamps(audio, model, sampling_rate=SR, threshold=0.22,
                             min_speech_duration_ms=60, min_silence_duration_ms=120,
                             speech_pad_ms=0)
WEAK = [(t['start']/SR, t['end']/SR) for t in ts_w]

merged = []
for a, b in VOICE:
    if merged and a - merged[-1][1] < 0.03:
        merged[-1] = (merged[-1][0], max(merged[-1][1], b)); continue
    merged.append((a, b))
VOICE = merged
# ⚠ 弱音【绝不并回 VOICE】。旧版把弱音块并进人声块还外扩 +0.3s，
# 22 处并回直接把该删的停顿又吃回去（成片 131.3s vs 终版 117.2s）。
# 块必须保持纯净——它的唯一职责是让真实停顿暴露成 gap。
# 弱音保护下沉到【词层面】：词落在 WEAK 内 = 有内容的轻声，标 weak 保留；
# 两者都不在 = 真静音。段边界的保词逻辑在 p1 里按词做，不在这里扩块。
ext = 0

# ---- 词表 ----
words = []
for s in json.load(open(wf))['segments']:
    for w in s.get('words', []):
        t = w['word'].strip()
        if t:
            row = {'t': t, 'ws': round(w['start'], 3), 'we': round(w['end'], 3)}
            # probability 不能证明识别正确，但它是人工复核低置信识别时不可替代的风险信号。
            if 'probability' in w:
                row['probability'] = w['probability']
            words.append(row)
words.sort(key=lambda w: w['ws'])

# ---- 块归属：词中点落在哪个人声块 ----
for w in words:
    mid = (w['ws'] + w['we']) / 2
    w['blk'] = -1
    for bi, (a, b) in enumerate(VOICE):
        if a - 0.05 <= mid <= b + 0.05:
            w['blk'] = bi; break
    if w['blk'] == -1:
        w['orphan'] = True                    # 不参与块边界校正

# ---- 真实发音时间 vs/ve：块首词锚块起点、块末词锚块终点 ----
by_blk = {}
for i, w in enumerate(words):
    if not w.get('orphan'):
        by_blk.setdefault(w['blk'], []).append(i)
fixed_h = fixed_t = 0
for w in words:
    w['vs'], w['ve'] = w['ws'], w['we']
for bi, idxs in by_blk.items():
    a, b = VOICE[bi]
    i0, i1 = idxs[0], idxs[-1]
    if abs(words[i0]['ws'] - a) > 0.01:
        words[i0]['vs'] = round(a, 3); fixed_h += 1
    if abs(words[i1]['we'] - b) > 0.01:
        words[i1]['ve'] = round(b, 3); fixed_t += 1
# ---- orphan 词重定位：词的时间必须等于【它真实发音的时间】，不多不少 ----
# 2026-07-28 关键修正：whisper 会把「前置长静音 + 短促发音」打包成一个词
# （实证：「怎么」标 43.78~45.14 共 1.36s，真实发音只有尾部 0.17s；
#  单汉字发音 0.15~0.3s，凡时长 >0.4s 的单/双字词几乎都是这种情况）。
# 按【词中点】判 orphan 会把这类真实内容误判成幻觉——32 个所谓「幻影词」里
# 「但是/很/如果/一个/怎么」全是真话，终版字幕里一个不少。
# 改为【区间重叠】重新定位：词区间内只要有人声，vs 就取那段人声的起点。
# 这一条同时解决两件事：① 内容不丢 ② 词前长静音正确暴露为待听审 gap。
reloc = 0
for wi, w in enumerate(words):
    if not w.get('orphan'):
        w['weak'] = False
        continue
    # ⚠ 搜索起点必须晚于【前一个词的结束】。否则 whisper 词区间的起点常落在前一个词
    # 所在块的内部，第一个重叠块是上一个词的尾巴，会把重定位截胡
    # （实证：「怎么」[43.78~45.14] 命中前词「活」的块尾 43.968，vs 原地不动，
    #  段内那个 0.99s 的静音就永远暴露不出来，表达审查也无法发现它）。
    lo = max(w['ws'], words[wi-1]['ve'] if wi > 0 else 0.0)
    hit = None
    for a, b in VOICE:                        # 先主检测
        x, y = max(a, lo), min(b, w['we'])
        if y - x > 0.02: hit = (x, y); break
    if hit is None:
        for a, b in WEAK:                     # 再弱音检测
            x, y = max(a, lo), min(b, w['we'])
            if y - x > 0.02: hit = (x, y); break
    if hit:
        if hit[0] > w['ws'] + 0.02:
            w['vs'] = round(hit[0], 3); reloc += 1
        # 词尾同样要收到真实人声的终点。只修 vs 不修 ve 会留下两个 bug（实测各一处）：
        #   ① 段尾拖：末词「了」实际 54.208 结束却标到 54.58，段尾多拖 0.39s；
        #   ② 气口漏删：「带」实际 134.464 结束却标到 134.98，把后面 0.42s 静音吃进词里，
        #      gap 算出来是负的，停顿候选永远发现不到——独立闸门 A3 却会报「段内 0.42s 无人声」。
        if hit[1] < w['we'] - 0.02:
            w['ve'] = round(hit[1], 3)
        w['weak'] = True                      # 有真实发音 = 内容红线
    else:
        w['weak'] = False                     # 词区间内彻底无声 = whisper 幻觉

# ---- 声学停顿表：字幕分割点唯一可靠的时间基准 ----
# 2026-07-28 逐帧实证（agent 分析 43 个段内分割点，完美分离）：
#   作者移动过的 11 个分割点，【切点之后 20 帧内】全部存在一段声学停顿（11/11）；
#   他没动的 32 个，一个都没有（0/32）。他的规则是：切点 = 该停顿末端 − 2 帧。
#   能量对照：他的切点处 0.12×片中位（静音谷底），AI 版切点处 2.90×（元音正中间）。
#
# ⚠ 为什么不能用 Silero VAD 找这些停顿：min_silence_duration_ms=120 = 7.2 帧，
#   而这 11 个停顿有 6 个短于 12 帧，silero 直接吞掉——VAD 在 9/11 个切点上都报「有语音」。
#   VAD 用来定【段】边界是对的（块边界可靠），定【字幕分割点】是错的工具。
#
# ⚠ 也不能把能量当 DP 打分权重（试过，被字数惩罚淹没，AUC 仅 0.54）。
#   这是【搜索】：从词起点往后扫，找第一个停顿，落到它末端。
#
# 参数来自 agent 在 43 个点上的网格搜索，tier1 处在宽平台上（阈值 .15-.20 / 长度 2-4
# / 窗口 20-22 / lead 2 全稳定），tier2 是 n=43 拟合的，标为暂定。
HOP = int(0.005 * SR)                       # 5ms hop
WIN = int(0.020 * SR)                       # 20ms 窗
_a = audio.numpy()
_n = max(0, (len(_a) - WIN) // HOP)
_fr = np.lib.stride_tricks.sliding_window_view(_a, WIN)[::HOP][:_n]
_rms = np.sqrt(np.maximum((_fr ** 2).mean(axis=1), 1e-12))
_med = float(np.median(_rms))
_E = _rms / max(_med, 1e-12)                # 归一化到片中位

def _runs(thresh, min_frames):
    """返回 E<thresh 且持续 ≥min_frames(60fps 帧) 的区间 [起, 止] 秒"""
    need = int(min_frames / 60 / 0.005)
    below = _E < thresh
    out, st_ = [], None
    for k, v in enumerate(below):
        if v and st_ is None: st_ = k
        elif not v and st_ is not None:
            if k - st_ >= need:
                out.append([round(st_ * 0.005, 3), round(k * 0.005, 3)])
            st_ = None
    if st_ is not None and len(below) - st_ >= need:
        out.append([round(st_ * 0.005, 3), round(len(below) * 0.005, 3)])
    return out

PAUSE1 = _runs(0.20, 2)      # tier1：稳健层，单独就能拿 40/43
PAUSE2 = _runs(0.50, 3)      # tier2：浅凹陷降级层（暂定）

# 出声点表：E 首次达到 0.5×片中位 = 真正开口说话的那一帧。
# ⚠ 停顿结束 ≠ 人声开始。停顿末端只是能量爬出 0.20，从 0.20 爬到 0.5 还要几帧——
# 实测两处字幕在人开口前 4~6 帧就切了，画面上就是「字幕先跳出来，人还没张嘴」。
# 作者 2026-07-28 定的硬原则：「说话部分开始，才有字幕出现，
# 而不是说话结尾空余没有音频处出现了第二段字幕的开始，否则视觉会出现错位」。
_v = _E >= 0.5
VSPAN, _st = [], None
for k, x in enumerate(_v):
    if x and _st is None: _st = k
    elif not x and _st is not None:
        VSPAN.append([round(_st * 0.005, 3), round(k * 0.005, 3)]); _st = None
if _st is not None: VSPAN.append([round(_st * 0.005, 3), round(len(_v) * 0.005, 3)])
VSPAN = [x for x in VSPAN if x[1] - x[0] >= 0.02]

for i, w in enumerate(words):
    w['i'] = i

orph = sum(1 for w in words if w.get('orphan'))
weak_n = sum(1 for w in words if w.get('weak'))
ghost = orph - weak_n
drift = [w['vs'] - w['ws'] for w in words if not w.get('orphan') and w['vs'] != w['ws']]
# ---- 画幅：按【素材原始比例】判定（作者 2026-07-28 定），决定字幕断句 profile ----
# BBC v1.2.5 是唯一官方区分横竖版的规范：横版行宽 68% 画面宽 ≈ 37 拉丁字符，
# 竖版 90% 宽 ≈ 25 字符（−32%）。中文换算 + Netflix 简中(16)/Subanana(8-10) 交叉验证
# → 横版单行 13 字 / 竖版 10 字（DSCF2925 用户精调证据）。
import subprocess as _sp
def _aspect(path):
    try:
        r = _sp.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                     '-show_entries', 'stream=width,height', '-of', 'csv=p=0:s=x', path],
                    capture_output=True, text=True, timeout=30)
        w_, h_ = (int(x) for x in r.stdout.strip().split('x')[:2])
        return ('portrait' if h_ > w_ else 'landscape'), f'{w_}x{h_}'
    except Exception:
        return 'landscape', '?'
_ar, _wh = _aspect(cfg.get('original_path') or cfg['proxy'])

out = {
    'fps': 60,
    'aspect': _ar,
    'resolution': _wh,
    'src': cfg.get('cname', cfg['name']),
    'transcript_source': words_identity,
    'audio_cache': {
        'path': os.path.realpath(wav16),
        'meta_path': os.path.realpath(wav_meta),
        'source': source_identity,
        'wav_sha256': _sha256(wav16),
    },
    'voice_blocks': [[round(a, 3), round(b, 3)] for a, b in VOICE],
    'pause_tier1': PAUSE1,      # E<0.20×中位, ≥2帧  → lead 2 帧
    'pause_tier2': PAUSE2,      # E<0.50×中位, ≥3帧  → lead 4 帧（暂定）
    'voiced_spans': VSPAN,      # 真正在出声的区间（E ≥0.5×中位）——字幕不许落在它之外的静音里干等
    'words': words,
}
json.dump(out, open(f'{WORK}/word_track.json', 'w'), ensure_ascii=False, indent=1)
print(f'词轴: 词 {len(words)} · 人声块 {len(VOICE)}(纯净,未被弱音污染) · 画幅 {_wh} → {_ar}')
print(f'      块首校正 {fixed_h} · 块末校正 {fixed_t}')
print(f"      无声区词 {orph} = 有发音 {weak_n}(重定位 {reloc} 处,内容保住) + 真幻觉 {ghost}")
if drift:
    drift.sort()
    print(f'      whisper 块首词虚早中位 {-drift[len(drift)//2]*1000:.0f}ms '
          f'(这就是旧架构段头早 0.2s 的来源)')
