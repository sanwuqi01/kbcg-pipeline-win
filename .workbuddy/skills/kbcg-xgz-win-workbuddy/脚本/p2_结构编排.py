#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工序② 粗剪结构编排（确定性校验 + 整段重排）

用法：p2_结构编排.py <工作目录>
输入：segments.json（p1 产出的全部保留段）
      structure.json（AI 做的结构决策）
输出：rough_segments.json

本层只做三件事：
1. 验证 sequence 覆盖全部保留段，且只有显式登记的爆点复现可出现第二次；
2. 验证 selected_hook 是候选 ID，且其全部段按声明顺序占据开头前缀；
3. 按 sequence 整段复制源段，不分割、不改字、不改源时码。

任何结构错误都非零退出，且不更新 rough_segments.json。
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
from pathlib import Path
from typing import Any


OUTPUT_META_KEY = "_rough_structure"
RESERVED_META_FIELDS = {
    "source_order",
    "sequence_order",
    "selected_hook",
    "playback_key",
    "intentional_repeat",
    "repeat_group_id",
    "repeat_occurrence",
    "body_reprise",
}
MUTATION_FIELDS = {
    "text",
    "display",
    "align",
    "words",
    "transcript",
    "audio",
    "in",
    "out",
    "in_f",
    "out_f",
    "raw_in",
    "raw_out",
    "source_in",
    "source_out",
    "start",
    "end",
    "duration",
}


class StructureError(ValueError):
    """结构输入不可安全投影到源段。"""


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise StructureError(f"缺少必需输入：{path}")
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise StructureError(
            f"JSON 格式错误：{path}:{exc.lineno}:{exc.colno} {exc.msg}"
        ) from exc


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StructureError(f"{label} 必须是非空字符串")
    return value


def _id_key(value: Any, label: str) -> tuple[str, Any]:
    # bool 是 int 的子类，若不单独拒绝，true 会与段 1 误配。
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise StructureError(f"{label} 必须是字符串或整数（不接受 bool）")
    if isinstance(value, str) and not value.strip():
        raise StructureError(f"{label} 不得为空字符串")
    # 保留 JSON 类型：整数 1 不得与字符串 "1" 混用。
    return (type(value).__name__, value)


def _segment_id(segment: dict[str, Any], index: int) -> Any:
    if "segment_id" in segment:
        value = segment["segment_id"]
    elif "part" in segment:
        value = segment["part"]
    else:
        raise StructureError(
            f"segments.json 第 {index} 段缺 segment_id/part，无法建立稳定引用"
        )
    _id_key(value, f"segments.json 第 {index} 段的 ID")
    return value


def _validate_segments(raw: Any) -> tuple[list[dict[str, Any]], dict[tuple[str, Any], int]]:
    if not isinstance(raw, list) or not raw:
        raise StructureError("segments.json 必须是非空数组（p1 产出的全部保留段）")

    segments: list[dict[str, Any]] = []
    index_by_id: dict[tuple[str, Any], int] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise StructureError(f"segments.json 第 {index} 段必须是对象")
        if OUTPUT_META_KEY in item:
            raise StructureError(
                f"segments.json 第 {index} 段已含保留字段 {OUTPUT_META_KEY}，"
                "请使用 p1 的原始 segments.json"
            )
        _nonempty_text(item.get("text"), f"segments.json 第 {index} 段 text")
        start, end = item.get("in"), item.get("out")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(start)
            or not math.isfinite(end)
            or end <= start
        ):
            raise StructureError(
                f"segments.json 第 {index} 段源时码非法：in={start!r}, out={end!r}"
            )

        sid = _segment_id(item, index)
        key = _id_key(sid, f"segments.json 第 {index} 段的 ID")
        if key in index_by_id:
            raise StructureError(
                f"segments.json 存在重复 ID {sid!r}（第 {index_by_id[key]} 与 {index} 段）"
            )
        index_by_id[key] = index
        segments.append(item)
    return segments, index_by_id


def _reject_mutation_fields(item: dict[str, Any], label: str) -> None:
    hits = sorted(MUTATION_FIELDS.intersection(item))
    if hits:
        raise StructureError(
            f"{label} 含禁止字段 {hits}；结构层只能引用整段，"
            "不得提供或改写文本/源时码/词轴"
        )


def _validate_structure(
    raw: Any,
    source_index: dict[tuple[str, Any], int],
) -> tuple[
    dict[str, str],
    Any,
    list[Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    if not isinstance(raw, dict):
        raise StructureError("structure.json 顶层必须是对象")

    context = {
        key: _nonempty_text(raw.get(key), f"structure.json.{key}")
        for key in ("goal", "audience", "main_claim")
    }

    candidates = raw.get("hook_candidates")
    if not isinstance(candidates, list) or not candidates:
        raise StructureError("structure.json.hook_candidates 必须是非空数组")
    candidates_by_id: dict[tuple[str, Any], dict[str, Any]] = {}
    for index, candidate in enumerate(candidates):
        label = f"hook_candidates[{index}]"
        if not isinstance(candidate, dict):
            raise StructureError(f"{label} 必须是对象")
        _reject_mutation_fields(candidate, label)

        has_new_ids = "segment_ids" in candidate
        has_old_id = "segment_id" in candidate
        if has_new_ids and has_old_id:
            raise StructureError(f"{label} 不得同时使用 segment_ids 和旧字段 segment_id")
        if has_new_ids:
            segment_ids = candidate["segment_ids"]
            if not isinstance(segment_ids, list) or not segment_ids:
                raise StructureError(f"{label}.segment_ids 必须是非空数组")
            if "candidate_id" not in candidate:
                raise StructureError(f"{label} 使用 segment_ids 时必须提供 candidate_id")
            candidate_id = candidate["candidate_id"]
        elif has_old_id:
            # 旧格式兼容：单段 segment_id 同时充当候选 ID。
            segment_ids = [candidate["segment_id"]]
            candidate_id = candidate.get("candidate_id", candidate["segment_id"])
        else:
            raise StructureError(f"{label} 缺 segment_ids（或兼容用的单段 segment_id）")

        candidate_key = _id_key(candidate_id, f"{label}.candidate_id")
        if candidate_key in candidates_by_id:
            raise StructureError(f"hook_candidates 存在重复 candidate_id {candidate_id!r}")

        segment_keys: list[tuple[str, Any]] = []
        seen_segment_keys: set[tuple[str, Any]] = set()
        for segment_order, segment_id in enumerate(segment_ids):
            segment_label = f"{label}.segment_ids[{segment_order}]"
            segment_key = _id_key(segment_id, segment_label)
            if segment_key not in source_index:
                raise StructureError(f"{segment_label} 引用不存在的段 {segment_id!r}")
            if segment_key in seen_segment_keys:
                raise StructureError(f"{label}.segment_ids 重复引用段 {segment_id!r}")
            seen_segment_keys.add(segment_key)
            segment_keys.append(segment_key)
        candidates_by_id[candidate_key] = {
            "candidate_id": candidate_id,
            "segment_ids": list(segment_ids),
            "segment_keys": segment_keys,
        }
        # 强度与未闭合性由语义决策层判定；本层要求留下可审计理由。
        _nonempty_text(candidate.get("strength_reason"), f"{label}.strength_reason")
        _nonempty_text(candidate.get("open_loop_reason"), f"{label}.open_loop_reason")
        if "hook_type" in candidate:
            _nonempty_text(candidate["hook_type"], f"{label}.hook_type")

    if "selected_hook" not in raw:
        raise StructureError("structure.json 缺 selected_hook")
    selected_hook = raw["selected_hook"]
    selected_key = _id_key(selected_hook, "structure.json.selected_hook")
    if selected_key not in candidates_by_id:
        raise StructureError("selected_hook 必须是 hook_candidates 中的一个 candidate_id")
    selected_candidate = candidates_by_id[selected_key]

    repeats_raw = raw.get("intentional_repeats", [])
    if not isinstance(repeats_raw, list):
        raise StructureError("structure.json.intentional_repeats 必须是数组")
    repeat_groups: list[dict[str, Any]] = []
    repeat_keys: dict[tuple[str, Any], str] = {}
    repeat_ids: set[str] = set()
    selected_hook_keys = selected_candidate["segment_keys"]
    for index, item in enumerate(repeats_raw):
        label = f"intentional_repeats[{index}]"
        if not isinstance(item, dict):
            raise StructureError(f"{label} 必须是对象")
        allowed = {
            "repeat_id",
            "segment_ids",
            "opening_function",
            "body_function",
            "deletion_impact",
        }
        extra = sorted(set(item) - allowed)
        if extra:
            raise StructureError(f"{label} 含未知字段 {extra}")
        repeat_id = _nonempty_text(item.get("repeat_id"), f"{label}.repeat_id").strip()
        if repeat_id in repeat_ids:
            raise StructureError(f"intentional_repeats 重复 repeat_id {repeat_id!r}")
        repeat_ids.add(repeat_id)
        for field in ("opening_function", "body_function", "deletion_impact"):
            _nonempty_text(item.get(field), f"{label}.{field}")
        segment_ids = item.get("segment_ids")
        if not isinstance(segment_ids, list) or not segment_ids:
            raise StructureError(f"{label}.segment_ids 必须是非空数组")
        group_keys: list[tuple[str, Any]] = []
        for order, segment_id in enumerate(segment_ids):
            key = _id_key(segment_id, f"{label}.segment_ids[{order}]")
            if key not in source_index:
                raise StructureError(f"{label} 引用不存在的段 {segment_id!r}")
            if key not in selected_hook_keys:
                raise StructureError(
                    f"{label} 只能复现 selected_hook 内的段：{segment_id!r}"
                )
            if key in group_keys:
                raise StructureError(f"{label}.segment_ids 重复 {segment_id!r}")
            if key in repeat_keys:
                raise StructureError(
                    f"段 {segment_id!r} 已属于复现组 {repeat_keys[key]!r}"
                )
            group_keys.append(key)
        start = selected_hook_keys.index(group_keys[0])
        if selected_hook_keys[start:start + len(group_keys)] != group_keys:
            raise StructureError(
                f"{label}.segment_ids 必须按 Hook 原顺序构成连续片段"
            )
        for key in group_keys:
            repeat_keys[key] = repeat_id
        repeat_groups.append(
            {
                "repeat_id": repeat_id,
                "segment_ids": list(segment_ids),
                "segment_keys": group_keys,
                "opening_function": item["opening_function"],
                "body_function": item["body_function"],
                "deletion_impact": item["deletion_impact"],
            }
        )

    sequence = raw.get("sequence")
    if not isinstance(sequence, list) or not sequence:
        raise StructureError("structure.json.sequence 必须是非空数组")

    normalized: list[dict[str, Any]] = []
    seen: dict[tuple[str, Any], list[int]] = {}
    for index, entry in enumerate(sequence):
        label = f"sequence[{index}]"
        if not isinstance(entry, dict):
            raise StructureError(f"{label} 必须是对象")
        _reject_mutation_fields(entry, label)
        if "segment_id" not in entry:
            raise StructureError(f"{label} 缺 segment_id")
        sid = entry["segment_id"]
        key = _id_key(sid, f"{label}.segment_id")
        if key not in source_index:
            raise StructureError(f"{label} 引用不存在的段 {sid!r}")
        occurrences = seen.setdefault(key, [])
        allowed_count = 2 if key in repeat_keys else 1
        if len(occurrences) >= allowed_count:
            if key in repeat_keys:
                raise StructureError(
                    f"已登记复现的段 {sid!r} 也只能出现两次"
                )
            raise StructureError(
                f"sequence 重复使用未登记的段 {sid!r}"
            )
        occurrences.append(index)
        role = _nonempty_text(entry.get("role"), f"{label}.role")
        reason = _nonempty_text(entry.get("reason"), f"{label}.reason")
        reserved = sorted(RESERVED_META_FIELDS.intersection(entry))
        if reserved:
            raise StructureError(f"{label} 使用了保留注释字段 {reserved}")
        annotations = {
            key_: copy.deepcopy(value)
            for key_, value in entry.items()
            if key_ not in {"segment_id", "role", "reason"}
        }
        normalized.append(
            {
                "segment_id": sid,
                "role": role,
                "reason": reason,
                "annotations": annotations,
            }
        )

    source_keys = set(source_index)
    sequence_keys = set(seen)
    expected_length = len(source_index) + len(repeat_keys)
    if len(sequence) != expected_length or sequence_keys != source_keys:
        missing = [
            repr(_segment_id_from_key(key))
            for key in source_index
            if key not in sequence_keys
        ]
        raise StructureError(
            "sequence 必须覆盖全部保留 segments；未登记段息仅一次，"
            "登记的爆点复现段息恰好两次；"
            f"源段 {len(source_index)} 个，应播放 {expected_length} 段，"
            f"sequence {len(sequence)} 个，遗漏 {missing or '无'}"
        )

    for key, repeat_id in repeat_keys.items():
        if len(seen.get(key, [])) != 2:
            raise StructureError(
                f"复现组 {repeat_id!r} 的段 {_segment_id_from_key(key)!r} "
                "必须在 sequence 中恰好出现两次"
            )

    expected_prefix = selected_candidate["segment_keys"]
    actual_prefix = [
        _id_key(item["segment_id"], f"sequence[{index}].segment_id")
        for index, item in enumerate(normalized[:len(expected_prefix)])
    ]
    if actual_prefix != expected_prefix:
        expected_ids = selected_candidate["segment_ids"]
        actual_ids = [item["segment_id"] for item in normalized[:len(expected_prefix)]]
        if len(actual_prefix) == len(expected_prefix) and set(actual_prefix) == set(expected_prefix):
            raise StructureError(
                f"selected_hook {selected_hook!r} 的多段 Hook 前缀顺序错误；"
                f"期望 {expected_ids!r}，实际 {actual_ids!r}"
            )
        raise StructureError(
            f"selected_hook {selected_hook!r} 的全部 segment_ids 必须按声明顺序"
            f"完整占据 sequence 前缀；期望 {expected_ids!r}，实际 {actual_ids!r}"
        )

    hook_length = len(expected_prefix)
    for group in repeat_groups:
        second_positions = [seen[key][1] for key in group["segment_keys"]]
        expected_positions = list(
            range(second_positions[0], second_positions[0] + len(second_positions))
        )
        if second_positions != expected_positions or second_positions[0] < hook_length:
            raise StructureError(
                f"复现组 {group['repeat_id']!r} 的第二次出现必须在 Hook 前缀之后，"
                "并保持原顺序连续播放"
            )

    # 源时序倒退不能仅靠 sequence 中泛化的 reason 混过去。Hook 前缀、
    # Hook 回到正文和已登记复现已有专门契约；其余倒序必须留下独立审计。
    occurrence_counts: dict[tuple[str, Any], int] = {}
    previous_source_order: int | None = None
    for index, item in enumerate(normalized):
        key = _id_key(item["segment_id"], f"sequence[{index}].segment_id")
        occurrence_counts[key] = occurrence_counts.get(key, 0) + 1
        source_order = source_index[key]
        is_inversion = (
            previous_source_order is not None and source_order < previous_source_order
        )
        is_hook_prefix = index < hook_length
        is_hook_body_return = index == hook_length
        is_registered_repeat = key in repeat_keys and occurrence_counts[key] == 2
        exception = item["annotations"].get("source_order_exception")
        needs_exception = (
            is_inversion
            and not is_hook_prefix
            and not is_hook_body_return
            and not is_registered_repeat
        )
        if needs_exception:
            if not isinstance(exception, dict) or set(exception) != {
                "editing_function", "truth_boundary", "listen_check"
            }:
                raise StructureError(
                    f"sequence[{index}] 发生非 Hook/复现的源时序倒退，必须提供 "
                    "source_order_exception.editing_function/truth_boundary/listen_check"
                )
            for field in ("editing_function", "truth_boundary", "listen_check"):
                _nonempty_text(
                    exception.get(field),
                    f"sequence[{index}].source_order_exception.{field}",
                )
        elif exception is not None:
            raise StructureError(
                f"sequence[{index}].source_order_exception 只能用于非 Hook/复现的源时序倒退"
            )
        previous_source_order = source_order

    return (
        context,
        selected_hook,
        selected_candidate["segment_ids"],
        normalized,
        repeat_groups,
    )


def _segment_id_from_key(key: tuple[str, Any]) -> Any:
    return key[1]


def compile_rough_segments(
    segments: list[dict[str, Any]],
    source_index: dict[tuple[str, Any], int],
    sequence: list[dict[str, Any]],
    selected_hook: Any,
    selected_hook_segment_ids: list[Any],
    repeat_groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    hook_segment_keys = [
        _id_key(segment_id, f"selected_hook.segment_ids[{index}]")
        for index, segment_id in enumerate(selected_hook_segment_ids)
    ]
    hook_order_by_key = {key: index for index, key in enumerate(hook_segment_keys)}
    repeat_group_by_key = {
        key: group["repeat_id"]
        for group in repeat_groups
        for key in group["segment_keys"]
    }
    occurrence_by_key: dict[tuple[str, Any], int] = {}
    for order, decision in enumerate(sequence):
        key = _id_key(decision["segment_id"], f"sequence[{order}].segment_id")
        source_order = source_index[key]
        source = segments[source_order]
        occurrence = occurrence_by_key.get(key, 0) + 1
        occurrence_by_key[key] = occurrence
        line, part = source.get("line"), source.get("part")
        if not isinstance(line, str) or isinstance(part, bool) or not isinstance(part, int):
            raise StructureError(
                f"segments.json 第 {source_order} 段缺合法 line/part，无法生成播放 key"
            )
        base_playback_key = f"{line}.{part}"
        playback_key = base_playback_key if occurrence == 1 else f"{base_playback_key}@{occurrence}"
        is_hook_prefix = order < len(hook_segment_keys) and key == hook_segment_keys[order]
        repeat_group_id = repeat_group_by_key.get(key)
        projected = copy.deepcopy(source)
        meta = copy.deepcopy(decision["annotations"])
        meta.update({
            "segment_id": decision["segment_id"],
            "source_order": source_order,
            "sequence_order": order,
            "role": decision["role"],
            "reason": decision["reason"],
            "selected_hook": is_hook_prefix,
            "hook_candidate_id": selected_hook if is_hook_prefix else None,
            "hook_segment_order": hook_order_by_key.get(key) if is_hook_prefix else None,
            "playback_key": playback_key,
            "intentional_repeat": repeat_group_id is not None,
            "repeat_group_id": repeat_group_id,
            "repeat_occurrence": occurrence if repeat_group_id is not None else None,
            "body_reprise": repeat_group_id is not None and occurrence == 2,
        })
        projected[OUTPUT_META_KEY] = meta

        # 防未来修改不小心动到媒体字段：在写盘前再做一次不变式断言。
        for field in MUTATION_FIELDS:
            if field in source and projected.get(field) != source[field]:
                raise StructureError(f"内部错误：投影时改动了源段字段 {field}")
        output.append(projected)
    return output


def _write_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def run(workdir: Path) -> Path:
    if not workdir.is_dir():
        raise StructureError(f"工作目录不存在：{workdir}")
    segments_raw = _load_json(workdir / "segments.json")
    structure_raw = _load_json(workdir / "structure.json")
    segments, source_index = _validate_segments(segments_raw)
    _context, selected_hook, hook_segment_ids, sequence, repeat_groups = _validate_structure(
        structure_raw, source_index
    )
    output = compile_rough_segments(
        segments,
        source_index,
        sequence,
        selected_hook,
        hook_segment_ids,
        repeat_groups,
    )
    output_path = workdir / "rough_segments.json"
    _write_atomic(output_path, output)
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验 structure.json 并将 segments.json 按完整语义段重排"
    )
    parser.add_argument("工作目录", type=Path)
    args = parser.parse_args(argv)
    workdir: Path = getattr(args, "工作目录")
    try:
        output_path = run(workdir)
    except (OSError, StructureError) as exc:
        print(f"⛔ p2 结构编排中止：{exc}", file=sys.stderr)
        return 1

    result = _load_json(output_path)
    hook_meta = [
        item[OUTPUT_META_KEY] for item in result if item[OUTPUT_META_KEY]["selected_hook"]
    ]
    hook_id = hook_meta[0]["hook_candidate_id"]
    hook_segments = [item["segment_id"] for item in hook_meta]
    print(
        f"结构编排：{len(result)} 段 · hook={hook_id!r} {hook_segments!r}"
        f" · 已写入 {output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
