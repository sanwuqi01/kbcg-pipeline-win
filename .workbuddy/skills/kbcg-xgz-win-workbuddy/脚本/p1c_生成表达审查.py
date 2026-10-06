#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成或校验口语微成分/词轴外残声逐项审查表。

用法：
  p1c_生成表达审查.py <工作目录>
  p1c_生成表达审查.py <工作目录> --force
  p1c_生成表达审查.py <工作目录> --check
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from expression_review_contract import (
    ExpressionReviewError,
    build_review,
    validate_review,
)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExpressionReviewError(f"无法读取 {path.name}: {exc}") from exc


def atomic_json(path: Path, data) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def merge_decisions(old, new):
    """仅在候选 ID+上下文指纹都一致时继承裁决，防止旧判断粘到新词轴。"""
    if not isinstance(old, dict):
        return new
    decision_fields = {
        "micro_candidates": ("decision", "function", "caption_action", "reason", "acoustic_review"),
        "residual_candidates": ("decision", "recognized_text", "reason", "acoustic_review"),
        "pause_candidates": ("decision", "function", "reason", "acoustic_review"),
    }
    id_fields = {
        "micro_candidates": "candidate_id",
        "residual_candidates": "residual_id",
        "pause_candidates": "candidate_id",
    }
    for group, fields in decision_fields.items():
        id_field = id_fields[group]
        old_by_id = {
            row.get(id_field): row
            for row in old.get(group, [])
            if isinstance(row, dict) and isinstance(row.get(id_field), str)
        }
        for row in new[group]:
            previous = old_by_id.get(row[id_field])
            if not previous:
                continue
            if row.get("context_fingerprint") != previous.get("context_fingerprint"):
                continue
            for field in fields:
                if field in previous:
                    row[field] = previous[field]
    return new


def write_example(path: Path, data) -> None:
    example = {
        "schema": data["schema"],
        "candidate_policy": data["candidate_policy"],
        "micro_candidate_example": {
            **(
                data["micro_candidates"][0]
                if data["micro_candidates"]
                else {
                    "candidate_id": "M000000",
                    "candidate_type": "micro_term",
                    "word_index": 0,
                    "text": "呢",
                    "context_before": "为什么这么做",
                    "context_after": "接下来解释",
                }
            ),
            "decision": "retain",
            "function": "question_rhythm",
            "caption_action": "show",
            "reason": "承担设问收束，删除会让句子和语气不完整",
            "acoustic_review": "heard",
        },
        "residual_candidate_example": {
            **(
                data["residual_candidates"][0]
                if data["residual_candidates"]
                else {
                    "residual_id": "R0000",
                    "block_index": 0,
                    "start": 0.0,
                    "end": 0.18,
                    "duration": 0.18,
                    "next_word_index": 0,
                    "next_word_text": "所以",
                }
            ),
            "decision": "filler_drop",
            "recognized_text": "呃",
            "reason": "无语义、情绪或承接功能，删除后更完整自然",
            "acoustic_review": "heard",
        },
        "pause_candidate_example": {
            **(
                data["pause_candidates"][0]
                if data["pause_candidates"]
                else {
                    "candidate_id": "P000000-000001-example",
                    "left_word_index": 0,
                    "right_word_index": 1,
                    "left_text": "这个",
                    "right_text": "方法",
                    "source_gap_s": 0.72,
                }
            ),
            "decision": "compress",
            "function": "ineffective_wait",
            "reason": "句法连续且停顿无思考、强调、情绪或动作功能",
            "acoustic_review": "heard",
        },
        "note": "示例只说明字段填写方法，不是对当前候选的真实决定；逐项听原音后填写 expression_review.json",
    }
    atomic_json(path, example)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("work", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    work = args.work.expanduser().resolve()
    track_path = work / "word_track.json"
    output = work / "expression_review.json"
    try:
        track = load_json(track_path)
        keep = load_json(work / "keep.json")
        if args.check:
            summary = validate_review(
                load_json(output), track_path=track_path, track=track, keep=keep
            )
            print(
                f"✅ 表达审查通过：口语候选 {summary['micro']} / "
                f"词轴外残声 {summary['residual']}"
            )
            return 0
        if output.exists() and not args.force:
            raise ExpressionReviewError(
                f"{output} 已存在；拒绝覆盖既有人工/Agent 裁决。确认重建时使用 --force"
            )
        keep_path = work / "keep.json"
        data = build_review(track_path, track, keep_path, keep)
        if output.exists() and args.force:
            data = merge_decisions(load_json(output), data)
        atomic_json(output, data)
        write_example(work / "expression_review.填写示例.json", data)
        print(f"✅ 已生成 {output}")
        print(
            f"   口语候选 {len(data['micro_candidates'])} / "
            f"词轴外残声 {len(data['residual_candidates'])} / "
            f"可审停顿 {len(data['pause_candidates'])}"
        )
        print("   词表只用于发现候选；逐项听审、填写决定和理由后运行 --check")
        return 0
    except ExpressionReviewError as exc:
        print(f"⛔ 表达审查失败: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
