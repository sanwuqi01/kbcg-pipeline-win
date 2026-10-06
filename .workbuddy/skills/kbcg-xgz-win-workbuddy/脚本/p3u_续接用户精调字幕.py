#!/usr/bin/env python3
"""锁住用户已精调字幕前缀，只续写未完成区间并生成剪映共用字幕真值。

这个入口用于“用户只精调到某个时间点”的反馈场景。它不会重新解释已经精调的
Basic Title，而是把前缀逐卡回灌；后缀必须由显式语义分卡文件提供。粗剪中的
继续修订也必须写在用户续剪计划里，不能由本脚本暗中猜测。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path


ANALYSIS_FPS = 60


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("--plan", type=Path)
    p.add_argument("--manual-cards", type=Path)
    p.add_argument(
        "--reuse-revised-rough", action="store_true",
        help="只重建字幕；rough_segments 已按同一续剪计划修订时使用",
    )
    return p


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sec(value: str) -> float:
    if not value.endswith("s"):
        raise ValueError(f"非法 FCPXML 时间: {value}")
    return float(Fraction(value[:-1]))


def af(value: float) -> int:
    return round(float(value) * ANALYSIS_FPS)


def norm(value: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:]", "", str(value))


def xml_titles_by_clip(xml_path: Path) -> list[list[dict]]:
    root = ET.parse(xml_path).getroot()
    clips = root.findall(".//project/sequence/spine/asset-clip")
    result: list[list[dict]] = []
    for clip in clips:
        clip_start = sec(clip.attrib["start"])
        rows = []
        for title in clip.findall(".//title"):
            text = "".join((node.text or "") for node in title.findall(".//text-style")).strip()
            if not text:
                continue
            rows.append({
                "text": text,
                "local_s": sec(title.attrib["offset"]) - clip_start,
                "duration_s": sec(title.attrib["duration"]),
            })
        rows.sort(key=lambda row: row["local_s"])
        result.append(rows)
    return result


def filter_words(words: list[dict], part: dict) -> list[dict]:
    minimum = part.get("source_word_i_min")
    maximum = part.get("source_word_i_max")
    selected = []
    for word in words:
        index = int(word["source_word_i"])
        if minimum is not None and index < int(minimum):
            continue
        if maximum is not None and index > int(maximum):
            continue
        selected.append(copy.deepcopy(word))
    if not selected:
        raise ValueError(f"修订范围没有命中任何词: {part}")
    return selected


def rebuild_segment(row: dict, words: list[dict], part: dict, native_fps: float) -> dict:
    out = copy.deepcopy(row)
    out["words"] = words
    out["text"] = "".join(str(word.get("w", "")) for word in words)
    in_s = float(part.get("in_s", out["in"]))
    out_s = float(part.get("out_s", out["out"]))
    if not in_s < out_s:
        raise ValueError(f"修订片段时长非法: {in_s}–{out_s}")
    out["in"] = in_s
    out["out"] = out_s
    out["in_f"] = af(in_s)
    out["out_f"] = af(out_s)
    out["source_native_in_f"] = round(in_s * native_fps)
    out["source_native_out_f"] = round(out_s * native_fps)
    out["raw_in"] = in_s
    out["clip_name"] = out["text"]
    return out


def apply_segment_operations(rough: list[dict], operations: list[dict], native_fps: float) -> list[dict]:
    revised = copy.deepcopy(rough)
    # 逆序执行，较小的原索引不会被后面的 split 改写。
    for operation in sorted(operations, key=lambda row: int(row["si"]), reverse=True):
        si = int(operation["si"])
        if not 0 <= si < len(revised):
            raise ValueError(f"segment operation.si 越界: {si}")
        source = revised[si]
        if operation["type"] == "trim":
            words = filter_words(source["words"], operation)
            revised[si] = rebuild_segment(source, words, operation, native_fps)
        elif operation["type"] == "split_keep":
            parts = []
            for part in operation.get("parts") or []:
                words = filter_words(source["words"], part)
                parts.append(rebuild_segment(source, words, part, native_fps))
            if len(parts) < 2:
                raise ValueError("split_keep 至少需要两段")
            revised[si:si + 1] = parts
        else:
            raise ValueError(f"未知 segment operation.type: {operation['type']}")
    for index, row in enumerate(revised):
        row["line"] = f"H{index + 1:03d}"
        row["part"] = 0
        row["_rough_structure"] = {
            "playback_key": f"H{index + 1:03d}.0",
            "human_approved_prefix": index <= 16,
            "continued_by_agent": index > 16,
        }
    return revised


def display_word(word: dict, fixes: dict[str, str]) -> str:
    return str(fixes.get(str(word["source_word_i"]), word.get("w", "")))


def partition_manual_cards(segment: dict, texts: list[str], fixes: dict[str, str]) -> list[dict]:
    words = segment.get("words") or []
    cursor = 0
    rows = []
    for text in texts:
        wanted = norm(text)
        if not wanted:
            raise ValueError("后缀分卡出现空文本")
        start = cursor
        assembled = ""
        while cursor < len(words) and len(norm(assembled)) < len(wanted):
            assembled += display_word(words[cursor], fixes)
            cursor += 1
        if norm(assembled) != wanted:
            raise ValueError(
                f"字幕卡没有落在完整词边界：{text!r}，命中 {assembled!r}；"
                "不得拆开 ASR 多字词来凑字数"
            )
        rows.append({"text": text, "word_start": start, "word_end": cursor})
    remaining = "".join(display_word(word, fixes) for word in words[cursor:])
    if norm(remaining):
        raise ValueError(f"后缀分卡未覆盖完整保留原声，剩余: {remaining!r}")
    return rows


def make_card(
    segment: dict,
    si: int,
    ci: int,
    text: str,
    local_sf: int,
    local_ef: int,
    tl_in_f: int,
    align: str | None = None,
) -> dict:
    if not 0 <= local_sf < local_ef <= int(segment["out_f"]) - int(segment["in_f"]):
        raise ValueError(f"字幕时码越出片段 {si}.{ci}: {local_sf}–{local_ef}")
    sf = tl_in_f + local_sf
    ef = tl_in_f + local_ef
    return {
        "sf": sf,
        "ef": ef,
        "text": text,
        "align": align if align is not None else text,
        "line": segment.get("line", f"H{si + 1:03d}"),
        "si": si,
        "card_index": ci,
        "playback_key": f"H{si + 1:03d}.0",
        "local_sf": local_sf,
        "local_ef": local_ef,
        "source_sf": int(segment["in_f"]) + local_sf,
        "source_ef": int(segment["in_f"]) + local_ef,
        "start_us": round(sf * 1_000_000 / ANALYSIS_FPS),
        "end_us": round(ef * 1_000_000 / ANALYSIS_FPS),
    }


def prefix_cards(
    segment: dict,
    si: int,
    tl_in_f: int,
    xml_rows: list[dict],
    override: list[str] | None,
    fixes: dict[str, str],
) -> list[dict]:
    duration_f = int(segment["out_f"]) - int(segment["in_f"])
    if override:
        partitions = partition_manual_cards(segment, override, fixes)
        starts = [0]
        for row in partitions[1:]:
            starts.append(max(1, af(float(segment["words"][row["word_start"]]["s"]) - float(segment["in"]))))
        texts = override
    else:
        usable = [row for row in xml_rows if row["local_s"] < duration_f / ANALYSIS_FPS]
        if not usable:
            raise ValueError(f"用户精调前缀的片段 {si} 没有基础字幕")
        texts = [row["text"] for row in usable]
        starts = [max(0, min(duration_f - 1, af(row["local_s"]))) for row in usable]
        starts[0] = 0
    if any(right <= left for left, right in zip(starts, starts[1:])):
        raise ValueError(f"用户前缀字幕起点不递增: si={si} {starts}")
    cards = []
    for ci, text in enumerate(texts):
        end = starts[ci + 1] if ci + 1 < len(starts) else duration_f
        cards.append(make_card(segment, si, ci, text, starts[ci], end, tl_in_f))
    return cards


def suffix_cards(
    segment: dict,
    si: int,
    tl_in_f: int,
    texts: list[str],
    fixes: dict[str, str],
) -> list[dict]:
    duration_f = int(segment["out_f"]) - int(segment["in_f"])
    partitions = partition_manual_cards(segment, texts, fixes)
    starts = [0]
    for row in partitions[1:]:
        word = segment["words"][row["word_start"]]
        starts.append(max(1, min(duration_f - 1, af(float(word["s"]) - float(segment["in"])))))
    if any(right <= left for left, right in zip(starts, starts[1:])):
        raise ValueError(f"后缀字幕起点不递增: si={si} {starts}")
    cards = []
    for ci, (text, part) in enumerate(zip(texts, partitions)):
        end = starts[ci + 1] if ci + 1 < len(starts) else duration_f
        align = "".join(str(word.get("w", "")) for word in segment["words"][part["word_start"]:part["word_end"]])
        cards.append(make_card(segment, si, ci, text, starts[ci], end, tl_in_f, align))
    return cards


def main() -> int:
    args = parser().parse_args()
    work = args.work.expanduser().resolve()
    plan_path = (args.plan or work / "用户续剪计划.json").expanduser().resolve()
    cards_path = (args.manual_cards or work / "后半段语义分卡.json").expanduser().resolve()
    plan = read_json(plan_path)
    if plan.get("schema") != "user-refined-continuation@1":
        raise SystemExit("⛔ 用户续剪计划 schema 无效")
    baseline_path = work / "human_baseline.json"
    baseline = read_json(baseline_path)
    xml_path = Path(baseline["xml_path"]).expanduser().resolve()
    if sha256(xml_path) != baseline["xml_sha256"]:
        raise SystemExit("⛔ 用户精调 XML 已漂移，拒绝续写")
    original = read_json(work / "rough_segments.json")
    native_fps = float(baseline["project_fps"])
    try:
        rough = (
            copy.deepcopy(original)
            if args.reuse_revised_rough
            else apply_segment_operations(original, plan.get("segment_operations") or [], native_fps)
        )
        xml_cards = xml_titles_by_clip(xml_path)
        manual = read_json(cards_path)
        fixes = {str(key): str(value) for key, value in (plan.get("display_fixes") or {}).items()}
        prefix_max = int(plan["refined_prefix_clip_max"])
        overrides = plan.get("prefix_card_overrides") or {}
        normal = []
        segments_tl = []
        cursor = 0
        prefix_texts = []
        for si, segment in enumerate(rough):
            duration = int(segment["out_f"]) - int(segment["in_f"])
            if duration <= 0:
                raise ValueError(f"粗剪片段时长非正: {si}")
            tl = {
                "in_f": int(segment["in_f"]),
                "out_f": int(segment["out_f"]),
                "tl_in_f": cursor,
                "tl_out_f": cursor + duration,
            }
            segments_tl.append(tl)
            if si <= prefix_max:
                cards = prefix_cards(
                    segment, si, cursor,
                    xml_cards[si] if si < len(xml_cards) else [],
                    overrides.get(str(si)), fixes,
                )
                prefix_texts.extend(card["text"] for card in cards)
            else:
                texts = manual.get(str(si))
                if not isinstance(texts, list) or not texts:
                    raise ValueError(f"后缀片段 {si} 缺少显式语义分卡")
                cards = suffix_cards(segment, si, cursor, texts, fixes)
            normal.extend(cards)
            cursor += duration
        refined_end = cursor if prefix_max >= len(rough) - 1 else segments_tl[prefix_max]["tl_out_f"]
        declared_end = af(float(plan["refined_through_timeline_s"]))
        if abs(refined_end - declared_end) > 12:
            raise ValueError(
                f"用户精调前缀终点漂移 {abs(refined_end - declared_end)} 帧，"
                "超过 0.2 秒"
            )
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise SystemExit(f"⛔ 用户精调续写失败：{exc}") from exc

    captions = {
        "fps": ANALYSIS_FPS,
        "total_f": cursor,
        "total_us": round(cursor * 1_000_000 / ANALYSIS_FPS),
        "aspect": "vertical",
        "segments_source": [
            {"si": i, "source_id": row.get("source_id", "source"), "in_f": row["in_f"], "out_f": row["out_f"]}
            for i, row in enumerate(rough)
        ],
        "title": baseline.get("project_name", "用户精调续写"),
        "bridges": [],
        "normal": normal,
        "highlight": [],
        "segments_tl": segments_tl,
    }
    write_json(work / "rough_segments.json", rough)
    write_json(work / "captions_plan.json", captions)
    baseline["analysis_duration_f"] = cursor
    baseline["continued_after_user_refinement"] = {
        "plan_path": str(plan_path),
        "plan_sha256": sha256(plan_path),
        "manual_cards_path": str(cards_path),
        "manual_cards_sha256": sha256(cards_path),
        "refined_prefix_clip_max": prefix_max,
        "refined_through_timeline_s": plan["refined_through_timeline_s"],
    }
    write_json(baseline_path, baseline)
    regression = {
        "schema": "user-refined-prefix-lock@1",
        "human_xml_path": str(xml_path),
        "human_xml_sha256": baseline["xml_sha256"],
        "plan_path": str(plan_path),
        "plan_sha256": sha256(plan_path),
        "manual_cards_path": str(cards_path),
        "manual_cards_sha256": sha256(cards_path),
        "refined_prefix_clip_max": prefix_max,
        "refined_through_timeline_s": plan["refined_through_timeline_s"],
        "prefix_card_count": sum(1 for card in normal if int(card["si"]) <= prefix_max),
        "prefix_text_sha256": hashlib.sha256("\n".join(prefix_texts).encode("utf-8")).hexdigest(),
    }
    write_json(work / "user_refinement_regression.json", regression)
    lock = {
        "schema": "human-rough-lock@2",
        "human_xml_path": str(xml_path),
        "human_xml_sha256": baseline["xml_sha256"],
        "clip_count": len(rough),
        "duration_f": cursor,
        "rough_segments_sha256": sha256(work / "rough_segments.json"),
        "word_track_sha256": sha256(work / "word_track.json"),
        "captions_plan_sha256": sha256(work / "captions_plan.json"),
        "continuation_plan_path": str(plan_path),
        "continuation_plan_sha256": sha256(plan_path),
        "manual_cards_path": str(cards_path),
        "manual_cards_sha256": sha256(cards_path),
        "user_refinement_regression_sha256": sha256(work / "user_refinement_regression.json"),
    }
    write_json(work / "human_rough_lock.json", lock)
    print(
        f"✅ 已锁住用户精调前缀并续写：{len(rough)} 段 · "
        f"{len(normal)} 张字幕 · {cursor / ANALYSIS_FPS:.3f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
