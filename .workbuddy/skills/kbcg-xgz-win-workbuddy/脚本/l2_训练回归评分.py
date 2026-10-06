#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取式评估生成 FCPXML 与用户精调 XML。

这是训练回归，不是前向盲测。P3 只比同文逻辑卡的原素材起音锺点；
成片绝对时间差不得把 P1 多留内容重复扣到 P3。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from fractions import Fraction
from pathlib import Path

from media_contract import file_uri_to_path


def sec(raw: str | None) -> float:
    if not raw:
        return 0.0
    return float(Fraction(raw[:-1] if raw.endswith("s") else raw))


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return "".join(ch for ch in text if ch.isalnum() or "\u3400" <= ch <= "\u9fff")


def title_text(node: ET.Element) -> str:
    return "".join((item.text or "") for item in node.findall("./text/text-style"))


def parse_xml(path: Path) -> dict:
    root = ET.parse(path).getroot()
    assets = {}
    for asset in root.findall("./resources/asset"):
        rep = asset.find("media-rep")
        src = rep.get("src") if rep is not None else ""
        assets[asset.get("id")] = {
            "source": file_uri_to_path(src).name,
            "origin": sec(asset.get("start")),
        }
    effect_names = {
        item.get("id"): item.get("name") or ""
        for item in root.findall("./resources/effect")
    }
    primary, titles = [], []
    for clip_index, clip in enumerate(root.findall(".//project/sequence/spine/asset-clip")):
        asset = assets[clip.get("ref")]
        source_start_abs = sec(clip.get("start"))
        source_start = source_start_abs - asset["origin"]
        duration = sec(clip.get("duration"))
        timeline_start = sec(clip.get("offset"))
        primary.append(
            {
                "source": asset["source"], "start": source_start,
                "end": source_start + duration,
                "timeline_start": timeline_start, "timeline_end": timeline_start + duration,
            }
        )
        for title in clip.findall("./title"):
            # lane=2 为顶部标题，不属于基础字幕。
            if title.get("lane") not in (None, "1"):
                continue
            text = title_text(title)
            if not text.strip():
                continue
            visible_abs = sec(title.get("offset"))
            title_start = sec(title.get("start"))
            timeline = timeline_start + visible_abs - source_start_abs
            source_visible = visible_abs - asset["origin"]
            params = {
                (node.get("key") or node.get("name") or ""): node.get("value")
                for node in title.findall("./param")
            }
            position = next(
                (
                    value for key, value in params.items()
                    if key.endswith("/1/100/101") or key in {"Position", "位置"}
                ),
                None,
            )
            styles = [
                {
                    key: node.get(key)
                    for key in ("font", "fontSize", "fontColor", "alignment")
                }
                for node in title.findall("./text-style-def/text-style")
            ]
            titles.append(
                {
                    "text": text,
                    "norm": norm(text),
                    "source": asset["source"],
                    "parent_clip": clip_index,
                    "timeline_start": timeline,
                    "timeline_end": timeline + sec(title.get("duration")),
                    "source_anchors": sorted({source_visible, source_visible - title_start}),
                    "effect": effect_names.get(title.get("ref"), title.get("ref") or ""),
                    "position": position,
                    "styles": styles,
                }
            )
    return {"primary": primary, "titles": logical_titles(titles)}


def logical_titles(rows: list[dict]) -> list[dict]:
    """同文/同样式 title 被 Final Cut 在视频刀口裁开时，合并为一张逻辑卡。"""
    output = []
    for row in sorted(rows, key=lambda item: (item["timeline_start"], item["timeline_end"])):
        previous = output[-1] if output else None
        if previous and previous["norm"] == row["norm"] \
           and previous["styles"] == row["styles"] \
           and previous["effect"] == row["effect"] \
           and abs(previous["timeline_end"] - row["timeline_start"]) <= 0.002 \
           and min(abs(a - b) for a in previous["source_anchors"] for b in row["source_anchors"]) <= 0.101:
            previous["timeline_end"] = row["timeline_end"]
            previous.setdefault("fragments", []).append(row)
        else:
            output.append({**row, "fragments": [row]})
    return output


def merge(rows):
    output = []
    for start, end in sorted(rows):
        if not output or start > output[-1][1] + 1e-6:
            output.append([start, end])
        else:
            output[-1][1] = max(output[-1][1], end)
    return output


def coverage(data):
    grouped = {}
    for row in data["primary"]:
        grouped.setdefault(row["source"], []).append((row["start"], row["end"]))
    return {key: merge(rows) for key, rows in grouped.items()}


def duration(rows):
    return sum(end - start for start, end in rows)


def intersection(left, right):
    i = j = 0
    total = 0.0
    while i < len(left) and j < len(right):
        total += max(0.0, min(left[i][1], right[j][1]) - max(left[i][0], right[j][0]))
        if left[i][1] <= right[j][1]: i += 1
        else: j += 1
    return total


def f1(p, r):
    return 2 * p * r / (p + r) if p + r else 0.0


def coverage_f1(generated, reference):
    g, r = coverage(generated), coverage(reference)
    gt = sum(duration(rows) for rows in g.values())
    rt = sum(duration(rows) for rows in r.values())
    common = sum(intersection(g.get(key, []), r.get(key, [])) for key in set(g) | set(r))
    p, rc = common / gt if gt else 0, common / rt if rt else 0
    return f1(p, rc), {"precision": p, "recall": rc, "generated_s": gt, "reference_s": rt}


def boundary_f1(generated, reference, tolerance=0.20):
    def points(data):
        return [(source, value) for source, rows in coverage(data).items() for pair in rows for value in pair]
    g, r = points(generated), points(reference)
    choices = sorted(
        (abs(gv - rv), gi, ri)
        for gi, (gs, gv) in enumerate(g)
        for ri, (rs, rv) in enumerate(r)
        if gs == rs and abs(gv - rv) <= tolerance
    )
    used_g, used_r = set(), set()
    for _, gi, ri in choices:
        if gi not in used_g and ri not in used_r:
            used_g.add(gi); used_r.add(ri)
    return f1(len(used_g) / len(g) if g else 0, len(used_r) / len(r) if r else 0), {
        "generated": len(g), "reference": len(r), "matches": len(used_g)
    }


def sequence_metrics(generated, reference, timing_tolerance=0.101):
    gt, rt = generated["titles"], reference["titles"]
    gs, rs = [row["norm"] for row in gt], [row["norm"] for row in rt]
    gtext, rtext = "".join(gs), "".join(rs)
    text_score = difflib.SequenceMatcher(None, gtext, rtext, autojunk=False).ratio()

    def breaks(rows):
        result, cursor = [], 0
        for text in rows[:-1]:
            cursor += len(text); result.append(cursor)
        return result
    gb, rb = breaks(gs), breaks(rs)
    sm_chars = difflib.SequenceMatcher(None, gtext, rtext, autojunk=False)
    blocks = [block for block in sm_chars.get_matching_blocks() if block.size]
    mapped = []
    for point in gb:
        value = next(
            (block.b + point - block.a for block in blocks if block.a <= point <= block.a + block.size),
            None,
        )
        mapped.append(value)
    choices = sorted(
        (abs(mapped[gi] - value), gi, ri)
        for gi in range(len(mapped)) if mapped[gi] is not None
        for ri, value in enumerate(rb) if abs(mapped[gi] - value) <= 1
    )
    ug, ur = set(), set()
    for _, gi, ri in choices:
        if gi not in ug and ri not in ur: ug.add(gi); ur.add(ri)
    break_score = f1(len(ug) / len(gb) if gb else 1, len(ur) / len(rb) if rb else 1)

    sm_cards = difflib.SequenceMatcher(None, gs, rs, autojunk=False)
    exact = [
        (block.a + offset, block.b + offset)
        for block in sm_cards.get_matching_blocks()
        for offset in range(block.size)
    ]
    timing_good = []
    for gi, ri in exact:
        if gt[gi]["source"] != rt[ri]["source"]:
            continue
        error = min(abs(a - b) for a in gt[gi]["source_anchors"] for b in rt[ri]["source_anchors"])
        if error <= timing_tolerance:
            timing_good.append(error)
    timing_score = len(timing_good) / len(exact) if exact else 0.0
    return text_score, break_score, timing_score, {
        "generated_cards": len(gs), "reference_cards": len(rs),
        "exact_text_pairs": len(exact), "timing_matches": len(timing_good),
    }


def style_score(generated, reference):
    def signature(data):
        titles = data["titles"]
        styles = [style for title in titles for style in title["styles"]]
        def majority(values):
            values = [value for value in values if value is not None]
            return Counter(values).most_common(1)[0][0] if values else None
        return {
            "font": majority([row.get("font") for row in styles]),
            "size": majority([row.get("fontSize") for row in styles]),
            "align": majority([row.get("alignment") for row in styles]),
            "position": majority([title.get("position") for title in titles]),
        }
    g, r = signature(generated), signature(reference)
    return sum(g[key] == r[key] for key in g) / len(g), {"generated": g, "reference": r}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("generated", type=Path)
    parser.add_argument("reference", type=Path)
    parser.add_argument("--mode", choices=("rough", "full"), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    generated = parse_xml(args.generated.resolve())
    reference = parse_xml(args.reference.resolve())
    cov, cov_detail = coverage_f1(generated, reference)
    boundary, boundary_detail = boundary_f1(generated, reference)
    result = {
        "schema": "kbcg-xgz/in_sample_regression@1",
        "dataset_role": "in_sample_regression",
        "threshold": 90.0,
        "coverage": cov,
        "boundary": boundary,
        "coverage_detail": cov_detail,
        "boundary_detail": boundary_detail,
    }
    if args.mode == "rough":
        score = 100 * (0.70 * cov + 0.30 * boundary)
        result["weights"] = "coverage 70% + source boundary F1@0.20s 30%"
    else:
        text, breaks, timing, timing_detail = sequence_metrics(generated, reference)
        style, style_detail = style_score(generated, reference)
        score = 100 * (
            0.35 * cov + 0.20 * boundary + 0.15 * text
            + 0.10 * breaks + 0.15 * timing + 0.05 * style
        )
        result.update(
            {
                "subtitle_text": text, "subtitle_breaks": breaks,
                "subtitle_source_timing": timing, "style": style,
                "timing_detail": timing_detail, "style_detail": style_detail,
                "weights": "P1 coverage 35/boundary 20 + P2 text 15/breaks 10 + P3 source timing 15 + P4 style 5",
            }
        )
    result["score"] = score
    result["qualified"] = score >= result["threshold"]
    result["claim_limit"] = "已参与规则开发，只证明训练回归，不证明新素材泛化"
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
