#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工序① 建段：把已经审阅的词级留删机械投影为原声段。
用法：p1_建段.py <工作目录>
输入：word_track.json（唯一真相源，p1a 产出）
      keep.json（**必需**，词级留删，AI 内容决策）
输出：segments.json

缺 keep.json 必须中止。没有词级留删决策时，不允许退回秒级 EDL 或生成“看似通过”的低质量产物。
"""
import sys, os, re, json, hashlib, math
from pathlib import Path

from expression_review_contract import (
    ExpressionReviewError,
    pause_compress_boundaries,
    validate_review as validate_expression_review,
)

WORK = sys.argv[1]
cfg = json.load(open(f'{WORK}/config.json'))
SR, FPS = 16000, 60
FULL_F = cfg['full_us'] * FPS // 1_000_000

HEAD_ROOM_TARGET_F = 2  # DSCF2925 用户精调：段头不贴起音，先留约 2 帧预卷
TAIL_ROOM_TARGET_F = 11 # C2897 + DSCF2925：真实发声衰减后默认留约 0.183s/11帧
NOOP_CUT_MAX_F = 3     # 最终只删 0–3 帧且无显式边界，这一刀没有可感知编辑价值
KEEP_ORPHAN_PAD = 0.02 # VAD 漏检的弱音词：按 whisper 值保内容（内容红线）

# ---- 段长参考上限（2026-08-02 · 「非常不好，我也说不出来哪里不好」的真正病灶）----
# 真值只认【早期 8 份成品(注:曾被误称「早期成品」,2026-08-03 实测全带 AI 指纹)】（他从头自己剪的：AI圈的两撮人 / AI编程要不要懂代码 /
# 企业AI落地难在哪 ×2 / 用上AI反而更忙 ×4）——跨 5 部片、跨 4 轮迭代：
#     最长段 4.38~6.98s，>7s 出现 0 次，零例外。
# 而 AI 版 C2864 最长段 23.87s、7 个段 >7s。他看完只说「非常不好，我也说不出来哪里不好」——
# 说不出来是因为这压根不是内容问题：一个说话人镜头 24 秒不切，观感就是「没剪过」。
# ⚠ 阈值【绝不许】拿 C2867 当依据。那份是他在 AI 草稿上改的，「他没改的部分」不代表认可，
#   只代表没顾上改——里面 16 秒的长段、字号 118、位置 −424.52 全是 AI 残留。
#   它只能用来看【他改了什么】，不能用来看【他保留了什么】。
#
# ★★ 2026-08-03 外部审核（Codex）改判：这条是【参考区间】，不是硬法律 ★★
#   反例就在手边——作者 2026-08-03 亲手精调的 C2864（采访类）里有 2 个 >7s 的段
#   （8.47s / 7.58s）。上面那行 7.0 的出处只是【历史样本的统计特征】，
#   「8 份旧片里没出现过」≠「他不会这么剪」，他的新片随时可能突破它。
#   把统计特征编码成不可违反的法律，是本 skill 同一错误模式的第三次发作
#   （前两次：第 5 轮词性对统计、第 11 轮 145 号字）——所以变量名从 SEG_MAX_S
#   改成 SEG_WARN_S：它是参考刻度，不是 FAIL 档。
#   审核原文：「23.87 秒不切，明显不合格；但强行把所有段切成 6.9 秒，也不代表符合你的节奏；
#   应在语义节拍、自然停顿或真正删除内容的位置切；找不到合理切点时，返回内容层重新取舍，
#   而不是为了通过 B7 制造无意义跳切。」
# 2026-08-10 C2897 用户反馈进一步澄清：粗剪不应为段长参考线主动加刀。
# 参考线只负责把可疑长段报出来；真正刀口必须来自内容删减、停顿压缩、
# 结构重排或显式 split_after。需要改节奏时回内容层，不靠“多一刀”伪装编辑。
# ── 段长两档（2026-08-04 分类型表拆除后的固定值）──
# WARN 7.0s：**纯参考，不是标准**。它原本的依据写着「早期 8 份成品(注:曾被误称「早期成品」,2026-08-03 实测全带 AI 指纹) >7s 零例外」，
#   而「早期成品」这个定性 2026-08-03 已被全盘推翻（11 份成品全带 AI 指纹，
#   那个分布很可能只是 AI 早期版本自己的段长分布）。依据没了，只留刻度。
# REVIEW 15.0s：必看线，不自动造刀。C2897 的这次反馈说明“超时长”不能
# 直接推导“要多切几刀”；应把它交给内容/包装决策。
# FAIL 22.0s：只拦明显未分析的超长连续段，并覆盖已被用户否定的 23.87s 案例。
SEG_WARN_S = 7.0
SEG_REVIEW_S = 15.0
SEG_FAIL_S = 22.0
MTYPE = cfg.get('material_type') or '口播'   # 缺省口播（全链唯一真实现的类型）

track = json.load(open(f'{WORK}/word_track.json'))
words = track['words']

# ---- 留删掩码 ----
keep = [True] * len(words)
drop_log = []
split_after = set()
boundary_exception_words = set()
kp = f'{WORK}/keep.json'
if os.path.exists(kp):
    kd = json.load(open(kp))
    # ★ 下标自校验（2026-07-29）：keep.json 的理由里如果写了引号文本（如 口头禅：'怎么讲呢'），
    #   就拿它跟【实际删掉的字】比对。此前 6 处 drop 区间少删 1-2 个词（103「呢」/511「作」/
    #   824-825「这个业」…）全靠人肉发现——写错一个下标就毁掉一句话，必须机器兜。
    _q = re.compile(r"[「'\"]([^」'\"]{1,20})[」'\"]")
    _norm = lambda s: re.sub(r'[\s,，。、！？!?…·]', '', s)
    _bad = []
    for item in kd.get('drop', []):
        i0, i1 = item[0], item[1]
        why = item[2] if len(item) > 2 else ''
        for i in range(max(0, i0), min(len(words), i1 + 1)):
            keep[i] = False
        got = ''.join(w['t'] for w in words[i0:i1 + 1])
        # ⚠ 理由里的引号【不止一处】，常见写法是先引上下文再点明被删部分：
        #     「句尾赘词：'但我现在就这样子了就'的末尾'就'」
        #   只比对第一个引号会把 3 处正确的下标全报成错（2026-07-31 在 C2854 实测）。
        #   判据改为：任一引号文本命中实际删除内容即通过。
        ms = [x for x in _q.findall(why) if _norm(x)]
        if ms and not any(_norm(x) == _norm(got) for x in ms):
            _bad.append(f'  [{i0}-{i1}] 理由提到「{"／".join(ms)}」，实际删「{got}」'
                        f'（若引号是上下文引用则可忽略；把被删内容也写进理由即可消除）')
        drop_log.append(f'  删 [{i0}-{i1}] {got[:24]} ← {why}')
    for i in kd.get('split_after', []):
        if not isinstance(i, int) or isinstance(i, bool) or not (0 <= i < len(words)):
            sys.exit(f'⛔ p1: split_after 越界或不是整数：{i!r}')
        split_after.add(i)
    for item in kd.get('boundary_exceptions', []):
        if isinstance(item, dict) and item.get('source') == 'human_approved_fcpxml':
            boundary_exception_words.update(
                i for i in item.get('drop_word_indices', []) if isinstance(i, int)
            )
    for item in kd.get('fix', []):
        i0, i1 = item[0], item[1]
        why = item[3] if len(item) > 3 else ''
        got = ''.join(w['t'] for w in words[i0:i1 + 1])
        # fix 的理由写成「'新值'被听成'旧值'」——要比对的是【最后一个】引号（旧值），
        # 拿第一个比会把 4 处正确的纠错全报成错。
        ms = _q.findall(why)
        if ms and _norm(ms[-1]) and _norm(ms[-1]) != _norm(got):
            _bad.append(f'  fix [{i0}-{i1}] 理由说原文是「{ms[-1]}」实际指向「{got}」')
    if _bad:
        sys.stderr.write(f'⛔ keep.json 下标与理由不符 {len(_bad)} 处：\n' + '\n'.join(_bad) + '\n')
    _split_dropped = sorted(i for i in split_after if not keep[i])
    if _split_dropped:
        sys.exit(f'⛔ p1: split_after 必须指向保留词，但以下词已 drop：{_split_dropped[:12]}')
    print(f'留删: keep.json · 删 {sum(1 for k in keep if not k)}/{len(words)} 词 · '
          f'{len(kd.get("drop", []))} 处 · 语义强制分段 {len(split_after)} 处')
else:
    sys.exit(
        f'\n⛔ p1 建段中止：keep.json 不存在（{kp}）\n'
        '   先通读词轴并完成 drop/fix/retain/split_after，再重跑本步；'
        '本流程不提供 EDL 兜底。\n'
    )

# ---- 停顿不再由全局秒数阈值自动删除 ----
# 候选发现只负责提醒听审；真正下刀必须是 expression_review@2
# 逐处判定的 ineffective_wait/compress。思考、强调、情绪、动作、
# 转折或呼吸功能均必须 retain，不允许用“超过 0.x 秒”一刀切。
expression_path = f'{WORK}/expression_review.json'
if not os.path.exists(expression_path):
    sys.exit('⟂ p1: 缺 expression_review.json；必须先完成微成分/残声/停顿逐项听审')
try:
    expression_review = json.load(open(expression_path))
    validate_expression_review(
        expression_review,
        track_path=Path(f'{WORK}/word_track.json'),
        track=track,
        keep=kd,
    )
except (OSError, json.JSONDecodeError, ExpressionReviewError) as exc:
    sys.exit(f'⟂ p1: expression_review 未闭环：{exc}')
pause_compress_after = pause_compress_boundaries(expression_review)
split_boundaries = split_after | pause_compress_after
print(f'停顿裁决: 显式压缩 {len(pause_compress_after)} 处（未审候选不自动下刀）')

# ---- 转写纠错：whisper 同音字错误只能靠语义识别，脚本无从判断 ----
# 2026-07-29 实证（企业AI落地难在哪）：whisper 把「熵」听成「伤」、「熵增」听成「上针」、
# 「绩效」听成「计较」——而它对这些错误的置信度高达 0.989~0.999（全片中位 0.998），
# 靠 probability 根本筛不出来。只能由 AI 通读时按语义发现，写进 keep.json 的 fix 字段。
# keep.json 只能删词，不能改字，所以必须有这个机制。
n_fix = 0
for item in (kd.get('fix', []) if os.path.exists(kp) else []):
    i0, i1, new = item[0], item[1], item[2]
    old = ''.join(w['t'] for w in words[i0:i1+1])
    if len(new) == len(old):                      # 等长：逐字替换，时间完全不动
        k = 0
        for i in range(i0, i1 + 1):
            L = len(words[i]['t'])
            words[i]['t'] = new[k:k+L]; k += L
    else:                                          # 不等长：整并到首词，其余置空
        words[i0]['t'] = new
        for i in range(i0 + 1, i1 + 1):
            words[i]['t'] = ''
        words[i0]['ve'] = words[i1]['ve']
    n_fix += 1
if n_fix:
    print(f'转写纠错: {n_fix} 处')

# ---- mute_caption 已于 2026-07-31 整体删除 ----
# 它是从【粗调版】终版反推出来的伪机制：那一版里几个 clip 有画面有音频却零 title，
# 我据此认定「他保留声音、只删字幕」。对标精调版后真相是——他把那些素材整段删了。
# 一条从错误样本推出的机制，在三个脚本里活了整整一轮。
# 留着支持代码等于留个地雷：任何人往 keep.json 写一条 mute_caption 就会立刻复活它。
# 若日后真要「只删字幕」这个手法，先拿两份精调终版验证它存在，再重新实现。

kept = [i for i in range(len(words)) if keep[i]]
if not kept:
    sys.exit('!! 无保留词')
drop_indices = {i for i, is_kept in enumerate(keep) if not is_kept}


def _merge_crosses_locked_boundary(left, right):
    """显式 drop/split_after/停顿压缩都是已审边界，后置并段不得撤销。"""
    if not left.get('words') or not right.get('words'):
        return True
    a = left['words'][-1]['i']
    b = right['words'][0]['i']
    crosses_drop = any(i in drop_indices for i in range(a + 1, b))
    crosses_split = any(i in split_after for i in range(a, b))
    crosses_pause_compress = any(i in pause_compress_after for i in range(a, b))
    # 近连续无效停顿的“压缩”不能最终只剩 0–3 帧；
    # 这种刀口听觉上无价值，应并回成一个连续源段。只有
    # 真实删词与语义 split_after 是绝对不得撤销的边界。
    if crosses_pause_compress:
        gap_f = round((right.get('in', 0) - left.get('out', 0)) * FPS)
        crosses_pause_compress = gap_f > NOOP_CUT_MAX_F
    return crosses_drop or crosses_split or crosses_pause_compress

# ---- 保留词 → 段：切段依据是【人声块边界】，不是词轴 gap ----
# 2026-07-28 架构修正（作者终版实证）：
#   他的段27 = [102.17, 107.87] 一整段、内部 6 张字幕卡，中间【没有切段】。
#   而该处词 274→275 的词轴 gap 有 0.46s，旧逻辑按 GAP_CUT 一刀切成两段。
#   但 VAD 判定这 0.46s 是【连续有声】（同一个人声块）——那是拖音/换气/语气延续，
#   属于语流内部，不是停顿。「一段完整语流绝不切」是他定死的铁律。
# 所以：块内部无论词间隙多大都不切段（交给字幕分卡去处理）；
#       只有明确内容删除、语义 split_after 或已听审的无效停顿压缩才切段。
# 附带好处：段边界永远落在块边界上，p1b 的 A1/A2 才有可断言的物理基准。
VB = track['voice_blocks']

def blk_of(t):
    for i, (x, y) in enumerate(VB):
        if x - 0.02 <= t <= y + 0.02:
            return i
    return -1

groups, cur = [], [kept[0]]
for a, b in zip(kept, kept[1:]):
    ba, bb = blk_of(words[a]['ve']), blk_of(words[b]['vs'])
    if a in split_boundaries:                         # 语义边界/已听审停顿，即使同 VAD 块也必切
        groups.append(cur); cur = [b]; continue
    if b != a + 1:                                   # 中间有词被内容决策删掉 → 必切
        groups.append(cur); cur = [b]; continue
    cur.append(b)
groups.append(cur)

# ---- 人声块：直接复用 p1a 的全片结果，【绝不再算第二次】----
# 2026-07-28 教训：旧写法在每个段边界附近用 ±0.45s 窗口【局部重跑】VAD。
# 参数与 p1a 逐字相同，但 Silero 对输入长度敏感，窗口不同结果就不同——
# 于是出现「段24 的发音止(91.36) 晚于 段25 的发音起(90.51)」这种自相矛盾，
# 闸门一口气报 14 处 ±25 帧的边界偏差。
# 真相只能算一次：切段用哪份块，定边界就必须用同一份块。

def onsets(a, b):
    """返回与 [a,b] 相交的人声块，且【裁剪到窗口内】。

    ⚠ 裁剪不可省。2026-07-28 实证事故：一个 5.70s 的大人声块 [102.176, 107.872]
    里面按词间隙切出两组词，两组都拿到未裁剪的整块边界，算出【完全相同】的段区间；
    随后防重叠把前段裁成零长度并过滤掉 —— 「我在想呀如果我是对面我是AI」
    整句 2.46s 凭空蒸发，而闸门抓不到（丢掉的段已经不存在了）。
    """
    out = []
    lo, hi = a - 0.15, b + 0.15
    for x, y in VB:
        # ⚠ 必须要求与词组【实质重叠】(>0.05s)，不能只看 ±0.15s 容差沾边。
        # 2026-07-28 实证：段23 删掉末尾的「就」后，末词是零时长的幻觉词「了」(86.14)，
        # 容差把下一个块 [86.272, 86.528] 的边缘也纳入，段尾被拉到 86.28，
        # 而真实内容 85.888 就结束了 —— 拖尾 24 帧，闸门 A2 报警。
        if min(y, b) - max(x, a) > 0.05:
            out.append((max(x, lo), min(y, hi)))
    return out

def q(x):
    return round(round(x * FPS) / FPS, 6)


def _ceil_frame(t):
    return int(math.ceil(t * FPS - 1e-9))


def _floor_frame(t):
    return int(math.floor(t * FPS + 1e-9))


def _safe_tail(last_i, acoustic_end):
    """保住末词、声学衰减和一小段安静尾部。

    目标不是给每段盲加固定时长：实际 out 会被下一个声学起点、
    下一词（尤其是 drop）和素材尾钳住。若安全空间不足，保发音完整并交听审，
    不为满足数字把下一词/被删声音包回来。
    """
    word_end = words[last_i]['we']
    word_end_f = _ceil_frame(word_end)
    acoustic_end_f = _ceil_frame(acoustic_end)
    # 旧实现只在 word_end 后留 11 帧，当 VAD/RMS 证明尾韵比
    # 词轴更长时，实际只剩 0–2 帧安静余量，正是 DSCF2925 卡耳
    # 的根因。目标必须从「词尾」和「真实声学尾」中更晚者起算。
    desired_f = max(word_end_f, acoustic_end_f) + TAIL_ROOM_TARGET_F
    cap_f = FULL_F
    clamped_by_drop = False
    next_i = last_i + 1
    if next_i < len(words):
        word_cap_f = _floor_frame(words[next_i]['ws'])
        next_is_drop = not keep[next_i]
        # 两个都保留的相邻词可能因为浮点时码落在帧内不同位置，出现
        # ceil(前词词尾) 比 floor(后词词头) 大 1 帧。此时它不是删词
        # 冲突，只是同一句在帧网格上的量化空隙；允许二者共用边界帧，
        # 后面的相邻段防重叠仍会检查真实词时轴。若下一词被 drop，或
        # 差值超过 1 帧，继续使用原硬拦截，不能借量化容差包回声音。
        if not next_is_drop and word_end_f - word_cap_f == 1:
            word_cap_f = word_end_f
        # ASR 词时码可把人工刀口后的 drop 词起点与前一保留词
        # 尾音标成同一时刻。只有哈希锁定的 human_approved_fcpxml
        # boundary_exception 可允许把安全上界放到前词完整词尾；未经人工
        # 批准的普通 drop 仍然硬拦，防止包回被删声音。
        if next_is_drop and next_i in boundary_exception_words \
           and word_end_f - word_cap_f <= 1:
            word_cap_f = word_end_f
        cap_f = min(cap_f, word_cap_f)
        clamped_by_drop = next_is_drop
    # 转写可能漏掉下一声轻呼吸/语气词；比“下一词”更早的
    # VAD 起点也是硬上界，否则尾柄会把未转出的声音包回来。
    next_voice_start = min((x for x, _ in VB if x > acoustic_end + 0.01), default=None)
    if next_voice_start is not None:
        cap_f = min(cap_f, _floor_frame(next_voice_start))
    if cap_f < word_end_f:
        nxt = words[last_i + 1]['t'] if last_i + 1 < len(words) else '<片尾>'
        relation = (
            '下一 drop' if last_i + 1 < len(words) and not keep[last_i + 1]
            else '下一保留词'
        )
        sys.exit(
            f'⛔ p1: 末词[{last_i}]「{words[last_i]["t"]}」结束 {word_end:.3f}s '
            f'与{relation}「{nxt}」/声学上界冲突；'
            '无法在不咬字的同时证明下一词已删净'
        )
    out_f = min(desired_f, cap_f)
    return out_f / FPS, clamped_by_drop, out_f - word_end_f

segments = []
saved_h = saved_t = 0
def _forced_aligned(w):
    return bool(
        w.get('align_text')
        and w.get('char_times')
        and w.get('ve', 0) > w.get('vs', 0)
    )


def _real(k):
    """幻觉词（VAD 主检测和 0.22 弱音检测都抓不到）不得参与边界计算。
    它们常是零时长（ws==we）、落在两块之间的静音里——拿它当段首/段尾，
    边界就会被拽到静音区去（实证：段23 段尾被拽出 24 帧）。
    文本仍然保留它，只是不用它定边界。"""
    w = words[k]
    # p1a 阶段标成 orphan 只表示当时没有落入主 VAD 块；p1a2 若随后已经
    # 给出逐字强制对齐，便获得了新的独立声学证据，不能继续把它当幻词排除。
    # C2901 的「那么」已有完整 char_times，却因遗留 orphan=true 被跳过，
    # 结果段头按下一词「什么」计算，实测咬掉 140ms 首字。
    if _forced_aligned(w):
        return True
    no_acoustic_evidence = (
        w.get('orphan')
        or (w.get('blk', -1) < 0 and w.get('ve', 0) - w.get('vs', 0) <= 0.02)
    )
    return not (no_acoustic_evidence and not w.get('weak'))

# ---- 被删词与保留词同块时的边界内收（2026-07-29 · 「废话没删干净」的真正病灶）----
# 病灶：段头恒取【人声块起点】。可如果这个块里，在第一个保留词之前还躺着被 drop 的词
# （口吃/语气词几乎总是紧挨着正文，同属一个人声块），块起点就把那些词的【声音】
# 原封不动带回成片——drop 只把它们从字幕里拿掉了，观众照样听得见。
# 实证（企业AI落地难在哪，对标精调终版）：18 处「我留他删」共 38 字，
# 其中「呢」「作」「不对那」「这个业」全属此类：keep.json 明明删了，成片里还在响。
# 修法：这种情形下段头改用【RMS 出声区间】——第一个保留词自己的起音点，而不是整块起点。
# RMS 是声学证据，与 VAD/whisper 两套模型推断相互独立，正是块内定位唯一可信的尺子。
VSPAN = track['voiced_spans']

def _on(t):
    """t 所在 RMS 出声区间的【起点】；t 落在区间外（凹陷/弱音）则返回 None。"""
    for x, y in VSPAN:
        if x - 0.01 <= t <= y + 0.01:
            return x
    return None

n_trim_h = n_trim_t = 0
n_skip_phantom = 0
for g in groups:
    real = [k for k in g if _real(k)]
    if not real:
        # 整组词连低阈值弱音检测都没有声学证据，不能因尾部余量
        # 把零时长 ASR 幻词膨胀成一个闪帧片段。旧版也会因 <0.05s 过滤它，
        # 这里把真正的理由显式化。
        n_skip_phantom += 1
        continue
    w0, w1 = words[real[0]], words[real[-1]]
    a0, b0 = w0['vs'], w1['ve']                      # 粗定位（词轴，已剔除幻觉词）
    ins = onsets(a0, b0)
    if ins:
        # 2026-08-06 对齐词轴（p1a2 Qwen3 ForcedAligner）后启用：词时间已帧级可信，
        # 段头基准优先使用强制对齐的 ws；未完成强制对齐时使用 p1a 依据
        # 声学重定位的 vs。块头的词轴外残声（whisper 漏转的呃/嗯，
        # C2637 扫出 74 处候选）自然被排除；同时不能用 max(块起, ws)：
        # C2901 实测一处 VAD 块起比强制对齐的「那么」晚 140ms，max 会直接
        # 咬掉首字。强制对齐通过后，首词 ws 才是“既不带回前声、也不咬词头”
        # 的硬基准；反之不能盲信 whisper 原始 ws——C2901 一处比真实发音
        # 早 220ms。头柄用显式帧网格计算，避免浮点四舍五入吃掉一帧。
        # 段尾不再做负 pad 内收：先取声学结束，再由 _safe_tail
        # 保住衰减和安静尾部；若后面紧跟 drop/下一发音，则钳在起音前。
        # 词级 ws 仍可能晚于该词首个字符的强制对齐起音 1–3 帧。
        # DSCF2925 回归中因此出现 12/28 段真实头柄低于 2 帧、最差切进 1 帧。
        # 有字符级证据时直接取首字起音；没有时才回词级定位。
        first_char_starts = [
            float(row['s'])
            for row in (w0.get('char_times') or [])
            if isinstance(row, dict) and isinstance(row.get('s'), (int, float))
        ]
        if first_char_starts:
            start_evidence = min(first_char_starts)
        else:
            start_evidence = w0['ws'] if _forced_aligned(w0) else w0['vs']
        # 用帧网格显式预留，不能先减 2/60 秒再 q() 四舍五入；
        # 后者在首字落于帧中时常只剩 1 帧。
        a = max(0, _floor_frame(start_evidence) - HEAD_ROOM_TARGET_F) / FPS
        acoustic_tail_cap = ins[-1][1]
        # 前一个词被内容决策删掉、【且与本段首词同块】→ 段头不许再退回块起点
        # （那里是被删词的声音）。不同块时块边界本来就干净，内收反而会咬掉起音——
        # 实证：段87「啪」后面 1.5s 才是被删的「一群」，误内收把 20 帧的段砍成 17 帧，
        # 触发 B5 闪帧 FAIL，而他终版里「啪」保留得好好的。
        _b0 = blk_of(a0)
        if real[0] > 0 and not keep[real[0] - 1] and _b0 >= 0 and blk_of(words[real[0] - 1]['ve']) == _b0:
            a2 = _on(a0)
            # 同块前词已 drop 时，VAD 块起只是“整块有声”的证据，不能用它
            # 把当前首字的 2 帧头柄吃掉。首字字符级起音已经把前词排除；
            # 只有没有字级证据时才允许旧 RMS 内收。
            if not first_char_starts and a2 is not None:
                a2_safe = max(0, _floor_frame(a2) - HEAD_ROOM_TARGET_F) / FPS
                if a2_safe > a:
                    a = a2_safe; n_trim_h += 1
    else:                                            # 整段 VAD 无声（纯弱音）：按词保内容
        a = a0 - KEEP_ORPHAN_PAD
        acoustic_tail_cap = b0 + KEEP_ORPHAN_PAD
    # 内容红线：VAD 主检测漏掉、但低阈值能检出的弱音词（weak）必须保住。
    # 只保 weak，不保幻影词（连 0.22 阈值都检不出 = whisper 幻觉或纯静音）——
    # 旧版不分青红皂白按词扩边界，把静音一起拉进段里。
    # ⚠ 只许用 vs/ve（p1a 重定位后的真实发音时间），绝不许用 ws/we——
    # ws 带着 whisper 的前置静音，用它扩边界等于把旧架构的 0.2s 系统性偏早原样搬回来
    # （实测：误用 ws 让段头中位从 +0.5 帧劣化回 +12.0 帧）。
    if w0.get('orphan') and w0.get('weak') and w0['vs'] < a:
        a = w0['vs'] - KEEP_ORPHAN_PAD; saved_h += 1
    if w1.get('orphan') and w1.get('weak') and w1['ve'] > acoustic_tail_cap:
        acoustic_tail_cap = w1['ve'] + KEEP_ORPHAN_PAD; saved_t += 1
    b, _tail_clamped, _tail_handle = _safe_tail(real[-1], acoustic_tail_cap)
    if _tail_clamped:
        n_trim_t += 1
    if b - a < 0.05:
        continue
    segments.append({
        'in': q(a), 'out': q(b),
        'text': ''.join(words[i]['t'] for i in g),
        'words': [{'w': words[i]['t'], 's': words[i]['vs'], 'e': words[i]['ve'],
                   'i': i,
                   # p1a2 已按同一份 keep.fix 对齐；p1 再应用 fix 后必须透传
                   # 字级时间，供 p2 重排后 p3 仍在最终播放段上精确定位。
                   'align_text': words[i].get('align_text'),
                   'char_times': words[i].get('char_times'),
                   } for i in g],
    })

# ══════════════════════════════════════════════════════════════════════
# 记录机械投影前的原始边界，供审计与回归比较。
for s_ in segments:
    s_['raw_in'] = s_['in']

# ---- 防回声：相邻段源区间绝不许重叠 ----
segments.sort(key=lambda s: s['in'])
for a, b in zip(segments, segments[1:]):
    if b['in'] < a['out']:
        # 不能像旧版一样直接把前段 out 拉到后段 in：后段有声学头柄，
        # 那个时间点可能早于前词结束，会为了「不重叠」直接咬掉末音节。
        left_end_f = _ceil_frame(a['words'][-1]['e'])
        right_start_f = _ceil_frame(b['words'][0]['s'])
        if left_end_f > right_start_f:
            sys.exit(
                f'⛔ p1: 相邻保留词时轴交叉：词[{a["words"][-1]["i"]}] 结束 '
                f'{left_end_f}f > 词[{b["words"][0]["i"]}] 起音 {right_start_f}f；'
                '无法在不咬字的同时消除重叠'
            )
        desired_out_f = round(a['out'] * FPS)
        cut_f = max(left_end_f, min(desired_out_f, right_start_f))
        a['out'] = cut_f / FPS
        b['in'] = max(b['in'], cut_f / FPS)
# 过短段处理：<0.30s 的段在画面上就是一个闪帧（终版最短卡 0.42s）。
# 2026-07-29 实证：C2855 片尾孤立的单字「好」只有 0.19s，前后隔 3.9s 无法合并，
# 而闸门 B2 只对【多卡段】断言，单卡段漏网 —— 自检才抓到。
# 规则：能并入相邻段（间隙 ≤0.35s）就并；并不了且只含语气词/单字就丢；含实词则保留并告警。
MIN_SEG = 0.30
_drop_short = []
_out = []
for k, s in enumerate(segments):
    if s['out'] - s['in'] >= MIN_SEG:
        _out.append(s); continue
    prev_gap = s['in'] - _out[-1]['out'] if _out else 9.9
    next_gap = segments[k+1]['in'] - s['out'] if k + 1 < len(segments) else 9.9
    # ⚠ 只允许【并入前一段】。曾经还写过「留给下一段吸收」——它改的是循环里
    # 尚未处理或已处理过的元素，实测把词 661「筛」塞进了下一段却排在末尾，
    # 字幕直接变成「选进行跟踪筛」。要吸收就往回并，绝不往前改别人。
    if prev_gap <= 0.35 and _out and not _merge_crosses_locked_boundary(_out[-1], s):
        _out[-1]['out'] = s['out']
        _out[-1]['text'] += s['text']
        _out[-1]['words'] += s['words']
    else:
        # 不在这里丢——丢内容是内容决策，只能写进 keep.json（否则闸门 A0 会抓）
        _drop_short.append((s['in'], s['text'], s['words'][0]['i'], s['words'][-1]['i']))
        _out.append(s)
segments = _out

# 防御：段内词序必须按词轴下标递增，边界必须覆盖所有词
for s in segments:
    s['words'].sort(key=lambda w: w['i'])
    s['text'] = ''.join(w['w'] for w in s['words'])

segments = [s for s in segments if s['out'] - s['in'] > 0.04]

# ---- 段长只做观测，不生成刀口 ----
# C2897 用户反馈确认：「在自然停顿处可以切」不等于「应该为了段长切」。
# 粗剪的刀口只服务真实删减、停顿压缩、结构重排或显式 split_after；
# 说话人镜头节奏可在包装层用放大/B-roll 处理，不在粗剪层造假删接。
_pre_long = sum(1 for s in segments if s['out'] - s['in'] > SEG_WARN_S)
n_cut7 = 0

# ---- 帧量化 ----
for s in segments:
    fi, fo = round(s['in'] * FPS), round(s['out'] * FPS)
    # 素材内不存在负时间：首段发音从 0.000s 起时，头柄会被素材起点安全钳制，
    # p4f 原样写进 asset-clip 的 start，FCPXML 非法（终检 fr() 断言「非帧网格时间: -100/6000s」）。
    # 上界 min(fo, FULL_F) 早就钳了，下界一直漏着——只有「素材第一帧就有人说话」才踩到，
    # 实证：九紫离火（C2859 一开机就是对话，首词「你在」vs=0.000）。
    fi = max(fi, 0)
    if fo <= fi:
        fo = fi + 1
    fo = min(fo, FULL_F)
    s['in_f'], s['out_f'] = fi, fo
    s['in'], s['out'] = round(fi / FPS, 6), round(fo / FPS, 6)

# ---- 跨段语块合并：段边界不许劈开一个词 ----
# 2026-07-29 实证（企业AI落地难在哪）：块间隙 583ms 把「企业应用」劈成
# 「…AI落地企业」+「应用…」，333ms 把「筛选」劈成「筛」+「选…」——
# 作者的终版这两处都是一整段。字幕层再怎么调都救不回来，因为词已经跨段了。
# 判据：劈开的必须是【真词】（jieba ≥2 字），且块间隙 < 0.6s
# （「Agent」那处间隙 2250ms，中间是真停顿，合并会把静音带进成片，他也没合并）。
try:
    import jieba as _jb
    _jb.setLogLevel(60)
    _merged2, _nglue = [], 0
    for _s in segments:
        if _merged2:
            _prev = _merged2[-1]
            _gap = _s['in'] - _prev['out']
            # 这是“不动已听审停顿刀口”之外的跨块真词修复上界；
            # 更大静音不得由词法合并器自作主张带回段内。
            # 「筛选」333ms 可合并；「企业应用」583ms 不能——他也没合并那处，
            # 他是把字幕写全（段4 字幕「AI落地企业应用」而「应用」的音频在段5，段5 字幕为空）。
            if 0 <= _gap < 0.38 and not _merge_crosses_locked_boundary(_prev, _s):
                _tail, _head = _prev['text'][-3:], _s['text'][:3]
                _pos, _broke = 0, None
                for _t in _jb.cut(_tail + _head):
                    if _pos < len(_tail) < _pos + len(_t) and len(_t) > 1:
                        _broke = _t; break
                    _pos += len(_t)
                if _broke:
                    # ⚠ out 和 out_f 必须同步（2026-08-02 实证事故）：
                    #   本循环跑在帧量化【之后】，只改 out 不改 out_f 会让 segments.json
                    #   自相矛盾——p3 按 text 分卡（拿到合并后的全部字），
                    #   p4f 按 out_f 出片（画面还是合并前的长度）。
                    #   实测 C2864 段32：out=142.20s 但 out_f=8198帧=136.63s，差 334 帧；
                    #   后果是一张 35 字 0.90 秒的卡，25 个词 3.54 秒的内容观众看得到字听不到音。
                    #   而闸门 A0 读的恰好是被写坏的 out（见 p1b 同批修正），所以全绿放行。
                    _prev['out'] = _s['out']
                    _prev['out_f'] = _s['out_f']
                    _prev['text'] += _s['text']
                    _prev['words'] += _s['words']
                    _nglue += 1
                    continue
        _merged2.append(_s)
    segments = _merged2
    print(f'      跨段语块合并 {_nglue} 处(段边界劈开了词)')
except ImportError:
    # ⚠ 绝不静默跳过。此前 `except ImportError: pass` + 「只在 >0 时打印」两条叠加，
    #   使「机制没跑」和「机制跑了但一处没合」在 stdout 上完全无法区分。
    sys.stderr.write('⛔ jieba 不可用，跨段语块合并已跳过——段边界可能劈开完整词\n')

# ---- 源近连续的相邻段强制合并（消灭「几乎什么都没剪」的切点）----
# ⚠ 必须放在【防重叠 + 帧量化之后】。放在前面时，防重叠会把前段尾拉到后段头
# （a.out = b.in），量化后两者恰好相等，反而【新造出】源连续切点——
# 闸门实测抓到 3 处。这一刀源上连续、时间线上也连续，什么都没剪掉，
# 只给 FCP 时间线多一个刀口，作者明确说不要。
merged, nmerge = [], 0
for s in segments:
    _gap_f = s['in_f'] - merged[-1]['out_f'] if merged else None
    if merged and 0 <= _gap_f <= NOOP_CUT_MAX_F \
       and not _merge_crosses_locked_boundary(merged[-1], s):
        merged[-1]['out_f'] = s['out_f']
        merged[-1]['out'] = s['out']
        merged[-1]['text'] += s['text']
        merged[-1]['words'] += s['words']
        nmerge += 1
    else:
        merged.append(s)
segments = merged

# 硬断言：合并后任何段的词下标包络区间内都不得出现 drop。
# 这不依赖时间容差，专门防「短段合并/跨段语块合并」把明确删掉的词包回源区间。
for si, s in enumerate(segments):
    idx = [w['i'] for w in s.get('words') or []]
    if not idx:
        continue
    wrapped = sorted(i for i in drop_indices if idx[0] < i < idx[-1])
    if wrapped:
        sys.exit(f'⛔ p1: 段{si} 合并跨过明确 drop 词下标 {wrapped[:12]}，拒绝把已删音频包回')
    undone_semantic = sorted(i for i in split_after if idx[0] <= i < idx[-1])
    if undone_semantic:
        sys.exit(
            f'⛔ p1: 段{si} 后置合并撤销 split_after 边界 '
            f'{undone_semantic[:12]}，拒绝写盘'
        )
    # 停顿压缩边界若在保全左右声学安全柄后只剩 0–3 帧，
    # 生产结果就应合并：审查的“可压缩”不等于必须留一道无感刀口。
    # 这不是撤销人工决策，而是在不咬字的物理上界下拒绝伪剪辑。
for i, s in enumerate(segments):
    s['line'], s['part'] = 'L%02d' % (i // 6 + 1), i

json.dump(segments, open(f'{WORK}/segments.json', 'w'), ensure_ascii=False, indent=1)
d = [s['out'] - s['in'] for s in segments]
c = [len(s['text']) for s in segments]
print('\n'.join(drop_log[:20]))
if n_trim_h or n_trim_t:
    print(f'      删词边界钳制 {n_trim_h} 头 / {n_trim_t} 尾（同块内被删词的声音不带进成片）')
print(f'建段: {len(segments)} 段 · 成片 {sum(d):.1f}s · 平均 {sum(d)/len(d):.2f}s/{sum(c)/len(c):.1f}字')
print(f'      源连续合并 {nmerge} 处(消灭无意义切点) · 弱音保头 {saved_h} 保尾 {saved_t}'
      f' · 零声学证据幻词组跳过 {n_skip_phantom}')
# 摘要行是最后才滚出来的，开头那句警告早被几十行输出顶走了 —— 在这儿再钉一次，
# 让「本次产物抄了答案、指标不作数」这件事没法被事后忽略（同 ALLOW_JIEBA_FALLBACK 的处理）。
if _drop_short:
    print(f'      ⚠ 孤立短段 {len(_drop_short)} 处(<{MIN_SEG}s=闪帧，无法并入相邻段)：')
    for t, x, i0, i1 in _drop_short[:6]:
        print(f'         @{t:.1f}s 词[{i0}-{i1}]「{x}」 ← 要删就写进 keep.json，p1 不代劳')
# ---- 段长统计（口径必须是【最终 segments】）----
# 不能用强切当场的结果报数：后面还有跨段语块合并和源连续合并两道并段，
# 它们可以把两个合法段并成一个新的超长段。报「切完就没有了」而实际还有，
# 就是又一次生产侧默默放行、验收侧判死的不一致（p1b B7 拿的正是最终 segments.json）。
# 2026-08-03：措辞跟着降级——超过参考线只是【提示去看一眼】，
# 只有超过 FAIL 档（明显不合格）才是真拦路虎，两者必须在同一行里分清楚。
_over = [s for s in segments if s['out'] - s['in'] > SEG_WARN_S + 1e-6]
_over_fail = [s for s in segments if s['out'] - s['in'] > SEG_FAIL_S + 1e-6]  # 名字别用 _bad：
                                          # 上面 keep.json 自校验已经占了那个名字
print(f'      段长参考线 {SEG_WARN_S}s（{MTYPE}·WARN 档，非硬法律）：'
      f'为段长额外切 {n_cut7} 刀（必须为 0） · 超参考线 {_pre_long} → {len(_over)}'
      f'（其中超 {SEG_REVIEW_S}s 必看线 '
      f'{sum(1 for s in segments if s["out"]-s["in"] > SEG_REVIEW_S)} 个；'
      f'超 {SEG_FAIL_S}s 异常硬线 {len(_over_fail)} 个）'
      f'（最长段 {max(d):.2f}s；早期 8 份成品(注:曾被误称「早期成品」,2026-08-03 实测全带 AI 指纹) 4.38~6.98s，他精调的 C2864 有 8.47s）')
if _over:
    print(f'      ⚠ 超参考线 {len(_over)} 处(>{SEG_WARN_S}s，仅提示复核；'
          f'十几秒不切的说话人镜头 = 「没剪过」才是真问题)：')
    for s in _over[:6]:
        why = ('该段没有真实删减/停顿压缩边界，p1 不为段长额外加刀 '
               '← 需改节奏请回 keep.json 做内容取舍')
        print(f'         @{s["in"]:.1f}s {s["out"]-s["in"]:.2f}s '
              f'词[{s["words"][0]["i"]}-{s["words"][-1]["i"]}]「{s["text"][:18]}」 ← {why}')
    # 处理方向写死在这儿，防止下一个人（或下一个 AI）把「消灭超参考线的段」当成目标：
    print('         ▶ 不要为了压到参考线以下制造无意义跳切——应在语义节拍/自然停顿/'
          '真正删除内容处切；找不到合理切点就回内容层重新取舍（外部审核 Codex 2026-08-03）')
