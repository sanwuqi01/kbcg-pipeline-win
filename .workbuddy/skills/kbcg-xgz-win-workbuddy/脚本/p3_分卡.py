#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工序③：把已审阅的 cards.json 投影为精确字幕时码。

输入：
- rough_segments.json：已冻结粗剪的最终播放顺序。
- word_track.json：字符级声学时码。
- cards.json：AI 按句法人工决策的字幕卡；缺失或文本不守恒即停止。

本脚本不决定断句，不调用 jieba/词性统计，也没有静默兜底。它只校验 cards，
用字符级时码生成 captions_plan.json，并处理显式 bridge_to_next 字幕桥接。
生产交付禁止绕过 rough_segments.json。
"""
import json, sys, os, re, unicodedata

from caption_boundary_contract import CaptionBoundaryError, validate_atomic_boundaries

WORK = sys.argv[1]
FPS = 60
MIN_CARD_F = 10                     # 最短卡 0.167s（多卡段才断言；单卡段卡长==段长）
FIRST_CARD_SNAP_F = 2               # 首字距视频切口≤2帧时，首卡必须贴切口，不能留1帧黑缝
                                    # 2026-08-04 从 30（0.5s）下调：旧值会判掉他自己的切法。
                                    # 全部 7 份有多卡段的终版实测，多卡段内 <0.5s 的卡共 20 张：
                                    #   C2861   3 张（最短 12 帧 0.20s「因为」）← 全库下界
                                    #   C2880   5 张（17~29 帧）「对不对」「对吧」「也就意味着」…
                                    #   九紫离火 5 张（15~29 帧）「但是」「是吧」「消费」…
                                    #   C2862 4 张 · C2867 1 张 · 用上AI反而更忙 2 张
                                    # 取 10 帧 = 全库下界 12 帧再留 2 帧余量；
                                    # 距旧版那个 0.02s(1.2 帧) 闪帧病灶仍有 8 倍距离，拦得住。
                                    # ⚠ 先定 15 帧是错的——那是只查了 C2880+九紫离火两份得出的下界，
                                    #   C2861 的 12 帧卡会被误判。跨全部终版复核才定到 10。

# ---- 画幅 profile：max_chars 是单行字宽护栏，不是机械切句器 ----
PROFILES = {
    # DSCF2925 4K 横版：AI 版最长 15 字的卡被用户按句法/意群
    # 重切，精调版 237 张基础字幕 max=13，且没有用卡内换行逃避。
    'landscape': {'max_chars': 13.0, 'name': '横版 16:9'},
    'portrait':  {'max_chars': 10.0, 'name': '竖版 9:16'},
}

rough_path = f'{WORK}/rough_segments.json'
if os.path.isfile(rough_path):
    segs = json.load(open(rough_path))
    SEGMENTS_SOURCE = 'rough_segments.json'
elif os.environ.get('ALLOW_SOURCE_SEGMENTS_FALLBACK') == '1':
    segs = json.load(open(f'{WORK}/segments.json'))
    SEGMENTS_SOURCE = 'segments.json（显式兼容模式）'
else:
    sys.exit('⛔ p3: 生产锁定模式缺 rough_segments.json；必须先跑 p2_结构编排.py')
track = json.load(open(f'{WORK}/word_track.json'))
ar = track.get('aspect', 'landscape')
P = PROFILES.get(ar, PROFILES['landscape'])
MAX_CHARS = P['max_chars']
VOICE_SPANS = track.get('voiced_spans') or []

def clen(s):
    """字符宽度：CJK 全角计 1，拉丁/数字/空格/标点计 0.5。
    出处 Netflix 韩语分册「Latin characters, spaces, punctuation count as 0.5 character」。
    一律 len() 会把「叫做loopengineer」误算成 14 字触顶而强行切开。"""
    return sum(1.0 if unicodedata.east_asian_width(c) in ('W', 'F') else 0.5 for c in s)


def _no_cards(reason):
    """生产铁闸：cards 决策有任何缺口都中止。"""
    sys.exit(
        f'\n⛔ p3 分卡中止：{reason}\n'
        f'   字幕分卡必须与 {SEGMENTS_SOURCE} 逐段严格对齐；禁止静默降级。'
    )


def segment_key(segment, pos):
    meta = segment.get('_rough_structure')
    if isinstance(meta, dict):
        playback_key = meta.get('playback_key')
        if isinstance(playback_key, str) and playback_key:
            return playback_key
    line, part = segment.get('line'), segment.get('part')
    if not isinstance(line, str) or not isinstance(part, int) or isinstance(part, bool):
        return _no_cards(f'{SEGMENTS_SOURCE}[{pos}] 缺合法 line/part 或 playback_key')
    return f'{line}.{part}'


def load_cards():
    f = f'{WORK}/cards.json'
    if not os.path.exists(f):
        return _no_cards('cards.json 不存在')
    try:
        cards = json.load(open(f))
    except Exception as e:
        # 文件在但读不出来（JSON 语法错/编码错/权限）绝不能静默降级——
        # 那会把 AI 精心写的分卡和语义关键词候选全部丢掉，而输出看上去一切正常。
        # 2026-08-03：以前这里只往 stderr 写一行就 return None 继续跑（退出码仍是 0），
        # 那是「静默降级」的另一种形态（同一个审核意见），故并到同一个闸门。
        return _no_cards(f'cards.json 存在但无法读取：{e}')

    if not isinstance(cards, dict):
        return _no_cards('cards.json 顶层必须是 key→分卡数组的对象')
    expected = [segment_key(s, i) for i, s in enumerate(segs)]
    if len(expected) != len(set(expected)):
        return _no_cards(f'{SEGMENTS_SOURCE} 存在重复播放 key，p2 必须为最终播放段分配唯一 key')
    missing = sorted(set(expected) - set(cards))
    extra = sorted(set(cards) - set(expected))
    if missing or extra:
        return _no_cards(
            f'cards key 与 {SEGMENTS_SOURCE} 不全等：'
            f'缺 {missing or "无"}；多 {extra or "无"}'
        )
    by_key = {segment_key(s, i): s for i, s in enumerate(segs)}
    normalized = {}
    bridge_specs = []
    for key in expected:
        entry = cards[key]
        bridge = None
        if isinstance(entry, dict):
            extra_fields = sorted(set(entry) - {'cards', 'bridge_to_next'})
            if extra_fields:
                return _no_cards(f'{key} 对象存在未知字段 {extra_fields}')
            frags = entry.get('cards')
            bridge = entry.get('bridge_to_next')
        else:
            frags = entry
        if not isinstance(frags, list) or not frags:
            return _no_cards(f'{key} 必须至少有一张字幕卡')
        aligned = []
        for ci, frag in enumerate(frags):
            if not isinstance(frag, list) or not 1 <= len(frag) <= 3:
                return _no_cards(f'{key} 卡{ci} 必须是 [text] / [text,keyword] / 兼容旧 [align,display,keyword]')
            if not all(isinstance(x, str) for x in frag):
                return _no_cards(f'{key} 卡{ci} 所有字段必须是字符串')
            text = frag[0]
            if not text.strip():
                return _no_cards(f'{key} 卡{ci} 主文本为空')
            if '\n' in text or '\r' in text:
                return _no_cards(
                    f'{key} 卡{ci}「{text}」含卡内换行；'
                    '基础字幕保持单行，请在句法/意群边界分成连续字幕卡'
                )
            if clen(text) > MAX_CHARS:
                return _no_cards(
                    f'{key} 卡{ci}「{text}」字宽 {clen(text):.1f} > '
                    f'{P["name"]} 单行上限 {MAX_CHARS:.0f}；'
                    '必须回 cards.json 按谓语、宾语、转折或新意群重切，禁止按字窗自动均分'
                )
            # v1 新二元组是 [text, keyword]，不存在 display 通道。
            # 旧三元组只做迁移兼容：display 为空或与 align 相同都投影成 align；
            # 任何删字、改字、重排都失败，纠错只能走 keep.fix。
            if len(frag) == 3:
                legacy_display = frag[1]
                if legacy_display and legacy_display != text:
                    return _no_cards(
                        f'{key} 卡{ci} 旧 display「{legacy_display}」与 align「{text}」不一致；'
                        'v1 禁止声音/屏显分叉，口头语要么连音频 drop，要么字幕照实显示'
                    )
            keyword = frag[1] if len(frag) == 2 else (frag[2] if len(frag) == 3 else '')
            if keyword and keyword not in text:
                return _no_cards(f'{key} 卡{ci} 关键词「{keyword}」不是主文本「{text}」的子串')
            aligned.append(text)
        actual = ''.join(w['w'] for w in (by_key[key].get('words') or []))
        if ''.join(aligned) != actual:
            return _no_cards(
                f'{key} 文本不符：cards=「{"|」「".join(aligned)}」；segment=「{actual}」'
            )
        try:
            validate_atomic_boundaries(
                actual,
                [w['w'] for w in (by_key[key].get('words') or [])],
                aligned,
                label=key,
            )
        except CaptionBoundaryError as exc:
            return _no_cards(str(exc))
        normalized[key] = frags
        if bridge is not None:
            if not isinstance(bridge, dict) or set(bridge) != {'left_text', 'right_text', 'reason'}:
                return _no_cards(
                    f'{key}.bridge_to_next 必须且只能包含 left_text/right_text/reason'
                )
            if any(not isinstance(bridge.get(k), str) or not bridge[k].strip()
                   for k in ('left_text', 'right_text', 'reason')):
                return _no_cards(f'{key}.bridge_to_next 三个字段都必须是非空字符串')
            bridge_specs.append({'left_key': key, **bridge})

    # 桥接只允许声明在前段，并且只能消费「前段末卡 + 紧邻后段首卡」。
    # 两段原 cards 仍各自文本守恒；桥接只改变最终 XML 的显示承载方式。
    key_pos = {key: i for i, key in enumerate(expected)}
    used_cards = set()
    for bridge in bridge_specs:
        li = key_pos[bridge['left_key']]
        if li + 1 >= len(expected):
            return _no_cards(f'{bridge["left_key"]}.bridge_to_next 位于最后一段，后面没有紧邻段')
        right_key = expected[li + 1]
        left_frags, right_frags = normalized[bridge['left_key']], normalized[right_key]
        left_text = left_frags[-1][0]
        right_text = right_frags[0][0]
        if bridge['left_text'] != left_text:
            return _no_cards(
                f'{bridge["left_key"]}.bridge_to_next.left_text 必须精确等于前段末卡「{left_text}」'
            )
        if bridge['right_text'] != right_text:
            return _no_cards(
                f'{bridge["left_key"]}.bridge_to_next.right_text 必须精确等于紧邻后段首卡「{right_text}」'
            )
        merged_text = left_text + right_text
        if clen(merged_text) > MAX_CHARS:
            return _no_cards(
                f'{bridge["left_key"]}.bridge_to_next 合并后「{merged_text}」宽 '
                f'{clen(merged_text):.1f} 字，超过 {P["name"]} 单行上限 {MAX_CHARS:.0f} 字'
            )
        endpoints = {(li, len(left_frags) - 1), (li + 1, 0)}
        overlap = used_cards & endpoints
        if overlap:
            return _no_cards(
                f'{bridge["left_key"]}.bridge_to_next 与另一桥接重复消费字幕卡 {sorted(overlap)}'
            )
        used_cards |= endpoints
        bridge.update({
            'left_si': li,
            'right_si': li + 1,
            'right_key': right_key,
            'left_card_index': len(left_frags) - 1,
            'right_card_index': 0,
            'left_text': left_text,
            'right_text': right_text,
        })
    return normalized, bridge_specs
CARDS, BRIDGE_SPECS = load_cards()
BRIDGE_CARD_IDS = {
    identity
    for spec in BRIDGE_SPECS
    for identity in (
        (spec['left_si'], spec['left_card_index']),
        (spec['right_si'], spec['right_card_index']),
    )
}

# ══════════════════════════════════════════════════════════════════════
# 2026-08-04：字幕切换点覆盖表整体拆除（圆桌 I3）。它把上一轮的切换点抄回来，
# 是同片重跑缓存不是学习能力。断句唯一权威 = cards.json（AI 按句法决策）
# + 知识库/决策模板/字幕分卡.md。




# ---- 时间线 ----
timeline, cur_f = [], 0
for s in segs:
    n_f = s['out_f'] - s['in_f']
    timeline.append({'tl_in_f': cur_f, 'tl_out_f': cur_f + n_f})
    cur_f += n_f
total_f = cur_f

normal, highlight = [], []
n_char_boundaries = 0
track_words = track.get('words') or []
_KEEP_CH = re.compile(r'[0-9A-Za-z㐀-䶿一-鿿぀-ヿ]')


def _char_boundary_time(seg, boundary, allow_vad_late_clamp=True):
    """用 p1a2 持久化的字级对齐定位卡切点。缺失/错位立即失败，绝不整段线性估时。"""
    acoustic = []  # (segment 原文字符位置, char timing)
    cursor = 0
    for sw in seg.get('words') or []:
        i = sw.get('i')
        if not isinstance(i, int) or not 0 <= i < len(track_words):
            sys.exit(f'⛔ p3: 段 {seg.get("line")}.{seg.get("part")} 词下标 {i} 无法绑定 word_track')
        tw = track_words[i]
        rows = sw.get('char_times')
        align_text = sw.get('align_text')
        # rough_segments 应由 p2 完整传递字级数据；同时与 word_track 对账，防重排后挂错词。
        if rows is None and os.environ.get('ALLOW_SOURCE_SEGMENTS_FALLBACK') == '1':
            rows = tw.get('char_times')
            align_text = tw.get('align_text')
        raw = sw.get('w', '')
        expected = ''.join(ch for ch in raw if _KEEP_CH.fullmatch(ch))
        if rows is None or align_text != expected or tw.get('align_text') != expected \
           or tw.get('char_times') != rows or ''.join(r.get('c', '') for r in rows) != expected:
            sys.exit(
                f'⛔ p3: 段 {seg.get("line")}.{seg.get("part")} 词{i}「{raw}」'
                '缺精确字级对齐，必须重跑 p1a2；生产模式禁止线性回退'
            )
        ri = 0
        for off, ch in enumerate(raw):
            if _KEEP_CH.fullmatch(ch):
                acoustic.append((cursor + off, rows[ri]))
                ri += 1
        cursor += len(raw)
    if boundary < 0 or boundary >= cursor or not acoustic:
        sys.exit(f'⛔ p3: 非法卡边界 {boundary}/{cursor}')
    # 卡在 boundary 处切换，时刻取该位置之后第一个真实发音字的起音。
    nxt = next((row for pos_, row in acoustic if pos_ >= boundary), None)
    if nxt is None:
        sys.exit(f'⛔ p3: 卡边界 {boundary} 之后没有可对齐的发音字')
    t = float(nxt['s'])
    # 强制对齐字时偶尔会比独立 VAD 的真实开口早 4–22 帧。若当前时刻位于
    # 静音且半秒内存在下一次开口，只允许向后钳到该开口；不得向前抢跑，
    # 也不得越过较长停顿自动改写语义断点。
    if allow_vad_late_clamp and not any(a <= t < b for a, b in VOICE_SPANS):
        next_onset = min((a for a, _ in VOICE_SPANS if a > t), default=None)
        # 若只在未量化浮点上用 3.5 帧阈值，3.1–3.5 帧的差值经
        # 60Hz 取整后可能变成屏幕上整整 4 帧抢跑。生成侧因此在
        # >3.0 帧时就向后钳制，给量化留 0.5 帧安全余量；验收侧仍
        # 独立使用“不得早出 4 帧”的观感红线。
        if next_onset is not None and 3.0 / FPS < next_onset - t <= 0.5:
            t = next_onset
    return t


n_first_card_snaps = 0
for si, (s, tl) in enumerate(zip(segs, timeline)):
    ws = s.get('words') or []
    if not ws:
        continue
    key = segment_key(s, si)
    kw_map = {}
    # load_cards 已硬断言 key 全等与文本一致，此处绝不存在规则兜底。
    want = [c[0] for c in CARDS[key]]
    texts = want
    for i, c in enumerate(CARDS[key]):
        if len(c) == 2 and c[1]:
            kw_map[i] = c[1]
        elif len(c) == 3 and c[2]:
            kw_map[i] = c[2]
    if not texts: continue

    # 文本切分 → 字符位置 → 时间 → 时间线帧
    pos = sum(len(w['w']) for w in ws)
    # 段首与段内换卡不是同一问题：段首已由粗剪决定画面/声音从哪里开始。
    # 若第一个字符的声学起音只比切口晚 1–2 帧，量化后留下的空档会被人眼看成
    # “视频先开始、字幕后出现”，C2897 用户在 FCP 截图中明确指出该问题。
    # 因此首卡不走 VAD 向后钳制。用户多轮精调进一步证明：只要片段已经
    # 开始播放，基础字幕就必须同步承担该片段声音；若段首有不该显示的残声，
    # 应退回 P1 修刀口，不能在 P3 用延迟字幕遮住。
    ft = _char_boundary_time(s, 0, allow_vad_late_clamp=False)
    acoustic_first_f = tl['tl_in_f'] + round((ft - s['in']) * FPS)
    first_f = tl['tl_in_f']
    if acoustic_first_f > tl['tl_in_f']:
        n_first_card_snaps += 1
    bounds, acc = [first_f], 0
    for t_ in texts[:-1]:
        acc += len(t_)
        tt = _char_boundary_time(s, acc)
        # 以字符级起音为主；只有它落在静音且半秒内即将开口时，才由上面的
        # 独立 VAD 安全钳制向后，防止字幕先于人声跳出。
        tt2 = s['in'] + round((tt - s['in']) * FPS) / FPS
        n_char_boundaries += 1
        f = tl['tl_in_f'] + round((tt2 - s['in']) * FPS)
        bounds.append(max(bounds[-1] + 1, min(f, tl['tl_out_f'] - 1)))
    bounds.append(tl['tl_out_f'])

    # cards 是人工语义决策；不得为过时长而静默合卡或丢关键词。
    durs = [bounds[i + 1] - bounds[i] for i in range(len(texts))]
    # 桥接端点最终不会单独显示；短词会与相邻卡组成一张完整字幕，
    # 因此只对真正独立显示的多卡段执行最短卡闸门。
    too_short = [
        (i, d)
        for i, d in enumerate(durs)
        if len(texts) > 1
        and d < MIN_CARD_F
        and (si, i) not in BRIDGE_CARD_IDS
    ]
    if too_short:
        i, d = too_short[0]
        sys.exit(f'⛔ p3: 段 {key} 卡{i}「{texts[i]}」仅 {d} 帧 < {MIN_CARD_F} 帧；请回 cards.json 重切，禁止自动合卡')

    for gi, t_ in enumerate(texts):
        if not t_: continue
        # 屏显文本就是对齐文本；v1 不再保留“声音在、字幕删字”的第三通道。
        align_ = t_
        local_sf = bounds[gi] - tl['tl_in_f']
        local_ef = bounds[gi + 1] - tl['tl_in_f']
        rec = {
            'sf': bounds[gi], 'ef': bounds[gi+1], 'text': t_, 'align': align_,
            'line': s['line'], 'si': si, 'card_index': gi,
            # 三级对齐的可审计锺点：成片帧、段内帧、原素材帧。
            # 后续 FCP/剪映都必须从同一组锺点序列化，不得各自重算。
            'playback_key': key,
            'local_sf': local_sf, 'local_ef': local_ef,
            'source_sf': s['in_f'] + local_sf,
            'source_ef': s['in_f'] + local_ef,
        }
        if rec['sf'] != tl['tl_in_f'] + rec['local_sf'] \
           or rec['ef'] != tl['tl_in_f'] + rec['local_ef'] \
           or rec['source_sf'] != s['in_f'] + rec['local_sf'] \
           or rec['source_ef'] != s['in_f'] + rec['local_ef']:
            sys.exit(f'⟂ p3: 段 {key} 卡{gi} 本地/原素材锺点投影不守恒')
        kw = kw_map.get(gi)
        if kw and kw in t_: highlight.append({**rec, 'keyword': kw})
        else: normal.append(rec)

# 默认仍禁止跨段借词。显式 bridge_to_next 也不挪字、不删卡：normal/highlight 保留
# 两段原始卡作为验收底账，p4f 只按下面的 bridges 审计表合并最终显示。
_covered_segments = {c['si'] for c in normal + highlight if c.get('text')}
_missing_caps = [i for i, s in enumerate(segs)
                 if (s.get('words') or s.get('out_f', 0) > s.get('in_f', 0))
                 and i not in _covered_segments]
if _missing_caps:
    sys.exit(f'⛔ p3: 最终播放段 {_missing_caps[:12]} 没有主字幕；覆盖率必须 100%')

all_by_identity = {
    (c['si'], c['card_index']): c for c in normal + highlight
}
bridges = []
for spec in BRIDGE_SPECS:
    left_id = (spec['left_si'], spec['left_card_index'])
    right_id = (spec['right_si'], spec['right_card_index'])
    left = all_by_identity.get(left_id)
    right = all_by_identity.get(right_id)
    if not left or not right:
        sys.exit(f'⛔ p3: 桥接 {spec["left_key"]} 无法绑定前段末卡/后段首卡')
    cut_f = timeline[spec['left_si']]['tl_out_f']
    if left['ef'] != cut_f or right['sf'] < cut_f:
        sys.exit(
            f'⛔ p3: 桥接 {spec["left_key"]} 时码非法：'
            f'left.ef={left["ef"]}, cut={cut_f}, right.sf={right["sf"]}'
        )
    if left['text'] != spec['left_text'] or right['text'] != spec['right_text']:
        sys.exit(f'⛔ p3: 桥接 {spec["left_key"]} 投影后左右文本发生变化')
    bridges.append({
        'left_si': spec['left_si'],
        'right_si': spec['right_si'],
        'left_card_index': spec['left_card_index'],
        'right_card_index': spec['right_card_index'],
        'left_sf': left['sf'],
        'cut_f': cut_f,
        'right_ef': right['ef'],
        'left_text': left['text'],
        'right_text': right['text'],
        'text': left['text'] + right['text'],
        'source_spans': [
            {
                'playback_key': left['playback_key'],
                'source_sf': left['source_sf'], 'source_ef': left['source_ef'],
            },
            {
                'playback_key': right['playback_key'],
                'source_sf': right['source_sf'], 'source_ef': right['source_ef'],
            },
        ],
        'reason': spec['reason'].strip(),
    })

US = lambda f: round(f * 1_000_000 / FPS)
plan = {'fps': FPS, 'total_f': total_f, 'total_us': US(total_f),
        'aspect': ar,
        'segments_source': SEGMENTS_SOURCE,
        'title': (json.load(open(f'{WORK}/config.json')).get('top_title') or ''),
        'bridges': bridges,
        'normal': [{**c, 'start_us': US(c['sf']), 'end_us': US(c['ef'])} for c in normal],
        'highlight': [{**c, 'start_us': US(c['sf']), 'end_us': US(c['ef'])} for c in highlight],
        'segments_tl': [{'in_f': s['in_f'], 'out_f': s['out_f'],
                         'tl_in_f': t['tl_in_f'], 'tl_out_f': t['tl_out_f']}
                        for s, t in zip(segs, timeline)]}
json.dump(plan, open(f'{WORK}/captions_plan.json', 'w'), ensure_ascii=False, indent=1)

allc = normal + highlight
durs = sorted((c['ef'] - c['sf']) / FPS for c in allc)
w = sorted(clen(c['text']) for c in allc)
cps = sorted(clen(c['text'])/max((c['ef']-c['sf'])/FPS, 1e-6) for c in allc)
print(f'分卡: {len(allc)} 张（无候选 {len(normal)} 有关键词候选 {len(highlight)}）· {P["name"]} 单行上限 {MAX_CHARS:.0f} 字')
if bridges:
    print(f'      显式跨切点句法桥接: {len(bridges)} 处（原卡与逐段文本均保留作审计）')
print(f'      字宽 中位{w[len(w)//2]:.1f} max{w[-1]:.1f} · 卡长 中位{durs[len(durs)//2]:.2f}s min{durs[0]:.2f}s'
      f' · CPS 中位{cps[len(cps)//2]:.1f} 超9的 {sum(1 for x in cps if x>9)} 张')
if n_char_boundaries:
    print(f'      字幕换卡: {n_char_boundaries} 处直接采用字符级强制对齐时码（仅量化到 60 fps）')
if n_first_card_snaps:
    print(f'      段首字幕: {n_first_card_snaps} 处 1–{FIRST_CARD_SNAP_F} 帧量化空档已吸附视频切口')
