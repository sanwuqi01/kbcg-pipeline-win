#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""帧级验收：以真实音频和最终播放时间线验证粗剪与字幕。

用法：p1b_帧级验收.py <工作目录> [--captions]

音频断言检查保留词/删除词覆盖、段头段尾、段内静音和重叠；字幕断言检查
时间连续、字符级对齐、可读宽度与速率。结构不变量只用于防回归，不能冒充
用户审美判断。任何 FAIL 都非零退出；调用方不得自动改参数重试或降级放行。
"""
import sys, os, json, subprocess, wave, unicodedata, hashlib, math
from pathlib import Path
import numpy as np, torch

from expression_review_contract import (
    ExpressionReviewError,
    validate_review as validate_expression_review,
)

WORK = sys.argv[1]
CHECK_CAPS = '--captions' in sys.argv
cfg = json.load(open(f'{WORK}/config.json'))
SR = 16000
rough_path = f'{WORK}/rough_segments.json'
if os.path.isfile(rough_path):
    segs = json.load(open(rough_path))
    SEGMENTS_SOURCE = 'rough_segments.json'
elif os.environ.get('ALLOW_SOURCE_SEGMENTS_FALLBACK') == '1':
    segs = json.load(open(f'{WORK}/segments.json'))
    SEGMENTS_SOURCE = 'segments.json（显式兼容模式）'
else:
    sys.exit('⛔ p1b: 生产锁定模式缺 rough_segments.json；必须先跑 p2_结构编排.py')
_wt_ = json.load(open(f'{WORK}/word_track.json'))

HEAD_PAD = -0.014                       # 必须与 p1 一致
TAIL_ROOM_TARGET_F = 11                # 生成目标：约 0.183s；不是越界许可
TAIL_ROOM_MIN_F = 6                    # 少于 0.10s 且未受下一发音钳制，必须听审
TAIL_ROOM_REVIEW_F = 24                # 安静尾部 >0.40s 需听审
TAIL_ROOM_FAIL_F = 48                  # >0.80s 当作可疑死空气/包回漏词
NOOP_CUT_MAX_F = 3                     # 只删 0–3 帧且无显式边界=无意义切点
BOUND_TOL = 0.10                        # 段边界容差 6 帧
INDEPENDENT_GAP_HARD_S = 2.40           # 独立 VAD 的强异常线，不是自动删停顿阈值
MIN_CARD_F = 10                         # 字幕卡最短 0.167s（B2；与 p3 的最短卡保护同值，见文件末「结构不变量」说明）
FIRST_CARD_SNAP_F = 2                   # 首字距段首≤2帧时，首卡必须贴视频切口
                                        # 2026-08-04 从 30（0.5s）下调：旧值会判掉作者自己的切法——
                                        # 7 份终版的多卡段内共 20 张卡 <0.5s，全库下界是 C2861 的 12 帧(0.20s)「因为」。
                                        # 取 10 帧 = 下界再留 2 帧余量；距 0.02s 闪帧病灶仍有 8 倍距离。
MIN_SEG_REVIEW_F = 18                   # 短段提醒线，不是机械删除线；短不等于无功能
MAX_CPS    = 15.0                       # 字幕阅读速率上限 字/秒（B6；9 份精调实测 max 14.2）

# 段长只做异常提示；不得为了压到 7 秒制造无意义跳切。
# 15 秒改为必看线：不得为了过线自动加刀。22 秒异常线覆盖已被用户否定的 23.87s 案例。
SEG_WARN_S = 7.0
SEG_REVIEW_S = 15.0
SEG_FAIL_S = 22.0
B7_GUIDE = ('段长超出常见范围。不要为了压到阈值以下制造无意义跳切——'
            '应在语义节拍/自然停顿/真正删除内容处切；找不到合理切点时回到内容层重新取舍。')
MTYPE_NOTE = ''
MTYPE = cfg.get('material_type') or '口播'
CHAR_CAP   = {'landscape': 13.0, 'portrait': 10.0}   # 单行字宽上限（B6；与 p3 的 PROFILES 同值）


def clen(s):
    """字符宽度：CJK 全角计 1.0，拉丁/数字/空格/标点计 0.5。
    出处 Netflix 韩语分册「Latin characters, spaces, punctuation count as 0.5 character」。

    ⚠ 这是【照抄】p3_分卡.py 的 clen()，**故意不 import**——
      闸门的判据必须独立于生产侧。共用同一个函数意味着 p3 哪天把口径改错，
      闸门会跟着一起错，这条断言就等于从来没存在过（A1/A3 就是这么废掉的）。
      两边口径若要变，必须两边都改，改不同步正是这条断言该拦下来的事。"""
    return sum(1.0 if unicodedata.east_asian_width(c) in ('W', 'F') else 0.5 for c in s)


def read_audio(path):
    with wave.open(path, 'rb') as w:
        raw = w.readframes(w.getnframes())
    return torch.from_numpy(np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0)


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

from silero_vad import load_silero_vad, get_speech_timestamps
model = load_silero_vad()
wav16 = f'{WORK}/_vad16k.wav'
wav_meta = f'{WORK}/_vad16k.meta.json'
if not os.path.isfile(wav16) or not os.path.isfile(wav_meta):
    sys.exit('⛔ p1b: 缺 _vad16k.wav 或指纹侧车；必须先重跑 p1a')
try:
    _meta = json.load(open(wav_meta))
    _source_identity = _file_identity(cfg['proxy'])
    _wav_hash = _sha256(wav16)
except Exception as e:
    sys.exit(f'⛔ p1b: 无法核验音频缓存身份：{e}')
_track_cache = _wt_.get('audio_cache') or {}
if _meta.get('source') != _source_identity or _track_cache.get('source') != _source_identity:
    sys.exit('⛔ p1b: 音频缓存/词轴与当前 config.proxy 身份不一致；请重跑 p1a')
if _meta.get('wav_sha256') != _wav_hash or _track_cache.get('wav_sha256') != _wav_hash:
    sys.exit('⛔ p1b: _vad16k.wav 内容与词轴/侧车指纹不一致；拒绝验收陈旧缓存')
audio = read_audio(wav16)

# 表达审查由 F1 在调用 p1b 前做 schema 校验；这里再独立确认所有残声都已听审且
# 不存在待修回词轴的有意义漏字，避免绕过统一入口直接运行 p1b 时误报通过。
try:
    _expression_review = json.load(open(f'{WORK}/expression_review.json'))
    _residual_rows = _expression_review.get('residual_candidates')
    if not isinstance(_residual_rows, list):
        raise ValueError('residual_candidates 不是数组')
    for _pos, _row in enumerate(_residual_rows):
        if not isinstance(_row, dict) or _row.get('acoustic_review') != 'heard':
            raise ValueError(f'residual_candidates[{_pos}] 尚未真实听审')
        if _row.get('decision') not in {'filler_drop', 'breath_noise'}:
            raise ValueError(
                f'residual_candidates[{_pos}]={_row.get("decision")!r}；'
                '有意义漏字必须先修回词轴，pending 不得验收'
            )
except (OSError, json.JSONDecodeError, ValueError) as _e:
    sys.exit(f'⛔ p1b: expression_review 残声裁决未闭环：{_e}')

# ⚠ 验收器【独立重算】一次全片 VAD（不读 p1a 的 word_track.json），
# 保持对生成侧的独立性；但必须用【全片】口径而非局部窗口——
# Silero 对输入长度敏感，局部重跑会与生成侧系统性失配，
# 闸门会一口气报十几处 ±25 帧的假偏差（2026-07-28 实测）。
ts_all = get_speech_timestamps(audio, model, sampling_rate=SR, threshold=0.35,
                               min_speech_duration_ms=60, min_silence_duration_ms=120,
                               speech_pad_ms=0)
VB = []
for t_ in ts_all:
    a_, b_ = t_['start']/SR, t_['end']/SR
    if VB and a_ - VB[-1][1] < 0.03:
        VB[-1] = (VB[-1][0], max(VB[-1][1], b_)); continue
    VB.append((a_, b_))

VSPAN = []
ASPECT = 'landscape'                    # B6 用：横 13 字 / 竖 10 字，真相源是 p1a 写的 word_track.json
VSPAN = _wt_.get('voiced_spans', [])
ASPECT = _wt_.get('aspect', 'landscape')

fails, warns = [], []
_contract = {}
_contract_path = f'{WORK}/keep.json'
if os.path.exists(_contract_path):
    try:
        _contract = json.load(open(_contract_path))
    except (OSError, json.JSONDecodeError) as e:
        fails.append(f'A0 无法读取 keep.json：{e}')
_explicit_drop = set()
for _item in _contract.get('drop', []):
    if isinstance(_item, list) and len(_item) >= 2:
        _explicit_drop.update(range(_item[0], _item[1] + 1))
_split_after = {i for i in _contract.get('split_after', []) if isinstance(i, int)}
_drop_boundary_exceptions = set()
for _pos, _item in enumerate(_contract.get('boundary_exceptions', [])):
    try:
        if _item.get('source') != 'human_approved_fcpxml':
            raise ValueError('source 非 human_approved_fcpxml')
        _xml_path = _item['xml_path']
        if _sha256(_xml_path).lower() != _item['xml_sha256'].lower():
            raise ValueError('人工批准 XML 哈希漂移')
        _drop_boundary_exceptions.update(_item['drop_word_indices'])
    except (KeyError, OSError, TypeError, ValueError) as _e:
        fails.append(f'A0R boundary_exceptions[{_pos}] 非法：{_e}')
try:
    validate_expression_review(
        _expression_review,
        track_path=Path(f'{WORK}/word_track.json'),
        track=_wt_,
        keep=_contract,
    )
except ExpressionReviewError as _e:
    fails.append(f'A0E expression_review 与当前词轴/keep 不一致：{_e}')
_retained_long_pauses = [
    _row for _row in _expression_review.get('pause_candidates', [])
    if isinstance(_row, dict) and _row.get('decision') == 'retain'
]
try:
    _cr = json.load(open(f'{WORK}/cut_review.json'))
    _cr_review = _cr.get('review') or {}
    _human_boundary_approved = (
        _cr_review.get('reviewer_type') == 'human'
        and _cr_review.get('status') == 'approved'
    )
except (OSError, json.JSONDecodeError):
    _human_boundary_approved = False
# B7 的报警单独收一份：warns 打印时只取前 10 条，段头/段尾那类结构 warn 动辄十几条，
# 会把 B7 挤出屏幕——**一个被截断的报警等于没有报警**。收完之后插到 warns 最前面。
_b7_warn = []
_cut_review = []

# ---- A0 零丢词：keep 的词必须 100% 出现在成片里 ----
# 2026-07-28 补。此前只对【存在的段】断言，段整个消失反而抓不到——
# 实证：「我在想呀如果我是对面我是AI」11 个词 2.46s 蒸发，闸门全绿放行。
# 这是最该有的一条断言，缺了它其余断言再严也只是在残缺内容上自证清白。
try:
    _tk = json.load(open(f'{WORK}/word_track.json'))['words']
    _keep = set(range(len(_tk)))
    _kp = f'{WORK}/keep.json'
    if not os.path.exists(_kp):
        sys.exit('⛔ p1b: 缺少 keep.json；本流程不提供 EDL 兜底')
    for _it in json.load(open(_kp)).get('drop', []):
        for _i in range(_it[0], _it[1] + 1):
            _keep.discard(_i)
    # ★ 2026-07-31 改判据：原来查的是「词 id 有没有被列进某段的 words 名册」——
    #   那是 p1 自己写的名册，属于自证。段被腰斩时词还挂在名册上，A0 照样放行。
    #   实证：段83「筛选进行跟踪」被覆盖表砍成 0.30s（6 个字 0.05s/字），
    #   后 5 个词的声音全被切掉，而 A0 全绿。
    #   现在改成【发音区间是否真被某个段的时间范围覆盖】——这是对时间的断言，不是对名册的。
    # ⚠ 必须用 in_f/out_f（帧），不能用 in/out（秒）——2026-08-02 实证：
    #   p4f 出片用的是 in_f/out_f，闸门若读 in/out 就是在【量计划、不量成品】。
    #   C2864 段32 曾出现 out=142.20s 而 out_f=136.63s（跨段语块合并只同步了秒），
    #   A0 按 out 判定丢词 31 个、按真实成品判定丢词 56 个，25 个词落在闸门盲区。
    #   根因已在 p1 修掉，这里改口径是第二道保险：闸门永远对着【真正会变成时间线的数字】断言。
    _spans = [(s['in_f'] / 60.0, s['out_f'] / 60.0) for s in segs]
    def _covered(w):
        d = w['ve'] - w['vs']
        if d <= 0:                                  # 零时长幻觉词，按中点判
            m = w['vs']
            return any(a - 0.02 <= m <= b + 0.02 for a, b in _spans)
        ov = sum(max(0.0, min(b, w['ve']) - max(a, w['vs'])) for a, b in _spans)
        return ov >= d * 0.5                        # 发音至少一半在成片里
    # 阈值以【作者终版】定标，不是拍脑袋（2026-07-31 实测同一份 keep.json 下他的终版）：
    #   他的 keep 词里 8 个重叠 =0%、17 个 <50%；drop 词残留合计 1.81s、最大 0.41s。
    #   单个词的起音被切掉在人工精剪里是常态——按「一半重叠」当 FAIL，他本人的终版也过不了。
    #   A0 真正要抓的是【整段蒸发】：连续多个词一起消失。
    _lost = sorted(i for i in _keep if not _covered(_tk[i]))
    _byname = {x['i'] for s in segs for x in (s.get('words') or [])}
    _gone = [i for i in _lost if sum(max(0.0, min(b, _tk[i]['ve']) - max(a, _tk[i]['vs']))
                                     for a, b in _spans) <= 0.001]
    _runs2, _c2 = [], []
    for _i in _gone:
        if _c2 and _i == _c2[-1] + 1: _c2.append(_i)
        else:
            if _c2: _runs2.append(_c2)
            _c2 = [_i]
    if _c2: _runs2.append(_c2)
    _vanish = [r for r in _runs2
               if len(r) >= 2 and sum(_tk[i]['ve'] - _tk[i]['vs'] for i in r) >= 0.30]
    if _vanish:
        for _r in _vanish[:6]:
            _d = sum(_tk[i]['ve'] - _tk[i]['vs'] for i in _r)
            fails.append(f'A0 整段蒸发 词[{_r[0]}-{_r[-1]}] @{_tk[_r[0]]["vs"]:.2f}s / {_d:.2f}s '
                         f'「{"".join(_tk[i]["t"] for i in _r)[:30]}」')
    if _lost:
        _txt = ''.join(_tk[i]['t'] for i in _lost)
        _dur = sum(_tk[i]['ve'] - _tk[i]['vs'] for i in _lost)
        _g = sum(1 for i in _lost if i in _byname)
        warns.append(f'A0 {len(_lost)} 个 keep 词声音被切掉 / {_dur:.2f}s'
                     f'（{_g} 个仍挂在段名册上，旧口径看不见）：{_txt[:40]}'
                     f'　他终版同口径 8 个，超出即需复核')
    # ---- A0R 反向：被删的词，声音也必须真的删掉（2026-07-31 新增）----
    # 此前闸门【只有单向断言】：keep 的词不许丢。而反方向——drop 的词声音还在不在——
    # 一条断言都没有。于是 p1「段头取人声块起点」把同块内被删词的声音原样带回成片，
    # keep.json 明明删了「呢」「作」「这个业」，观众照样听得见，闸门全绿。
    # 作者的判词「废话结巴的位置没删干净」指的就是它。
    # 这是本轮最贵的教训：**闸门只断言一个方向，另一个方向就是盲区。**
    _dropped = sorted(set(range(len(_tk))) - _keep)
    _still, _zero_drop_leaks = [], []
    for _i in _dropped:
        _w = _tk[_i]
        _d = _w['ve'] - _w['vs']
        if _d <= 0.02:
            # 零/极短时长没有可积的音频区间，不能套“覆盖时长 ≥50%”。但它也并非
            # 永远不可判：若该词的时码点完全落在最终播放区间之外，声音不可能被
            # 这条成片带回；若词仍挂在段名册或时码点落在播放区间内，才需要失败。
            _m = (_w['vs'] + _w['ve']) / 2
            _point_covered = any(a - 0.02 <= _m <= b + 0.02 for a, b in _spans)
            if _i in _byname or _point_covered:
                _zero_drop_leaks.append(_i)
            continue
        _ov = sum(max(0.0, min(b, _w['ve']) - max(a, _w['vs'])) for a, b in _spans)
        if _ov >= _d * 0.5 and _i not in _drop_boundary_exceptions:
            _still.append((_i, _ov))
        elif _ov >= _d * 0.5:
            warns.append(
                f'A0R 人工批准 XML 豁免词[{_i}]「{_w["t"]}」的旧转写时码重叠；'
                '已核验 XML 哈希，豁免只针对本词几何误报'
            )
    if _zero_drop_leaks:
        _msg = (f'A0R 零时长/极短 drop 的时码点仍落在成片内或仍挂段名册 '
                f'{_zero_drop_leaks[:12]}，无法证明声音已删净')
        fails.append(_msg)
    # explicit drop 是内容决策，不存在“累计残留配额”。任何一个词有至少一半发音仍被
    # 最终段覆盖都必须失败；0.4s 口头语不能再被旧的 >0.45s 门槛放行。
    if _still:
        _t = ''.join(_tk[i]['t'] for i, _ in _still)
        _s = sum(o for _, o in _still)
        fails.append(f'A0R 已删的词声音仍在成片里 {len(_still)} 个 / 合计 {_s:.2f}s'
                     f'（任一 explicit drop 覆盖≥50% 即失败）：{_t[:50]}')
        for _i, _o in _still[:6]:
            fails.append(f'   └ 词[{_i}] @{_tk[_i]["vs"]:.2f}s 「{_tk[_i]["t"]}」残留 {_o:.2f}s')
except FileNotFoundError:
    warns.append('A0 跳过（无 word_track.json）')

# ---- A7 段内词序必须递增（防止合并/吸收逻辑把词顺序打乱）----
# 2026-07-29 实证：过短段「吸收」逻辑把词 661「筛」塞进下一段却排在末尾，
# 字幕变成「选进行跟踪筛」——文本顺序错乱，观众直接看到乱码般的字幕。
for i, s in enumerate(segs):
    ws_ = s.get('words') or []
    idx = [w['i'] for w in ws_]
    if idx != sorted(idx):
        bad = [f"{w['w']}[{w['i']}]" for w in ws_]
        fails.append(f'A7 段{i} 词序错乱: {" ".join(bad[:10])}')
    txt = ''.join(w['w'] for w in ws_)
    if ws_ and txt != s['text']:
        fails.append(f'A7 段{i} text 与 words 不一致: {s["text"][:20]!r} vs {txt[:20]!r}')

# ---- A8 切口安全：词内切口硬失败，贴 drop/VAD 边界必须明示听审 ----
def _ceil_f(t):
    return int(math.ceil(t * 60 - 1e-9))


def _floor_f(t):
    return int(math.floor(t * 60 + 1e-9))


for i, s in enumerate(segs):
    listed = s.get('words') or []
    idx = [w.get('i') for w in listed if isinstance(w.get('i'), int)]
    if not idx:
        continue
    first_i, last_i = min(idx), max(idx)
    if not (0 <= first_i < len(_wt_['words']) and 0 <= last_i < len(_wt_['words'])):
        fails.append(f'A8 段{i} 词下标越界：{first_i}-{last_i}')
        continue
    first_w, last_w = _wt_['words'][first_i], _wt_['words'][last_i]
    in_f, out_f = s['in_f'], s['out_f']
    # 强制对齐存在时用逐字 ws；否则使用 p1a 的声学重定位 vs。
    # 不能拿未经对齐的 whisper ws 当硬起音：C2901 的「一定」原始 ws
    # 比独立 VAD/重定位早 220ms，会把一段安全的静音内收误判成“词内切口”。
    first_forced_aligned = bool(
        first_w.get('align_text')
        and first_w.get('char_times')
        and first_w.get('ve', 0) > first_w.get('vs', 0)
    )
    first_start_f = _ceil_f(
        first_w['ws'] if first_forced_aligned else first_w['vs']
    )
    last_end_f = _ceil_f(last_w['we'])
    if in_f > first_start_f:
        (_cut_review if _human_boundary_approved else fails).append(
            f'A8 段{i} 词内切口：段头 {in_f}f 晚于首词[{first_i}]'
            f'「{first_w["t"]}」安全起音帧 {first_start_f}f'
            + ('；边界来自用户批准 XML，降为人工基准提示' if _human_boundary_approved else '')
        )
    if out_f < last_end_f:
        (_cut_review if _human_boundary_approved else fails).append(
            f'A8 段{i} 词内切口：段尾 {out_f}f 早于末词[{last_i}]'
            f'「{last_w["t"]}」完整结束帧 {last_end_f}f'
            + ('；边界来自用户批准 XML，降为人工基准提示' if _human_boundary_approved else '')
        )
    next_i = last_i + 1
    next_is_drop = next_i < len(_wt_['words']) and next_i in _explicit_drop
    if next_is_drop:
        drop_cap_f = _floor_f(_wt_['words'][next_i]['ws'])
        if out_f > drop_cap_f:
            (_cut_review if _human_boundary_approved else fails).append(
                f'A8 段{i} 段尾 {out_f}f 越过下一 drop 词[{next_i}]'
                f'「{_wt_["words"][next_i]["t"]}」起音上界 {drop_cap_f}f'
                + ('；用户批准 XML 与词轴几何冲突，已由 A0R 独立复核' if _human_boundary_approved
                   else '，已包回被删声音')
            )
    tail_room_f = out_f - last_end_f
    if tail_room_f < TAIL_ROOM_MIN_F:
        reason = ('下一 drop/发音起点钳制' if next_is_drop else '安静尾部不足')
        _cut_review.append(
            f'A8 段{i} 段尾贴末词边界（{tail_room_f} 帧尾部，{reason}），'
            '需逐切口听审，不得默认正常'
        )

# 同一 VAD 块内删掉中间词后再接起来，声学上没有天然块口可依赖；
# 机器只能确认几何不包 drop，最终音节是否自然必须听审。
_source_order = sorted(
    (s for s in segs if s.get('words')),
    key=lambda s: min(w['i'] for w in s['words']),
)
for left, right in zip(_source_order, _source_order[1:]):
    li = max(w['i'] for w in left['words'])
    ri = min(w['i'] for w in right['words'])
    between = sorted(i for i in _explicit_drop if li < i < ri)
    if not between:
        continue
    lb = _wt_['words'][li].get('blk', -1)
    rb = _wt_['words'][ri].get('blk', -1)
    if lb >= 0 and lb == rb:
        _cut_review.append(
            f'A8 同 VAD 块删接：词[{li}]→[{ri}] 中间 drop {between[:12]} '
            f'均在块{lb}，需逐切口听审'
        )

# ---- A4/A5/A6：段间结构（纯几何，不用跑 VAD）----
def _registered_repeat_pair(a, b):
    """只有结构层登记的同组 Hook 正文复现，才允许相同源区间播放两次。"""
    am = a.get('_rough_structure') or {}
    bm = b.get('_rough_structure') or {}
    return (
        am.get('intentional_repeat') is True
        and bm.get('intentional_repeat') is True
        and isinstance(am.get('repeat_group_id'), str)
        and am.get('repeat_group_id') == bm.get('repeat_group_id')
        and round(a['in'], 6) == round(b['in'], 6)
        and round(a['out'], 6) == round(b['out'], 6)
    )

ss = sorted(segs, key=lambda x: x['in'])
for a, b in zip(ss, ss[1:]):
    if b['in'] < a['out'] - 1e-6 and not (
        _registered_repeat_pair(a, b) or _registered_repeat_pair(b, a)
    ):
        fails.append(f"A4 音频重叠 {a['out']-b['in']:.2f}s：[{a['in']:.2f}-{a['out']:.2f}] 与 [{b['in']:.2f}-] = 回声")
seen = {}
for i, s in enumerate(segs):
    k = (round(s['in'], 3), round(s['out'], 3))
    if k in seen:
        previous = segs[seen[k]]
        if _registered_repeat_pair(previous, s) or _registered_repeat_pair(s, previous):
            _cut_review.append(
                f"A5R 段{seen[k]} 与 段{i} 是结构层登记的 Hook/正文条件式复现 "
                f"[{s['in']:.2f}-{s['out']:.2f}]"
            )
        else:
            fails.append(f"A5 段{seen[k]} 与 段{i} 源区间完全相同 [{s['in']:.2f}-{s['out']:.2f}]"
                         f" = 未登记的同一素材重复播放")
    seen[k] = i
tl = 0
for i, (a, b) in enumerate(zip(segs, segs[1:])):
    # 源上只删 0–3 帧，听觉/画面上没有可感知价值，不应留一刀。
    gap_f = b['in_f'] - a['out_f']
    if 0 <= gap_f <= NOOP_CUT_MAX_F:
        last_i = max((w['i'] for w in a.get('words') or []), default=-1)
        if last_i in _split_after:
            _cut_review.append(
                f'A6S 段{i}→{i+1} @{a["out"]:.2f}s 是 split_after[{last_i}] '
                '语义强制分段；允许源连续，但需听审切口'
            )
        else:
            fails.append(f"A6 段{i}→{i+1} @{a['out']:.2f}s 只删 {gap_f} 帧 = 零剪辑意义的切点，应合并")

# ---- A1/A2/A3：段边界与段内，逐段独立重跑 VAD ----
for i, s in enumerate(segs):
    a, b = s['in'], s['out']
    ins = [(x, y) for x, y in VB if y > a + 0.02 and x < b - 0.02]
    tag = f'段{i} [{a:.2f}-{b:.2f}] {s["text"][:14]}'
    # B5 短段只能提醒，不能凭时长判死。C2897 用户版保留了
    # 0.25s 的「过程」，它承担必要命题连接；相反，0.19s 的孤立
    # 「好」可能没有功能。正确问题是“这个短段承担什么”，不是“它是否超过 18 帧”。
    if (b - a) * 60 < MIN_SEG_REVIEW_F:
        _cut_review.append(
            f'{tag} | B5 短段 {(b-a)*60:.0f} 帧 <{MIN_SEG_REVIEW_F} 帧；'
            '必须确认承担完整词义/语法/结构功能，否则并回或删除'
        )
    # ---- B7 段长两档：WARN=超出常见范围（不中止）/ FAIL=明显不合格（中止）----
    # 2026-08-02 新增时是【FAIL 硬闸门 7.0s】；2026-08-03 按外部审核降为分档报警，
    # 阈值与依据见文件头「段长两档」那一段（他自己精调的 C2864 有 8.47s/7.58s 两个段，
    # 旧版会把他的手艺判成不合格）。
    # ⚠ 原始病灶记录保留，这条断言本身没有错、绝不能撤：
    #   AI 版 C2864 有 7 个 >7s 的段、最长 23.87s（一镜到底不换景），
    #   而此前【一条断言都没有】，p1b 全绿 → p4f DTD 终检 → 交付，一路放行。
    #   错的只是「把 7.0 当法律」，不是「关心段长」。
    # ⚠ 别拿 C2867 说事——那份是他在 AI 草稿上改的，里面的 16 秒长段是他没顾上改的 AI 残留，
    #   不是他的偏好。
    # 【已作废的旧说法·存档备忘】旧注释写着「留 0.02s 余量是有意的：这条线本来就该贴着
    #   他的上界画」——这句话正是把统计上界当成法律的自白，2026-08-03 随本次改判一并废止。
    #   贴着历史上界画线，等于规定他以后不许剪出比过去更长的段。留着这行是为了记住这个念头。
    # ⚠ 不给 ov 段免检（A1/A2/B4 那种免检不适用）：人工覆盖段的长度照样报出来，
    #   只是同样只报警不判死——覆盖表脏了要看得见。
    _sl = b - a
    if _sl > SEG_FAIL_S:
        _b7_warn.append(f'{tag} | B7 连续原片 {_sl:.2f}s 超强提醒线 {SEG_FAIL_S:.1f}s'
                        f'（{MTYPE}；超 {_sl-SEG_FAIL_S:.2f}s）'
                        f'{"［人工覆盖段，请查覆盖表］" if s.get("ov") else ""}'
                        '；只提示复核内容密度/后续包装，不允许为了段长凭空加刀')
    elif _sl > SEG_REVIEW_S:
        _b7_warn.append(f'{tag} | B7 段长 {_sl:.2f}s 超必看线 {SEG_REVIEW_S:.1f}s；'
                        '不自动加刀，必须在内容取舍/包装节奏中明确处理')
    elif _sl > SEG_WARN_S:
        _b7_warn.append(f'{tag} | B7 段长 {_sl:.2f}s 超出{MTYPE}常见范围 {SEG_WARN_S:.1f}s'
                        f'（仅报警，不判不合格）{MTYPE_NOTE}'
                        f'{"［人工覆盖段］" if s.get("ov") else ""}')
    if not ins:
        # 整段 VAD 无声：只有当段内确有弱音词时才容忍
        if s.get('words'):
            warns.append(f'{tag} | 弱音段（VAD 无声但有词，内容红线保留）')
        else:
            fails.append(f'{tag} | 段内完全无人声且无词')
        continue
    dh = a - (ins[0][0] + HEAD_PAD)
    dt = b - ins[-1][1]
    # 断言是【单向】的，方向不能搞反：
    #   段头【早于】发音起 = 段头带进静音或咬到前一句 → 真错误，FAIL
    #   段头【晚于】发音起 = 内容决策删掉了本块前半段，段从块内部起 → 正常，放行
    # 段尾晚于发音止不再等同“拖尾”：C2897 用户明确要求保留尾音衰减后的
    # 一小段安静余量。只有超长安静/疑似包回漏词才拦截；早于块止可能是 drop/split_after，
    # 也可能是段尾咬字，因此必须显式进听审清单，不再用「内容删除所致，正常」概括放行。
    if dh < -BOUND_TOL:
        fails.append(f'{tag} | A1 段头早于真实发音起 {-dh*60:.0f} 帧（带静音/咬前句）')
    elif dh > BOUND_TOL:
        warns.append(f'{tag} | 段头在块内部 +{dh*60:.0f} 帧（内容删除所致，正常）')
    if dt * 60 > TAIL_ROOM_FAIL_F:
        fails.append(f'{tag} | A2 段尾晚于真实发音止 {dt*60:.0f} 帧'
                     f'（>{TAIL_ROOM_FAIL_F}帧，疑似死空气或包回漏词）')
    elif dt * 60 > TAIL_ROOM_REVIEW_F:
        _cut_review.append(
            f'{tag} | A2 安静尾部 {dt*60:.0f} 帧超常用余量，需听审确认不是死空气/漏词'
        )
    elif dt < -BOUND_TOL:
        listed = s.get('words') or []
        last_i = max((w['i'] for w in listed), default=-1)
        next_drop = last_i + 1 in _explicit_drop
        if next_drop:
            why = '同块 drop 删接'
        elif last_i in _split_after:
            why = 'split_after 语义边界'
        else:
            why = '原因未证明'
        _cut_review.append(
            f'{tag} | A2 段尾位于 VAD 块内 {dt*60:.0f} 帧（{why}），'
            '需逐切口听审，不得自动判正常'
        )
    for (p0, p1), (n0, n1) in zip(ins, ins[1:]):
        if n0 - p1 >= INDEPENDENT_GAP_HARD_S:
            retained = next(
                (
                    row for row in _retained_long_pauses
                    if abs(float(row['left_end']) - p1) <= 0.35
                    and abs(float(row['right_start']) - n0) <= 0.35
                ),
                None,
            )
            if retained:
                _cut_review.append(
                    f'{tag} | A3 段内 {n0-p1:.2f}s 无人声 @{p1:.2f}s，'
                    f'已听审 retain/{retained.get("function")}：{retained.get("reason")}'
                )
            else:
                fails.append(
                    f'{tag} | A3 段内 {n0-p1:.2f}s 无人声 @{p1:.2f}s'
                    f'（超过独立异常线 {INDEPENDENT_GAP_HARD_S:.1f}s，'
                    '需在 pause_candidates 明确压缩或登记功能保留）'
                )

# B7 报警插到 warns 最前面（保证不被 warns[:10] 截掉），并带上处理方向那句——
# 这句话是本条改造的重点：报警要说「怎么办」，否则下一个人只会去压阈值。
if _b7_warn:
    warns[:0] = [f'B7 段长超常见范围 {len(_b7_warn)} 处（{MTYPE}：>{SEG_WARN_S:.1f}s，'
                 f'不合格线是 {SEG_FAIL_S:.1f}s）。{B7_GUIDE}'] + _b7_warn
if _cut_review:
    _a8_state = (
        '边界来自用户批准 XML，机器几何冲突已降为可审计提示；Agent 未冒充独立听审'
        if _human_boundary_approved
        else '词内切口依然是 FAIL'
    )
    warns[:0] = [f'A8 切口提示 {len(_cut_review)} 处（{_a8_state}）'] + _cut_review

# ---- B：字幕层 ----
if CHECK_CAPS:
    plan = json.load(open(f'{WORK}/captions_plan.json'))
    stl = plan['segments_tl']
    cards = sorted(plan['normal'] + plan['highlight'], key=lambda c: c['sf'])
    _bridge_card_ids = {
        identity
        for bridge in (plan.get('bridges') or [])
        for identity in (
            (bridge.get('left_si'), bridge.get('left_card_index')),
            (bridge.get('right_si'), bridge.get('right_card_index')),
        )
    }
    if plan.get('segments_source') != SEGMENTS_SOURCE:
        fails.append(f'B0 字幕计划绑定 {plan.get("segments_source")!r}，当前验收绑定 {SEGMENTS_SOURCE!r}')
    if len(stl) != len(segs):
        fails.append(f'B0 字幕段表 {len(stl)} 段 ≠ 最终播放段 {len(segs)} 段')
    for i, (st, sg) in enumerate(zip(stl, segs)):
        if st.get('in_f') != sg.get('in_f') or st.get('out_f') != sg.get('out_f'):
            fails.append(f'B0 段{i} 字幕计划素材边界与 {SEGMENTS_SOURCE} 不一致')

    # ---- B6 字宽 / CPS：每张卡都不许超画幅单行上限（2026-08-02 新增）----
    # 这是本文件迟到最久的一条断言。skill 自己的规范白纸黑字写着「横版普通字幕只能一行」，
    # p3 也确实算了 MAX_CHARS——但那个上限【只长在 jieba/DP 那条路径上】。
    # 第十一轮把断句权交给 AI 写 cards.json 之后走 texts = want 分支，**字宽约束整条蒸发**，
    # 而闸门这边从来没查过，于是两头都没人管。
    # 实证放行：AI 版 C2864 出了一张 35 字 / 0.90s = 38.9 字/秒 的卡
    #   「你肯定不是这样子我反而如果说我经过招了我肯定是要我必须是招这种会沟通的」
    #   = 硬上限 16 字的 2.2 倍、估算宽度是画面的 2.15 倍、必然折 3 行，
    #   却一路过了 p1b 全绿 + p4f DTD 终检 + 交付。他 8 份纯剪终版的卡最宽只有 13 字。
    # 判据用【显示文本 text】而不是 align（对齐原文）：观众看见的是 text，
    #   折不折行、读不读得完都由它决定；v1 已禁止跨段借字与 display 删字旁路。
    _cap = CHAR_CAP.get(ASPECT, CHAR_CAP['landscape'])
    _wide, _fast = [], []
    for c in cards:
        _w = clen(c['text'])
        _d = (c['ef'] - c['sf']) / 60.0
        if _w > _cap + 1e-9:
            _wide.append((c, _w))
        if _d > 0 and _w / _d > MAX_CPS:
            _fast.append((c, _w, _w / _d))
    if _wide:
        fails.append(f'B6 字宽超限 {len(_wide)}/{len(cards)} 张（{ASPECT} 单行上限 {_cap:.0f} 字，'
                     f'超了必折行——作者的硬规范是「横版普通字幕只能一行」）')
        for c, _w in sorted(_wide, key=lambda x: -x[1])[:6]:
            fails.append(f'   └ 卡@{c["sf"]/60:.2f}s 宽 {_w:.1f} 字（超 {_w-_cap:.1f}，'
                         f'{_w/_cap:.2f}× 画幅）/ {(c["ef"]-c["sf"])/60.0:.2f}s「{c["text"]}」')
    if _fast:
        fails.append(f'B6 CPS 超限 {len(_fast)}/{len(cards)} 张（>{MAX_CPS:.0f} 字/秒 观众读不完；'
                     f'9 份精调终版实测 max 14.2 字/秒）')
        for c, _w, _cps in sorted(_fast, key=lambda x: -x[2])[:6]:
            fails.append(f'   └ 卡@{c["sf"]/60:.2f}s {_cps:.1f} 字/秒（超 {_cps-MAX_CPS:.1f}）'
                         f' = {_w:.1f} 字 / {(c["ef"]-c["sf"])/60.0:.2f}s「{c["text"][:24]}」')

    by_seg = {}
    for c in cards:
        hit = [i for i, st in enumerate(stl) if st['tl_in_f'] <= c['sf'] < st['tl_out_f']]
        if not hit:
            fails.append(f'B0 卡@{c["sf"]/60:.2f}s「{c["text"][:10]}」不在任何段内'); continue
        if not c.get('text'):
            fails.append(f'B0 卡@{c["sf"]/60:.2f}s 主字幕为空'); continue
        if c.get('align', c['text']) != c['text']:
            fails.append(f'B0 卡@{c["sf"]/60:.2f}s text 与 align 不一致；v1 禁止声音/屏显分叉'); continue
        if c.get('ef', 0) <= c['sf'] or c['ef'] > stl[hit[0]]['tl_out_f']:
            fails.append(f'B0 卡@{c["sf"]/60:.2f}s 越界或零时长'); continue
        by_seg.setdefault(hit[0], []).append(c)
    _need_caps = [i for i, (sg, st) in enumerate(zip(segs, stl))
                  if sg.get('words') or st['tl_out_f'] > st['tl_in_f']]
    _missing_caps = [i for i in _need_caps if i not in by_seg]
    if _missing_caps:
        fails.append(f'B0 最终播放段缺主字幕 {_missing_caps[:12]}；覆盖率必须 100%')
    for si_, cs in by_seg.items():
        st = stl[si_]
        _sg = segs[si_]
        cs.sort(key=lambda c: c['sf'])
        # B2 最短卡 0.5s——只对【段内多卡】断言。单卡段的卡长 == 段长，
        # 由内容决定（终版里就有 0.50s 的「系统」段），不归字幕层管。
        if len(cs) > 1:
            for c in cs:
                if (
                    c['ef'] - c['sf'] < MIN_CARD_F
                    and (si_, c.get('card_index')) not in _bridge_card_ids
                ):
                    fails.append(f'B2 段{si_} 卡@{c["sf"]/60:.2f}s「{c["text"][:10]}」只有 '
                                 f'{(c["ef"]-c["sf"])/60:.2f}s（<0.5s，闪一下就没，旧版出过 0.02s）')
        # 段首微差与真正的开口等待分开：若首字声学起点距视频切口≤2帧，量化后的
        # 1帧空档在 FCP 中会直接表现为“视频先走、字幕后出”，必须贴切口；只有首字
        # 明确晚于2帧时，才允许首卡等待真实开口（最多24帧）。
        _fd = cs[0]['sf'] - st['tl_in_f']
        _first_word = (_sg.get('words') or [{}])[0]
        _first_rows = _first_word.get('char_times') or []
        _first_t = (_first_rows[0].get('s') if _first_rows else _first_word.get('s'))
        # ⚠ 这里必须用【帧网格】口径，不能「连续时间再四舍五入」。
        #   同一件事（首字声学起点距段头几帧）全链只有一个可审计的度量：
        #   p2b_生成切口清单 / decision_contract 写进 cut_review.json 的
        #   head_preroll_frames = floor(首字起音 * 60) - in_f，它也是 cut_review
        #   逐段听审要签字的数字。B1S/B4 若改用 round((t - in) * 60)，同一段会算出
        #   不同的帧数——2026-09-14 Windows 实测（中文口播视频-测试 段 L01.3）：
        #   切口 in=12.466667(748f)，首字「4」(sì)起音 12.512 → 帧网格 750-748 = 2 帧
        #   （cut_review 记 2，满足 2 帧预卷目标），而 round((12.512-12.466667)*60)=3，
        #   恰好越过 FIRST_CARD_SNAP_F=2 的豁免线，于是首卡（按 p3 设计必贴切口）
        #   被 B4 判成「比人开口早 10 帧」——那 10 帧是 VAD 听不见「s」清擦音造成的，
        #   真实可听语音只比切口晚 2.72 帧（≈45ms）。量法不统一会在这里制造假 FAIL。
        _first_acoustic_delta = (
            math.floor((float(_first_t) - _sg['in']) * 60 + 1e-9)
            if isinstance(_first_t, (int, float)) else None
        )
        if _first_acoustic_delta is not None and _first_acoustic_delta <= FIRST_CARD_SNAP_F:
            if _fd != 0:
                fails.append(
                    f'B1S 段{si_} 首字距切口 {_first_acoustic_delta} 帧，首卡却晚 {_fd} 帧；'
                    f'≤{FIRST_CARD_SNAP_F}帧的量化微差必须吸附视频切口'
                )
        elif _fd < 0 or _fd > 24:
            fails.append(f'B1 段{si_} 首卡起点差 {_fd} 帧（首字明确晚于切口时允许 0~24 帧）')
        if abs(cs[-1]['ef'] - st['tl_out_f']) > 1:
            fails.append(f'B1 段{si_} 末卡终点差 {cs[-1]["ef"]-st["tl_out_f"]} 帧')
        for x, y in zip(cs, cs[1:]):
            if y['sf'] - x['ef'] > 1:
                fails.append(f'B1 段{si_} 卡间空 {y["sf"]-x["ef"]} 帧')
        # B4 字幕切换点必须跟人声对齐（作者 2026-07-28 定的硬原则）
        # 「说话部分开始，才有字幕出现，而不是说话结尾空余没有音频处出现了
        #   第二段字幕的开始……字幕和人声对齐，要不然视觉会出现错位。」
        # 允许：切点落在说话中（句中换卡）、或落在静音但 ≤3 帧就开口。
        # 禁止：切点落在静音里干等 >3 帧——画面上就是字幕先跳出来、人还没张嘴。
        # 2026-08-01 第十一轮：B4 纳入首卡。旧版只查 cs[1:]，「首卡=段起」被当公理免检，
        # 而段起是 VAD 块边界（含吸气/起音噪声）——实测 C2867 有 12% 的首卡抢跑 3~22 帧，
        # 作者逐张手动后移。现在首卡由 p3 独立对齐开口（限幅 24 帧），这里全卡统一断言。
        # 距开口 >30 帧不报：那是弱音段（VAD 无声但有词、内容红线保留），由 A 类断言把关。
        if VSPAN:
            for _ci, _c in enumerate(cs):
                # 首字已由字符级强制对齐证明就在切口附近时，弱辅音可能尚未触发 VAD；
                # 此处不得再用较迟的 VAD 元音起点推翻 B1S 的贴切口结论。
                if (
                    _ci == 0
                    and _first_acoustic_delta is not None
                    and _first_acoustic_delta <= FIRST_CARD_SNAP_F
                    and _c['sf'] == st['tl_in_f']
                ):
                    continue
                _t = _sg['in'] + (_c['sf'] - st['tl_in_f']) / 60.0
                _inv = any(a <= _t < b for a, b in VSPAN)
                if _inv:
                    continue
                _nxt = min((a for a, b in VSPAN if a > _t), default=None)
                if _nxt is not None and 3.5 < (_nxt - _t) * 60 <= 30:
                    fails.append(f'B4 段{si_} 卡「{_c["text"][:10]}」在人开口前 '
                                 f'{(_nxt-_t)*60:.0f} 帧就切了（字幕先跳出来，视觉错位）')

        # B3 字幕必须与段词【逐字全等】——既防 AI 凭空编字，也防声音保留但屏显删字。
        # ⚠ 判据是 segments.json 的 words 名册文本，不是「段时间区间内真实响过的音」，
        #   所以它管不住「名册里的词音频被切掉、字幕却还在」这种情形。
        #   那一类由 A0 负责（A0 已于 2026-07-31 改成时间口径）。这里只管文本来源合法。
        # v1 中 text 与 align 已在 B0 强制全等；这里仍读 align 作为来源对账字段。
        seg_txt = ''.join(w['w'] for w in (segs[si_].get('words') or []))
        card_txt = ''.join(c.get('align', c['text']) for c in cs)
        if card_txt != seg_txt:
            fails.append(f'B3 段{si_} 卡文本与段词文本不一致:\n'
                         f'      卡: {card_txt[:40]}\n      段: {seg_txt[:40]}')

print('=' * 64)
# ---- 覆盖表免检声明（2026-08-03 补，外部审核验证员发现的缺口）----
# ov 段（边界抄自作者终版）会跳过 A1/A2/B4。以前这个事实只是 warns 池里一条普通 warn，
# 而 warns 只打印前 10 条——**一个被截断的免检声明等于没有声明**，
# 于是「✅ 帧级验收全过 — 边界贴合真实发音(A1/A2)」可以在 A1/A2 从未被评估的情况下打出来。
for w_ in warns[:10]:
    print('  ⚠ ' + w_)
if len(warns) > 10:
    print(f'  ⚠ …另有 {len(warns)-10} 条 warn 未显示')
if fails:
    print(f'⛔ 帧级验收 FAIL {len(fails)} 处（段 {len(segs)}）：')
    for f in fails[:30]:
        print('  ' + f)
    sys.exit(1)
# 这行只能说「没踩到不合格线」，不能说「剪得像他」——B7 现在是报警不是质量证明，
# 措辞必须跟着降级，否则「全过」两个字又会被当成质量背书（第 12 轮的老毛病）。
print(f'✅ 帧级验收全过：{len(segs)} 段'
      f'{"（含字幕）" if CHECK_CAPS else "（仅音频边界）"}'
      + (' — 用户批准 XML 的边界例外已逐项留痕(A1/A2)'
         if _human_boundary_approved else ' — 边界贴合真实发音(A1/A2)')
      + f' · 留删双向一致(A0/A0R) · 段内无 >{INDEPENDENT_GAP_HARD_S:.1f}s 未处理停顿(A3)'
      + ' · 段长只报警、不制造跳切(B7)'
      + ((f' · {len(_cut_review)} 处机器几何提示已有用户批准，Agent未独立听审(A8)'
          if _human_boundary_approved else f' · {len(_cut_review)} 处切口仍待人工听审(A8)')
         if _cut_review else '')
      + (f'，{len(_b7_warn)} 段超出{MTYPE}常见范围已报警(非不合格)' if _b7_warn else '')
      + (f' · 字幕单行可读(B6)' if CHECK_CAPS else ''))
