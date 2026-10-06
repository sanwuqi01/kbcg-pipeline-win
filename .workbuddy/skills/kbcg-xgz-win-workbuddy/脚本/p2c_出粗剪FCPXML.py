#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 rough_segments.json 序列化为可直接导入 Final Cut Pro 的粗剪听审 FCPXML。

这不是包装出口：不生成字幕、音乐、花字或其他视觉元素，也不把待听审粗剪
冒充已冻结终版。素材始终引用原文件，因此不会转码或降低原始分辨率。

用法：python3 p2c_出粗剪FCPXML.py <工作目录> <输出.fcpxml>
"""

from __future__ import annotations

import glob
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import xml.dom.minidom as minidom
from pathlib import Path

from media_contract import (
    ANALYSIS_FPS, analysis_to_native, fcpxml_color_space, fcpxml_format_name,
    fcpxml_source_timecode, frame_duration, native_time, probe, rate,
    round_fraction,
)


def fail(message: str) -> None:
    raise SystemExit(f"⛔ 粗剪 FCPXML：{message}")


if len(sys.argv) != 3:
    fail("用法：p2c_出粗剪FCPXML.py <工作目录> <输出.fcpxml>")

work = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2]).resolve()
config_path = work / "config.json"
rough_path = work / "rough_segments.json"
if not config_path.is_file() or not rough_path.is_file():
    fail("缺 config.json 或 rough_segments.json；必须先完成结构编排")

cfg = json.loads(config_path.read_text(encoding="utf-8"))
segments = json.loads(rough_path.read_text(encoding="utf-8"))
if not isinstance(segments, list) or not segments:
    fail("rough_segments.json 必须是非空数组")

multi_sources = cfg.get("sources")
if multi_sources is not None and (
    not isinstance(multi_sources, list) or len(multi_sources) < 2
):
    fail("config.sources 若存在，必须至少包含两条素材")

source_specs: list[dict] = []
if multi_sources:
    for index, item in enumerate(multi_sources):
        if not isinstance(item, dict):
            fail(f"config.sources[{index}] 不是对象")
        source_id = item.get("id")
        source_path = Path(item.get("path") or "").resolve()
        if not isinstance(source_id, str) or not source_id.strip():
            fail(f"config.sources[{index}].id 缺失")
        if any(spec["id"] == source_id for spec in source_specs):
            fail(f"config.sources 出现重复 id：{source_id}")
        if not source_path.is_file():
            fail(f"原素材不存在：{source_path}")
        source_specs.append(
            {
                "id": source_id,
                "path": source_path,
                "full_us": item.get("full_us"),
                "media": item.get("media") or {},
            }
        )
else:
    source = Path(cfg.get("original_path") or cfg.get("proxy") or "").resolve()
    if not source.is_file():
        fail(f"原素材不存在：{source}")
    source_specs.append(
        {
            "id": "source",
            "path": source,
            "full_us": cfg.get("full_us"),
            "media": cfg.get("media") or {},
        }
    )

for spec in source_specs:
    try:
        spec["actual"] = probe(spec["path"])
    except ValueError as exc:
        fail(str(exc))
primary = source_specs[0]["actual"]
identity_keys = (
    "width", "height", "encoded_width", "encoded_height", "rotation",
    "frame_rate_raw", "color_primaries", "color_transfer", "color_space",
    "audio_sample_rate", "audio_channels",
)
for spec in source_specs[1:]:
    drift = [key for key in identity_keys if spec["actual"][key] != primary[key]]
    if drift:
        fail(
            f"多素材技术属性不一致（{spec['id']}：{', '.join(drift)}）；"
            "不得自动统一画幅、帧率、旋转或音频属性"
        )
width = primary["width"]
height = primary["height"]
channels = primary["audio_channels"]
audio_layout = "mono" if channels == 1 else "stereo"
audio_rate = primary["audio_sample_rate"]
fps_raw = primary["frame_rate_raw"]
native_rate = rate(fps_raw)
native_frame_duration = frame_duration(fps_raw)
try:
    format_name = fcpxml_format_name(width, height, fps_raw)
    color_space = fcpxml_color_space(primary, cfg.get("color_contract"))
except ValueError as exc:
    fail(str(exc))

core = cfg.get("core_expression")
if not isinstance(core, str) or not core.strip() or not re.search(r"[\u3400-\u9fff]", core):
    fail("config.core_expression 必须是包含中文的非空核心表达")
core = core.strip()
project_name = output.stem
if not re.match(r"^\d{4}-\d{2}-\d{2}_", project_name) or not re.search(r"[\u3400-\u9fff]", project_name):
    fail("输出文件名必须以 YYYY-MM-DD_ 开头并包含中文核心表达")

timeline_fps = ANALYSIS_FPS


def q(frames: int) -> int:
    return analysis_to_native(frames, fps_raw)


def fcptime(frames: int) -> str:
    return native_time(frames, fps_raw)


def source_resource(spec: dict, index: int) -> tuple[str, str]:
    asset_id = f"rA{index}"
    actual = spec["actual"]
    asset_frames = round_fraction(actual["duration"] * native_rate)
    asset_duration = fcptime(asset_frames)
    try:
        asset_start_native, tc_format = fcpxml_source_timecode(actual)
    except ValueError as exc:
        fail(str(exc))
    spec["asset_start_native"] = asset_start_native
    spec["tc_format"] = tc_format
    source_url = "file://" + urllib.parse.quote(str(spec["path"]))
    asset_xml = (
        f'<asset id="{asset_id}" name="{html.escape(spec["path"].name, quote=True)}" '
        f'start="{fcptime(asset_start_native)}" duration="{asset_duration}" '
        f'hasVideo="1" hasAudio="1" format="r1" videoSources="1" '
        f'audioSources="1" audioChannels="{channels}" '
        f'audioRate="{audio_rate}"><media-rep kind="original-media" src="{source_url}"/></asset>'
    )
    return asset_xml, asset_id


resources = [source_resource(spec, index) for index, spec in enumerate(source_specs)]
asset_by_source = {
    spec["id"]: resource[1] for spec, resource in zip(source_specs, resources)
}
spec_by_source = {spec["id"]: spec for spec in source_specs}

offset_f = 0
clips: list[str] = []
for index, segment in enumerate(segments):
    if not isinstance(segment, dict):
        fail(f"第 {index} 段不是对象")
    in_f = segment.get("in_f")
    out_f = segment.get("out_f")
    if not isinstance(in_f, int) or not isinstance(out_f, int) or out_f <= in_f:
        fail(f"第 {index} 段源帧非法：{in_f!r}→{out_f!r}")
    source_start_native = q(in_f)
    source_end_native = q(out_f)
    duration_native = source_end_native - source_start_native
    if duration_native <= 0:
        fail(f"第 {index} 段量化到原素材帧率后不足一帧")
    source_id = segment.get("source_id") if multi_sources else "source"
    if source_id not in asset_by_source:
        fail(f"第 {index} 段 source_id 未登记：{source_id!r}")
    source_spec = spec_by_source[source_id]
    name = html.escape(
        str(segment.get("clip_name") or segment.get("text") or f"片段{index + 1}"),
        quote=True,
    )
    clips.append(
        f'<asset-clip ref="{asset_by_source[source_id]}" offset="{fcptime(offset_f)}" '
        f'start="{fcptime(source_spec["asset_start_native"] + source_start_native)}" '
        f'duration="{fcptime(duration_native)}" name="{name}" '
        f'tcFormat="{source_spec["tc_format"]}" audioRole="dialogue"/>'
    )
    offset_f += duration_native

asset_resources = "\n    ".join(item[0] for item in resources)
xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
  <resources>
    <format id="r1"{f' name="{format_name}"' if format_name else ''} frameDuration="{native_frame_duration}" width="{width}" height="{height}" colorSpace="{color_space}"/>
    {asset_resources}
  </resources>
  <library>
    <event name="粗剪听审">
      <project name="{html.escape(project_name, quote=True)}">
        <sequence format="r1" duration="{fcptime(offset_f)}" tcStart="0s" tcFormat="NDF" audioLayout="{audio_layout}" audioRate="{audio_rate // 1000}k">
          <spine>
            {''.join(clips)}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
'''

minidom.parseString(xml)
if output.exists():
    fail(f"输出已存在，拒绝静默覆盖：{output}")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(xml, encoding="utf-8")

# 独立验证：片段相接、sequence 总长、素材存在、官方 DTD。
cursor = sum(
    q(segment["out_f"]) - q(segment["in_f"])
    for segment in segments
)
if cursor != offset_f:
    fail("sequence 时长计算不一致")

dtds = sorted(glob.glob(
    "/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/"
    "Versions/A/Resources/FCPXMLv1_11.dtd"
))
if not dtds:
    fail("找不到 Final Cut Pro 官方 FCPXMLv1_11.dtd")
checked = subprocess.run(
    ["xmllint", "--dtdvalid", os.path.basename(dtds[-1]), "--noout", str(output)],
    capture_output=True,
    text=True,
    cwd=os.path.dirname(dtds[-1]),
)
if checked.returncode != 0:
    output.unlink(missing_ok=True)
    fail("官方 DTD 失败：\n" + "\n".join(checked.stderr.splitlines()[:8]))

print(f"✅ 粗剪听审 FCPXML：{output}")
print(
    f"   项目 {project_name} · {width}×{height} · 原始帧率 {float(native_rate):.3f} · "
    f"{len(source_specs)} 条素材 · {len(segments)} clips · "
    f"{offset_f / float(native_rate):.2f}s · 原素材属性未改 · 官方 DTD 通过"
)
