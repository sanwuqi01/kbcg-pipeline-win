#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收剪映原生草稿的原片、真主轨、画幅样式、稀疏重点与切点对齐。"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from media_contract import analysis_to_native, probe, rate, round_fraction
from jianying_contract import (
    MAX_HIGHLIGHT_RATIO,
    card_identity,
    caption_profile,
    font_for_work,
    load_highlight_plan,
    project_card_timerange,
    resolve_font,
)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("draft", type=Path)
    p.add_argument("--work", type=Path)
    p.add_argument("--report", type=Path, help="写入剪映结构验收报告")
    return p


def norm_path(value) -> str:
    """把路径归一化为可比较形式：统一分隔符与大小写，仅用于集合比对。

    剪映草稿正文与元数据历史上都用正斜杠，而 Windows 侧由 pathlib 派生的
    期望路径是反斜杠——两者指向同一文件却字符串不等。这里只归一化用于
    「是否同一文件」的判断，真正不同的路径依旧判为不一致。
    """
    if not isinstance(value, str) or not value:
        return ""
    return os.path.normcase(os.path.normpath(value))


def frame_us(frame: int, fps_raw: str) -> int:
    native = analysis_to_native(frame, fps_raw)
    return round_fraction(native * 1_000_000 / rate(fps_raw))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def expected_sources(work: Path) -> tuple[list[dict], list[dict]]:
    cfg = json.loads((work / "config.json").read_text(encoding="utf-8"))
    rough = json.loads((work / "rough_segments.json").read_text(encoding="utf-8"))
    delivery_path = work / "media_delivery.json"
    delivery_by_id = {}
    if delivery_path.is_file():
        delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
        if delivery.get("schema") != "kbcg-xgz/jianying_media_delivery@1":
            raise ValueError("media_delivery.json schema 不匹配")
        delivery_by_id = {
            row["id"]: row for row in delivery.get("sources", [])
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
    declared = cfg.get("sources")
    if isinstance(declared, list) and declared:
        by_id = {row["id"]: str(Path(row["path"]).expanduser().resolve()) for row in declared}
        used = []
        for pos, row in enumerate(rough):
            source_id = row.get("source_id")
            if source_id not in by_id:
                raise ValueError(f"rough_segments[{pos}] source_id 无对应素材")
            if source_id not in used:
                used.append(source_id)
        return ([{
            "id": sid,
            "path": str(Path(delivery_by_id.get(sid, {}).get("delivery_path") or by_id[sid]).expanduser().resolve()),
            "sha256": delivery_by_id.get(sid, {}).get("sha256"),
        } for sid in used], rough)
    source = str(Path(cfg.get("original_path") or cfg.get("proxy")).expanduser().resolve())
    row = delivery_by_id.get("source", {})
    return ([{
        "id": "source",
        "path": str(Path(row.get("delivery_path") or source).expanduser().resolve()),
        "sha256": row.get("sha256"),
    }], rough)


def main() -> int:
    args = parser().parse_args()
    root = args.draft.expanduser().resolve()
    info = root / "draft_info.json"
    content = root / "draft_content.json"
    fails: list[str] = []
    report_path = args.report.expanduser().resolve() if args.report else (
        args.work.expanduser().resolve() / "jianying_structural_verification.json"
        if args.work else root / "jianying_structural_verification.json"
    )
    if not info.is_file() or not content.is_file():
        raise SystemExit("⛔ 草稿缺 draft_info.json 或 draft_content.json")
    # 期望字体来自工作区 config.json（p0 --font 写入）；没给 --work 时回落默认字体。
    try:
        expected_font = (
            font_for_work(args.work.expanduser().resolve()) if args.work
            else resolve_font(None)
        )
    except ValueError as exc:
        raise SystemExit(f"⛔ {exc}") from exc
    a = json.loads(info.read_text(encoding="utf-8"))
    b = json.loads(content.read_text(encoding="utf-8"))
    if a != b:
        fails.append("draft_info.json 与 draft_content.json 不一致")

    tracks = b.get("tracks", [])
    # 配图轨（p4i，name="配图"）是合法的第二条 video 轨；主轨以 name="main_track" 定位。
    # 踩过的坑（2026-09-15）：这里原写死「video 轨必须恰好 1 条」，配图功能上线后
    # 首次真机交付时 timeline_segments 被置空，验收在 expected_video_ranges[si] 越界。
    main_tracks = [t for t in tracks if t.get("name") == "main_track"]
    overlay_tracks = [t for t in tracks if t.get("name") == "配图"]
    if len(main_tracks) != 1:
        fails.append(f"真主轨（main_track）数量必须为 1，实际 {len(main_tracks)}")
    if len(overlay_tracks) > 1:
        fails.append(f"配图轨最多 1 条，实际 {len(overlay_tracks)}")
    if main_tracks and (
        not main_tracks[0].get("segments") or any(
            seg.get("render_index") != 0 for seg in main_tracks[0].get("segments", [])
        )
    ):
        fails.append("视频没有全部落在 render_index=0 的真实主轨")

    materials = b.get("materials", {}).get("texts", [])
    if not materials:
        fails.append("没有原生字幕 materials.texts")
    profile = None
    for index, item in enumerate(materials):
        if item.get("type") != "subtitle":
            fails.append(f"字幕{index} type 不是 subtitle")
        try:
            body = json.loads(item.get("content") or "{}")
            base = body.get("styles", [])[0]
        except (ValueError, IndexError, TypeError):
            fails.append(f"字幕{index} content 非法")
            continue
        font = base.get("font", {})
        if font.get("id") != expected_font["resource_id"]:
            fails.append(
                f"字幕{index} 未绑定剪映「{expected_font['name']}」资源"
                f"（期望 {expected_font['resource_id']}，实际 {font.get('id')}）"
            )

    video_materials = b.get("materials", {}).get("videos", [])
    if not video_materials:
        fails.append("没有视频素材 materials.videos")
    # 配图素材（type=photo，p4i 画中画）不是原片：不参与 probe / 原片集比较，
    # 只验文件存在。原片集比较只看 type=video 的素材。
    photo_materials = [m for m in video_materials if m.get("type") == "photo"]
    main_materials = [m for m in video_materials if m.get("type") != "photo"]
    for m in photo_materials:
        p = m.get("path")
        if not p or not Path(p).is_file():
            fails.append(f"配图素材路径失效: {p}")
    paths = [m.get("path") for m in main_materials]
    normalized = [norm_path(path) for path in paths]
    if len(normalized) != len(set(normalized)):
        fails.append("同一份原素材被重复登记")
    for path in paths:
        if not path or not Path(path).is_file():
            fails.append(f"视频素材路径失效: {path}")
    actuals = []
    for path in paths:
        if not path or not Path(path).is_file():
            continue
        try:
            actuals.append(probe(path))
        except ValueError as exc:
            fails.append(str(exc))
    actual = actuals[0] if actuals else None
    if actuals:
        try:
            profile = caption_profile(actual["width"], actual["height"])
        except ValueError as exc:
            fails.append(str(exc))
        technical_keys = (
            "width", "height", "encoded_width", "encoded_height", "rotation",
            "frame_rate_raw", "audio_sample_rate", "audio_channels",
        )
        for other in actuals[1:]:
            if any(actual.get(key) != other.get(key) for key in technical_keys):
                fails.append("多素材的画幅/帧率/旋转/音频属性不一致")
                break
        canvas = b.get("canvas_config") or {}
        if (int(canvas.get("width", 0)), int(canvas.get("height", 0))) != (
            actual["width"], actual["height"]
        ):
            fails.append("剪映画布没有保持原素材显示画幅")
        if abs(float(b.get("fps", 0)) - actual["fps"]) > 0.0001:
            fails.append(
                f"剪映项目帧率被改变：草稿 {b.get('fps')}，素材 {actual['fps']}"
            )

    if profile:
        for index, item in enumerate(materials):
            try:
                body = json.loads(item.get("content") or "{}")
                base = body.get("styles", [])[0]
                color = base["fill"]["content"]["solid"]["color"]
            except (ValueError, IndexError, KeyError, TypeError):
                continue
            if float(base.get("size", -1)) != profile["font_size"]:
                fails.append(
                    f"字幕{index} 字号不是当前画幅的 {profile['font_size']:g}"
                )
            if [float(x) for x in color] != [1.0, 1.0, 1.0]:
                fails.append(f"字幕{index} 基础颜色不是纯白 #FFFFFF")

    meta_path = root / "draft_meta_info.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        library_items = [
            item
            for group in meta.get("draft_materials", [])
            if group.get("type") == 0
            for item in group.get("value", [])
        ]
    except (OSError, ValueError, TypeError):
        library_items = []
        fails.append("项目素材库 draft_meta_info.json 非法")
    library_paths = [item.get("file_Path") for item in library_items]
    # 素材库应与时间线引用的全集（主片 + 配图）一致；配图文件也算素材库成员。
    all_declared_paths = paths + [m.get("path") for m in photo_materials]
    if len(library_paths) != len(all_declared_paths) or set(map(norm_path, library_paths)) != set(
        map(norm_path, all_declared_paths)
    ):
        fails.append("项目素材库与时间线引用的完整原文件集不一致")
    for item in library_items:
        path = item.get("file_Path")
        if path and item.get("extra_info") != Path(path).name:
            fails.append(f"项目素材库原片名称不正确: {path}")

    material_path_by_id = {
        item.get("id"): item.get("path") for item in video_materials if item.get("id")
    }
    timeline_segments = main_tracks[0].get("segments", []) if len(main_tracks) == 1 else []
    for pos, segment in enumerate(timeline_segments):
        if segment.get("material_id") not in material_path_by_id:
            fails.append(f"主轨片段{pos}引用了不存在的视频素材")

    if args.work:
        try:
            work = args.work.expanduser().resolve()
            specs, rough = expected_sources(work)
            expected_paths = [row["path"] for row in specs]
            if set(normalized) != set(map(norm_path, expected_paths)):
                fails.append("草稿原素材集与冻结粗剪实际引用集不一致")
            if len(timeline_segments) != len(rough):
                fails.append(
                    f"草稿主轨 {len(timeline_segments)} 段，冻结粗剪 {len(rough)} 段"
                )
            spec_by_id = {row["id"]: row for row in specs}
            actual_by_id = {row["id"]: probe(row["path"]) for row in specs}
            for row in specs:
                expected_hash = row.get("sha256")
                if expected_hash and sha256(Path(row["path"])) != expected_hash:
                    fails.append(f"剪映交付素材哈希漂移: {row['path']}")
            cursor = 0
            expected_video_ranges = []
            for pos, (segment, row) in enumerate(zip(timeline_segments, rough)):
                source_id = row.get("source_id") if len(specs) > 1 else specs[0]["id"]
                source = spec_by_id[source_id]
                fps_raw = actual_by_id[source_id]["frame_rate_raw"]
                start = frame_us(int(row["in_f"]), fps_raw)
                end = frame_us(int(row["out_f"]), fps_raw)
                expected_source = {"start": start, "duration": end - start}
                expected_target = {"start": cursor, "duration": end - start}
                if norm_path(material_path_by_id.get(segment.get("material_id"))) != norm_path(source["path"]):
                    fails.append(f"主轨片段{pos}原素材映射错误")
                if segment.get("source_timerange") != expected_source:
                    fails.append(f"主轨片段{pos}原素材入出点改变")
                if segment.get("target_timerange") != expected_target:
                    fails.append(f"主轨片段{pos}时间线位置改变")
                expected_video_ranges.append(expected_target)
                cursor += end - start

            captions = json.loads((work / "captions_plan.json").read_text(encoding="utf-8"))
            cards = list(captions.get("normal", [])) + list(captions.get("highlight", []))
            cards.sort(key=lambda row: (
                int(row["sf"]), int(row["ef"]), row.get("text", "")
            ))
            if actual is None or profile is None:
                raise ValueError("无法建立剪映字幕 profile")
            highlight_plan = load_highlight_plan(
                work, cards, actual["width"], actual["height"]
            )
            text_tracks = [track for track in tracks if track.get("type") == "text"]
            if len(text_tracks) != 1:
                fails.append(f"基础字幕轨必须为 1，实际 {len(text_tracks)}")
                subtitle_segments = []
            else:
                subtitle_segments = text_tracks[0].get("segments", [])
            if len(subtitle_segments) != len(cards):
                fails.append(
                    f"字幕段 {len(subtitle_segments)} 张，计划 {len(cards)} 张"
                )
            text_by_id = {row.get("id"): row for row in materials}
            segments_tl = captions.get("segments_tl") or []
            actual_highlights = 0
            video_cuts = {
                tr["start"] for tr in expected_video_ranges
            } | {
                tr["start"] + tr["duration"] for tr in expected_video_ranges
            }
            for pos, (segment, card) in enumerate(zip(subtitle_segments, cards)):
                si = int(card["si"])
                source_id = rough[si].get("source_id") if len(specs) > 1 else specs[0]["id"]
                expected = project_card_timerange(
                    card,
                    segments_tl[si],
                    rough[si],
                    expected_video_ranges[si]["start"],
                    expected_video_ranges[si]["duration"],
                    actual_by_id[source_id]["frame_rate_raw"],
                )
                if segment.get("target_timerange") != expected:
                    fails.append(f"字幕{pos}没有按归属片段的声学时码对齐")
                start = expected["start"]
                end = start + expected["duration"]
                if any(start < cut < end for cut in video_cuts):
                    fails.append(f"字幕{pos}越过视频切点")
                clip = segment.get("clip") or {}
                transform = clip.get("transform") or {}
                if float(transform.get("x", 99)) != profile["transform_x"]:
                    fails.append(f"字幕{pos} X 位置错误")
                if float(transform.get("y", 99)) != profile["transform_y"]:
                    fails.append(f"字幕{pos} Y 位置错误")
                body = json.loads(
                    (text_by_id.get(segment.get("material_id")) or {}).get("content") or "{}"
                )
                if body.get("text") != card.get("text"):
                    fails.append(f"字幕{pos}文字与 P2 真值不一致")
                styled = len(body.get("styles") or []) > 1
                expected_styled = card_identity(card) in highlight_plan["selected"]
                if styled != expected_styled:
                    fails.append(f"字幕{pos}重点上色与 P5 显式清单不一致")
                actual_highlights += int(styled)
            ratio = actual_highlights / len(cards) if cards else 0.0
            if ratio > MAX_HIGHLIGHT_RATIO + 1e-12:
                fails.append(f"重点字幕密度 {ratio:.1%} 超过 6%")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            fails.append(f"无法反查冻结粗剪: {exc}")

    if fails:
        atomic_json(report_path, {
            "schema": "kbcg-xgz/jianying_structural_verification@1",
            "checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "draft_path": str(root),
            "structural_verified": False,
            "material_bin_declared": bool(library_paths),
            "material_bin_visible": None,
            "timeline_media_accessible": None,
            "preview_frame_visible": None,
            "failures": fails,
        })
        print(f"⛔ 剪映草稿验收 FAIL {len(fails)}:")
        for fail in fails[:20]:
            print("  " + fail)
        return 1
    atomic_json(report_path, {
        "schema": "kbcg-xgz/jianying_structural_verification@1",
        "checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "draft_path": str(root),
        "structural_verified": True,
        "material_bin_declared": bool(library_paths),
        "material_bin_visible": None,
        "timeline_media_accessible": None,
        "preview_frame_visible": None,
        "source_paths": paths,
        "failures": [],
    })
    print(
        f"✅ 剪映草稿结构验收通过: 项目素材记录已声明 {len(paths)} 份 · "
        f"真主轨 {len(main_tracks[0]['segments'])} 段 · "
        + (f"配图 {len(overlay_tracks[0]['segments'])} 张 · " if overlay_tracks else "")
        +
        f"原生字幕 {len(materials)} 张 · {expected_font['name']} · "
        f"{profile['font_size']:g} 号 · 纯白 · "
        f"{actual['width']}x{actual['height']} · {actual['fps']:.3f}fps\n"
        "   ⚠️ 本步不证明剪映界面已显示素材或剪映进程已取得文件权限"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
