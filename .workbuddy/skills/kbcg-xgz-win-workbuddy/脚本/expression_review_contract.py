#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口语微成分、词轴外残声与可压缩停顿的候选发现、校验契约。"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


SCHEMA = "kbcg-xgz/expression_review@2"
PAUSE_CANDIDATE_MIN_S = 0.30
MICRO_TERMS = {
    "呃", "额", "嗯", "啊", "呀", "哦", "诶", "唉", "呢", "吧", "嘛",
    "对吧", "是吧", "这个", "那个", "就是", "然后", "那么", "其实",
    "所以", "但是", "好", "OK", "ok",
}
CORRECTION_MARKERS = {
    "不是", "不对", "应该说", "准确来说", "换句话说", "或者说", "也就是说",
}
FUNCTIONS = {
    "pure_filler", "thinking_rhythm", "emphasis", "emotion", "transition",
    "question_rhythm", "proposition_correction", "dependent_fragment",
    "lexical_content",
}
MICRO_DECISIONS = {"drop", "retain", "not_micro"}
CAPTION_ACTIONS = {"show", "hide", "normalize", "not_applicable"}
RESIDUAL_DECISIONS = {"filler_drop", "breath_noise", "restore_text"}
PAUSE_DECISIONS = {"compress", "retain"}
PAUSE_FUNCTIONS = {
    "ineffective_wait", "thinking", "sentence_boundary", "transition",
    "emphasis", "emotion", "action", "breath",
}


class ExpressionReviewError(ValueError):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _clean(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    return re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()\[\]]+", "", text)


def _word_text(word: dict[str, Any]) -> str:
    aligned = word.get("align_text")
    return str(aligned if isinstance(aligned, str) and aligned else word.get("t", ""))


def _context(words: list[dict[str, Any]], index: int, radius: int = 3) -> tuple[str, str]:
    before = "".join(_word_text(words[i]) for i in range(max(0, index - radius), index))
    after = "".join(
        _word_text(words[i])
        for i in range(index + 1, min(len(words), index + radius + 1))
    )
    return before, after


def _fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def discover_micro_candidates(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    source_texts = [_word_text(word) for word in words]
    cleaned = [_clean(text) for text in source_texts]
    for index, text in enumerate(cleaned):
        candidate_type = None
        if text in MICRO_TERMS:
            candidate_type = "micro_term"
        elif text in CORRECTION_MARKERS:
            candidate_type = "self_correction_marker"
        elif text and index > 0 and text == cleaned[index - 1]:
            candidate_type = "immediate_repeat"
        if not candidate_type or index in seen:
            continue
        seen.add(index)
        before, after = _context(words, index)
        word = words[index]
        fingerprint = _fingerprint(
            {
                "candidate_type": candidate_type,
                "word_index": index,
                "text": source_texts[index],
                "start": word.get("ws"),
                "end": word.get("we"),
                "context_before": before,
                "context_after": after,
            }
        )
        rows.append(
            {
                "candidate_id": f"M{index:06d}-{fingerprint[:12]}",
                "context_fingerprint": fingerprint,
                "candidate_type": candidate_type,
                "word_index": index,
                "text": source_texts[index],
                "start": word.get("ws"),
                "end": word.get("we"),
                "context_before": before,
                "context_after": after,
                "decision": "pending",
                "function": "pending",
                "caption_action": "pending",
                "reason": "",
                "acoustic_review": "pending",
            }
        )
    return rows


def _drop_mask(keep: dict[str, Any], word_count: int) -> list[bool]:
    dropped = [False] * word_count
    for start, end in _span_rows(keep, "drop"):
        if start < 0 or end >= word_count:
            raise ExpressionReviewError("keep.drop 越出词轴")
        for index in range(start, end + 1):
            dropped[index] = True
    return dropped


def discover_pause_candidates(
    words: list[dict[str, Any]], keep: dict[str, Any]
) -> list[dict[str, Any]]:
    """只发现相邻保留词之间的可审停顿；发现阈值不等于自动删除阈值。"""
    dropped = _drop_mask(keep, len(words))
    kept = [index for index in range(len(words)) if not dropped[index]]
    rows: list[dict[str, Any]] = []
    for left, right in zip(kept, kept[1:]):
        if right != left + 1:
            continue
        try:
            gap = float(words[right].get("vs", words[right].get("ws"))) - float(
                words[left].get("ve", words[left].get("we"))
            )
        except (TypeError, ValueError):
            raise ExpressionReviewError(f"词 {left}→{right} 缺少可用声学时码")
        if gap + 1e-9 < PAUSE_CANDIDATE_MIN_S:
            continue
        before, _ = _context(words, left)
        _, after = _context(words, right)
        payload = {
            "left_word_index": left,
            "right_word_index": right,
            "left_text": _word_text(words[left]),
            "right_text": _word_text(words[right]),
            "left_end": words[left].get("ve", words[left].get("we")),
            "right_start": words[right].get("vs", words[right].get("ws")),
            "source_gap_s": round(gap, 6),
            "context_before": before,
            "context_after": after,
        }
        fingerprint = _fingerprint(payload)
        rows.append(
            {
                "candidate_id": f"P{left:06d}-{right:06d}-{fingerprint[:12]}",
                "context_fingerprint": fingerprint,
                **payload,
                "decision": "pending",
                "function": "pending",
                "reason": "",
                "acoustic_review": "pending",
            }
        )
    return rows


def discover_residual_candidates(track: dict[str, Any]) -> list[dict[str, Any]]:
    raw = track.get("residual_candidates", [])
    if not isinstance(raw, list):
        raise ExpressionReviewError("word_track.residual_candidates 必须是数组")
    rows = []
    for pos, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ExpressionReviewError(f"residual_candidates[{pos}] 必须是对象")
        rows.append(
            {
                **item,
                "decision": "pending",
                "recognized_text": "",
                "reason": "",
                "acoustic_review": "pending",
            }
        )
    return rows


def build_review(
    track_path: Path, track: dict[str, Any], keep_path: Path, keep: dict[str, Any]
) -> dict[str, Any]:
    words = track.get("words")
    if not isinstance(words, list) or not words:
        raise ExpressionReviewError("word_track.words 必须是非空数组")
    return {
        "schema": SCHEMA,
        "word_track_sha256": sha256(track_path),
        "keep_sha256": sha256(keep_path),
        "candidate_policy": (
            "词表只发现候选，不决定删留；必须听原音并按句法、命题、情绪和上下文逐项裁决"
        ),
        "micro_candidates": discover_micro_candidates(words),
        "residual_candidates": discover_residual_candidates(track),
        "pause_candidates": discover_pause_candidates(words, keep),
    }


def _span_rows(keep: dict[str, Any], kind: str) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    rows = keep.get(kind, [])
    if not isinstance(rows, list):
        raise ExpressionReviewError(f"keep.{kind} 必须是数组")
    for pos, item in enumerate(rows):
        if not isinstance(item, list) or len(item) < 2 or not all(
            isinstance(value, int) and not isinstance(value, bool) for value in item[:2]
        ):
            raise ExpressionReviewError(f"keep.{kind}[{pos}] 区间非法")
        result.append((item[0], item[1]))
    return result


def _spans(keep: dict[str, Any], kind: str) -> set[int]:
    result: set[int] = set()
    for start, end in _span_rows(keep, kind):
        result.update(range(start, end + 1))
    return result


def validate_review(
    data: Any,
    *,
    track_path: Path,
    track: dict[str, Any],
    keep: dict[str, Any],
) -> dict[str, int]:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ExpressionReviewError(f"expression_review.schema 必须是 {SCHEMA}")
    if data.get("word_track_sha256") != sha256(track_path):
        raise ExpressionReviewError("expression_review 与当前 word_track 哈希不一致，请重新生成并复核")
    keep_path = track_path.parent / "keep.json"
    if data.get("keep_sha256") != sha256(keep_path):
        raise ExpressionReviewError("expression_review 与当前 keep 哈希不一致，请重新生成并复核")
    expected_micro = discover_micro_candidates(track.get("words", []))
    actual_micro = data.get("micro_candidates")
    if not isinstance(actual_micro, list):
        raise ExpressionReviewError("expression_review.micro_candidates 必须是数组")
    expected_ids = [row["candidate_id"] for row in expected_micro]
    actual_ids = [row.get("candidate_id") for row in actual_micro if isinstance(row, dict)]
    if actual_ids != expected_ids or len(actual_ids) != len(actual_micro):
        raise ExpressionReviewError("口语候选集合与当前词轴不一致，不得漏审、增删或换序")
    drop_words = _spans(keep, "drop")
    retain_words = _spans(keep, "retain")
    for pos, (actual, expected) in enumerate(zip(actual_micro, expected_micro)):
        label = f"micro_candidates[{pos}]"
        for field in (
            "context_fingerprint", "candidate_type", "word_index", "text", "start", "end",
            "context_before", "context_after",
        ):
            if actual.get(field) != expected[field]:
                raise ExpressionReviewError(f"{label}.{field} 与候选真值不一致")
        decision = actual.get("decision")
        if decision not in MICRO_DECISIONS:
            raise ExpressionReviewError(f"{label}.decision 尚未裁决")
        if actual.get("function") not in FUNCTIONS:
            raise ExpressionReviewError(f"{label}.function 尚未按语用功能裁决")
        if actual.get("caption_action") not in CAPTION_ACTIONS:
            raise ExpressionReviewError(f"{label}.caption_action 尚未裁决")
        if actual.get("acoustic_review") != "heard":
            raise ExpressionReviewError(f"{label} 必须真实听审并写 acoustic_review=heard")
        if not isinstance(actual.get("reason"), str) or not actual["reason"].strip():
            raise ExpressionReviewError(f"{label}.reason 不得为空")
        word_index = expected["word_index"]
        if decision == "drop" and word_index not in drop_words:
            raise ExpressionReviewError(f"{label} 决定 drop，但 keep.drop 没有覆盖词 {word_index}")
        if decision == "retain" and word_index not in retain_words:
            raise ExpressionReviewError(f"{label} 决定 retain，但 keep.retain 没有覆盖词 {word_index}")
        if decision == "not_micro" and word_index in drop_words:
            raise ExpressionReviewError(f"{label} 认定为有效内容，却被 keep.drop 删除")
        caption_action = actual["caption_action"]
        if decision == "drop" and caption_action != "not_applicable":
            raise ExpressionReviewError(f"{label} 已删声音，caption_action 必须是 not_applicable")
        if decision in {"retain", "not_micro"} and caption_action not in {"show", "normalize"}:
            raise ExpressionReviewError(f"{label} 保留声音必须显示或规范化字幕，不得隐藏")

    expected_residual = discover_residual_candidates(track)
    actual_residual = data.get("residual_candidates")
    if not isinstance(actual_residual, list):
        raise ExpressionReviewError("expression_review.residual_candidates 必须是数组")
    expected_residual_ids = [row.get("residual_id") for row in expected_residual]
    actual_residual_ids = [
        row.get("residual_id") for row in actual_residual if isinstance(row, dict)
    ]
    if actual_residual_ids != expected_residual_ids or len(actual_residual_ids) != len(actual_residual):
        raise ExpressionReviewError("词轴外残声候选集合与当前词轴不一致")
    for pos, (actual, expected) in enumerate(zip(actual_residual, expected_residual)):
        label = f"residual_candidates[{pos}]"
        for field in (
            "residual_id", "block_index", "start", "end", "duration",
            "next_word_index", "next_word_text",
        ):
            if actual.get(field) != expected[field]:
                raise ExpressionReviewError(f"{label}.{field} 与残声真值不一致")
        decision = actual.get("decision")
        if decision not in RESIDUAL_DECISIONS:
            raise ExpressionReviewError(f"{label}.decision 尚未裁决")
        if actual.get("acoustic_review") != "heard":
            raise ExpressionReviewError(f"{label} 必须真实听审并写 acoustic_review=heard")
        if not isinstance(actual.get("reason"), str) or not actual["reason"].strip():
            raise ExpressionReviewError(f"{label}.reason 不得为空")
        if decision == "restore_text":
            text = actual.get("recognized_text")
            if not isinstance(text, str) or not text.strip():
                raise ExpressionReviewError(f"{label}.recognized_text 不得为空")
            raise ExpressionReviewError(
                f"{label} 是有意义漏字：必须先修回词轴并重跑对齐/候选生成，禁止带病冻结"
            )

    expected_pause = discover_pause_candidates(track.get("words", []), keep)
    actual_pause = data.get("pause_candidates")
    if not isinstance(actual_pause, list):
        raise ExpressionReviewError("expression_review.pause_candidates 必须是数组")
    expected_pause_ids = [row["candidate_id"] for row in expected_pause]
    actual_pause_ids = [
        row.get("candidate_id") for row in actual_pause if isinstance(row, dict)
    ]
    if actual_pause_ids != expected_pause_ids or len(actual_pause_ids) != len(actual_pause):
        raise ExpressionReviewError("停顿候选集合与当前词轴/keep 不一致，不得漏审或换序")
    for pos, (actual, expected) in enumerate(zip(actual_pause, expected_pause)):
        label = f"pause_candidates[{pos}]"
        for field in (
            "context_fingerprint", "left_word_index", "right_word_index",
            "left_text", "right_text", "left_end", "right_start", "source_gap_s",
            "context_before", "context_after",
        ):
            if actual.get(field) != expected[field]:
                raise ExpressionReviewError(f"{label}.{field} 与停顿候选真值不一致")
        if actual.get("decision") not in PAUSE_DECISIONS:
            raise ExpressionReviewError(f"{label}.decision 尚未裁决 compress/retain")
        if actual.get("function") not in PAUSE_FUNCTIONS:
            raise ExpressionReviewError(f"{label}.function 尚未按停顿功能裁决")
        if actual.get("acoustic_review") != "heard":
            raise ExpressionReviewError(f"{label} 必须真实听审并写 acoustic_review=heard")
        if not isinstance(actual.get("reason"), str) or not actual["reason"].strip():
            raise ExpressionReviewError(f"{label}.reason 不得为空")
        if actual["decision"] == "compress" and actual["function"] != "ineffective_wait":
            raise ExpressionReviewError(
                f"{label} 只有判定为 ineffective_wait 才能压缩；"
                "思考、强调、情绪、转折、动作或呼吸功能必须保留"
            )
    return {
        "micro": len(actual_micro),
        "residual": len(actual_residual),
        "pause": len(actual_pause),
        "pause_compress": sum(row["decision"] == "compress" for row in actual_pause),
    }


def pause_compress_boundaries(data: dict[str, Any]) -> set[int]:
    """返回已听审并决定压缩的左侧词下标。"""
    return {
        int(row["left_word_index"])
        for row in data.get("pause_candidates", [])
        if isinstance(row, dict) and row.get("decision") == "compress"
    }
