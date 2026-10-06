#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 rough_segments.json 生成逐切口听审模板；绝不代替实际听审。

用法：p2b_生成切口清单.py <工作目录>

每个最终播放段都要分别审核词头预卷和韵尾释放余量；只有前段
out_f 与后段 in_f 不连续或显式 split_after 的位置，才另外作为
真实删接/重排切口连续播放。脚本把检查初始化为 false，并拒绝覆盖
已有 cut_review.json。
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from expression_review_contract import pause_compress_boundaries


CUT_REVIEW_SCHEMA = "kbcg-xgz/cut_review@2"
ANALYSIS_FPS = 60
HEAD_ROOM_TARGET_F = 2
TAIL_ROOM_TARGET_F = 11


def key(row: dict[str, Any], pos: int) -> str:
    meta = row.get("_rough_structure")
    if isinstance(meta, dict):
        playback_key = meta.get("playback_key")
        if isinstance(playback_key, str) and playback_key:
            return playback_key
    line, part = row.get("line"), row.get("part")
    if not isinstance(line, str) or not isinstance(part, int) or isinstance(part, bool):
        raise ValueError(f"rough_segments[{pos}] 缺少合法 line/part")
    return f"{line}.{part}"


def atomic_json(path: Path, data: Any) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def acoustic_margins(row: dict[str, Any], pos: int) -> tuple[int, int]:
    starts: list[float] = []
    ends: list[float] = []
    for wi, word in enumerate(row.get("words") or []):
        if not isinstance(word, dict):
            raise ValueError(f"rough_segments[{pos}].words[{wi}] 不是对象")
        for ci, timing in enumerate(word.get("char_times") or []):
            if not isinstance(timing, dict):
                raise ValueError(
                    f"rough_segments[{pos}].words[{wi}].char_times[{ci}] 不是对象"
                )
            start, end = timing.get("s"), timing.get("e")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                raise ValueError(f"rough_segments[{pos}] 字级时码非法")
            starts.append(float(start))
            ends.append(float(end))
    if not starts:
        raise ValueError(f"rough_segments[{pos}] 没有可用字级声学时码")
    in_f, out_f = row.get("in_f"), row.get("out_f")
    if not isinstance(in_f, int) or not isinstance(out_f, int):
        raise ValueError(f"rough_segments[{pos}] 缺合法 in_f/out_f")
    first_f = math.floor(min(starts) * ANALYSIS_FPS + 1e-9)
    last_f = math.ceil(max(ends) * ANALYSIS_FPS - 1e-9)
    return first_f - in_f, out_f - last_f


def edit_purpose(
    left: dict[str, Any], right: dict[str, Any], split_after: set[int],
    pause_after: set[int],
) -> str:
    left_words = left.get("words") or []
    right_words = right.get("words") or []
    last_i = left_words[-1].get("i") if left_words and isinstance(left_words[-1], dict) else None
    first_i = right_words[0].get("i") if right_words and isinstance(right_words[0], dict) else None
    if isinstance(last_i, int) and isinstance(first_i, int) and first_i <= last_i:
        return "reorder"
    if isinstance(last_i, int) and last_i in split_after:
        return "split_after"
    if isinstance(last_i, int) and isinstance(first_i, int) and first_i > last_i + 1:
        return "content_delete"
    if isinstance(last_i, int) and last_i in pause_after:
        return "pause_compression"
    raise ValueError("相邻保留词出现切口，但没有 split_after 或显式停顿压缩裁决")


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("用法: p2b_生成切口清单.py <工作目录>")
    work = Path(sys.argv[1]).expanduser().resolve()
    source = work / "rough_segments.json"
    target = work / "cut_review.json"
    if target.exists():
        sys.exit(f"⛔ {target} 已存在；拒绝覆盖已有听审记录")
    try:
        rough = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"⛔ 无法读取 {source}: {exc}")
    if not isinstance(rough, list) or not rough:
        sys.exit("⛔ rough_segments.json 必须是非空数组")
    keep_path = work / "keep.json"
    try:
        keep = json.loads(keep_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"⛔ 无法读取 {keep_path}: {exc}")
    split_after = set(keep.get("split_after", [])) if isinstance(keep, dict) else set()
    try:
        expression = json.loads((work / "expression_review.json").read_text(encoding="utf-8"))
        pause_after = pause_compress_boundaries(expression)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        sys.exit(f"⟂ 无法读取停顿裁决: {exc}")
    segment_reviews: list[dict[str, Any]] = []
    for pos, row in enumerate(rough):
        if not isinstance(row, dict):
            sys.exit(f"⛔ rough_segments[{pos}] 不是对象")
        head_f, tail_f = acoustic_margins(row, pos)
        segment_reviews.append(
            {
                "key": key(row, pos),
                "head_preroll_frames": head_f,
                "tail_release_frames": tail_f,
                "head_margin_exception": "",
                "tail_margin_exception": "",
                "head_complete": False,
                "tail_complete": False,
                "head_note": "",
                "tail_note": "",
            }
        )

    cuts: list[dict[str, Any]] = []
    for pos, (left, right) in enumerate(zip(rough, rough[1:])):
        if not isinstance(left, dict) or not isinstance(right, dict):
            sys.exit(f"⛔ rough_segments[{pos}] 或下一段不是对象")
        left_out, right_in = left.get("out_f"), right.get("in_f")
        if not isinstance(left_out, int) or not isinstance(right_in, int):
            sys.exit(f"⛔ rough_segments[{pos}] 缺合法 out_f/in_f")
        left_words = left.get("words") or []
        last_i = left_words[-1].get("i") if left_words and isinstance(left_words[-1], dict) else None
        if left_out == right_in and last_i not in split_after:
            continue
        cuts.append(
            {
                "left_key": key(left, pos),
                "right_key": key(right, pos + 1),
                "edit_purpose": edit_purpose(left, right, split_after, pause_after),
                "no_deleted_audio": False,
                "semantic_natural": False,
                "joined_playback_checked": False,
                "joined_note": "",
            }
        )
    payload = {
        "schema": CUT_REVIEW_SCHEMA,
        "segments": segment_reviews,
        "cuts": cuts,
        "review": {
            "status": "pending",
            "reviewer_type": "agent",
            "reviewed_by": "",
            "reviewed_at": "",
        },
        "_instruction": (
            f"逐段播放词头/气口和韵尾/衰减；段头默认目标≥{HEAD_ROOM_TARGET_F}帧，"
            f"段尾默认目标≥{TAIL_ROOM_TARGET_F}帧。低于目标时必须填写安全上界例外；"
            "每个真实切口再连续播放左右两段，确认没有吸住、弹断、吞字或跳气口。"
            "不得批量填 true，也不得用统一补帧代替听审。"
        ),
    }
    atomic_json(target, payload)
    print(f"✅ 已生成 {target}: {len(cuts)} 个真实切口（当前全部未审）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
