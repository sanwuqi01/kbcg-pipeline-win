#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
p4f 出 FCPXML(2026-07-24 · FCP 12.3 / fcpxml 1.11)
用法:p4f_出FCPXML.py <工作目录> [输出.fcpxml]

作者自剪出口:剪映草稿箱路线保留给分享;他自己剪辑用本产物导入 Final Cut Pro。
- 视频段:asset-clip 引用【原画】(FCP 原生吃 PCM/422 10bit,无需转码),零气口相接
- 字幕:内置 Basic Title,聚珍体(系统注册名 XQjuzhentiZJSY),基础纯白；只有 P5 显式登记的少量关键词使用暖金
- 字幕挂在所属段上(connected),offset 用父 clip 源内坐标系 → 与段边界帧级相等
- 内部决策仍用 60fps 分析网格；交付时量化到原素材真实帧率，不改分辨率、旋转或帧率
"""
import sys, os, json, html, urllib.parse, re, datetime, glob, argparse
from pathlib import Path

from fcpxml_packaging_contract import load_final_cut_packaging
from media_contract import (
    ANALYSIS_FPS, analysis_to_native, assert_config_matches, caption_profile,
    fcpxml_color_space, fcpxml_format_name, fcpxml_shadow_attributes,
    fcpxml_source_timecode,
    frame_duration, native_time, probe, rate, round_fraction,
)

_parser = argparse.ArgumentParser(description=__doc__)
_parser.add_argument('work')
_parser.add_argument('output', nargs='?')
_parser.add_argument('--project-name')
_parser.add_argument('--verify-only', action='store_true')
_args = _parser.parse_args()

W = _args.work
cfg = json.load(open(f'{W}/config.json'))
plan = json.load(open(f'{W}/captions_plan.json'))
rough_path = f'{W}/rough_segments.json'
if not os.path.isfile(rough_path):
    sys.exit('⛔ p4f: 缺 rough_segments.json；包装层不得绕过已锁定的粗剪顺序')
segs = json.load(open(rough_path))
OUT = _args.output or f'{W}/{cfg["name"]}_v7.fcpxml'
VERIFY_ONLY = _args.verify_only
PROJECT_NAME_OVERRIDE = _args.project_name

FPS = ANALYSIS_FPS
# 1920×1080 主字幕 profile 已按用户在 Final Cut 中确认的字号与位置固化。
# 参数只保留在本文件，文档不得复制数值；样本 XML 只用于差异审计。
WHITE = '1 1 1 1'
GOLD = '0.995808 0.800124 0.399987 1'
FONT_FACE = 'Regular'
ALIGN = 'center'

src = cfg.get('original_path') or cfg['proxy']
src_url = 'file://' + urllib.parse.quote(os.path.abspath(src))
try:
    ACTUAL = probe(src)
    assert_config_matches(cfg, ACTUAL)
except ValueError as exc:
    sys.exit(f'⛔ p4f: {exc}')
SOURCE_FPS_RAW = ACTUAL['frame_rate_raw']
SOURCE_RATE = rate(SOURCE_FPS_RAW)
FRAME_DURATION = frame_duration(SOURCE_FPS_RAW)
try:
    FORMAT_NAME = fcpxml_format_name(ACTUAL['width'], ACTUAL['height'], SOURCE_FPS_RAW)
    COLOR_SPACE = fcpxml_color_space(ACTUAL, cfg.get('color_contract'))
    ASSET_START_NATIVE, SOURCE_TC_FORMAT = fcpxml_source_timecode(ACTUAL)
except ValueError as exc:
    sys.exit(f'⛔ p4f: {exc}')


def q(frame):
    return analysis_to_native(frame, SOURCE_FPS_RAW)


def nt(frame):
    return native_time(frame, SOURCE_FPS_RAW)


media = cfg.get('media') or {}
video_meta = media.get('video') or {}
audio_meta = media.get('audio') or {}
WIDTH = ACTUAL['width']
HEIGHT = ACTUAL['height']
AUDIO_CHANNELS = ACTUAL['audio_channels']
AUDIO_LAYOUT = 'mono' if AUDIO_CHANNELS == 1 else 'stereo'
AUDIO_RATE = ACTUAL['audio_sample_rate']

# ---- 交付命名：日期 + 中文核心表达，技术编号只留在内部工作区 ----
CORE_EXPRESSION = cfg.get('core_expression')
if not isinstance(CORE_EXPRESSION, str) or not CORE_EXPRESSION.strip():
    sys.exit('⛔ p4f: config 缺少非空 core_expression（中文核心表达）；C2861/C2637 等技术编号不能作为最终名')
CORE_EXPRESSION = CORE_EXPRESSION.strip()
if not re.search(r'[\u3400-\u9fff]', CORE_EXPRESSION):
    sys.exit(f'⛔ p4f: 「中文核心表达」必须包含中文，当前为 {CORE_EXPRESSION!r}')
if re.fullmatch(r'[Cc]\d+', CORE_EXPRESSION, re.I):
    sys.exit(f'⛔ p4f: 「中文核心表达」不能仅为技术编号 {CORE_EXPRESSION!r}')
if re.search(r'[\\/:\0\r\n\t]', CORE_EXPRESSION):
    sys.exit('⛔ p4f: 「中文核心表达」含路径分隔符或控制字符')
_delivery_stem = f'{datetime.date.today().isoformat()}_{CORE_EXPRESSION}'
PROJECT_NAME = PROJECT_NAME_OVERRIDE or _delivery_stem
if PROJECT_NAME_OVERRIDE and not re.fullmatch(
    re.escape(_delivery_stem) + r'(?:_第(?:[2-9]|[1-9]\d+)版)?', PROJECT_NAME_OVERRIDE
):
    sys.exit(
        f'⛔ p4f: project name 必须是「{_delivery_stem}」或其「_第2版」递增形式'
    )

# ---- 字幕 profile：按已确认的横/竖屏视觉比例计算，绝不靠缩放视频适配字幕 ----
_profile_cfg = cfg.get('caption_profile')
try:
    EXPECTED_PROFILE = caption_profile(WIDTH, HEIGHT)
except ValueError:
    EXPECTED_PROFILE = None
if EXPECTED_PROFILE is None:
    if not isinstance(_profile_cfg, dict):
        sys.exit(f'⛔ p4f: {WIDTH}x{HEIGHT} 不是已确认画幅，必须显式提供 caption_profile')
    CAPTION_PROFILE = _profile_cfg
else:
    if _profile_cfg not in (None, EXPECTED_PROFILE['name'], EXPECTED_PROFILE):
        sys.exit(
            f'⛔ p4f: caption_profile 与 {WIDTH}x{HEIGHT} 已确认标准不一致；'
            f'期望 {EXPECTED_PROFILE}'
        )
    CAPTION_PROFILE = EXPECTED_PROFILE
_required_profile = {'name', 'width', 'height', 'font', 'font_size', 'xml_y'}
_allowed_profile = _required_profile | {'shadow'}
if not _required_profile.issubset(CAPTION_PROFILE) \
   or not set(CAPTION_PROFILE).issubset(_allowed_profile) \
   or (CAPTION_PROFILE.get('width'), CAPTION_PROFILE.get('height')) != (WIDTH, HEIGHT):
    sys.exit('⛔ p4f: caption_profile 字段或画幅非法')
try:
    SHADOW_ATTRIBUTES = fcpxml_shadow_attributes(CAPTION_PROFILE)
except ValueError as exc:
    sys.exit(f'⛔ p4f: {exc}')
_SHADOW_ATTR = ''.join(
    f' {key}="{html.escape(value, quote=True)}"'
    for key, value in SHADOW_ATTRIBUTES.items()
)

FONT = CAPTION_PROFILE['font']
SZ_N = SZ_HL = SZ_T = CAPTION_PROFILE['font_size']
Y_SUB, Y_TITLE = CAPTION_PROFILE['xml_y'], 430

# ---- 工程与素材使用同一真实帧率；不再让 Final Cut 纠正错误的 60fps 声明 ----
asset_native_frames = round_fraction(ACTUAL['duration'] * SOURCE_RATE)
asset_dur = nt(asset_native_frames)
total_f = plan['total_f']
stl = plan['segments_tl']
if len(stl) != len(segs):
    sys.exit(f'⛔ p4f: captions_plan 段数 {len(stl)} 与 rough_segments {len(segs)} 不一致')
for i, (timeline_seg, rough_seg) in enumerate(zip(stl, segs)):
    if timeline_seg.get('in_f') != rough_seg.get('in_f') or \
       timeline_seg.get('out_f') != rough_seg.get('out_f'):
        sys.exit(f'⛔ p4f: 第 {i} 段源时码与 rough_segments 不一致，拒绝错序包装')
cards = sorted(plan['normal'] + plan['highlight'], key=lambda c: c['sf'])
bridges = plan.get('bridges') or []
try:
    PACKAGING = load_final_cut_packaging(Path(W), cards)
except (OSError, ValueError, KeyError, TypeError) as exc:
    sys.exit(f'⛔ Final Cut 包装计划无效：{exc}')
TITLE_TOP = (
    PACKAGING['top_title']['text'] if PACKAGING.get('top_title') else ''
)

# cards 仍是逐段文本守恒的底账；bridges 只授权 p4 把两张相邻卡合成一张显示卡。
by_identity = {}
by_segment = {}
for c in cards:
    ident = (c.get('si'), c.get('card_index'))
    if ident in by_identity:
        sys.exit(f'⛔ p4f: 重复字幕卡身份 {ident}')
    by_identity[ident] = c
    si = c.get('si')
    if not isinstance(si, int) or not 0 <= si < len(stl):
        sys.exit(f'⟂ p4f: 字幕卡 si 非法 {si!r}')
    timeline_seg = stl[si]
    rough_seg = segs[si]
    expected = {
        'local_sf': c.get('sf') - timeline_seg['tl_in_f'],
        'local_ef': c.get('ef') - timeline_seg['tl_in_f'],
    }
    expected['source_sf'] = rough_seg['in_f'] + expected['local_sf']
    expected['source_ef'] = rough_seg['in_f'] + expected['local_ef']
    for field, value in expected.items():
        if c.get(field) != value:
            sys.exit(f'⟂ p4f: 字幕卡 {ident}.{field}={c.get(field)!r}，锺点真值为 {value}')
    by_segment.setdefault(c.get('si'), []).append(c)
for cs in by_segment.values():
    cs.sort(key=lambda c: c['sf'])

used_bridge_cards = set()
render_bridges = {}
for bi, b in enumerate(bridges):
    if not isinstance(b, dict):
        sys.exit(f'⛔ p4f: bridges[{bi}] 必须是对象')
    li, ri = b.get('left_si'), b.get('right_si')
    if not isinstance(li, int) or ri != li + 1 or not (0 <= li < len(stl) - 1):
        sys.exit(f'⛔ p4f: bridges[{bi}] 只能连接紧邻最终段')
    lids = (li, b.get('left_card_index'))
    rids = (ri, b.get('right_card_index'))
    left, right = by_identity.get(lids), by_identity.get(rids)
    if not left or not right:
        sys.exit(f'⛔ p4f: bridges[{bi}] 无法绑定左右字幕卡')
    if left is not by_segment[li][-1] or right is not by_segment[ri][0]:
        sys.exit(f'⛔ p4f: bridges[{bi}] 必须绑定前段末卡和后段首卡')
    cut_f = stl[li]['tl_out_f']
    if b.get('cut_f') != cut_f or left['ef'] != cut_f or right['sf'] < cut_f:
        sys.exit(f'⛔ p4f: bridges[{bi}] 切点或卡时码不一致')
    if b.get('left_text') != left['text'] or b.get('right_text') != right['text'] \
       or b.get('text') != left['text'] + right['text']:
        sys.exit(f'⛔ p4f: bridges[{bi}] 左右文本必须精确拼接，禁止改字/隐藏/重排')
    if b.get('left_sf') != left['sf'] or b.get('right_ef') != right['ef']:
        sys.exit(f'⛔ p4f: bridges[{bi}] 起止时码与原卡不一致')
    if used_bridge_cards & {lids, rids}:
        sys.exit(f'⛔ p4f: bridges[{bi}] 重复消费同一字幕卡')
    used_bridge_cards |= {lids, rids}
    render_bridges[li] = {
        **b,
        'sf': left['sf'],
        'ef': right['ef'],
        'left_card': left,
        'right_card': right,
    }
# text-style id:fcpxml 的 ID 必须【全文档唯一】(DTD 校验,FCP 实测报 already defined)
_ts_counter = [0]
def next_ts():
    _ts_counter[0] += 1
    return f'ts{_ts_counter[0]}'

def title_xml(name, offset_native, duration_native, runs, y, lane=1):
    """runs = [(text, size, color)];每个 run 独立唯一 text-style id"""
    ids = [next_ts() for _ in runs]
    texts = ''.join(
        f'<text-style ref="{i}">{html.escape(txt)}</text-style>'
        for i, (txt, s2, c) in zip(ids, runs) if txt)
    defs = ''.join(
        f'<text-style-def id="{i}">'
        f'<text-style font="{FONT}" fontSize="{s2}" fontFace="Regular" '
        f'fontColor="{c}" alignment="center"{_SHADOW_ATTR}/></text-style-def>'
        for i, (txt, s2, c) in zip(ids, runs) if txt)
    # 不写 name 属性(2026-07-27 用户实证):写死 name 会让 FCP 时间线标签永远停在初始文本,
    # 用户改字幕后标签不跟随;省略 name 则 FCP 用文本内容作显示名并动态跟随
    return (f'<title ref="rTitle" lane="{lane}" offset="{nt(offset_native)}" duration="{nt(duration_native)}">'
            f'<param name="Position" key="9999/999166631/999166633/1/100/101" value="0 {y}"/>'
            f'<text>{texts}</text>{defs}</title>')   # DTD 顺序: param → text → text-style-def

def card_runs(c):
    decision = PACKAGING['selected'].get(
        (int(c.get('si', -1)), int(c.get('card_index', -1)))
    )
    kw = decision.get('keyword') if decision else None
    if kw:
        pre, _, post = c['text'].partition(kw)
        return [(pre, SZ_HL, WHITE), (kw, SZ_HL, GOLD), (post, SZ_HL, WHITE)]
    return [(c['text'], SZ_N, WHITE)]


# 先建立原生帧时间线。每段源边界独立量化，主轨 offset 按量化后时长连续累加。
native_rows = []
timeline_cursor_native = 0
for st in stl:
    in_f = st['in_f']
    dur_f = st['tl_out_f'] - st['tl_in_f']
    source_start_native = q(in_f)
    source_end_native = q(in_f + dur_f)
    duration_native = source_end_native - source_start_native
    if duration_native <= 0:
        sys.exit('⛔ p4f: 有片段量化到原始帧率后不足一帧')
    native_rows.append({
        'timeline_start': timeline_cursor_native,
        'source_start': source_start_native,
        'duration': duration_native,
    })
    timeline_cursor_native += duration_native


def timeline_native_at(frame):
    for index, st in enumerate(stl):
        if st['tl_in_f'] <= frame <= st['tl_out_f']:
            row = native_rows[index]
            relative = frame - st['tl_in_f']
            return row['timeline_start'] + q(st['in_f'] + relative) - row['source_start']
    sys.exit(f'⛔ p4f: 时间线帧越界：{frame}')


# 逐段生成 asset-clip + 段内字幕。桥接字幕也用全局原生帧边界，避免跨切点 1 帧重叠。
clips = []
for i, st in enumerate(stl):
    seg = segs[i]
    row = native_rows[i]
    in_f = st['in_f']
    source_start_native = row['source_start']
    duration_native = row['duration']
    seg_cards = [c for c in by_segment.get(i, [])
                 if (i, c.get('card_index')) not in used_bridge_cards]
    if i in render_bridges:
        seg_cards.append(render_bridges[i])
    seg_cards.sort(key=lambda c: c['sf'])
    inner = []
    for c in seg_cards:
        global_start_native = timeline_native_at(c['sf'])
        global_end_native = timeline_native_at(c['ef'])
        # 普通卡直接从 p3 的原素材锺点量化；bridge 仍按两段时间线
        # 范围显示，但其左右 source_spans 已独立记账。
        if 'left_card' in c:
            card_start_native = (
                ASSET_START_NATIVE + source_start_native
                + global_start_native - row['timeline_start']
            )
        else:
            anchored_source = q(c['source_sf'])
            if anchored_source - source_start_native != global_start_native - row['timeline_start']:
                sys.exit(f'⟂ p4f: 字幕卡 {c.get("si")}.{c.get("card_index")} 原素材/段内锺点不一致')
            card_start_native = ASSET_START_NATIVE + anchored_source
        card_duration_native = global_end_native - global_start_native
        if card_duration_native <= 0:
            sys.exit(f'⛔ p4f: 非正字幕时长 @{c["sf"]}')
        if 'left_card' in c:
            runs = card_runs(c['left_card']) + card_runs(c['right_card'])
        else:
            runs = card_runs(c)
        inner.append(title_xml(
            c['text'], card_start_native, card_duration_native, runs, Y_SUB, lane=1
        ))
    # 全片顶部标题挂第一段
    if i == 0 and TITLE_TOP:
        inner.append(title_xml(
            TITLE_TOP, ASSET_START_NATIVE + source_start_native, timeline_cursor_native,
            [(TITLE_TOP, SZ_T, GOLD)], Y_TITLE, lane=2
        ))
    # ---- 批注层已整体删除（2026-07-28 作者拍板「不要了」）----
    # 依据：终版对照——他把 15 个金句 rating【全部删掉】，章节 7 个和 B-roll 5 个
    # 虽原样留着但一帧没动过，问下来的回答是「不要了」。
    # 少写三种元素 = 少三种 DTD 顺序坑，时间线也干净。别再加回来。
    clips.append(
        f'<asset-clip ref="rA" offset="{nt(row["timeline_start"])}" '
        f'start="{nt(ASSET_START_NATIVE + source_start_native)}" duration="{nt(duration_native)}" '
        f'name="{html.escape(seg["text"]) or f"seg{i}"}" tcFormat="{SOURCE_TC_FORMAT}" audioRole="dialogue">'   # name=全段转写,是错配自检的基准,不许截断
        + ''.join(inner) + '</asset-clip>')

xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
  <resources>
    <format id="r1"{f' name="{FORMAT_NAME}"' if FORMAT_NAME else ''} frameDuration="{FRAME_DURATION}" width="{WIDTH}" height="{HEIGHT}" colorSpace="{COLOR_SPACE}"/>
    <asset id="rA" name="{html.escape(os.path.basename(src))}" start="{nt(ASSET_START_NATIVE)}" duration="{asset_dur}" hasVideo="1" hasAudio="1" format="r1" videoSources="1" audioSources="1" audioChannels="{AUDIO_CHANNELS}" audioRate="{AUDIO_RATE}">
      <media-rep kind="original-media" src="{src_url}"/>
    </asset>
    <effect id="rTitle" name="Basic Title" uid=".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"/>
  </resources>
  <library>
    <event name="金鳞0720切片">
      <project name="{html.escape(PROJECT_NAME)}">
        <sequence format="r1" duration="{nt(timeline_cursor_native)}" tcStart="0s" tcFormat="NDF" audioLayout="{AUDIO_LAYOUT}" audioRate="{AUDIO_RATE // 1000}k">
          <spine>
            {''.join(clips)}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
'''
import xml.dom.minidom as MD
MD.parseString(xml)   # XML 合法性自检

if VERIFY_ONLY:
    if not os.path.isfile(OUT):
        sys.exit(f'⛔ p4f verify: XML 不存在: {OUT}')
    actual = open(OUT, encoding='utf-8').read()
    if actual != xml:
        sys.exit('⛔ p4f verify: 现有 XML 与冻结输入重新序列化结果不一致')
    dtds = sorted(glob.glob(
        '/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/'
        'Versions/A/Resources/FCPXMLv1_11.dtd'
    ))
    if not dtds:
        sys.exit('⛔ p4f verify: 找不到 Final Cut Pro 官方 FCPXMLv1_11.dtd')
    import subprocess
    checked = subprocess.run(
        ['xmllint', '--dtdvalid', os.path.basename(dtds[-1]), '--noout', os.path.abspath(OUT)],
        capture_output=True, text=True, cwd=os.path.dirname(dtds[-1])
    )
    if checked.returncode != 0:
        sys.exit('⛔ p4f verify: DTD 失败\n' + '\n'.join(checked.stderr.splitlines()[:8]))
    print(
        f'✅ FCPXML 桥接感知终检通过: {len(stl)} clip · '
        f'{len(cards) - len(bridges)} 个显示 title · {len(bridges)} 处跨切点桥接 · 官方 DTD'
    )
else:
    open(OUT, 'w', encoding='utf-8').write(xml)
    print(f'✅ FCPXML 已生成: {OUT}')
    print(
        f'   项目 {PROJECT_NAME} · profile {CAPTION_PROFILE["name"]} · '
        f'段 {len(stl)} · 原卡 {len(cards)} · 桥接 {len(bridges)} · '
        f'显式重点 {len(PACKAGING["selected"])} · 顶部标题 {"有" if TITLE_TOP else "无"} · '
        f'总长 {timeline_cursor_native/float(SOURCE_RATE):.1f}s · '
        f'{WIDTH}x{HEIGHT} · {float(SOURCE_RATE):.3f}fps · 素材 {os.path.basename(src)}'
    )
