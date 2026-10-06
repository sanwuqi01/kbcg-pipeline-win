#!/usr/bin/env python3
"""把用户在 Final Cut 中调整过的纯粗剪 XML 变成只读包装基准。

用法：
  python3 p2d_回灌人工粗剪.py prepare <人工.fcpxml> <源工作区根目录> <包装工作区> --core-expression <中文核心表达>
  python3 p2d_回灌人工粗剪.py assemble <人工.fcpxml> <包装工作区>

prepare 只复制每份源素材的词轴/音频缓存，并按人工 XML 生成新的 keep；随后逐源运行
p1a2_对齐词轴.py。assemble 再把已对齐词轴投影到人工 XML 的每个真实源区间。
任何一步都不改变用户的片段范围、顺序或原 XML。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from media_contract import caption_profile, file_uri_to_path


ANALYSIS_FPS = 60


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def seconds(value: str) -> Fraction:
    if not value.endswith("s"):
        raise ValueError(f"非法 FCPXML 时间：{value}")
    return Fraction(value[:-1])


def frames(value: str, frame_duration: str) -> int:
    raw = seconds(value) / seconds(frame_duration)
    if raw.denominator != 1:
        raise ValueError(f"时间不在项目帧网格 {frame_duration}：{value}")
    return raw.numerator


def analysis_frame_at(value: Fraction) -> int:
    """把真实秒数投影到 Skill 统一 60fps 分析网格。"""
    raw = value * ANALYSIS_FPS
    return (2 * raw.numerator + raw.denominator) // (2 * raw.denominator)


def parse_xml(path: Path, *, allow_basic_titles: bool = False):
    tree = ET.parse(path)
    root = tree.getroot()
    if root.attrib.get("version") not in {"1.11", "1.12", "1.13", "1.14"}:
        raise ValueError(f"不支持 FCPXML {root.attrib.get('version')}")
    sequence = root.find(".//project/sequence")
    spine = root.find(".//project/sequence/spine")
    if sequence is None or spine is None:
        raise ValueError("找不到 project/sequence/spine")
    titles = list(spine.iter("title"))
    forbidden = list(spine.iter("caption")) + list(spine.iter("transition"))
    if forbidden or (titles and not allow_basic_titles):
        raise ValueError(
            "人工基准含字幕/标题/转场；纯粗剪入口默认拒绝。"
            "若这是用户精调过的基础字幕 XML，请显式使用 --allow-basic-titles"
        )
    resources = root.find("resources")
    if resources is None:
        raise ValueError("缺 resources")
    fmt_ref = sequence.attrib.get("format")
    fmt = root.find(f"./resources/format[@id='{fmt_ref}']")
    if fmt is None or not fmt.attrib.get("frameDuration"):
        raise ValueError("sequence format 缺 frameDuration")
    frame_duration = fmt.attrib["frameDuration"]
    assets = {}
    for asset in resources.findall("asset"):
        media = asset.find("media-rep")
        if media is None or not media.attrib.get("src"):
            continue
        src = file_uri_to_path(media.attrib["src"])
        assets[asset.attrib["id"]] = {
            "name": asset.attrib.get("name") or Path(src).name,
            "path": os.path.realpath(src),
        }
    clips = []
    for index, clip in enumerate(spine.findall("asset-clip")):
        ref = clip.attrib.get("ref")
        if ref not in assets:
            raise ValueError(f"片段 {index} 引用未知素材 {ref}")
        row = {
            "index": index,
            "ref": ref,
            "source_id": Path(assets[ref]["name"]).stem,
            "source_path": assets[ref]["path"],
            "name": clip.attrib.get("name", ""),
            "offset": clip.attrib["offset"],
            "start": clip.attrib["start"],
            "duration": clip.attrib["duration"],
            "offset_f": frames(clip.attrib["offset"], frame_duration),
            "in_f": frames(clip.attrib["start"], frame_duration),
            "duration_f": frames(clip.attrib["duration"], frame_duration),
        }
        row["out_f"] = row["in_f"] + row["duration_f"]
        clips.append(row)
    if not clips:
        raise ValueError("人工 XML 没有 asset-clip")
    expected_offset = 0
    for row in clips:
        if row["offset_f"] != expected_offset:
            raise ValueError(f"片段 {row['index']} 时间线不连续：{row['offset_f']} != {expected_offset}")
        expected_offset += row["duration_f"]
    if frames(sequence.attrib["duration"], frame_duration) != expected_offset:
        raise ValueError("sequence duration 与片段总长不一致")
    for source_id in sorted({c["source_id"] for c in clips}):
        ranges = sorted((c["in_f"], c["out_f"], c["index"]) for c in clips if c["source_id"] == source_id)
        for left, right in zip(ranges, ranges[1:]):
            if right[0] < left[1]:
                raise ValueError(f"{source_id} 源区间重叠：片段 {left[2]} / {right[2]}")
    if allow_basic_titles:
        effects = {row.get("id"): row for row in root.findall("./resources/effect")}
        for index, title in enumerate(titles):
            effect = effects.get(title.get("ref"))
            name = (effect.get("name") if effect is not None else "") or ""
            uid = (effect.get("uid") if effect is not None else "") or ""
            if title.get("lane") != "1" or not (
                "基本字幕" in name or "Basic Title" in name or "Basic Title" in uid
            ):
                raise ValueError(
                    f"title[{index}] 不是 lane=1 的基础字幕；"
                    "用户精调入口只允许已有基础字幕，不能吞入花字或其他包装"
                )
    return root, sequence, assets, clips, frame_duration, len(titles)


def ranges_from_indices(indices: set[int], reason: str):
    if not indices:
        return []
    out = []
    ordered = sorted(indices)
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value != previous + 1:
            out.append([start, previous, reason])
            start = value
        previous = value
    out.append([start, previous, reason])
    return out


def selected_word_indices(
    track: dict, intervals: list[tuple[int, int]], project_fps: Fraction
) -> set[int]:
    chosen = set()
    for word in track["words"]:
        start = float(word.get("vs", word["ws"])) * float(project_fps)
        end = float(word.get("ve", word["we"])) * float(project_fps)
        midpoint = (start + end) / 2
        # prepare 阶段只用词中点判断整词是否进入人工片段。旧逻辑把“与刀口
        # 重叠 2 帧”也当选中，遇到持续 2 秒的错识别词会把刀口前残字重新带回，
        # 还会让强制对齐任务从错误词开始。真正的词内刀口由 assemble 的逐字符
        # hit 处理，不能在这里先恢复整词。
        if any(a <= midpoint < b for a, b in intervals):
            chosen.add(int(word["i"]))
    return chosen


def command_prepare(args) -> None:
    xml_path = Path(args.xml).resolve()
    source_root = Path(args.source_work_root).resolve()
    work = Path(args.work).resolve()
    if work.exists() and any(work.iterdir()):
        raise SystemExit(f"⛔ 包装工作区非空，拒绝覆盖：{work}")
    work.mkdir(parents=True, exist_ok=True)
    root, sequence, assets, clips, frame_duration, title_count = parse_xml(
        xml_path, allow_basic_titles=args.allow_basic_titles
    )
    project_fps = 1 / seconds(frame_duration)
    project = root.find(".//project")
    fmt_ref = sequence.attrib.get("format")
    fmt = root.find(f"./resources/format[@id='{fmt_ref}']")
    if fmt is None:
        raise SystemExit("⛔ sequence format 无法解析")
    source_rows = []
    for source_id in sorted({c["source_id"] for c in clips}):
        old = source_root / source_id
        if not old.is_dir():
            raise SystemExit(f"⛔ 找不到源工作区：{old}")
        new = work / "sources" / source_id
        new.mkdir(parents=True)
        required = ["config.json", "word_track.json", "_vad16k.wav", "_vad16k.meta.json"]
        words_files = sorted(old.glob("*_words.json"))
        if len(words_files) != 1:
            raise SystemExit(f"⛔ {old} 必须且只能有一份 *_words.json")
        for name in required:
            shutil.copy2(old / name, new / name)
        shutil.copy2(words_files[0], new / words_files[0].name)
        track = read_json(old / "word_track.json")
        intervals = [(c["in_f"], c["out_f"]) for c in clips if c["source_id"] == source_id]
        selected = selected_word_indices(track, intervals, project_fps)
        dropped = set(range(len(track["words"]))) - selected
        old_keep = read_json(old / "keep.json") if (old / "keep.json").exists() else {}
        fix = [item for item in old_keep.get("fix", []) if any(i in selected for i in range(item[0], item[1] + 1))]
        retain = [item for item in old_keep.get("retain", []) if any(i in selected for i in range(item[0], item[1] + 1))]
        write_json(new / "keep.json", {
            "drop": ranges_from_indices(dropped, "人工粗剪 XML 未选中"),
            "fix": fix,
            "retain": retain,
            "split_after": [],
            "review": {
                "status": "approved",
                "reviewer_type": "human",
                "reviewed_by": "作者",
                "scope": ["human_approved_fcpxml", "source_ranges"],
                "human_xml_path": str(xml_path),
                "human_xml_sha256": sha256(xml_path),
            },
        })
        cfg = read_json(old / "config.json")
        source_rows.append({
            "id": source_id,
            "path": next(c["source_path"] for c in clips if c["source_id"] == source_id),
            "work": str(new),
            "sha256": sha256(Path(cfg.get("original_path") or cfg["proxy"])),
        })
    baseline = {
        "schema": "human-rough-cut@2",
        "xml_path": str(xml_path),
        "xml_sha256": sha256(xml_path),
        "project_name": project.attrib.get("name") if project is not None else "",
        "fcpxml_version": root.attrib["version"],
        "width": int(fmt.attrib["width"]),
        "height": int(fmt.attrib["height"]),
        "frame_duration": frame_duration,
        "project_fps": float(project_fps),
        "duration_f": sum(c["duration_f"] for c in clips),
        "clips": clips,
        "upstream_basic_title_count": title_count,
        "allow_basic_titles": bool(args.allow_basic_titles),
    }
    write_json(work / "human_baseline.json", baseline)
    write_json(work / "config.json", {
        "name": "人工确认粗剪包装",
        "cname": args.core_expression,
        "core_expression": args.core_expression,
        "material_type": "single_speaker",
        "human_approved_fcpxml": {"path": str(xml_path), "sha256": baseline["xml_sha256"]},
        "media": {"video": {"width": baseline["width"], "height": baseline["height"]}},
        "sources": source_rows,
        "caption_profile": caption_profile(baseline["width"], baseline["height"]),
    })
    duration_s = float(baseline["duration_f"] * seconds(frame_duration))
    baseline["analysis_duration_f"] = sum(
        analysis_frame_at(seconds(c["start"]) + seconds(c["duration"]))
        - analysis_frame_at(seconds(c["start"]))
        for c in clips
    )
    write_json(work / "human_baseline.json", baseline)
    print(
        f"✅ 人工粗剪已锁定：{len(clips)} 段 / {duration_s:.2f}s / "
        f"{float(project_fps):.3f}fps / SHA {baseline['xml_sha256']}"
    )
    for source in source_rows:
        print(f"   对齐下一步：{source['work']}")


def command_assemble(args) -> None:
    xml_path = Path(args.xml).resolve()
    work = Path(args.work).resolve()
    baseline = read_json(work / "human_baseline.json")
    if str(xml_path) != baseline["xml_path"] or sha256(xml_path) != baseline["xml_sha256"]:
        raise SystemExit("⛔ 人工 XML 路径或哈希已变化，拒绝静默包装")
    _, _, _, parsed_clips, frame_duration, _ = parse_xml(
        xml_path, allow_basic_titles=bool(baseline.get("allow_basic_titles"))
    )
    expected_frame_duration = baseline.get("frame_duration", "1/60s")
    # Final Cut 可能把同一帧率写成等价分数（如 1/60s 与 100/6000s）。
    # 这里比较真实时值，不能因字符串形式不同误判用户改变了项目帧率。
    if seconds(frame_duration) != seconds(expected_frame_duration):
        raise SystemExit("⛔ 人工 XML 项目帧率已变化")
    project_fps = 1 / seconds(frame_duration)
    if parsed_clips != baseline["clips"]:
        raise SystemExit("⛔ 人工 XML 片段范围或顺序已变化")
    cfg = read_json(work / "config.json")
    tracks = {s["id"]: read_json(Path(s["work"]) / "word_track.json") for s in cfg["sources"]}
    display_texts = {}
    for source in cfg["sources"]:
        sid = source["id"]
        track = tracks[sid]
        values = [str(word.get("align_text") or "") for word in track["words"]]
        keep = read_json(Path(source["work"]) / "keep.json")
        for start, end, replacement, *_ in keep.get("fix", []):
            values[start] = replacement
            for index in range(start + 1, end + 1):
                values[index] = ""
        display_texts[sid] = values
    for sid, track in tracks.items():
        missing = [w["i"] for w in track["words"] if not w.get("alignment_skipped") and w.get("align_text") and not w.get("char_times")]
        if missing:
            raise SystemExit(f"⛔ {sid} 仍有未完成字级对齐的保留词：{missing[:12]}")
    rough, master_words, boundary_notes = [], [], []
    for clip in parsed_clips:
        source_words = tracks[clip["source_id"]]["words"]
        chosen = []
        for word in source_words:
            rows = word.get("char_times") or []
            hit = [
                row for row in rows
                if clip["in_f"]
                <= ((float(row["s"]) + float(row["e"])) / 2) * float(project_fps)
                < clip["out_f"]
            ]
            if not hit:
                continue
            if len(hit) != len(rows):
                boundary_notes.append({
                    "clip_index": clip["index"], "source_id": clip["source_id"],
                    "source_word_i": word["i"], "source_text": word.get("align_text"),
                    "kept_text": "".join(r["c"] for r in hit), "reason": "人工刀口落在原 ASR 词内部",
                })
            if len(hit) == len(rows):
                text = display_texts[clip["source_id"]][int(word["i"])]
            else:
                # 人工刀口确实落在一个词内部时，只能显示真实保留下来的字符；
                # 不得把被切掉的半个词重新写回字幕。
                text = "".join(row["c"] for row in hit)
            if not text:
                continue
            global_i = len(master_words)
            row = {
                "w": text,
                "s": float(hit[0]["s"]),
                "e": float(hit[-1]["e"]),
                "i": global_i,
                "align_text": text,
                "char_times": hit,
                "source_id": clip["source_id"],
                "source_word_i": word["i"],
            }
            chosen.append(row)
            master_words.append({
                "t": text, "ws": row["s"], "we": row["e"], "vs": row["s"], "ve": row["e"],
                "i": global_i, "align_text": text, "char_times": hit,
                "source_id": clip["source_id"], "source_word_i": word["i"],
            })
        if not chosen:
            raise SystemExit(f"⛔ 片段 {clip['index']} 没有可对齐字幕文本")
        key = f"H{clip['index']+1:03d}.0"
        source_start_s = seconds(clip["start"])
        source_end_s = source_start_s + seconds(clip["duration"])
        analysis_in_f = analysis_frame_at(source_start_s)
        analysis_out_f = analysis_frame_at(source_end_s)
        rough.append({
            "in": float(source_start_s),
            "out": float(source_end_s),
            "in_f": analysis_in_f,
            "out_f": analysis_out_f,
            "source_native_in_f": clip["in_f"],
            "source_native_out_f": clip["out_f"],
            "raw_in": float(source_start_s),
            "text": "".join(w["w"] for w in chosen),
            "words": chosen,
            "line": f"H{clip['index']+1:03d}",
            "part": 0,
            "source_id": clip["source_id"],
            "clip_name": clip["name"],
            "human_clip_index": clip["index"],
            "_rough_structure": {"playback_key": key, "human_approved": True},
        })
    write_json(work / "rough_segments.json", rough)
    write_json(work / "word_track.json", {
        "fps": float(project_fps),
        "frame_duration": frame_duration,
        "aspect": "portrait" if baseline["height"] > baseline["width"] else "landscape",
        "resolution": f"{baseline['width']}x{baseline['height']}",
        "src": "human_approved_fcpxml",
        "voiced_spans": [],
        "words": master_words,
    })
    write_json(work / "boundary_notes.json", boundary_notes)
    lock_payload = {
        "schema": "human-rough-lock@1",
        "human_xml_path": str(xml_path),
        "human_xml_sha256": baseline["xml_sha256"],
        "clip_count": len(rough),
        "duration_f": sum(row["out_f"] - row["in_f"] for row in rough),
        "rough_segments_sha256": sha256(work / "rough_segments.json"),
        "word_track_sha256": sha256(work / "word_track.json"),
    }
    write_json(work / "human_rough_lock.json", lock_payload)
    print(f"✅ 人工粗剪投影完成：{len(rough)} 段 / {len(master_words)} 个字幕词 / 词内边界 {len(boundary_notes)} 处")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("xml")
    prepare.add_argument("source_work_root")
    prepare.add_argument("work")
    prepare.add_argument("--core-expression", required=True)
    prepare.add_argument(
        "--allow-basic-titles", action="store_true",
        help="允许把用户已精调的 lane=1 基础字幕 XML 当上游；仍拒绝花字/转场",
    )
    assemble = sub.add_parser("assemble")
    assemble.add_argument("xml")
    assemble.add_argument("work")
    args = parser.parse_args()
    if args.command == "prepare":
        command_prepare(args)
    else:
        command_assemble(args)


if __name__ == "__main__":
    main()
