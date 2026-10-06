#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基础字幕分卡的不可拆单元合同。

语义断句仍由 Agent 逐句判断；本模块只把任何情况下都不应发生的机械拆分
变成硬错误，避免“文字守恒、字数没超”却把词和英文专名拆坏后仍通过验收。
"""
from __future__ import annotations

import re
from typing import Iterable


ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[ ._+&/-][A-Za-z0-9]+)*")


class CaptionBoundaryError(ValueError):
    """字幕卡边界破坏不可拆单元。"""


def card_boundaries(texts: Iterable[str]) -> list[int]:
    rows = list(texts)
    out: list[int] = []
    cursor = 0
    for text in rows[:-1]:
        cursor += len(text)
        out.append(cursor)
    return out


def atomic_spans(segment_text: str, word_texts: Iterable[str]) -> list[tuple[int, int, str]]:
    """返回所有不可拆跨度：声学词 token 与连续英文/数字专名。"""
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for word in word_texts:
        start = cursor
        cursor += len(word)
        if len(word) > 1:
            spans.append((start, cursor, word))
    if cursor != len(segment_text):
        raise CaptionBoundaryError(
            f"词序列长度 {cursor} 与段文本长度 {len(segment_text)} 不一致"
        )
    for match in ASCII_TOKEN_RE.finditer(segment_text):
        if match.end() - match.start() > 1:
            spans.append((match.start(), match.end(), match.group(0)))
    return spans


def validate_atomic_boundaries(
    segment_text: str,
    word_texts: Iterable[str],
    card_texts: Iterable[str],
    *,
    label: str = "字幕",
) -> None:
    """拒绝落在不可拆词、英文专名或数字内部的字幕边界。"""
    cards = list(card_texts)
    if "".join(cards) != segment_text:
        raise CaptionBoundaryError(f"{label} 文本不守恒")
    spans = atomic_spans(segment_text, word_texts)
    for boundary in card_boundaries(cards):
        for start, end, token in spans:
            if start < boundary < end:
                left = segment_text[max(0, boundary - 8):boundary]
                right = segment_text[boundary:min(len(segment_text), boundary + 8)]
                raise CaptionBoundaryError(
                    f"{label} 在不可拆单元「{token}」内部断开："
                    f"「{left}|{right}」"
                )
