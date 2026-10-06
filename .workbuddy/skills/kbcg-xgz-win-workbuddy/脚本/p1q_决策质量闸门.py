#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
p1q 决策完整性闸门。

它只证明三件事：
1. content_plan 已先完整覆盖素材，再进入逐词删留；
2. keep / segments / structure / rough_segments 结构一致，且真实切口已逐项听审；
3. agent 或人类已留下诚实的分级审阅记录。

它不根据删除量、fix 数、字幕密度或 reason 关键词伪造“质量证据”。
未知 reason 只是自由文本，只要非空就留档，不会被归类成内容层决策。

用法: p1q_决策质量闸门.py <工作目录>
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from decision_contract import (
    ContractError,
    validate_review_record,
    validate_rough_workspace,
)


def _load(path: Path):
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{path.name} 无法读取: {exc}") from exc


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit("用法: p1q_决策质量闸门.py <工作目录>")
    work = Path(sys.argv[1]).resolve()
    try:
        summary = validate_rough_workspace(work)
        if summary["material_type"] not in {
            "single_speaker",
            "single_subject_interview",
        }:
            raise ContractError(
                f"material_type={summary['material_type']}；当前决策链只允许 "
                "single_speaker / single_subject_interview，"
                "multi_speaker/unknown 必须停止"
            )
        keep = _load(work / "keep.json")
        review = validate_review_record(keep, summary["material_type"])
    except ContractError as exc:
        print(f"⛔ p1q 决策完整性失败: {exc}", file=sys.stderr)
        return 1

    counts = summary["decision_counts"]
    print(f"p1q 决策完整性闸门 · {os.path.basename(work)}")
    print(
        f"  ✅ schema: {summary['word_count']} 词 / {summary['segment_count']} 源段 / "
        f"{summary['rough_segment_count']} 粗剪段 / "
        f"{summary['cut_summary']['segment_count']} 段头尾听审 / "
        f"{summary['cut_summary']['cut_count']} 个真实切口连续听审"
    )
    print(
        f"  ✅ 决策留痕: drop {counts['drop']} / fix {counts['fix']} / "
        f"retain {counts['retain']} / split_after {counts['split_after']}"
    )
    expression = summary["expression_summary"]
    print(
        f"  ✅ 表达逐项裁决: 口语候选 {expression['micro']} / "
        f"词轴外残声 {expression['residual']}"
    )
    plan = summary["content_summary"]
    print(
        f"  ✅ 内容地图: {sum(plan['decision_counts'].values())} 个主题块 · "
        f"selected_hook={plan['selected_hook']} · "
        f"主线={' → '.join(plan['story_ids'])}"
    )
    print(
        f"  ✅ 审阅留痕: {review['level']} · {review['reviewed_by']} · "
        f"{review['reviewed_at']} · "
        f"scope={','.join(review['scope'])}"
    )
    if summary["speaker_summary"]:
        speakers = summary["speaker_summary"]
        print(
            "  ✅ 采访分角: "
            f"interviewer {speakers['interviewer_turns']} 轮/{speakers['interviewer_words']} 词已全删 · "
            f"subject {speakers['subject_turns']} 轮/{speakers['subject_words']} 词"
        )
    print(
        "  ℹ 闸门能证明决策顺序、覆盖范围和数据一致性；"
        "不能单靠 schema 证明选材一定好，仍需语义审阅/用户反馈"
    )
    print("  ℹ reason 只做留档，不按关键词归类，不作删除量/fix 配额推断")
    print("✅ 决策完整性闸门通过；下一步可 freeze 生成 decision_lock.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
