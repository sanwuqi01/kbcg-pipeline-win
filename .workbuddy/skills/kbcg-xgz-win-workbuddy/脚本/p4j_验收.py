#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剪映导入版验收：主轨结构、原生字幕、底部锚点、时间线与 DTD。"""
from __future__ import annotations

import collections
import glob
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from media_contract import file_uri_to_path, frame_duration, frames_on_grid, probe, seconds


def main() -> int:
    path = Path(sys.argv[1]).expanduser().resolve()
    root = ET.parse(path).getroot()
    fails: list[str] = []
    seq = root.find(".//sequence")
    if seq is None:
        raise SystemExit("⛔ 剪映 XML 缺 sequence")
    fmt = root.find(f"./resources/format[@id='{seq.get('format')}']")
    if fmt is None or not fmt.get("frameDuration"):
        raise SystemExit("⛔ 剪映 XML 缺项目 frameDuration")
    grid = fmt.get("frameDuration")

    def frames(value: str | None) -> int:
        if value is None:
            raise ValueError("缺时间属性")
        return frames_on_grid(value, grid)

    spine = root.find(".//spine")
    if spine is None:
        raise SystemExit("⛔ 剪映 XML 缺 spine")

    direct = list(spine)
    clips = [node for node in direct if node.tag == "asset-clip"]
    if len(clips) != len(direct) or not clips:
        fails.append("spine 必须且只能直接包含 asset-clip，禁止 gap/secondary-storyline")
    cur = 0
    caption_rows: list[tuple[int, int, int]] = []
    clip_rows: list[tuple[int, int, int]] = []
    caption_count = 0
    for index, clip in enumerate(clips):
        try:
            off = frames(clip.get("offset"))
            start = frames(clip.get("start"))
            dur = frames(clip.get("duration"))
        except ValueError as exc:
            fails.append(f"clip{index}: {exc}")
            continue
        if clip.get("lane") is not None:
            fails.append(f"clip{index} 带 lane，不能作为主故事线视频")
        if off != cur:
            fails.append(f"clip{index} 未相接: {off} != {cur}")
        cur = off + dur
        clip_rows.append((index, off, cur))

        for title in clip.findall("title"):
            if title.get("lane") == "1":
                fails.append(f"clip{index} 仍含普通主字幕 title，剪映无法统一按字幕管理")
        for caption in clip.findall("caption"):
            caption_count += 1
            if caption.get("role") != "caption.ITT.zh":
                fails.append(f"clip{index} caption role 不是 caption.ITT.zh")
            if caption.find("param[@name='Position']") is not None:
                fails.append(f"clip{index} caption 携带 Final Cut Position 私有参数")
            text = caption.find("text")
            if text is None or text.get("placement") != "bottom":
                fails.append(f"clip{index} caption 缺少 placement=bottom")
            shown = "" if text is None else "".join(text.itertext()).strip()
            if not shown:
                fails.append(f"clip{index} caption 文本为空")
            for style in caption.findall(".//text-style"):
                # 文本正文中的 text-style 只有 ref；实际字号在 text-style-def 内。
                if style.get("font") is not None and style.get("fontSize") != "188":
                    fails.append(f"clip{index} caption 字号未标定为剪映 8 号")
            try:
                begin = off + frames(caption.get("offset")) - start
                end = begin + frames(caption.get("duration"))
                caption_rows.append((begin, end, index))
            except ValueError as exc:
                fails.append(f"clip{index} caption: {exc}")

    if caption_count == 0:
        fails.append("没有原生 caption 字幕")

    caption_rows.sort()
    for index, clip_start, clip_end in clip_rows:
        overlaps = sorted(
            (max(a, clip_start), min(b, clip_end))
            for a, b, _ in caption_rows if b > clip_start and a < clip_end
        )
        if not overlaps:
            fails.append(f"clip{index} 没有字幕覆盖")
            continue
        if not 0 <= overlaps[0][0] - clip_start <= 24:
            fails.append(f"clip{index} 首字幕与段起差值异常")
        cursor = overlaps[0][1]
        for begin, end in overlaps[1:]:
            if begin != cursor:
                fails.append(f"clip{index} 字幕覆盖不连续 @{cursor}→{begin}")
            cursor = max(cursor, end)
        if cursor != clip_end:
            fails.append(f"clip{index} 末字幕终点 {cursor} != 段终 {clip_end}")

    try:
        if frames(seq.get("duration")) != cur:
            fails.append("sequence 时长不等于主故事线总长")
    except ValueError as exc:
        fails.append(str(exc))

    ids = [node.get("id") for node in root.iter() if node.get("id")]
    dup = [key for key, count in collections.Counter(ids).items() if count > 1]
    if dup:
        fails.append(f"ID 重复: {dup[:5]}")
    media_rep = root.find(".//media-rep")
    if media_rep is None:
        fails.append("缺少原素材 media-rep")
    else:
        source = file_uri_to_path(media_rep.get("src") or "")
        if not os.path.isfile(source):
            fails.append(f"素材不存在: {source}")
        else:
            try:
                actual = probe(source)
                expected_fd = frame_duration(actual["frame_rate_raw"])
                if seconds(grid) != seconds(expected_fd):
                    fails.append(f"项目帧率被改变：XML {grid}，素材 {expected_fd}")
                if (int(fmt.get("width", 0)), int(fmt.get("height", 0))) != (
                    actual["width"], actual["height"]
                ):
                    fails.append("项目画幅没有保持原素材显示画幅")
            except ValueError as exc:
                fails.append(str(exc))

    dtds = sorted(glob.glob(
        "/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/"
        "Versions/A/Resources/FCPXMLv1_11.dtd"
    ))
    if not dtds:
        fails.append("找不到 Final Cut Pro 官方 FCPXMLv1_11.dtd")
    else:
        result = subprocess.run(
            ["xmllint", "--dtdvalid", os.path.basename(dtds[-1]), "--noout", str(path)],
            cwd=os.path.dirname(dtds[-1]), capture_output=True, text=True,
        )
        if result.returncode:
            fails.extend("DTD: " + line for line in result.stderr.splitlines()[:8])

    print("=" * 50)
    if fails:
        print(f"⛔ 剪映 XML 验收 FAIL {len(fails)}:")
        for fail in fails[:20]:
            print("  " + fail)
        return 1
    print(
        f"✅ 剪映 XML 验收通过: {len(clips)} 个主故事线 clip · "
        f"{caption_count} 张原生字幕 · 8 号 · bottom 锚点 · "
        f"原素材画幅/帧率未改 · 全帧网格 · 官方 DTD"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
