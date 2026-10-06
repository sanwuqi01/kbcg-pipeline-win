#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FCPXML 验收：原始画幅/帧率/音频属性、主轨、字幕覆盖、素材与 DTD。"""
from __future__ import annotations

import collections
import argparse
import json
import glob
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from media_contract import (
    caption_profile, fcpxml_color_space, fcpxml_format_name,
    fcpxml_shadow_attributes, file_uri_to_path, frame_duration,
    fcpxml_source_timecode, frames_on_grid, probe, seconds,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path)
    parser.add_argument("--work", type=Path)
    args = parser.parse_args()
    path = args.xml.expanduser().resolve()
    config = None
    if args.work:
        try:
            config = json.loads((args.work.expanduser().resolve() / "config.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"⟂ 无法读取工作区 config.json: {exc}")
    root = ET.parse(path).getroot()
    fails: list[str] = []
    sequence = root.find(".//sequence")
    spine = root.find(".//sequence/spine")
    if sequence is None or spine is None:
        raise SystemExit("⛔ FCPXML 缺 sequence/spine")
    fmt_ref = sequence.get("format")
    fmt = root.find(f"./resources/format[@id='{fmt_ref}']")
    if fmt is None or not fmt.get("frameDuration"):
        raise SystemExit("⛔ sequence format 缺 frameDuration")
    grid = fmt.get("frameDuration")

    def fr(value: str | None) -> int:
        if value is None:
            raise ValueError("缺时间属性")
        return frames_on_grid(value, grid)

    clips = spine.findall("asset-clip")
    cur = 0
    covered = 0
    clip_rows: list[tuple[int, int, int]] = []
    title_rows: list[tuple[int, int, int, int]] = []
    main_titles: list[ET.Element] = []
    for index, clip in enumerate(clips):
        try:
            off, start, dur = fr(clip.get("offset")), fr(clip.get("start")), fr(clip.get("duration"))
        except ValueError as exc:
            fails.append(f"clip{index}: {exc}")
            continue
        if off != cur:
            fails.append(f"clip{index} 未相接: offset={off} 期望{cur}")
        cur = off + dur
        clip_rows.append((index, off, cur))
        for title_index, title in enumerate(
            node for node in clip.findall("title") if node.get("lane") == "1"
        ):
            main_titles.append(title)
            shown = "".join((node.text or "") for node in title.findall(".//text-style")).strip()
            if not shown:
                fails.append(f"clip{index} 卡{title_index} 主字幕文本为空")
            try:
                begin = off + fr(title.get("offset")) - start
                end = begin + fr(title.get("duration"))
            except ValueError as exc:
                fails.append(f"clip{index} 卡{title_index}: {exc}")
                continue
            title_rows.append((begin, end, index, title_index))

    title_rows.sort()
    # 基础字幕必须从所属视频段首开始。旧的 0.4 秒容差会把肉眼可见的
    # 3～24 帧晚出误判为合格，导致同一问题在多轮交付中反复出现。
    max_first_gap = 0
    for index, clip_start, clip_end in clip_rows:
        overlaps = sorted(
            (max(a, clip_start), min(b, clip_end), owner, card)
            for a, b, owner, card in title_rows
            if b > clip_start and a < clip_end
        )
        if not overlaps:
            fails.append(f"clip{index} 没有主字幕覆盖")
            continue
        covered += 1
        first_gap = overlaps[0][0] - clip_start
        if not 0 <= first_gap <= max_first_gap:
            fails.append(f"clip{index} 首字幕晚于段起 {first_gap} 帧")
        cursor = overlaps[0][1]
        for begin, end, _, _ in overlaps[1:]:
            if begin != cursor:
                fails.append(f"clip{index} 字幕覆盖不连续 @{cursor}→{begin}")
            cursor = max(cursor, end)
        if cursor != clip_end:
            fails.append(f"clip{index} 末字幕终点 {cursor} ≠ 段终 {clip_end}")

    try:
        if fr(sequence.get("duration")) != cur:
            fails.append("sequence 时长不等于主轨总长")
    except ValueError as exc:
        fails.append(str(exc))

    ids = [node.get("id") for node in root.iter() if node.get("id")]
    duplicate_ids = [key for key, count in collections.Counter(ids).items() if count > 1]
    if duplicate_ids:
        fails.append(f"ID 重复：{duplicate_ids[:5]}")

    media_rep = root.find(".//media-rep")
    asset = root.find("./resources/asset")
    if media_rep is None or asset is None:
        fails.append("缺原素材 asset/media-rep")
        actual = None
    else:
        source = file_uri_to_path(media_rep.get("src") or "")
        if not os.path.isfile(source):
            fails.append(f"素材不存在：{source}")
            actual = None
        else:
            try:
                actual = probe(source)
            except ValueError as exc:
                fails.append(str(exc))
                actual = None
    if actual:
        expected_fd = frame_duration(actual["frame_rate_raw"])
        if seconds(grid) != seconds(expected_fd):
            fails.append(f"项目帧率被改变：XML {grid}，素材 {expected_fd}")
        if (int(fmt.get("width", 0)), int(fmt.get("height", 0))) != (
            actual["width"], actual["height"]
        ):
            fails.append("项目画幅没有保持原素材显示画幅")
        expected_name = fcpxml_format_name(
            actual["width"], actual["height"], actual["frame_rate_raw"]
        )
        if expected_name and fmt.get("name") != expected_name:
            fails.append(
                f"Final Cut 标准格式名称缺失或错误："
                f"XML {fmt.get('name')!r}，期望 {expected_name!r}"
            )
        try:
            expected_color_space = fcpxml_color_space(
                actual, config.get("color_contract") if isinstance(config, dict) else None
            )
        except ValueError as exc:
            fails.append(str(exc))
            expected_color_space = None
        if expected_color_space and fmt.get("colorSpace") != expected_color_space:
            fails.append(
                f"Final Cut 色彩空间声明缺失或错误："
                f"XML {fmt.get('colorSpace')!r}，期望 {expected_color_space!r}"
            )
        asset_fmt = root.find(f"./resources/format[@id='{asset.get('format')}']")
        if asset_fmt is None:
            fails.append("asset format 无法解析")
        elif (
            int(asset_fmt.get("width", 0)), int(asset_fmt.get("height", 0)),
            seconds(asset_fmt.get("frameDuration") or "0s"),
        ) != (actual["width"], actual["height"], seconds(expected_fd)):
            fails.append("asset 画幅或帧率声明与原素材不一致")
        if int(asset.get("audioRate", 0)) != actual["audio_sample_rate"]:
            fails.append("asset 音频采样率与原素材不一致")
        if int(asset.get("audioChannels", 0)) != actual["audio_channels"]:
            fails.append("asset 音频声道与原素材不一致")
        if asset.get("hasVideo") == "1" and asset.get("videoSources") != "1":
            fails.append("asset 已声明视频，但缺 videoSources=1")
        try:
            expected_asset_start, expected_tc_format = fcpxml_source_timecode(actual)
            asset_start = fr(asset.get("start"))
            asset_duration = fr(asset.get("duration"))
        except ValueError as exc:
            fails.append(str(exc))
        else:
            if asset_start != expected_asset_start:
                fails.append(
                    f"asset 源时间码起点错误：XML {asset_start} 帧，"
                    f"素材应为 {expected_asset_start} 帧（{actual.get('timecode') or '无内嵌时间码'}）"
                )
            asset_end = asset_start + asset_duration
            for index, clip in enumerate(clips):
                try:
                    clip_start = fr(clip.get("start"))
                    clip_end = clip_start + fr(clip.get("duration"))
                except ValueError:
                    continue
                if not asset_start <= clip_start < clip_end <= asset_end:
                    fails.append(
                        f"clip{index} 源区间 {clip_start}→{clip_end} 不在 "
                        f"asset {asset_start}→{asset_end} 内"
                    )
                if clip.get("tcFormat", "NDF") != expected_tc_format:
                    fails.append(
                        f"clip{index} tcFormat={clip.get('tcFormat')!r}，"
                        f"素材时间码期望 {expected_tc_format}"
                    )
        try:
            profile = caption_profile(actual["width"], actual["height"])
        except ValueError:
            # 非标准画幅由生成器要求显式 profile；验收器没有工作区 config，
            # 这里只跳过内置 profile 对照，仍继续结构/素材/DTD 验收。
            profile = None
        if profile:
            for index, title in enumerate(main_titles):
                position = next(
                    (
                        node.get("value")
                        for node in title.findall("param")
                        if (node.get("key") or "").endswith("/1/100/101")
                    ),
                    None,
                )
                try:
                    x_raw, y_raw = (position or "").split()
                    if abs(float(x_raw)) > 0.001 or abs(float(y_raw) - float(profile["xml_y"])) > 0.001:
                        raise ValueError
                except (ValueError, AttributeError):
                    fails.append(
                        f"主字幕{index} Position={position!r}，期望 0 {profile['xml_y']}"
                    )
                styles = title.findall("text-style-def/text-style")
                if not styles:
                    fails.append(f"主字幕{index} 缺 text-style")
                    continue
                for style in styles:
                    try:
                        size_ok = abs(float(style.get("fontSize", "nan")) - float(profile["font_size"])) <= 0.001
                    except ValueError:
                        size_ok = False
                    if style.get("font") != profile["font"] or not size_ok \
                       or style.get("alignment") != "center":
                        fails.append(
                            f"主字幕{index} 样式漂移：font={style.get('font')!r} "
                            f"size={style.get('fontSize')!r} align={style.get('alignment')!r}；"
                            f"期望 {profile['font']} / {profile['font_size']} / center"
                        )
                    expected_shadow = fcpxml_shadow_attributes(profile)
                    for key, expected_value in expected_shadow.items():
                        if style.get(key) != expected_value:
                            fails.append(
                                f"主字幕{index} {key}={style.get(key)!r}，"
                                f"期望 {expected_value!r}"
                            )

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
        print(f"⛔ FCPXML 验收 FAIL {len(fails)}:")
        for failure in fails[:20]:
            print("  " + failure)
        return 1
    print(
        f"✅ FCPXML 验收通过：{len(clips)} clip 零气口相接 · "
        f"主字幕覆盖 {covered}/{len(clips)} · {actual['width']}x{actual['height']} · "
        f"{actual['fps']:.3f}fps · profile {profile['name'] if profile else 'custom'} · "
        f"原素材属性未改 · 官方 DTD"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
