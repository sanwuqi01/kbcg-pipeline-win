#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决策契约：内容地图/删留/结构/字幕统一校验与冻结。

用法：
  decision_contract.py validate-keep <工作目录>
  decision_contract.py validate <工作目录>
  decision_contract.py freeze   <工作目录> [--force]
  decision_contract.py verify   <工作目录>

freeze 只在 p2 已产出 rough_segments.json、真实切口已经逐项听审后运行。
cards 属于冻结后的字幕包装，不进入粗剪锁。finalize 只调 verify，
不得在锁后重建词轴、建段或改写粗剪决策。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import importlib.util
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from expression_review_contract import (
    ExpressionReviewError,
    pause_compress_boundaries,
    validate_review as validate_expression_review,
)
from caption_boundary_contract import CaptionBoundaryError, validate_atomic_boundaries


# @4 把口语微成分/词轴外残声逐项裁决纳入冻结合同；
# @3 把「每个最终段的头尾余量」纳入冻结合同；
# @2 只审真实切口，会放过首尾贴字的中间段。
LOCK_SCHEMA = "kbcg-xgz/decision_lock@4"
PORTFOLIO_SCHEMA = "kbcg-xgz/interview_portfolio@1"
BASE_LOCKED_FILES = (
    "config.json",
    "word_track.json",
    "content_plan.json",
    "keep.json",
    "expression_review.json",
    "segments.json",
    "structure.json",
    "rough_segments.json",
    "cut_review.json",
)
INTERVIEW_TYPE = "single_subject_interview"
KEY_RE = re.compile(r"^L\d{2,}\.\d+(?:@\d+)?$")
ACOUSTIC_CH_RE = re.compile(r"[0-9A-Za-z㐀-䶿一-鿿぀-ヿ]")
MATERIAL_TYPES = {"single_speaker", INTERVIEW_TYPE, "multi_speaker", "unknown"}
CONTENT_DECISIONS = {"selected", "candidate", "exclude"}
CUT_REVIEW_SCHEMA = "kbcg-xgz/cut_review@2"
ANALYSIS_FPS = 60
HEAD_ROOM_TARGET_F = 2
TAIL_ROOM_TARGET_F = 11


class ContractError(ValueError):
    """决策数据不符合契约。"""


def _read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise ContractError(f"缺少 {path.name}: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{path.name} 无法读取: {exc}") from exc


def _atomic_json(path: Path, data: Any) -> None:
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


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
    except OSError as exc:
        raise ContractError(f"无法哈希 {path}: {exc}") from exc
    return h.hexdigest()


def file_entry(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ContractError(f"缺少待冻结文件: {path}")
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "sha256": sha256_file(path),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _verify_internal_evidence(
    skill_root: Path,
    asset: Any,
    label: str,
) -> None:
    """验证必须随 Skill 携带的不可变 XML/文档证据。"""
    if not isinstance(asset, dict):
        raise ContractError(f"{label} 必须是对象")
    relative = asset.get("path")
    expected = asset.get("sha256")
    if not isinstance(relative, str) or not relative:
        raise ContractError(f"{label}.path 不得为空")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise ContractError(f"{label}.sha256 非法")
    path = (skill_root / relative).resolve()
    try:
        path.relative_to(skill_root)
    except ValueError as exc:
        raise ContractError(f"{label}.path 越出 Skill") from exc
    if sha256_file(path).lower() != expected.lower():
        raise ContractError(f"{label}.sha256 与内置证据不一致")


def _sample_pair_registry() -> dict[tuple[str, str], dict[str, str]]:
    skill_root = Path(__file__).resolve().parents[1]
    manifest_path = skill_root / "样本库" / "成对样本清单.json"
    data = _read_json(manifest_path)
    if not isinstance(data, dict) or data.get("schema") != "kbcg-xgz/sample_pairs@2":
        raise ContractError("成对样本清单 schema 非法")
    pairs = data.get("pairs")
    if not isinstance(pairs, list):
        raise ContractError("成对样本清单 pairs 必须是数组")
    registry: dict[tuple[str, str], dict[str, str]] = {}
    for pos, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            raise ContractError(f"成对样本清单 pairs[{pos}] 必须是对象")
        required = (
            "name", "material_type", "evidence_family_id", "dataset_role",
            "ai_path", "ai_sha256",
            "final_path", "final_sha256", "evidence",
        )
        if any(not isinstance(pair.get(field), str) or not pair[field]
               for field in required):
            raise ContractError(f"成对样本清单 pairs[{pos}] 字段不完整")
        key = (pair["name"], pair["material_type"])
        if key in registry:
            raise ContractError(f"成对样本清单重复: {key}")
        if pair["dataset_role"] != "training":
            raise ContractError(f"成对样本 {pair['name']} dataset_role 必须是 training")
        scopes = pair.get("evidence_scopes")
        defects = pair.get("known_defects")
        if (
            not isinstance(scopes, list)
            or not scopes
            or any(not isinstance(scope, str) or not scope.strip() for scope in scopes)
            or len(scopes) != len(set(scopes))
        ):
            raise ContractError(f"成对样本 {pair['name']} evidence_scopes 必须是非空不重复字符串数组")
        if not isinstance(defects, list) or any(
            not isinstance(defect, str) or not defect.strip() for defect in defects
        ):
            raise ContractError(f"成对样本 {pair['name']} known_defects 必须是字符串数组")
        for path_field, hash_field in (
            ("ai_path", "ai_sha256"), ("final_path", "final_sha256")
        ):
            path = (skill_root / pair[path_field]).resolve()
            try:
                path.relative_to(skill_root)
            except ValueError as exc:
                raise ContractError(
                    f"成对样本路径越出 Skill: {pair[path_field]}"
                ) from exc
            if sha256_file(path).lower() != pair[hash_field].lower():
                raise ContractError(
                    f"成对样本哈希失效: {pair['name']} / {path_field}"
                )
        revisions = pair.get("revision_history", [])
        if not isinstance(revisions, list):
            raise ContractError(f"成对样本 {pair['name']} revision_history 必须是数组")
        seen_revisions: set[str] = set()
        for revision_pos, revision in enumerate(revisions):
            revision_label = f"成对样本 {pair['name']} revision_history[{revision_pos}]"
            required_revision = {
                "revision", "ai_path", "ai_sha256", "final_path", "final_sha256"
            }
            if not isinstance(revision, dict) or set(revision) != required_revision:
                raise ContractError(f"{revision_label} 字段非法")
            if (
                not isinstance(revision["revision"], str)
                or not revision["revision"].strip()
                or revision["revision"] in seen_revisions
            ):
                raise ContractError(f"{revision_label}.revision 为空或重复")
            seen_revisions.add(revision["revision"])
            if revision["ai_sha256"] == revision["final_sha256"]:
                raise ContractError(f"{revision_label} AI 与用户版哈希不得相同")
            for path_field, hash_field in (
                ("ai_path", "ai_sha256"), ("final_path", "final_sha256")
            ):
                revision_path = (skill_root / revision[path_field]).resolve()
                try:
                    revision_path.relative_to(skill_root)
                except ValueError as exc:
                    raise ContractError(f"{revision_label}.{path_field} 越出 Skill") from exc
                if sha256_file(revision_path).lower() != revision[hash_field].lower():
                    raise ContractError(f"{revision_label}.{hash_field} 与文件不一致")
        stage_chain = pair.get("stage_chain")
        if stage_chain is not None:
            expected_stages = [
                ("P1", "rough_cut_baseline"),
                ("F1", "rough_cut_user_refined"),
                ("P2-P4", "subtitle_alignment_style"),
                ("P5", "user_refined_packaging"),
            ]
            if not isinstance(stage_chain, list) or len(stage_chain) != len(expected_stages):
                raise ContractError(
                    f"成对样本 {pair['name']} stage_chain 必须是 P1→F1→P2-P4→P5 四阶段"
                )
            for stage_pos, (stage, expected) in enumerate(zip(stage_chain, expected_stages)):
                stage_label = f"成对样本 {pair['name']} stage_chain[{stage_pos}]"
                required_stage = {"stage", "path", "sha256", "scope"}
                if not isinstance(stage, dict) or set(stage) != required_stage:
                    raise ContractError(f"{stage_label} 字段非法")
                expected_name, expected_scope = expected
                if stage["stage"] != expected_name or stage["scope"] != expected_scope:
                    raise ContractError(f"{stage_label} 顺序或 scope 非法")
                _verify_internal_evidence(skill_root, stage, stage_label)
            if (
                stage_chain[0]["path"] != pair["ai_path"]
                or stage_chain[0]["sha256"].lower() != pair["ai_sha256"].lower()
                or stage_chain[1]["path"] != pair["final_path"]
                or stage_chain[1]["sha256"].lower() != pair["final_sha256"].lower()
            ):
                raise ContractError(
                    f"成对样本 {pair['name']} 的 P1/F1 必须与 AI 初稿/用户精调粗剪一致"
                )
        registry[key] = {
            **{field: pair[field] for field in required},
            "evidence_scopes": list(scopes),
            "known_defects": list(defects),
        }

    development_cases = data.get("development_cases", [])
    if not isinstance(development_cases, list):
        raise ContractError("成对样本清单 development_cases 必须是数组")
    for pos, case in enumerate(development_cases):
        if not isinstance(case, dict):
            raise ContractError(f"development_cases[{pos}] 必须是对象")
        required_case = (
            "name",
            "material_type",
            "evidence_family_id",
            "dataset_role",
            "holdout_eligible",
            "ai_path",
            "ai_sha256",
            "final_path",
            "final_sha256",
            "evidence",
            "usage",
        )
        if any(
            (not isinstance(case.get(field), str) or not case[field])
            if field != "holdout_eligible"
            else case.get(field) is not False
            for field in required_case
        ):
            raise ContractError(f"development_cases[{pos}] 字段不完整")
        if case["evidence"] != "direction_correction" or case["usage"] != "manual_reference_only":
            raise ContractError(
                f"development_cases[{pos}] 只能作 direction_correction / manual_reference_only"
            )
        if case["dataset_role"] != "development":
            raise ContractError(f"development_cases[{pos}] dataset_role 必须是 development")
        for path_field, hash_field in (
            ("ai_path", "ai_sha256"),
            ("final_path", "final_sha256"),
        ):
            path = (skill_root / case[path_field]).resolve()
            try:
                path.relative_to(skill_root)
            except ValueError as exc:
                raise ContractError(
                    f"开发案例路径越出 Skill: {case[path_field]}"
                ) from exc
            if sha256_file(path).lower() != case[hash_field].lower():
                raise ContractError(
                    f"开发案例哈希失效: {case['name']} / {path_field}"
                )

    portfolio_cases = data.get("portfolio_cases", [])
    if not isinstance(portfolio_cases, list):
        raise ContractError("成对样本清单 portfolio_cases 必须是数组")
    for pos, case in enumerate(portfolio_cases):
        label = f"portfolio_cases[{pos}]"
        if not isinstance(case, dict):
            raise ContractError(f"{label} 必须是对象")
        for field in (
            "name", "material_type", "evidence_family_id", "dataset_role",
            "relation", "evidence", "usage", "analysis_path", "analysis_sha256",
        ):
            if not isinstance(case.get(field), str) or not case[field]:
                raise ContractError(f"{label}.{field} 不得为空")
        if (
            case["dataset_role"] != "development"
            or case.get("holdout_eligible") is not False
            or case["relation"] != "one_to_many"
            or case["evidence"] != "portfolio_decomposition"
            or case["usage"] != "manual_reference_only"
        ):
            raise ContractError(f"{label} 的开发证据身份非法")
        analysis_path = (skill_root / case["analysis_path"]).resolve()
        try:
            analysis_path.relative_to(skill_root)
        except ValueError as exc:
            raise ContractError(f"{label}.analysis_path 越出 Skill") from exc
        if sha256_file(analysis_path).lower() != case["analysis_sha256"].lower():
            raise ContractError(f"{label}.analysis_sha256 与案例文件不一致")
        source = case.get("source")
        if not isinstance(source, dict):
            raise ContractError(f"{label}.source 必须是对象")
        if not isinstance(source.get("external_path"), str) or not source["external_path"]:
            raise ContractError(f"{label}.source.external_path 不得为空")
        if not isinstance(source.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", source["sha256"]
        ):
            raise ContractError(f"{label}.source.sha256 非法")
        _verify_internal_evidence(
            skill_root,
            case.get("common_baseline"),
            f"{label}.common_baseline",
        )
        outputs = case.get("outputs")
        if not isinstance(outputs, list) or len(outputs) < 2:
            raise ContractError(f"{label}.outputs 至少两条")
        seen_output_ids: set[str] = set()
        for output_pos, output in enumerate(outputs):
            output_label = f"{label}.outputs[{output_pos}]"
            if not isinstance(output, dict):
                raise ContractError(f"{output_label} 必须是对象")
            output_id = output.get("output_id")
            if not isinstance(output_id, str) or not output_id or output_id in seen_output_ids:
                raise ContractError(f"{output_label}.output_id 不得为空或重复")
            seen_output_ids.add(output_id)
            _verify_internal_evidence(skill_root, output, output_label)
    return registry


def _is_index(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def validate_word_track(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or not isinstance(data.get("words"), list):
        raise ContractError("word_track.json 必须是带 words 数组的对象")
    words = data["words"]
    if not words:
        raise ContractError("word_track.json.words 不得为空")
    previous = -1.0
    for i, word in enumerate(words):
        if not isinstance(word, dict):
            raise ContractError(f"word_track.words[{i}] 必须是对象")
        if not isinstance(word.get("t"), str):
            raise ContractError(f"word_track.words[{i}].t 必须是字符串")
        for field in ("ws", "we"):
            value = word.get(field)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ContractError(f"word_track.words[{i}].{field} 必须是数值")
            if value < 0:
                raise ContractError(f"word_track.words[{i}].{field} 不得为负")
        if word["we"] < word["ws"]:
            raise ContractError(f"word_track.words[{i}] we < ws")
        if word["ws"] + 1e-6 < previous:
            raise ContractError(f"word_track.words[{i}] 时间非单调")
        previous = word["ws"]
    return words


def validate_config(data: Any) -> str:
    if not isinstance(data, dict):
        raise ContractError("config.json 必须是对象")
    material_type = data.get("material_type")
    if material_type not in MATERIAL_TYPES:
        raise ContractError(
            "config.material_type 必须是 single_speaker / single_subject_interview / "
            "multi_speaker / unknown 之一，"
            "不得缺省后偷偷按口播处理"
        )
    for field in ("original_path", "proxy"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ContractError(f"config.{field} 必须是非空路径")
    expression = data.get("core_expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ContractError("config.core_expression 必须在粗剪冻结前写入中文核心表达")
    expression = expression.strip()
    if len(re.findall(r"[\u3400-\u9fff]", expression)) < 2:
        raise ContractError("config.core_expression 必须至少包含两个汉字")
    if len(expression) > 36 or re.search(r"[\\/:*?\"<>|\r\n\t]", expression):
        raise ContractError("config.core_expression 过长或含文件名非法字符")
    return material_type


def locked_files_for_type(material_type: str) -> tuple[str, ...]:
    """单主体采访额外冻结说话人决策；单人口播保持旧锁兼容。"""
    if material_type == INTERVIEW_TYPE:
        return BASE_LOCKED_FILES[:3] + ("speaker_turns.json",) + BASE_LOCKED_FILES[3:]
    return BASE_LOCKED_FILES


def locked_files_for_workspace(work: Path, material_type: str) -> tuple[str, ...]:
    """多条采访把共同组合计划一并冻结；单条兼容既有锁。"""
    files = locked_files_for_type(material_type)
    if material_type != INTERVIEW_TYPE:
        return files
    plan = _read_json(work / "content_plan.json")
    request = plan.get("output_request") if isinstance(plan, dict) else None
    if isinstance(request, dict) and request.get("mode") == "multiple":
        return files[:4] + ("portfolio_plan.json",) + files[4:]
    return files


# 保留旧常量给现有测试/调用方；新代码应使用 locked_files_for_type。
LOCKED_FILES = BASE_LOCKED_FILES


def _validate_interval_item(kind: str, item: Any, pos: int, word_count: int) -> tuple[int, int]:
    required = {"drop": 3, "fix": 4, "retain": 4}[kind]
    if not isinstance(item, list) or len(item) != required:
        formats = {
            "drop": "[起词, 止词, 理由]",
            "fix": "[起词, 止词, 新文本, 理由]",
            "retain": "[起词, 止词, 类别, 理由]",
        }
        raise ContractError(f"keep.{kind}[{pos}] 必须是 {formats[kind]}")
    start, end = item[0], item[1]
    if not _is_index(start) or not _is_index(end):
        raise ContractError(f"keep.{kind}[{pos}] 起止下标必须是整数")
    if start < 0 or end < 0:
        raise ContractError(f"keep.{kind}[{pos}] 不得使用负下标")
    if start > end:
        raise ContractError(f"keep.{kind}[{pos}] 起始下标大于结束下标")
    if end >= word_count:
        raise ContractError(
            f"keep.{kind}[{pos}] 越界 [{start}, {end}]，词轴只有 {word_count} 词"
        )
    if kind == "drop":
        if not isinstance(item[2], str) or not item[2].strip():
            raise ContractError(f"keep.drop[{pos}] 理由不得为空")
    elif kind == "fix":
        if not isinstance(item[2], str) or not item[2].strip():
            raise ContractError(f"keep.fix[{pos}] 新文本不得为空")
        if not isinstance(item[3], str) or not item[3].strip():
            raise ContractError(f"keep.fix[{pos}] 理由不得为空")
    else:
        if not isinstance(item[2], str) or not item[2].strip():
            raise ContractError(f"keep.retain[{pos}] 类别不得为空")
        if not isinstance(item[3], str) or not item[3].strip():
            raise ContractError(f"keep.retain[{pos}] 理由不得为空")
    return start, end


def validate_keep(data: Any, word_count: int) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ContractError("keep.json 必须是对象")
    spans: dict[str, list[tuple[int, int]]] = {}
    all_spans: list[tuple[int, int, str, int]] = []
    for kind in ("drop", "fix", "retain"):
        rows = data.get(kind, [])
        if not isinstance(rows, list):
            raise ContractError(f"keep.{kind} 必须是数组")
        spans[kind] = []
        for pos, item in enumerate(rows):
            start, end = _validate_interval_item(kind, item, pos, word_count)
            spans[kind].append((start, end))
            all_spans.append((start, end, kind, pos))

    # 三种决策互斥，同类内也不得重叠。相邻区间允许。
    ordered = sorted(all_spans, key=lambda row: (row[0], row[1], row[2], row[3]))
    for left, right in zip(ordered, ordered[1:]):
        if right[0] <= left[1]:
            raise ContractError(
                "keep 决策区间重叠/冲突: "
                f"{left[2]}[{left[3]}]=[{left[0]},{left[1]}] 与 "
                f"{right[2]}[{right[3]}]=[{right[0]},{right[1]}]"
            )

    review = data.get("review")
    if review is not None and not isinstance(review, dict):
        raise ContractError("keep.review 必须是对象")
    split_after = data.get("split_after", [])
    if not isinstance(split_after, list):
        raise ContractError("keep.split_after 必须是词下标数组")
    if any(not _is_index(index) for index in split_after):
        raise ContractError("keep.split_after 只能包含整数词下标")
    if split_after != sorted(set(split_after)):
        raise ContractError("keep.split_after 必须严格递增且不得重复")
    if any(index < 0 or index >= word_count - 1 for index in split_after):
        raise ContractError(
            f"keep.split_after 只能位于 0..{max(word_count - 2, 0)} 之间"
        )
    drop_mask = [False] * word_count
    for start, end in spans["drop"]:
        for index in range(start, end + 1):
            drop_mask[index] = True
    for index in split_after:
        if drop_mask[index] or drop_mask[index + 1]:
            raise ContractError(
                f"keep.split_after={index} 两侧必须都是保留词；"
                "删除区本身已形成强制边界"
            )
    spans["split_after"] = split_after

    boundary_exceptions = data.get("boundary_exceptions", [])
    if not isinstance(boundary_exceptions, list):
        raise ContractError("keep.boundary_exceptions 必须是数组")
    exception_words: set[int] = set()
    required_exception_fields = {
        "source", "xml_path", "xml_sha256", "reviewed_by", "reviewed_at",
        "drop_word_indices", "reason",
    }
    for pos, item in enumerate(boundary_exceptions):
        label = f"keep.boundary_exceptions[{pos}]"
        if not isinstance(item, dict) or set(item) != required_exception_fields:
            raise ContractError(
                f"{label} 必须且只能包含 " + ", ".join(sorted(required_exception_fields))
            )
        if item["source"] != "human_approved_fcpxml":
            raise ContractError(f"{label}.source 必须是 human_approved_fcpxml")
        for field in ("xml_path", "reviewed_by", "reviewed_at", "reason"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise ContractError(f"{label}.{field} 不得为空")
        if not isinstance(item["xml_sha256"], str) or not re.fullmatch(
            r"[0-9a-fA-F]{64}", item["xml_sha256"]
        ):
            raise ContractError(f"{label}.xml_sha256 必须是 64 位 SHA-256")
        xml_path = Path(item["xml_path"]).expanduser().resolve()
        if sha256_file(xml_path).lower() != item["xml_sha256"].lower():
            raise ContractError(f"{label}.xml_sha256 与人工批准 XML 不一致")
        try:
            parsed = _dt.datetime.fromisoformat(item["reviewed_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractError(f"{label}.reviewed_at 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ContractError(f"{label}.reviewed_at 必须带时区")
        indices = item["drop_word_indices"]
        if (
            not isinstance(indices, list)
            or any(not _is_index(index) for index in indices)
            or indices != sorted(set(indices))
        ):
            raise ContractError(f"{label}.drop_word_indices 必须是严格递增的不重复词下标")
        for index in indices:
            if index < 0 or index >= word_count or not drop_mask[index]:
                raise ContractError(
                    f"{label}.drop_word_indices[{index}] 必须引用 keep.drop 中的词"
                )
            if index in exception_words:
                raise ContractError(f"keep.boundary_exceptions 重复豁免 drop 词 {index}")
            exception_words.add(index)
    spans["boundary_exceptions"] = sorted(exception_words)
    return spans


def _validate_basic_review(review: Any, label: str) -> dict[str, Any]:
    if not isinstance(review, dict):
        raise ContractError(
            f"{label}.review 缺失；需留下 reviewer_type/status/reviewed_by/reviewed_at"
        )
    reviewer_type = review.get("reviewer_type")
    status = review.get("status")
    if reviewer_type not in {"agent", "human"}:
        raise ContractError(f"{label}.review.reviewer_type 必须是 agent 或 human")
    if status not in {"reviewed", "approved"}:
        raise ContractError(f"{label}.review.status 必须是 reviewed 或 approved")
    if reviewer_type == "agent" and status != "reviewed":
        raise ContractError(f"{label}: agent 只能记为 reviewed，不得伪造 approved")
    if status == "approved" and reviewer_type != "human":
        raise ContractError(f"{label}: approved 只能由真实 human 审阅后写入")
    if not isinstance(review.get("reviewed_by"), str) or not review["reviewed_by"].strip():
        raise ContractError(f"{label}.review.reviewed_by 不得为空")
    stamp = review.get("reviewed_at")
    if not isinstance(stamp, str) or not stamp.strip():
        raise ContractError(f"{label}.review.reviewed_at 不得为空")
    try:
        parsed = _dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{label}.review.reviewed_at 必须是 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{label}.review.reviewed_at 必须带时区")
    level = (
        "human_approved"
        if reviewer_type == "human" and status == "approved"
        else "human_reviewed"
        if reviewer_type == "human"
        else "agent_reviewed"
    )
    return {
        "level": level,
        "reviewer_type": reviewer_type,
        "status": status,
        "reviewed_by": review["reviewed_by"],
        "reviewed_at": stamp,
    }


def validate_content_plan(
    data: Any,
    word_count: int,
    drop_spans: list[tuple[int, int]] | None = None,
    material_type: str = "single_speaker",
) -> dict[str, Any]:
    """校验逐词删留之前的全素材内容地图与开头候选。"""
    if not isinstance(data, dict):
        raise ContractError("content_plan.json 必须是对象")
    context: dict[str, str] = {}
    for field in ("goal", "audience", "main_claim"):
        value = data.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ContractError(f"content_plan.{field} 必须是非空字符串")
        context[field] = value.strip()

    blocks = data.get("topic_blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ContractError("content_plan.topic_blocks 必须是非空数组")
    expected_start = 0
    block_ids: set[str] = set()
    blocks_by_id: dict[str, dict[str, Any]] = {}
    decisions_by_word: list[str] = []
    decision_counts = {decision: 0 for decision in sorted(CONTENT_DECISIONS)}
    for pos, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ContractError(f"content_plan.topic_blocks[{pos}] 必须是对象")
        block_id = block.get("block_id")
        if not isinstance(block_id, str) or not block_id.strip():
            raise ContractError(f"content_plan.topic_blocks[{pos}].block_id 不得为空")
        if block_id in block_ids:
            raise ContractError(f"content_plan.topic_blocks block_id 重复: {block_id}")
        block_ids.add(block_id)
        start, end = block.get("start_word"), block.get("end_word")
        if not _is_index(start) or not _is_index(end):
            raise ContractError(
                f"content_plan.topic_blocks[{pos}] start_word/end_word 必须是整数"
            )
        if start != expected_start:
            raise ContractError(
                "content_plan.topic_blocks 必须从 0 开始无缝覆盖全词轴；"
                f"block[{pos}].start_word={start}，期望 {expected_start}"
            )
        if end < start or end >= word_count:
            raise ContractError(
                f"content_plan.topic_blocks[{pos}] 区间非法 [{start},{end}]"
            )
        decision = block.get("decision")
        if decision not in CONTENT_DECISIONS:
            raise ContractError(
                f"content_plan.topic_blocks[{pos}].decision 必须是 "
                "selected / candidate / exclude"
            )
        for field in ("role", "reason"):
            if not isinstance(block.get(field), str) or not block[field].strip():
                raise ContractError(
                    f"content_plan.topic_blocks[{pos}].{field} 不得为空"
                )
        decisions_by_word.extend([decision] * (end - start + 1))
        decision_counts[decision] += 1
        blocks_by_id[block_id] = {
            "start": start,
            "end": end,
            "decision": decision,
        }
        expected_start = end + 1
    if expected_start != word_count:
        raise ContractError(
            f"content_plan.topic_blocks 未覆盖到词轴末尾："
            f"已到 {expected_start - 1}，应到 {word_count - 1}"
        )
    if not decision_counts["selected"]:
        raise ContractError("content_plan 至少要有一个 selected 内容块")

    selected_story = data.get("selected_story")
    if not isinstance(selected_story, list) or not selected_story:
        raise ContractError("content_plan.selected_story 必须是非空主线数组")
    story_ids: list[str] = []
    for pos, item in enumerate(selected_story):
        if not isinstance(item, dict):
            raise ContractError(f"content_plan.selected_story[{pos}] 必须是对象")
        block_id = item.get("block_id")
        if block_id not in blocks_by_id:
            raise ContractError(
                f"content_plan.selected_story[{pos}] 引用不存在的 block_id"
            )
        if blocks_by_id[block_id]["decision"] == "exclude":
            raise ContractError(
                f"content_plan.selected_story[{pos}] 不得引用 exclude 块 {block_id}"
            )
        if block_id in story_ids:
            raise ContractError(f"content_plan.selected_story 重复引用 {block_id}")
        for field in ("role", "reason"):
            if not isinstance(item.get(field), str) or not item[field].strip():
                raise ContractError(
                    f"content_plan.selected_story[{pos}].{field} 不得为空"
                )
        story_ids.append(block_id)
    selected_ids = {
        block_id
        for block_id, block in blocks_by_id.items()
        if block["decision"] == "selected"
    }
    if set(story_ids) != selected_ids:
        raise ContractError(
            "content_plan.selected_story 必须恰好覆盖所有 selected 内容块；"
            f"应为 {sorted(selected_ids)}，实际 {sorted(story_ids)}"
        )

    interview_output: dict[str, Any] | None = None
    if material_type == INTERVIEW_TYPE:
        request = data.get("output_request")
        if not isinstance(request, dict):
            raise ContractError(
                "采访 content_plan.output_request 必须明确用户要一条、多条或完整成片"
            )
        mode = request.get("mode")
        if mode not in {"single", "multiple", "complete"}:
            raise ContractError(
                "content_plan.output_request.mode 必须是 single / multiple / complete"
            )
        requested_count = request.get("requested_count")
        output_index = request.get("output_index")
        if not _is_index(requested_count) or requested_count < 1:
            raise ContractError("content_plan.output_request.requested_count 必须是正整数")
        if not _is_index(output_index) or not 1 <= output_index <= requested_count:
            raise ContractError(
                "content_plan.output_request.output_index 必须在 1..requested_count 之间"
            )
        if mode in {"single", "complete"} and requested_count != 1:
            raise ContractError(
                "single / complete 模式的 requested_count 必须为 1"
            )
        if mode == "multiple" and requested_count < 2:
            raise ContractError("multiple 模式的 requested_count 至少为 2")
        for field in ("focus", "source"):
            if not isinstance(request.get(field), str) or not request[field].strip():
                raise ContractError(f"content_plan.output_request.{field} 不得为空")
        if request["source"] not in {"user", "agent_proposed"}:
            raise ContractError(
                "content_plan.output_request.source 必须是 user 或 agent_proposed"
            )
        if mode == "multiple" and request["source"] != "user":
            raise ContractError(
                "multiple 模式必须由用户授权；Agent 发现候选不能擅自扩大输出条数"
            )

        candidates_raw = data.get("story_candidates")
        if not isinstance(candidates_raw, list) or not candidates_raw:
            raise ContractError(
                "采访 content_plan.story_candidates 必须列出长素材中可独立成片的主题候选"
            )
        seen_candidate_ids: set[str] = set()
        selected_candidates: list[dict[str, Any]] = []
        covered_story_blocks: set[str] = set()
        normalized_candidates: list[dict[str, Any]] = []
        candidate_uses_by_block: dict[str, list[str]] = {}
        for pos, candidate in enumerate(candidates_raw):
            label = f"content_plan.story_candidates[{pos}]"
            if not isinstance(candidate, dict):
                raise ContractError(f"{label} 必须是对象")
            candidate_id = candidate.get("candidate_id")
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                raise ContractError(f"{label}.candidate_id 不得为空")
            if candidate_id in seen_candidate_ids:
                raise ContractError(f"content_plan.story_candidates 重复 {candidate_id}")
            seen_candidate_ids.add(candidate_id)
            for field in ("core_claim", "evidence_reason", "independence_reason"):
                if not isinstance(candidate.get(field), str) or not candidate[field].strip():
                    raise ContractError(f"{label}.{field} 不得为空")
            candidate_block_ids = candidate.get("block_ids")
            if not isinstance(candidate_block_ids, list) or not candidate_block_ids:
                raise ContractError(f"{label}.block_ids 必须是非空数组")
            if (
                any(not isinstance(block_id, str) for block_id in candidate_block_ids)
                or len(candidate_block_ids) != len(set(candidate_block_ids))
            ):
                raise ContractError(f"{label}.block_ids 只能含不重复的字符串")
            unknown = [block_id for block_id in candidate_block_ids if block_id not in blocks_by_id]
            if unknown:
                raise ContractError(f"{label}.block_ids 引用不存在的块: {unknown}")
            excluded = [
                block_id
                for block_id in candidate_block_ids
                if blocks_by_id[block_id]["decision"] == "exclude"
            ]
            if excluded:
                raise ContractError(f"{label} 不得引用 exclude 块: {excluded}")
            selection = candidate.get("selection")
            if selection not in {"selected", "not_selected"}:
                raise ContractError(f"{label}.selection 必须是 selected / not_selected")
            normalized = {
                "candidate_id": candidate_id,
                "block_ids": list(candidate_block_ids),
                "selection": selection,
                "core_claim": candidate["core_claim"],
            }
            normalized_candidates.append(normalized)
            covered_story_blocks.update(candidate_block_ids)
            for block_id in candidate_block_ids:
                candidate_uses_by_block.setdefault(block_id, []).append(candidate_id)
            if selection == "selected":
                selected_candidates.append(normalized)

        overlapping_candidates = {
            block_id: candidate_ids
            for block_id, candidate_ids in candidate_uses_by_block.items()
            if len(candidate_ids) > 1
        }
        overlap_reviews_raw = data.get("candidate_overlap_reviews", [])
        if not isinstance(overlap_reviews_raw, list):
            raise ContractError("content_plan.candidate_overlap_reviews 必须是数组")
        overlap_reviews: dict[str, dict[str, Any]] = {}
        for pos, item in enumerate(overlap_reviews_raw):
            label = f"content_plan.candidate_overlap_reviews[{pos}]"
            if not isinstance(item, dict):
                raise ContractError(f"{label} 必须是对象")
            block_id = item.get("block_id")
            if not isinstance(block_id, str) or block_id not in blocks_by_id:
                raise ContractError(f"{label}.block_id 必须引用存在的内容块")
            if block_id in overlap_reviews:
                raise ContractError(f"candidate_overlap_reviews 重复 {block_id}")
            candidate_ids = item.get("candidate_ids")
            expected_ids = overlapping_candidates.get(block_id)
            if (
                not isinstance(candidate_ids, list)
                or any(not isinstance(value, str) for value in candidate_ids)
                or len(candidate_ids) != len(set(candidate_ids))
                or expected_ids is None
                or set(candidate_ids) != set(expected_ids)
            ):
                raise ContractError(
                    f"{label}.candidate_ids 必须精确列出共享 {block_id} 的全部候选"
                )
            functions = item.get("distinct_functions")
            if (
                not isinstance(functions, dict)
                or set(functions) != set(candidate_ids)
                or any(not isinstance(value, str) or not value.strip()
                       for value in functions.values())
            ):
                raise ContractError(
                    f"{label}.distinct_functions 必须逐候选写明不同功能"
                )
            for field in ("why_unavoidable", "conflict_check"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    raise ContractError(f"{label}.{field} 不得为空")
            overlap_reviews[block_id] = item
        if set(overlap_reviews) != set(overlapping_candidates):
            missing = sorted(set(overlapping_candidates) - set(overlap_reviews))
            extra = sorted(set(overlap_reviews) - set(overlapping_candidates))
            raise ContractError(
                "story_candidates 共享内容块必须逐块登记 candidate_overlap_reviews；"
                f"缺少 {missing}，多余 {extra}"
            )
        if len(selected_candidates) != 1:
            raise ContractError(
                "当前工作目录只能有一个 selected story_candidate；"
                "多条成片请每条建独立工作目录"
            )
        selected_candidate_blocks = set(selected_candidates[0]["block_ids"])
        if selected_candidate_blocks != selected_ids:
            raise ContractError(
                "selected story_candidate.block_ids 必须与 topic_blocks 中的 selected 块全等"
            )
        viable_blocks = {
            block_id
            for block_id, block in blocks_by_id.items()
            if block["decision"] != "exclude"
        }
        missing_candidates = sorted(viable_blocks - covered_story_blocks)
        if missing_candidates:
            raise ContractError(
                "story_candidates 未覆盖可成片内容块: " + ", ".join(missing_candidates)
            )
        interview_output = {
            "output_request": {
                "mode": mode,
                "requested_count": requested_count,
                "output_index": output_index,
                "focus": request["focus"].strip(),
                "source": request["source"],
            },
            "story_candidates": normalized_candidates,
            "selected_candidate_id": selected_candidates[0]["candidate_id"],
            "candidate_overlap_count": len(overlap_reviews),
        }

        bridges_raw = data.get("question_context_bridges", [])
        if not isinstance(bridges_raw, list):
            raise ContractError("content_plan.question_context_bridges 必须是数组")
        bridge_ids: set[str] = set()
        for pos, bridge in enumerate(bridges_raw):
            label = f"content_plan.question_context_bridges[{pos}]"
            if not isinstance(bridge, dict):
                raise ContractError(f"{label} 必须是对象")
            bridge_id = bridge.get("bridge_id")
            if not isinstance(bridge_id, str) or not bridge_id.strip():
                raise ContractError(f"{label}.bridge_id 不得为空")
            if bridge_id in bridge_ids:
                raise ContractError(f"question_context_bridges 重复 {bridge_id}")
            bridge_ids.add(bridge_id)
            question_ids = bridge.get("source_question_block_ids")
            answer_ids = bridge.get("answer_block_ids")
            for field, values in (
                ("source_question_block_ids", question_ids),
                ("answer_block_ids", answer_ids),
            ):
                if (
                    not isinstance(values, list)
                    or not values
                    or any(not isinstance(block_id, str) for block_id in values)
                    or len(values) != len(set(values))
                    or any(block_id not in blocks_by_id for block_id in values)
                ):
                    raise ContractError(f"{label}.{field} 必须引用不重复的存在内容块")
            if any(blocks_by_id[block_id]["decision"] != "exclude" for block_id in question_ids):
                raise ContractError(f"{label} 的提问块必须是 exclude")
            if any(blocks_by_id[block_id]["decision"] != "selected" for block_id in answer_ids):
                raise ContractError(f"{label} 的回答块必须属于当前 selected 主线")
            for field in ("question_scope", "necessity_reason", "fidelity_boundary"):
                if not isinstance(bridge.get(field), str) or not bridge[field].strip():
                    raise ContractError(f"{label}.{field} 不得为空")
        interview_output["question_context_bridge_count"] = len(bridge_ids)

    candidates = data.get("hook_candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ContractError("content_plan.hook_candidates 至少需要一个开头候选（可为 natural_premise）")
    candidate_ranges: dict[str, list[tuple[int, int]]] = {}
    for pos, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ContractError(f"content_plan.hook_candidates[{pos}] 必须是对象")
        candidate_id = candidate.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ContractError(
                f"content_plan.hook_candidates[{pos}].candidate_id 不得为空"
            )
        if candidate_id in candidate_ranges:
            raise ContractError(f"content_plan.hook_candidates id 重复: {candidate_id}")
        hook_block_ids = candidate.get("block_ids")
        if not isinstance(hook_block_ids, list) or not hook_block_ids:
            raise ContractError(
                f"content_plan.hook_candidates[{pos}].block_ids 必须是非空数组"
            )
        if any(not isinstance(block_id, str) for block_id in hook_block_ids):
            raise ContractError(
                f"content_plan.hook_candidates[{pos}].block_ids 只能是字符串"
            )
        if len(hook_block_ids) != len(set(hook_block_ids)):
            raise ContractError(
                f"content_plan.hook_candidates[{pos}].block_ids 不得重复"
            )
        unknown = [block_id for block_id in hook_block_ids if block_id not in blocks_by_id]
        if unknown:
            raise ContractError(
                f"content_plan.hook_candidates[{pos}] 引用不存在的块: {unknown}"
            )
        for field in ("hook_type", "strength_reason", "open_loop_reason"):
            if not isinstance(candidate.get(field), str) or not candidate[field].strip():
                raise ContractError(
                    f"content_plan.hook_candidates[{pos}].{field} 不得为空"
                )
        if any(blocks_by_id[block_id]["decision"] != "selected" for block_id in hook_block_ids):
            raise ContractError(
                f"开头候选 {candidate_id} 只能引用当前成片的 selected 内容块"
            )
        candidate_ranges[candidate_id] = [
            (blocks_by_id[block_id]["start"], blocks_by_id[block_id]["end"])
            for block_id in hook_block_ids
        ]
    selected_hook = data.get("selected_hook")
    if not isinstance(selected_hook, str) or selected_hook not in candidate_ranges:
        raise ContractError("content_plan.selected_hook 必须引用一个 hook candidate_id")

    review_raw = data.get("review")
    review = _validate_basic_review(review_raw, "content_plan")
    sample_pairs = review_raw.get("sample_pairs") if isinstance(review_raw, dict) else None
    if not isinstance(sample_pairs, list) or not 2 <= len(sample_pairs) <= 3:
        raise ContractError("content_plan.review.sample_pairs 必须登记 2–3 份同类型成对样本")
    registry = _sample_pair_registry()
    normalized_pairs: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    seen_families: set[str] = set()
    for pos, pair in enumerate(sample_pairs):
        if not isinstance(pair, dict):
            raise ContractError(f"content_plan.review.sample_pairs[{pos}] 必须是对象")
        for field in ("name", "material_type", "ai_sha256", "final_sha256"):
            if not isinstance(pair.get(field), str) or not pair[field].strip():
                raise ContractError(
                    f"content_plan.review.sample_pairs[{pos}].{field} 不得为空"
                )
        if pair["material_type"] not in {"single_speaker", INTERVIEW_TYPE}:
            raise ContractError(
                f"content_plan.review.sample_pairs[{pos}].material_type 不受支持"
            )
        for field in ("ai_sha256", "final_sha256"):
            if not re.fullmatch(r"[0-9a-fA-F]{64}", pair[field]):
                raise ContractError(
                    f"content_plan.review.sample_pairs[{pos}].{field} 必须是 64 位 SHA-256"
                )
        if pair["ai_sha256"].lower() == pair["final_sha256"].lower():
            raise ContractError(
                f"content_plan.review.sample_pairs[{pos}] AI 初稿与终版哈希不得相同"
            )
        if pair.get("evidence") != "diff_only":
            raise ContractError(
                f"content_plan.review.sample_pairs[{pos}].evidence 必须是 diff_only"
            )
        registered = registry.get((pair["name"], pair["material_type"]))
        if registered is None:
            raise ContractError(
                f"content_plan.review.sample_pairs[{pos}] 未登记在成对样本清单: "
                f"{pair['name']}"
            )
        pair_key = (pair["name"], pair["material_type"])
        if pair_key in seen_pairs:
            raise ContractError(
                f"content_plan.review.sample_pairs 重复登记: {pair['name']}"
            )
        seen_pairs.add(pair_key)
        family_id = registered["evidence_family_id"]
        if family_id in seen_families:
            raise ContractError(
                "content_plan.review.sample_pairs 必须来自不同 evidence_family_id；"
                f"重复家族: {family_id}"
            )
        seen_families.add(family_id)
        if (
            pair["ai_sha256"].lower() != registered["ai_sha256"].lower()
            or pair["final_sha256"].lower() != registered["final_sha256"].lower()
        ):
            raise ContractError(
                f"content_plan.review.sample_pairs[{pos}] 哈希与成对样本清单不一致"
            )
        normalized_pairs.append({
            **{
                field: pair[field]
                for field in ("name", "material_type", "ai_sha256", "final_sha256", "evidence")
            },
            "evidence_family_id": registered["evidence_family_id"],
            "evidence_scopes": registered["evidence_scopes"],
            "known_defects": registered["known_defects"],
        })
    review["sample_pairs"] = normalized_pairs
    if drop_spans is not None:
        drop_mask = [False] * word_count
        for start, end in drop_spans:
            for index in range(start, end + 1):
                drop_mask[index] = True
        leaked = [
            index
            for index, decision in enumerate(decisions_by_word)
            if decision != "selected" and not drop_mask[index]
        ]
        if leaked:
            raise ContractError(
                "content_plan 中未选入当前主线的 candidate/exclude 词必须全部进入 "
                "keep.drop；未删除词下标: "
                + ", ".join(map(str, leaked[:12]))
            )
    return {
        **context,
        "selected_story": selected_story,
        "story_ids": story_ids,
        "selected_hook": selected_hook,
        "candidate_ranges": candidate_ranges,
        "decision_counts": decision_counts,
        "review": review,
        "interview_output": interview_output,
    }


def validate_portfolio_plan(
    data: Any,
    *,
    work: Path,
    config: dict[str, Any],
    content_summary: dict[str, Any],
) -> dict[str, Any]:
    """校验同一长采访的多条组合计划，并绑定当前输出。"""
    if not isinstance(data, dict) or data.get("schema") != PORTFOLIO_SCHEMA:
        raise ContractError(f"portfolio_plan.json.schema 必须是 {PORTFOLIO_SCHEMA}")
    family_id = data.get("source_family_id")
    if not isinstance(family_id, str) or not family_id.strip():
        raise ContractError("portfolio_plan.source_family_id 不得为空")
    for field in ("source_sha256", "word_track_sha256"):
        value = data.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ContractError(f"portfolio_plan.{field} 必须是 64 位 SHA-256")
    configured_source_hash = config.get("source_sha256")
    if (
        not isinstance(configured_source_hash, str)
        or not re.fullmatch(r"[0-9a-fA-F]{64}", configured_source_hash)
    ):
        raise ContractError(
            "多条模式必须先将已核验的源视频 SHA-256 写入 config.source_sha256"
        )
    if data["source_sha256"].lower() != configured_source_hash.lower():
        raise ContractError("portfolio_plan.source_sha256 与 config.source_sha256 不一致")
    actual_word_track_hash = sha256_file(work / "word_track.json")
    if data["word_track_sha256"].lower() != actual_word_track_hash.lower():
        raise ContractError("portfolio_plan.word_track_sha256 与当前词轴不一致")

    request = content_summary.get("interview_output", {}).get("output_request", {})
    authorized_count = data.get("authorized_output_count")
    if not _is_index(authorized_count) or authorized_count < 2:
        raise ContractError("portfolio_plan.authorized_output_count 至少为 2")
    if data.get("authorization_source") != "user":
        raise ContractError("portfolio_plan.authorization_source 必须是 user")
    if authorized_count != request.get("requested_count"):
        raise ContractError(
            "portfolio_plan.authorized_output_count 与 output_request.requested_count 不一致"
        )

    candidates = {
        item["candidate_id"]: set(item["block_ids"])
        for item in content_summary["interview_output"]["story_candidates"]
    }
    outputs_raw = data.get("outputs")
    if not isinstance(outputs_raw, list) or len(outputs_raw) != authorized_count:
        raise ContractError("portfolio_plan.outputs 数量必须等于授权输出数")
    outputs_by_index: dict[int, dict[str, Any]] = {}
    output_ids: set[str] = set()
    candidate_ids: set[str] = set()
    audience_questions: set[str] = set()
    core_proofs: set[str] = set()
    block_uses: dict[str, list[str]] = {}
    normalized_outputs: list[dict[str, Any]] = []
    for pos, output in enumerate(outputs_raw):
        label = f"portfolio_plan.outputs[{pos}]"
        if not isinstance(output, dict):
            raise ContractError(f"{label} 必须是对象")
        output_id = output.get("output_id")
        candidate_id = output.get("candidate_id")
        output_index = output.get("output_index")
        if not isinstance(output_id, str) or not output_id.strip():
            raise ContractError(f"{label}.output_id 不得为空")
        if output_id in output_ids:
            raise ContractError(f"portfolio_plan.outputs 重复 output_id: {output_id}")
        output_ids.add(output_id)
        if not isinstance(candidate_id, str) or candidate_id not in candidates:
            raise ContractError(f"{label}.candidate_id 必须引用 story_candidates")
        if candidate_id in candidate_ids:
            raise ContractError(f"portfolio_plan.outputs 重复 candidate_id: {candidate_id}")
        candidate_ids.add(candidate_id)
        if not _is_index(output_index) or not 1 <= output_index <= authorized_count:
            raise ContractError(f"{label}.output_index 必须在授权范围内")
        if output_index in outputs_by_index:
            raise ContractError(f"portfolio_plan.outputs 重复 output_index: {output_index}")
        for field in (
            "audience_question",
            "core_proof",
            "course_value",
            "hook_promise",
            "body_payoff",
            "conclusion",
            "boundary",
        ):
            if not isinstance(output.get(field), str) or not output[field].strip():
                raise ContractError(f"{label}.{field} 不得为空")
        question_key = _norm_text(output["audience_question"])
        proof_key = _norm_text(output["core_proof"])
        if question_key in audience_questions:
            raise ContractError("portfolio_plan 不同输出不得使用相同 audience_question")
        if proof_key in core_proofs:
            raise ContractError("portfolio_plan 不同输出不得使用相同 core_proof")
        audience_questions.add(question_key)
        core_proofs.add(proof_key)
        primary = output.get("primary_block_ids")
        supporting = output.get("supporting_block_ids")
        if (
            not isinstance(primary, list)
            or not primary
            or any(not isinstance(block_id, str) for block_id in primary)
            or len(primary) != len(set(primary))
        ):
            raise ContractError(f"{label}.primary_block_ids 必须是非空不重复字符串数组")
        if (
            not isinstance(supporting, list)
            or any(not isinstance(block_id, str) for block_id in supporting)
            or len(supporting) != len(set(supporting))
        ):
            raise ContractError(f"{label}.supporting_block_ids 必须是不重复字符串数组")
        selected_blocks = set(primary) | set(supporting)
        if set(primary) & set(supporting):
            raise ContractError(f"{label} 的主证据与辅助证据不得重复")
        if selected_blocks != candidates[candidate_id]:
            raise ContractError(
                f"{label} 的主/辅助证据必须恰好覆盖候选 {candidate_id} 的 block_ids"
            )
        for block_id in selected_blocks:
            block_uses.setdefault(block_id, []).append(output_id)
        normalized = {
            "output_id": output_id,
            "output_index": output_index,
            "candidate_id": candidate_id,
            "selected_blocks": sorted(selected_blocks),
        }
        outputs_by_index[output_index] = normalized
        normalized_outputs.append(normalized)
    if set(outputs_by_index) != set(range(1, authorized_count + 1)):
        raise ContractError("portfolio_plan.output_index 必须完整覆盖 1..授权条数")

    duplicated_blocks = {
        block_id: uses for block_id, uses in block_uses.items() if len(uses) > 1
    }
    reuse_raw = data.get("cross_output_reuse", [])
    if not isinstance(reuse_raw, list):
        raise ContractError("portfolio_plan.cross_output_reuse 必须是数组")
    reviewed_reuse: set[str] = set()
    for pos, item in enumerate(reuse_raw):
        label = f"portfolio_plan.cross_output_reuse[{pos}]"
        if not isinstance(item, dict):
            raise ContractError(f"{label} 必须是对象")
        block_id = item.get("block_id")
        if not isinstance(block_id, str) or block_id in reviewed_reuse:
            raise ContractError(f"{label}.block_id 不得为空或重复")
        expected_uses = duplicated_blocks.get(block_id)
        uses = item.get("uses")
        if expected_uses is None or not isinstance(uses, list) or len(uses) != len(expected_uses):
            raise ContractError(f"{label} 必须对应一个实际跨片复用块")
        actual_output_ids: list[str] = []
        for use_pos, use in enumerate(uses):
            use_label = f"{label}.uses[{use_pos}]"
            if not isinstance(use, dict):
                raise ContractError(f"{use_label} 必须是对象")
            output_id = use.get("output_id")
            if not isinstance(output_id, str):
                raise ContractError(f"{use_label}.output_id 不得为空")
            actual_output_ids.append(output_id)
            for field in ("function", "why_unavoidable", "deletion_impact"):
                if not isinstance(use.get(field), str) or not use[field].strip():
                    raise ContractError(f"{use_label}.{field} 不得为空")
        if len(actual_output_ids) != len(set(actual_output_ids)) or set(actual_output_ids) != set(expected_uses):
            raise ContractError(f"{label}.uses 必须精确覆盖复用该块的全部输出")
        reviewed_reuse.add(block_id)
    if reviewed_reuse != set(duplicated_blocks):
        missing = sorted(set(duplicated_blocks) - reviewed_reuse)
        extra = sorted(reviewed_reuse - set(duplicated_blocks))
        raise ContractError(
            "跨片复用必须逐块登记 cross_output_reuse；"
            f"缺少 {missing}，多余 {extra}"
        )

    current_index = request.get("output_index")
    current = outputs_by_index.get(current_index)
    if current is None:
        raise ContractError("portfolio_plan 缺少当前 output_index")
    if current["candidate_id"] != content_summary["interview_output"]["selected_candidate_id"]:
        raise ContractError("当前 selected story_candidate 与 portfolio_plan 对应条目不一致")
    review = _validate_basic_review(data.get("review"), "portfolio_plan")
    return {
        "source_family_id": family_id.strip(),
        "authorized_output_count": authorized_count,
        "current_output_id": current["output_id"],
        "cross_output_reuse_count": len(reviewed_reuse),
        "review": review,
    }


def validate_speaker_turns(
    data: Any,
    word_count: int,
    drop_spans: list[tuple[int, int]],
) -> dict[str, int]:
    """校验“去提问者”单主体采访的逐词完整分角。"""
    if not isinstance(data, dict):
        raise ContractError("speaker_turns.json 必须是对象")
    if data.get("mode") != "remove_interviewer":
        raise ContractError("speaker_turns.mode 必须是 remove_interviewer")
    turns = data.get("turns")
    if not isinstance(turns, list) or not turns:
        raise ContractError("speaker_turns.turns 必须是非空数组")

    expected_start = 0
    interviewer_words = 0
    subject_words = 0
    interviewer_turns = 0
    subject_turns = 0
    drop_mask = [False] * word_count
    for start, end in drop_spans:
        for index in range(start, end + 1):
            drop_mask[index] = True

    for pos, turn in enumerate(turns):
        if not isinstance(turn, dict):
            raise ContractError(f"speaker_turns.turns[{pos}] 必须是对象")
        start, end, role = turn.get("start"), turn.get("end"), turn.get("role")
        if not _is_index(start) or not _is_index(end):
            raise ContractError(f"speaker_turns.turns[{pos}] start/end 必须是整数")
        if start != expected_start:
            raise ContractError(
                f"speaker_turns 必须从 0 开始无缝覆盖；turn[{pos}].start={start}，"
                f"期望 {expected_start}"
            )
        if end < start or end >= word_count:
            raise ContractError(f"speaker_turns.turns[{pos}] 区间非法 [{start},{end}]")
        if role not in {"interviewer", "subject"}:
            raise ContractError(
                f"speaker_turns.turns[{pos}].role 只能是 interviewer 或 subject；"
                "不确定时必须停止"
            )
        reason = turn.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ContractError(f"speaker_turns.turns[{pos}].reason 不得为空")
        width = end - start + 1
        if role == "interviewer":
            interviewer_turns += 1
            interviewer_words += width
            missing_drop = next((i for i in range(start, end + 1) if not drop_mask[i]), None)
            if missing_drop is not None:
                raise ContractError(
                    f"提问者词 {missing_drop} 未被 keep.drop 删除；"
                    "single_subject_interview 不允许提问者声音进入成片"
                )
        else:
            subject_turns += 1
            subject_words += width
        expected_start = end + 1

    if expected_start != word_count:
        raise ContractError(
            f"speaker_turns 未覆盖到词轴末尾：已到 {expected_start - 1}，"
            f"应到 {word_count - 1}"
        )
    if not interviewer_turns or not subject_turns:
        raise ContractError("single_subject_interview 必须同时包含提问者和单一学员")
    return {
        "interviewer_turns": interviewer_turns,
        "interviewer_words": interviewer_words,
        "subject_turns": subject_turns,
        "subject_words": subject_words,
    }


def validate_review_record(keep: Any, material_type: str = "single_speaker") -> dict[str, Any]:
    if not isinstance(keep, dict):
        raise ContractError("keep.json 必须是对象")
    review = keep.get("review")
    basic = _validate_basic_review(review, "keep")
    scope = review.get("scope")
    if not isinstance(scope, list) or any(not isinstance(x, str) for x in scope):
        raise ContractError("keep.review.scope 必须是字符串数组")
    required_scope = {"content_plan", "keep", "structure", "cut_review"}
    if material_type == INTERVIEW_TYPE:
        required_scope.add("speaker_turns")
    missing = required_scope - set(scope)
    if missing:
        raise ContractError("keep.review.scope 缺少: " + ", ".join(sorted(missing)))
    return {
        **basic,
        "scope": list(scope),
    }


def _segment_key(segment: dict[str, Any], pos: int) -> str:
    meta = segment.get("_rough_structure")
    if isinstance(meta, dict) and "playback_key" in meta:
        key = meta.get("playback_key")
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise ContractError(f"segments[{pos}] playback_key 不合法: {key!r}")
        return key
    line, part = segment.get("line"), segment.get("part")
    if not isinstance(line, str) or not _is_index(part):
        raise ContractError(f"segments[{pos}] 缺少合法 line/part")
    key = f"{line}.{part}"
    if not KEY_RE.fullmatch(key):
        raise ContractError(f"segments[{pos}] 键不合法: {key}")
    return key


def _segment_contract(
    data: Any,
    track_words: list[dict[str, Any]],
    *,
    allow_source_reorder: bool,
) -> dict[str, Any]:
    word_count = len(track_words)
    if not isinstance(data, list) or not data:
        raise ContractError("segments.json 必须是非空数组")
    result: list[dict[str, Any]] = []
    keys: set[str] = set()
    used_words: dict[int, str] = {}
    previous_out = -1
    for pos, segment in enumerate(data):
        if not isinstance(segment, dict):
            raise ContractError(f"segments[{pos}] 必须是对象")
        key = _segment_key(segment, pos)
        if key in keys:
            raise ContractError(f"segments.json 重复键: {key}")
        keys.add(key)
        in_f, out_f = segment.get("in_f"), segment.get("out_f")
        if not _is_index(in_f) or not _is_index(out_f):
            raise ContractError(f"segments[{pos}] in_f/out_f 必须是整数帧")
        if in_f < 0 or out_f <= in_f:
            raise ContractError(f"segments[{pos}] 帧区间非法: [{in_f},{out_f})")
        if not allow_source_reorder and in_f < previous_out:
            raise ContractError(
                f"segments[{pos}] 与前段源区间重叠: {in_f} < {previous_out}"
            )
        previous_out = out_f
        seg_words = segment.get("words")
        if not isinstance(seg_words, list) or not seg_words:
            raise ContractError(f"segments[{pos}].words 必须是非空数组")
        indices: list[int] = []
        word_texts: list[str] = []
        base_key = f"{segment.get('line')}.{segment.get('part')}"
        meta = segment.get("_rough_structure")
        intentional_repeat = (
            allow_source_reorder
            and isinstance(meta, dict)
            and meta.get("intentional_repeat") is True
            and _is_index(meta.get("repeat_occurrence"))
            and meta["repeat_occurrence"] >= 2
        )
        for wi, word in enumerate(seg_words):
            if not isinstance(word, dict) or not _is_index(word.get("i")):
                raise ContractError(f"segments[{pos}].words[{wi}].i 必须是整数")
            index = word["i"]
            if index < 0 or index >= word_count:
                raise ContractError(f"segments[{pos}].words[{wi}].i 越界: {index}")
            if indices and index <= indices[-1]:
                raise ContractError(f"segments[{pos}] 词下标必须严格递增")
            if index in used_words and not (
                intentional_repeat and used_words[index] == base_key
            ):
                raise ContractError(f"segments 重复使用词下标 {index}")
            if not isinstance(word.get("w"), str):
                raise ContractError(f"segments[{pos}].words[{wi}].w 必须是字符串")
            acoustic_text = "".join(
                ch for ch in word["w"] if ACOUSTIC_CH_RE.fullmatch(ch)
            )
            align_text = word.get("align_text")
            char_times = word.get("char_times")
            track_word = track_words[index]
            if align_text != acoustic_text:
                raise ContractError(
                    f"segments[{pos}].words[{wi}] align_text 与声学文本不一致: "
                    f"{align_text!r} != {acoustic_text!r}"
                )
            if track_word.get("align_text") != acoustic_text:
                raise ContractError(
                    f"word_track.words[{index}].align_text 与保留词声学文本不一致"
                )
            if char_times != track_word.get("char_times"):
                raise ContractError(
                    f"segments[{pos}].words[{wi}].char_times 与 word_track[{index}] 不一致"
                )
            if not isinstance(char_times, list) or len(char_times) != len(acoustic_text):
                raise ContractError(
                    f"segments[{pos}].words[{wi}].char_times 数量必须与 "
                    f"align_text 一致（{len(acoustic_text)}）"
                )
            previous_char_end = -1.0
            for ci, timing in enumerate(char_times):
                if not isinstance(timing, dict) or timing.get("c") != acoustic_text[ci]:
                    raise ContractError(
                        f"segments[{pos}].words[{wi}].char_times[{ci}] 字符不一致"
                    )
                start, end = timing.get("s"), timing.get("e")
                if (
                    isinstance(start, bool)
                    or isinstance(end, bool)
                    or not isinstance(start, (int, float))
                    or not isinstance(end, (int, float))
                    or not math.isfinite(start)
                    or not math.isfinite(end)
                    or start < 0
                    or end < start
                    or start + 1e-6 < previous_char_end
                ):
                    raise ContractError(
                        f"segments[{pos}].words[{wi}].char_times[{ci}] 时间非法"
                    )
                previous_char_end = end
            indices.append(index)
            word_texts.append(word["w"])
            used_words.setdefault(index, base_key)
        text = segment.get("text")
        if not isinstance(text, str):
            raise ContractError(f"segments[{pos}].text 必须是字符串")
        actual = "".join(word_texts)
        if _norm_text(text) != _norm_text(actual):
            raise ContractError(
                f"segments[{pos}] text 与 words 不一致: 声明「{text}」 / 实际「{actual}」"
            )
        acoustic_starts = [
            timing["s"]
            for word in seg_words
            for timing in word.get("char_times", [])
        ]
        acoustic_ends = [
            timing["e"]
            for word in seg_words
            for timing in word.get("char_times", [])
        ]
        if not acoustic_starts:
            raise ContractError(f"segments[{pos}] 没有可用字级声学时码")
        first_acoustic_f = math.floor(min(acoustic_starts) * ANALYSIS_FPS + 1e-9)
        last_acoustic_f = math.ceil(max(acoustic_ends) * ANALYSIS_FPS - 1e-9)
        result.append(
            {
                "key": key,
                "in_f": in_f,
                "out_f": out_f,
                "head_preroll_frames": first_acoustic_f - in_f,
                "tail_release_frames": out_f - last_acoustic_f,
                "word_indices": indices,
                "word_texts": word_texts,
                "text": text,
            }
        )
    return {"segments": result}


def _p2_module():
    path = Path(__file__).with_name("p2_结构编排.py")
    spec = importlib.util.spec_from_file_location("kbcg_p2_structure", path)
    if spec is None or spec.loader is None:
        raise ContractError(f"无法加载 p2 结构契约: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_edit_structure_and_rough(
    segments: Any,
    structure: Any,
    rough: Any,
) -> dict[str, Any]:
    """复用 p2 的唯一结构语义，并断言 rough 是精确投影。"""
    p2 = _p2_module()
    try:
        source, source_index = p2._validate_segments(segments)
        context, selected_hook, hook_segment_ids, sequence, repeat_groups = p2._validate_structure(
            structure, source_index
        )
        expected = p2.compile_rough_segments(
            source,
            source_index,
            sequence,
            selected_hook,
            hook_segment_ids,
            repeat_groups,
        )
    except (OSError, p2.StructureError) as exc:
        raise ContractError(f"structure.json 结构决策非法: {exc}") from exc
    if rough != expected:
        raise ContractError(
            "rough_segments.json 不是当前 structure.json 对 segments.json 的精确投影；"
            "请重跑 p2_结构编排.py"
        )
    hook_word_indices: list[int] = []
    for segment_id in hook_segment_ids:
        key = p2._id_key(segment_id, "selected_hook.segment_ids")
        source_pos = source_index[key]
        hook_word_indices.extend(word["i"] for word in source[source_pos]["words"])
    return {
        "context": context,
        "selected_hook": selected_hook,
        "hook_word_indices": hook_word_indices,
    }


def _card_rows(value: Any, key: str) -> tuple[list[Any], dict[str, Any] | None]:
    if isinstance(value, list):
        return value, None
    if not isinstance(value, dict):
        raise ContractError(f"cards.{key} 必须是分卡数组或带 cards 的对象")
    extra = set(value) - {"cards", "bridge_to_next"}
    if extra:
        raise ContractError(f"cards.{key} 含未知字段: {', '.join(sorted(extra))}")
    rows = value.get("cards")
    bridge = value.get("bridge_to_next")
    if bridge is not None and not isinstance(bridge, dict):
        raise ContractError(f"cards.{key}.bridge_to_next 必须是对象")
    return rows, bridge


def validate_cards(data: Any, rough_contract: dict[str, Any]) -> int:
    if not isinstance(data, dict):
        raise ContractError("cards.json 必须是以段键为 key 的对象")
    expected = {row["key"]: row for row in rough_contract["segments"]}
    actual_keys = set(data)
    expected_keys = set(expected)
    missing = sorted(expected_keys - actual_keys)
    extra = sorted(actual_keys - expected_keys)
    if missing or extra:
        parts = []
        if missing:
            parts.append("缺段 " + ", ".join(missing[:8]))
        if extra:
            parts.append("多段 " + ", ".join(extra[:8]))
        raise ContractError("cards.json key 与 rough_segments 不一致: " + "；".join(parts))
    normalized: dict[str, dict[str, Any]] = {}
    card_count = 0
    for key, row in expected.items():
        cards, bridge = _card_rows(data[key], key)
        if not isinstance(cards, list) or not cards:
            raise ContractError(f"cards.{key} 必须是非空分卡数组")
        aligned: list[str] = []
        for pos, card in enumerate(cards):
            if not isinstance(card, list) or len(card) not in (2, 3):
                raise ContractError(
                    f"cards.{key}[{pos}] 必须是 [text, keyword]；"
                    "仅兼容 [align, display, keyword]"
                )
            if any(not isinstance(value, str) for value in card):
                raise ContractError(f"cards.{key}[{pos}] 所有字段必须是字符串")
            if len(card) == 2:
                text, keyword = card
            else:
                align, display, keyword = card
                if display and _norm_text(display) != _norm_text(align):
                    raise ContractError(
                        f"cards.{key}[{pos}] 旧三元 display 不得删字/改字；"
                        "只允许为空或等于 align"
                    )
                text = align
            if not _norm_text(text):
                raise ContractError(f"cards.{key}[{pos}].text 不得为空")
            if keyword and keyword not in text:
                raise ContractError(f"cards.{key}[{pos}].keyword 不在 text 中")
            aligned.append(text)
        normalized[key] = {
            "cards": cards,
            "texts": aligned,
            "bridge": bridge,
        }
        card_count += len(cards)
        actual = _norm_text("".join(aligned))
        wanted = _norm_text(row["text"])
        if actual != wanted:
            raise ContractError(
                f"cards.{key} 文本与 rough_segments 不一致: 分卡「{actual}」 / 段「{wanted}」"
            )
        try:
            validate_atomic_boundaries(
                row["text"], row.get("word_texts") or [], aligned,
                label=f"cards.{key}",
            )
        except CaptionBoundaryError as exc:
            raise ContractError(str(exc)) from exc
    rough_keys = [row["key"] for row in rough_contract["segments"]]
    for index, key in enumerate(rough_keys):
        bridge = normalized[key]["bridge"]
        if bridge is None:
            continue
        if index == len(rough_keys) - 1:
            raise ContractError(f"cards.{key} 是末段，不得声明 bridge_to_next")
        if set(bridge) != {"left_text", "right_text", "reason"}:
            raise ContractError(
                f"cards.{key}.bridge_to_next 必须且只能包含 "
                "left_text/right_text/reason"
            )
        if any(not isinstance(bridge[field], str) or not bridge[field].strip()
               for field in ("left_text", "right_text", "reason")):
            raise ContractError(f"cards.{key}.bridge_to_next 三个字段都不得为空")
        next_key = rough_keys[index + 1]
        left = normalized[key]["texts"][-1]
        right = normalized[next_key]["texts"][0]
        if _norm_text(bridge["left_text"]) != _norm_text(left):
            raise ContractError(
                f"cards.{key}.bridge_to_next.left_text 必须等于本段末卡「{left}」"
            )
        if _norm_text(bridge["right_text"]) != _norm_text(right):
            raise ContractError(
                f"cards.{key}.bridge_to_next.right_text 必须等于下一段 {next_key} 首卡「{right}」"
            )
    return card_count


def validate_cut_review(
    data: Any,
    rough_contract: dict[str, Any],
    split_after: set[int] | None = None,
    pause_after: set[int] | None = None,
) -> dict[str, Any]:
    """校验每个最终段的头尾余量，以及真实删接/重排切口。"""
    if not isinstance(data, dict):
        raise ContractError("cut_review.json 必须是对象")
    if data.get("schema") != CUT_REVIEW_SCHEMA:
        raise ContractError(
            f"cut_review.schema 必须是 {CUT_REVIEW_SCHEMA}；"
            "旧版只勾选完整性，不能证明头尾余量和连续听感"
        )
    rows = rough_contract["segments"]
    split_after = split_after or set()
    pause_after = pause_after or set()

    segment_reviews = data.get("segments")
    if not isinstance(segment_reviews, list):
        raise ContractError("cut_review.segments 必须是数组")
    if len(segment_reviews) != len(rows):
        raise ContractError(
            "cut_review.segments 必须按播放顺序覆盖全部最终段；"
            f"期望 {len(rows)}，实际 {len(segment_reviews)}"
        )
    for pos, (review_row, segment) in enumerate(zip(segment_reviews, rows)):
        label = f"cut_review.segments[{pos}]"
        if not isinstance(review_row, dict) or review_row.get("key") != segment["key"]:
            raise ContractError(f"{label}.key 必须是 {segment['key']}")
        for field in ("head_preroll_frames", "tail_release_frames"):
            if not _is_index(review_row.get(field)):
                raise ContractError(f"{label}.{field} 必须是整数帧")
        if review_row["head_preroll_frames"] != segment["head_preroll_frames"]:
            raise ContractError(f"{label}.head_preroll_frames 与粗剪声学边界不一致")
        if review_row["tail_release_frames"] != segment["tail_release_frames"]:
            raise ContractError(f"{label}.tail_release_frames 与粗剪声学边界不一致")
        if review_row.get("head_complete") is not True:
            raise ContractError(f"{label}.head_complete 必须逐段听审为 true")
        if review_row.get("tail_complete") is not True:
            raise ContractError(f"{label}.tail_complete 必须逐段听审为 true")
        for field in ("head_note", "tail_note"):
            if not isinstance(review_row.get(field), str) or not review_row[field].strip():
                raise ContractError(f"{label}.{field} 不得为空")
        if review_row["head_preroll_frames"] < HEAD_ROOM_TARGET_F:
            exception = review_row.get("head_margin_exception")
            if not isinstance(exception, str) or not exception.strip():
                raise ContractError(
                    f"{label} 段头仅 {review_row['head_preroll_frames']}帧 < "
                    f"默认 {HEAD_ROOM_TARGET_F}帧；必须记录前方被删声音/"
                    "紧邻发音或素材起点这一安全上界"
                )
        if review_row["tail_release_frames"] < TAIL_ROOM_TARGET_F:
            exception = review_row.get("tail_margin_exception")
            if not isinstance(exception, str) or not exception.strip():
                raise ContractError(
                    f"{label} 段尾仅 {review_row['tail_release_frames']}帧 < "
                    f"默认 {TAIL_ROOM_TARGET_F}帧；必须记录下一发音/drop/"
                    "素材尾这一安全上界"
                )

    expected = [
        (left["key"], right["key"])
        for left, right in zip(rows, rows[1:])
        if left["out_f"] != right["in_f"]
        or left["word_indices"][-1] in split_after
        or left["word_indices"][-1] in pause_after
    ]
    cuts = data.get("cuts")
    if not isinstance(cuts, list):
        raise ContractError("cut_review.cuts 必须是数组")
    actual: list[tuple[str, str]] = []
    valid_purposes = {"content_delete", "pause_compression", "reorder", "split_after"}
    for pos, cut in enumerate(cuts):
        if not isinstance(cut, dict):
            raise ContractError(f"cut_review.cuts[{pos}] 必须是对象")
        left, right = cut.get("left_key"), cut.get("right_key")
        if not isinstance(left, str) or not isinstance(right, str):
            raise ContractError(
                f"cut_review.cuts[{pos}].left_key/right_key 必须是字符串"
            )
        actual.append((left, right))
        if cut.get("edit_purpose") not in valid_purposes:
            raise ContractError(
                f"cut_review.cuts[{pos}].edit_purpose 必须说明真实下刀用途"
            )
        left_row = next((row for row in rows if row["key"] == left), None)
        right_row = next((row for row in rows if row["key"] == right), None)
        if left_row and right_row:
            last_i = left_row["word_indices"][-1]
            first_i = right_row["word_indices"][0]
            if first_i <= last_i:
                expected_purpose = "reorder"
            elif last_i in split_after:
                expected_purpose = "split_after"
            elif first_i > last_i + 1:
                expected_purpose = "content_delete"
            elif last_i in pause_after:
                expected_purpose = "pause_compression"
            else:
                raise ContractError(
                    f"cut_review.cuts[{pos}] 是相邻保留词之间的无依据切口"
                )
            if cut.get("edit_purpose") != expected_purpose:
                raise ContractError(
                    f"cut_review.cuts[{pos}].edit_purpose={cut.get('edit_purpose')!r}，"
                    f"决策依据期望 {expected_purpose!r}"
                )
        for field in ("no_deleted_audio", "semantic_natural", "joined_playback_checked"):
            if cut.get(field) is not True:
                raise ContractError(
                    f"cut_review.cuts[{pos}].{field} 必须逐项听审并明确为 true"
                )
        if not isinstance(cut.get("joined_note"), str) or not cut["joined_note"].strip():
            raise ContractError(f"cut_review.cuts[{pos}].joined_note 不得为空")
    if actual != expected:
        raise ContractError(
            "cut_review.cuts 必须按播放顺序精确覆盖全部真实切口；"
            f"期望 {expected}，实际 {actual}"
        )
    review = _validate_basic_review(data.get("review"), "cut_review")
    return {
        "segment_count": len(rows),
        "cut_count": len(expected),
        "review": review,
    }


def validate_keep_stage(work: Path) -> dict[str, Any]:
    """p1a2 前的早期闸门；采访额外校验 speaker_turns。"""
    work = work.resolve()
    config = _read_json(work / "config.json")
    material_type = validate_config(config)
    track = _read_json(work / "word_track.json")
    words = validate_word_track(track)
    keep = _read_json(work / "keep.json")
    spans = validate_keep(keep, len(words))
    content_summary = validate_content_plan(
        _read_json(work / "content_plan.json"),
        len(words),
        spans["drop"],
        material_type,
    )
    wrong_sample_types = [
        pair["name"]
        for pair in content_summary["review"]["sample_pairs"]
        if pair["material_type"] != material_type
    ]
    if wrong_sample_types:
        raise ContractError(
            "content_plan.review.sample_pairs 必须与当前 material_type 同类型；"
            "不匹配: " + ", ".join(wrong_sample_types)
        )
    speaker_summary = None
    portfolio_summary = None
    if material_type == INTERVIEW_TYPE:
        speaker_summary = validate_speaker_turns(
            _read_json(work / "speaker_turns.json"), len(words), spans["drop"]
        )
        request = content_summary["interview_output"]["output_request"]
        if request["mode"] == "multiple":
            portfolio_summary = validate_portfolio_plan(
                _read_json(work / "portfolio_plan.json"),
                work=work,
                config=config,
                content_summary=content_summary,
            )
    return {
        "word_count": len(words),
        "decision_counts": {
            kind: len(spans[kind])
            for kind in ("drop", "fix", "retain", "split_after")
        },
        "material_type": material_type,
        "speaker_summary": speaker_summary,
        "portfolio_summary": portfolio_summary,
        "content_summary": content_summary,
        "spans": spans,
    }


def validate_rough_workspace(work: Path) -> dict[str, Any]:
    """校验并汇总可独立冻结的粗剪，不读取任何字幕包装文件。"""
    work = work.resolve()
    keep_summary = validate_keep_stage(work)
    word_count = keep_summary["word_count"]
    segments = _read_json(work / "segments.json")
    track_words = validate_word_track(_read_json(work / "word_track.json"))
    expression_data = _read_json(work / "expression_review.json")
    try:
        expression_summary = validate_expression_review(
            expression_data,
            track_path=work / "word_track.json",
            track=_read_json(work / "word_track.json"),
            keep=_read_json(work / "keep.json"),
        )
    except ExpressionReviewError as exc:
        raise ContractError(f"expression_review 未闭环: {exc}") from exc
    source_contract = _segment_contract(
        segments, track_words, allow_source_reorder=False
    )
    drop_indices = {
        index
        for start, end in keep_summary["spans"]["drop"]
        for index in range(start, end + 1)
    }
    leaked_drop = sorted(
        drop_indices
        & {
            index
            for segment in source_contract["segments"]
            for index in segment["word_indices"]
        }
    )
    if leaked_drop:
        raise ContractError(
            "segments.json 仍包含 keep.drop 词下标: "
            + ", ".join(map(str, leaked_drop[:12]))
        )
    structure = _read_json(work / "structure.json")
    rough = _read_json(work / "rough_segments.json")
    structure_summary = _validate_edit_structure_and_rough(segments, structure, rough)
    content_summary = keep_summary["content_summary"]
    for field in ("goal", "audience", "main_claim"):
        if structure_summary["context"][field] != content_summary[field]:
            raise ContractError(
                f"structure.json.{field} 必须与 content_plan.json 完全一致"
            )
    if structure_summary["selected_hook"] != content_summary["selected_hook"]:
        raise ContractError(
            "structure.json.selected_hook 必须与 content_plan.json.selected_hook 一致"
        )
    hook_ranges = content_summary["candidate_ranges"][content_summary["selected_hook"]]
    outside = [
        index
        for index in structure_summary["hook_word_indices"]
        if not any(start <= index <= end for start, end in hook_ranges)
    ]
    if outside:
        raise ContractError(
            "structure.json 的 selected_hook 段超出 content_plan 候选词区间；"
            "越界词下标: " + ", ".join(map(str, outside[:12]))
        )
    rough_contract = _segment_contract(
        rough, track_words, allow_source_reorder=True
    )
    cut_summary = validate_cut_review(
        _read_json(work / "cut_review.json"),
        rough_contract,
        set(keep_summary["spans"]["split_after"]),
        pause_compress_boundaries(expression_data),
    )
    return {
        "word_count": word_count,
        "segment_count": len(source_contract["segments"]),
        "rough_segment_count": len(rough_contract["segments"]),
        "decision_counts": keep_summary["decision_counts"],
        "material_type": keep_summary["material_type"],
        "speaker_summary": keep_summary["speaker_summary"],
        "portfolio_summary": keep_summary["portfolio_summary"],
        "content_summary": content_summary,
        "cut_summary": cut_summary,
        "expression_summary": expression_summary,
        "_rough_contract": rough_contract,
    }


def validate_workspace(work: Path) -> dict[str, Any]:
    """校验粗剪以及后续字幕包装；不会改变粗剪锁的范围。"""
    work = work.resolve()
    summary = validate_rough_workspace(work)
    cards = _read_json(work / "cards.json")
    card_count = validate_cards(cards, summary["_rough_contract"])
    return {
        **{key: value for key, value in summary.items() if not key.startswith("_")},
        "card_count": card_count,
    }


def freeze(work: Path, *, force: bool = False) -> dict[str, Any]:
    work = work.resolve()
    lock_path = work / "decision_lock.json"
    if lock_path.exists() and not force:
        raise ContractError(
            f"{lock_path} 已存在；决策锁不得静默覆盖。"
            "确认重审完成后显式使用 --force"
        )
    # structure.json 是 p2 的编辑结构决策，freeze 绝不生成或覆盖它。
    summary = validate_rough_workspace(work)
    material_type = summary["material_type"]
    review = validate_review_record(_read_json(work / "keep.json"), material_type)
    content_review = summary["content_summary"]["review"]
    cut_review = summary["cut_summary"]["review"]
    locked_files = locked_files_for_workspace(work, material_type)
    entries = {name: file_entry(work / name) for name in locked_files}
    lock = {
        "schema": LOCK_SCHEMA,
        "created_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "work": str(work),
        "files": entries,
        "counts": {
            "words": summary["word_count"],
            "segments": summary["segment_count"],
            "rough_segments": summary["rough_segment_count"],
            "cuts": summary["cut_summary"]["cut_count"],
            "expression_micro": summary["expression_summary"]["micro"],
            "expression_residual": summary["expression_summary"]["residual"],
            **summary["decision_counts"],
        },
        "review": review,
        "content_review": content_review,
        "cut_review": cut_review,
        "_rule": (
            "锁后任一文件变更都必须重新审阅并显式 freeze --force；"
            "finalize 只读本锁，不重建词轴/段/决策。"
        ),
    }
    _atomic_json(lock_path, lock)
    return lock


def verify(work: Path) -> dict[str, Any]:
    work = work.resolve()
    lock = _read_json(work / "decision_lock.json")
    if not isinstance(lock, dict) or lock.get("schema") != LOCK_SCHEMA:
        raise ContractError(f"decision_lock.json.schema 必须是 {LOCK_SCHEMA}")
    files = lock.get("files")
    if not isinstance(files, dict):
        raise ContractError("decision_lock.json.files 必须是对象")
    material_type = validate_config(_read_json(work / "config.json"))
    locked_files = locked_files_for_workspace(work, material_type)
    if set(files) != set(locked_files):
        raise ContractError("decision_lock.json.files 不完整")
    for name in locked_files:
        expected = files[name]
        if not isinstance(expected, dict) or not isinstance(expected.get("sha256"), str):
            raise ContractError(f"decision_lock.json 缺少 {name} 的 sha256")
        path = work / name
        actual = file_entry(path)
        if actual["sha256"] != expected["sha256"]:
            raise ContractError(
                f"决策锁失效: {name} sha256 {expected['sha256'][:12]} -> {actual['sha256'][:12]}"
            )
    summary = validate_rough_workspace(work)
    review = validate_review_record(_read_json(work / "keep.json"), material_type)
    content_review = summary["content_summary"]["review"]
    cut_review = summary["cut_summary"]["review"]
    counts = lock.get("counts")
    expected_counts = {
        "words": summary["word_count"],
        "segments": summary["segment_count"],
        "rough_segments": summary["rough_segment_count"],
        "cuts": summary["cut_summary"]["cut_count"],
        "expression_micro": summary["expression_summary"]["micro"],
        "expression_residual": summary["expression_summary"]["residual"],
        **summary["decision_counts"],
    }
    if counts != expected_counts:
        raise ContractError("decision_lock.json.counts 与当前决策不一致")
    if lock.get("review") != review:
        raise ContractError("decision_lock.json.review 与 keep.review 不一致")
    if lock.get("content_review") != content_review:
        raise ContractError(
            "decision_lock.json.content_review 与 content_plan.review 不一致"
        )
    if lock.get("cut_review") != cut_review:
        raise ContractError(
            "decision_lock.json.cut_review 与 cut_review.review 不一致"
        )
    return lock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate_p = sub.add_parser("validate")
    validate_p.add_argument("work", type=Path)
    keep_p = sub.add_parser("validate-keep")
    keep_p.add_argument("work", type=Path)
    freeze_p = sub.add_parser("freeze")
    freeze_p.add_argument("work", type=Path)
    freeze_p.add_argument("--force", action="store_true")
    verify_p = sub.add_parser("verify")
    verify_p.add_argument("work", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            summary = validate_workspace(args.work)
            print(
                "✅ 决策 schema 通过："
                f"{summary['word_count']} 词 / {summary['segment_count']} 源段 / "
                f"{summary['rough_segment_count']} 粗剪段 / "
                f"{summary['card_count']} 卡 / "
                f"split_after {summary['decision_counts']['split_after']} 处"
            )
        elif args.command == "validate-keep":
            summary = validate_keep_stage(args.work)
            counts = summary["decision_counts"]
            print(
                f"✅ keep schema 通过：{summary['word_count']} 词 / "
                f"drop {counts['drop']} / fix {counts['fix']} / "
                f"retain {counts['retain']} / split_after {counts['split_after']}"
            )
        elif args.command == "freeze":
            lock = freeze(args.work, force=args.force)
            print(
                f"✅ 决策已冻结: {args.work / 'decision_lock.json'} "
                f"({lock['counts']['segments']} 段 / {lock['counts']['cuts']} 个真实切口)"
            )
        else:
            lock = verify(args.work)
            print(
                f"✅ 决策锁有效: {lock['created_at']} · "
                f"{lock['counts']['segments']} 段 / {lock['counts']['cuts']} 个真实切口"
            )
    except ContractError as exc:
        parser.exit(1, f"⛔ 决策契约失败: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
