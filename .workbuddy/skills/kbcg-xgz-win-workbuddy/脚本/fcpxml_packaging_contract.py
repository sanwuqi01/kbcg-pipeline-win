#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Final Cut P5 包装的显式授权契约。

P2 的 keyword 与 captions_plan.title 都只是候选。只有本契约中逐项登记的
重点字幕和顶部标题，才允许进入最终 FCPXML。
"""
from __future__ import annotations

import json
from pathlib import Path


MAX_HIGHLIGHT_RATIO = 0.06


def _identity(card: dict) -> tuple[int, int]:
    return int(card["si"]), int(card["card_index"])


def load_final_cut_packaging(work: Path, cards: list[dict]) -> dict:
    path = Path(work) / "final_cut_packaging.json"
    empty = {
        "path": None,
        "selected": {},
        "ratio": 0.0,
        "top_title": None,
    }
    if not path.is_file():
        return empty

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "final-cut-packaging@1":
        raise ValueError(
            "final_cut_packaging.json schema 必须为 final-cut-packaging@1"
        )
    if payload.get("delivery_branch") != "final-cut":
        raise ValueError("包装计划不属于 Final Cut 分支")

    by_id = {_identity(card): card for card in cards}
    if len(by_id) != len(cards):
        raise ValueError("字幕卡身份重复，无法绑定 Final Cut 包装决策")

    selected: dict[tuple[int, int], dict] = {}
    for row in payload.get("highlight_cards") or []:
        ident = (int(row.get("si", -1)), int(row.get("card_index", -1)))
        if ident in selected:
            raise ValueError(f"重点字幕重复: {ident}")
        card = by_id.get(ident)
        if card is None:
            raise ValueError(f"重点字幕不存在: {ident}")
        if row.get("text") != card.get("text"):
            raise ValueError(f"重点字幕文字漂移: {ident}")
        keyword = str(row.get("keyword") or "").strip()
        if not keyword or keyword not in str(card.get("text") or ""):
            raise ValueError(f"重点词不在字幕中: {ident} {keyword!r}")
        reason = str(row.get("reason") or "").strip()
        if not reason:
            raise ValueError(f"重点字幕缺少语义理由: {ident}")
        selected[ident] = {"keyword": keyword, "reason": reason}

    ratio = len(selected) / len(cards) if cards else 0.0
    if ratio > MAX_HIGHLIGHT_RATIO + 1e-12:
        raise ValueError(
            f"重点字幕密度 {ratio:.1%} 超过 {MAX_HIGHLIGHT_RATIO:.0%}；"
            "不得把 P2 keyword 候选全部上色"
        )

    top_title = payload.get("top_title")
    if top_title is not None:
        if not isinstance(top_title, dict):
            raise ValueError("top_title 必须是对象")
        title_text = str(top_title.get("text") or "").strip()
        title_reason = str(top_title.get("reason") or "").strip()
        if not title_text:
            raise ValueError("top_title.text 不能为空")
        if not title_reason:
            raise ValueError("top_title.reason 不能为空")
        top_title = {"text": title_text, "reason": title_reason}

    return {
        "path": path,
        "selected": selected,
        "ratio": ratio,
        "top_title": top_title,
    }
