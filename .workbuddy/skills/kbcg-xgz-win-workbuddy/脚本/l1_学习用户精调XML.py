#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读比较 AI FCPXML 与用户精调 FCPXML，按 P0–P6 输出学习证据。

用法：l1_学习用户精调XML.py <AI版.fcpxml|fcpxmld> <用户版> --output <报告.json>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from fractions import Fraction
from pathlib import Path
from typing import Any

from media_contract import file_uri_to_path


SCHEMA = "kbcg-xgz/fcpxml_feedback_diff@2"


def resolve_xml(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path.is_file():
        return path
    if path.is_dir():
        candidate = path / "Info.fcpxml"
        if candidate.is_file():
            return candidate
    raise ValueError(f"找不到 FCPXML: {path}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def seconds(value: str | None) -> Fraction:
    if not value:
        return Fraction(0)
    raw = value[:-1] if value.endswith("s") else value
    return Fraction(raw)


def as_float(value: Fraction) -> float:
    return round(float(value), 6)


def source_name(asset: ET.Element) -> str:
    media_rep = asset.find("media-rep")
    src = media_rep.get("src") if media_rep is not None else None
    if src:
        name = file_uri_to_path(src).name
        if name:
            return name
    return asset.get("name") or asset.get("id") or "unknown"


def title_text(title: ET.Element) -> str:
    return "".join((node.text or "") for node in title.findall("./text/text-style"))


def title_style(title: ET.Element) -> dict[str, Any]:
    params = {
        node.get("name", node.get("key", "")): node.get("value")
        for node in title.findall("./param")
    }
    styles = []
    for node in title.findall("./text-style-def/text-style"):
        styles.append(
            {
                key: node.get(key)
                for key in ("font", "fontSize", "fontFace", "fontColor", "alignment")
            }
        )
    return {"params": params, "text_styles": styles}


def parse(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    resources = root.find("./resources")
    assets = {}
    effects = {}
    formats = {}
    if resources is not None:
        for asset in resources.findall("./asset"):
            assets[asset.get("id")] = {
                "name": source_name(asset),
                "origin": seconds(asset.get("start")),
            }
        for effect in resources.findall("./effect"):
            effects[effect.get("id")] = effect.get("name") or ""
        for fmt in resources.findall("./format"):
            formats[fmt.get("id")] = dict(fmt.attrib)
    project = root.find(".//project")
    sequence = project.find("./sequence") if project is not None else None
    spine = sequence.find("./spine") if sequence is not None else None
    primary = []
    if spine is not None:
        for clip in spine.findall("./asset-clip"):
            ref = clip.get("ref")
            start = seconds(clip.get("start"))
            duration = seconds(clip.get("duration"))
            primary.append(
                {
                    "source": (assets.get(ref) or {}).get("name", ref or "unknown"),
                    "start": start - (assets.get(ref) or {}).get("origin", Fraction(0)),
                    "end": start - (assets.get(ref) or {}).get("origin", Fraction(0)) + duration,
                    "duration": duration,
                    "offset": seconds(clip.get("offset")),
                }
            )
    titles = []
    if spine is not None:
        for clip_index, clip in enumerate(spine.findall("./asset-clip")):
            ref = clip.get("ref")
            asset = assets.get(ref) or {"name": ref or "unknown", "origin": Fraction(0)}
            clip_source_start = seconds(clip.get("start"))
            clip_timeline_start = seconds(clip.get("offset"))
            for title in clip.findall(".//title"):
                visible_source_abs = seconds(title.get("offset"))
                title_start = seconds(title.get("start"))
                duration = seconds(title.get("duration"))
                timeline_start = clip_timeline_start + visible_source_abs - clip_source_start
                visible_source = visible_source_abs - asset["origin"]
                origin_candidate = visible_source - title_start
                text = title_text(title)
                titles.append(
                    {
                        "text": text,
                        "offset": visible_source_abs,
                        "duration": duration,
                        "title_start": title_start,
                        "timeline_start": timeline_start,
                        "timeline_end": timeline_start + duration,
                        "source": asset["name"],
                        "parent_clip_index": clip_index,
                        "source_visible_offset": visible_source,
                        "source_origin_candidate": origin_candidate,
                        "source_anchor_candidates": sorted(
                            {as_float(visible_source), as_float(origin_candidate)}
                        ),
                        "effect": effects.get(title.get("ref"), title.get("ref") or ""),
                        "style": title_style(title),
                    }
                )
    return {
        "fcpxml_version": root.get("version"),
        "project_name": project.get("name") if project is not None else None,
        "sequence_format": sequence.get("format") if sequence is not None else None,
        "duration": seconds(sequence.get("duration")) if sequence is not None else Fraction(0),
        "formats": formats,
        "assets": sorted({row["name"] for row in assets.values()}),
        "primary": primary,
        "titles": titles,
    }


def merge_intervals(rows: list[tuple[Fraction, Fraction]]) -> list[tuple[Fraction, Fraction]]:
    output: list[list[Fraction]] = []
    for start, end in sorted(rows):
        if not output or start > output[-1][1]:
            output.append([start, end])
        else:
            output[-1][1] = max(output[-1][1], end)
    return [(start, end) for start, end in output]


def subtract(
    left: list[tuple[Fraction, Fraction]],
    right: list[tuple[Fraction, Fraction]],
) -> list[tuple[Fraction, Fraction]]:
    output = []
    for start, end in merge_intervals(left):
        remaining = [(start, end)]
        for other_start, other_end in merge_intervals(right):
            next_remaining = []
            for current_start, current_end in remaining:
                if other_end <= current_start or other_start >= current_end:
                    next_remaining.append((current_start, current_end))
                    continue
                if current_start < other_start:
                    next_remaining.append((current_start, other_start))
                if other_end < current_end:
                    next_remaining.append((other_end, current_end))
            remaining = next_remaining
        output.extend(remaining)
    return output


def serial_intervals(rows: list[tuple[Fraction, Fraction]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "duration_s": as_float(sum((end - start for start, end in rows), Fraction(0))),
        "intervals": [
            {"start_s": as_float(start), "end_s": as_float(end)} for start, end in rows
        ],
    }


def source_intervals(data: dict[str, Any]) -> dict[str, list[tuple[Fraction, Fraction]]]:
    result: dict[str, list[tuple[Fraction, Fraction]]] = {}
    for row in data["primary"]:
        result.setdefault(row["source"], []).append((row["start"], row["end"]))
    return result


def normalized_order(data: dict[str, Any]) -> list[tuple[str, float, float]]:
    return [
        (row["source"], as_float(row["start"]), as_float(row["end"]))
        for row in data["primary"]
    ]


def title_summary(data: dict[str, Any]) -> dict[str, Any]:
    texts = [row["text"] for row in data["titles"]]
    return {
        "count": len(texts),
        "empty_count": sum(not text.strip() for text in texts),
        "text_sha256": hashlib.sha256("\n".join(texts).encode()).hexdigest(),
        "duplicate_texts": {
            text: count for text, count in Counter(texts).items() if text and count > 1
        },
    }


def logical_titles(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Final Cut 在视频刀口处可把一张 title 裁成多个节点；学习时合并为一张逻辑卡。"""
    output: list[dict[str, Any]] = []
    tolerance = Fraction(1, 1000)
    for row in sorted(data["titles"], key=lambda item: (item["timeline_start"], item["timeline_end"])):
        previous = output[-1] if output else None
        same_style = previous and previous["style"] == row["style"] and previous["effect"] == row["effect"]
        continuous = previous and abs(previous["timeline_end"] - row["timeline_start"]) <= tolerance
        same_text = previous and previous["text"] == row["text"]
        anchor_close = previous and min(
            abs(Fraction(str(a)) - Fraction(str(b)))
            for a in previous["source_anchor_candidates"]
            for b in row["source_anchor_candidates"]
        ) <= Fraction(1, 10)
        if same_style and continuous and same_text and anchor_close:
            previous["timeline_end"] = row["timeline_end"]
            previous["duration"] += row["duration"]
            previous["source_fragments"].append(
                {
                    "source": row["source"],
                    "parent_clip_index": row["parent_clip_index"],
                    "source_anchor_candidates": row["source_anchor_candidates"],
                }
            )
            continue
        output.append({**row, "source_fragments": [{
            "source": row["source"],
            "parent_clip_index": row["parent_clip_index"],
            "source_anchor_candidates": row["source_anchor_candidates"],
        }]})
    return output


def normalized_formats(data: dict[str, Any]) -> list[dict[str, Any]]:
    """资源 ID 是工程局部符号，不应把 r2→r3 当成技术属性变化。"""
    result = []
    for fmt in data["formats"].values():
        result.append(
            {
                key: value
                for key, value in fmt.items()
                if key not in {"id", "name"}
            }
        )
    return sorted(result, key=lambda row: json.dumps(row, sort_keys=True))


def style_counter(data: dict[str, Any]) -> Counter[str]:
    return Counter(
        json.dumps(row["style"], ensure_ascii=False, sort_keys=True)
        for row in data["titles"]
    )


def build_report(ai_path: Path, user_path: Path) -> dict[str, Any]:
    ai = parse(ai_path)
    user = parse(user_path)
    ai_sources = source_intervals(ai)
    user_sources = source_intervals(user)
    source_diff = {}
    for source in sorted(set(ai_sources) | set(user_sources)):
        source_diff[source] = {
            "user_only": serial_intervals(subtract(user_sources.get(source, []), ai_sources.get(source, []))),
            "ai_only": serial_intervals(subtract(ai_sources.get(source, []), user_sources.get(source, []))),
        }
    ai_styles = style_counter(ai)
    user_styles = style_counter(user)
    return {
        "schema": SCHEMA,
        "inputs": {
            "ai": {"path": str(ai_path), "sha256": sha256(ai_path)},
            "user": {"path": str(user_path), "sha256": sha256(user_path)},
        },
        "P0_media_project": {
            "changed": normalized_formats(ai) != normalized_formats(user) or ai["assets"] != user["assets"],
            "ai": {"sequence_format": ai["sequence_format"], "formats": ai["formats"], "assets": ai["assets"]},
            "user": {"sequence_format": user["sequence_format"], "formats": user["formats"], "assets": user["assets"]},
            "note": "资源 id/name 只作工程局部引用，技术属性比较忽略其改名",
        },
        "P1_rough_cut": {
            "changed": normalized_order(ai) != normalized_order(user),
            "ai_clip_count": len(ai["primary"]),
            "user_clip_count": len(user["primary"]),
            "ai_duration_s": as_float(ai["duration"]),
            "user_duration_s": as_float(user["duration"]),
            "source_coverage_diff": source_diff,
            "order_changed": normalized_order(ai) != normalized_order(user),
            "note": "须结合原音判断内容、词头、尾音、呼吸和语气功能；clip name 不作声学真值",
        },
        "P2_caption_text_segmentation": {
            "changed": [row["text"] for row in ai["titles"]] != [row["text"] for row in user["titles"]],
            "ai": title_summary(ai),
            "user": title_summary(user),
            "note": "本项包含所有 title；包装标题与基础字幕须在人工复核时分层",
        },
        "P3_caption_timing": {
            "changed": [
                (row["text"], as_float(row["offset"]), as_float(row["duration"]))
                for row in ai["titles"]
            ] != [
                (row["text"], as_float(row["offset"]), as_float(row["duration"]))
                for row in user["titles"]
            ],
            "ai_title_count": len(ai["titles"]),
            "user_title_count": len(user["titles"]),
            "ai_logical_title_count": len(logical_titles(ai)),
            "user_logical_title_count": len(logical_titles(user)),
            "comparison_rule": (
                "P3 使用同文逻辑卡的原素材起音锺点候选比较；"
                "title.start 为 Final Cut 内部裁切起点，成片绝对时间差归 P1 诊断，不给 P3 重复扣分"
            ),
        },
        "P4_caption_style": {
            "changed": ai_styles != user_styles,
            "ai_unique_style_count": len(ai_styles),
            "user_unique_style_count": len(user_styles),
        },
        "P5_other_packaging": {
            "needs_manual_classification": True,
            "ai_effects": dict(Counter(row["effect"] for row in ai["titles"])),
            "user_effects": dict(Counter(row["effect"] for row in user["titles"])),
            "note": "标题、重点色与字幕节点可能混用，必须结合文字职责和时间线人工分层",
        },
        "P6_delivery_structure": {
            "changed": ai["fcpxml_version"] != user["fcpxml_version"],
            "ai_fcpxml_version": ai["fcpxml_version"],
            "user_fcpxml_version": user["fcpxml_version"],
        },
        "evidence_limits": [
            "只学习 AI→用户实际差异，不把未改处认定为用户偏好",
            "XML 不能单独证明某个区间是词、尾音、呼吸还是静音",
            "同源修订只算一个证据家族",
            "报告不会自动修改 Skill；规则升级还需语义/声学复核与前向测试",
        ],
    }


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ai", type=Path)
    parser.add_argument("user", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        ai = resolve_xml(args.ai)
        user = resolve_xml(args.user)
        if sha256(ai) == sha256(user):
            raise ValueError("AI 版与用户版哈希相同，没有可学习差异")
        report = build_report(ai, user)
        output = args.output.expanduser().resolve()
        atomic_json(output, report)
        changed = [
            stage for stage, payload in report.items()
            if stage.startswith("P") and isinstance(payload, dict) and payload.get("changed")
        ]
        print(f"✅ 已写入分层差异报告: {output}")
        print("   检测到变化: " + (" / ".join(changed) if changed else "需人工分类"))
        print("   报告只读，不会自动修改 Skill 规则")
        return 0
    except (OSError, ET.ParseError, ValueError) as exc:
        parser.exit(1, f"⛔ XML 学习失败: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
