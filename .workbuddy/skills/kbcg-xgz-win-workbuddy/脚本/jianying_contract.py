#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剪映原生草稿的独立交付合同。

这里只存放剪映分支的精确画幅样式和字幕投影规则，不与 Final Cut
或“人工粗剪 XML 只加字幕”共享可变参数。
"""
from __future__ import annotations

import json
from pathlib import Path

from media_contract import analysis_to_native, rate, round_fraction


# 字幕字体是**可配置项**，不再硬编码。
# 真值链：p0_建工作区 --font → config.json 的 font 字段 → 生成/验收都读它。
# 字体表（711 个剪映字体）由 脚本_win/gen_font_table.py 从 CapCutMate 固化而来，
# 这样管线解释器不需要 import CapCutMate 也能解析字体名 → 资源 id。
DEFAULT_FONT_NAME = "新青年体"
FONT_TABLE_PATH = Path(__file__).resolve().parent.parent / "字体" / "剪映字体表.json"
_FONT_TABLE: dict[str, dict] | None = None

PURE_WHITE = (1.0, 1.0, 1.0)
WARM_GOLD = (1.0, 0.72, 0.24)
MAX_HIGHLIGHT_RATIO = 0.06


def font_table() -> dict[str, dict]:
    """加载随 skill 走的剪映字体表（字体名 → member / resource_id / is_vip）。"""
    global _FONT_TABLE
    if _FONT_TABLE is None:
        if not FONT_TABLE_PATH.is_file():
            raise ValueError(
                f"剪映字体表缺失: {FONT_TABLE_PATH}；"
                "请运行 脚本_win/gen_font_table.py 从 CapCutMate 生成"
            )
        payload = json.loads(FONT_TABLE_PATH.read_text(encoding="utf-8"))
        if payload.get("schema") != "jianying-font-table@1":
            raise ValueError("剪映字体表 schema 必须为 jianying-font-table@1")
        fonts = payload.get("fonts")
        if not isinstance(fonts, dict) or not fonts:
            raise ValueError("剪映字体表为空")
        _FONT_TABLE = fonts
    return _FONT_TABLE


def resolve_font(spec: str | dict | None) -> dict:
    """把字体名 / 资源 id / 已存 config 片段统一成一份字体定义。

    返回 {'name', 'member', 'resource_id', 'is_vip'}。spec 为 None 时取默认字体。
    """
    if isinstance(spec, dict):
        name = str(spec.get("name") or "").strip()
        resource_id = str(spec.get("resource_id") or "").strip()
        if not name or not resource_id:
            raise ValueError(f"config.font 缺少 name/resource_id: {spec!r}")
        row = font_table().get(name)
        if row is not None and row["resource_id"] != resource_id:
            raise ValueError(
                f"config.font 与字体表不一致: {name} 记录 {resource_id}，"
                f"字体表为 {row['resource_id']}"
            )
        return {
            "name": name,
            "member": (row or {}).get("member") or name,
            "resource_id": resource_id,
            "is_vip": bool((row or {}).get("is_vip", False)),
        }

    text = str(spec).strip() if spec is not None else ""
    if not text:
        text = DEFAULT_FONT_NAME

    table = font_table()
    if text in table:
        row = table[text]
        return {
            "name": text,
            "member": row["member"],
            "resource_id": row["resource_id"],
            "is_vip": bool(row["is_vip"]),
        }
    for name, row in table.items():
        if row["resource_id"] == text:
            return {
                "name": name,
                "member": row["member"],
                "resource_id": row["resource_id"],
                "is_vip": bool(row["is_vip"]),
            }
    raise ValueError(
        f"剪映字体表里找不到字体 {text!r}；请传剪映里的字体名或它的资源 id"
    )


def font_for_work(work: Path) -> dict:
    """从工作区 config.json 读字体；缺字段时回落到默认字体。"""
    path = Path(work) / "config.json"
    if not path.is_file():
        return resolve_font(None)
    config = json.loads(path.read_text(encoding="utf-8"))
    return resolve_font(config.get("font"))

# 只登记已有实机证据的精确单元格。禁止分辨率等比推导。
CAPTION_PROFILES = {
    (1080, 1920): {
        "name": "jianying_portrait_1080x1920",
        "evidence_status": "user_confirmed",
        "font_size": 14.0,
        "transform_x": 0.0,
        # 剪映 11.2 首次载入时，JSON 的半画布高坐标并不等于属性面板像素值。
        # 剪映首次载入按完整画布高度换算：-728 / 1920 = -0.3791667。
        "transform_y": -0.3791667,
        "ordinary_color": PURE_WHITE,
        "highlight_color": WARM_GOLD,
    },
    (2160, 3840): {
        "name": "jianying_portrait_2160x3840",
        # 用户已在剪映中完成 2160x3840 实机反查；这是独立确认的剪映
        # profile，不是从 Final Cut 52 号字或 1080p 像素线性换算。
        "evidence_status": "user_confirmed",
        "font_size": 14.0,
        "transform_x": 0.0,
        "transform_y": -0.3791667,
        "ordinary_color": PURE_WHITE,
        "highlight_color": WARM_GOLD,
    },
    (3840, 2160): {
        "name": "jianying_landscape_3840x2160",
        "evidence_status": "user_confirmed",
        "font_size": 8.0,
        "transform_x": 0.0,
        "transform_y": -0.72,
        "ordinary_color": PURE_WHITE,
        "highlight_color": WARM_GOLD,
    },
}


def caption_profile(width: int, height: int) -> dict:
    try:
        return dict(CAPTION_PROFILES[(int(width), int(height))])
    except KeyError as exc:
        raise ValueError(
            f"剪映尚无 {width}x{height} 的用户实机字幕 profile，"
            "禁止从其他分辨率缩放推导"
        ) from exc


def frame_us(frame: int, fps_raw: str) -> int:
    native = analysis_to_native(int(frame), fps_raw)
    return round_fraction(native * 1_000_000 / rate(fps_raw))


def project_card_timerange(
    card: dict,
    segment_tl: dict,
    rough_row: dict,
    target_start: int,
    target_duration: int,
    fps_raw: str,
) -> dict:
    """以“归属视频段 + 段内声学时码”投影字幕。

    视频和字幕若分别按整条时间线换算，逐段取整会在切点累积出微秒/半帧级
    漂移。本函数始终以已冻结视频段的 target 起点为锚，并将段首/段尾硬
    吸附到视频边界。
    """
    sf = int(card["sf"])
    ef = int(card["ef"])
    tl_in = int(segment_tl["tl_in_f"])
    tl_out = int(segment_tl["tl_out_f"])
    if not (tl_in <= sf < ef <= tl_out):
        raise ValueError(
            f"字幕越出归属视频段: si={card.get('si')} "
            f"card={sf}-{ef} segment={tl_in}-{tl_out}"
        )

    expected_local_sf = sf - tl_in
    expected_local_ef = ef - tl_in
    expected_source_sf = int(rough_row["in_f"]) + expected_local_sf
    expected_source_ef = int(rough_row["in_f"]) + expected_local_ef
    for field, expected in (
        ("local_sf", expected_local_sf),
        ("local_ef", expected_local_ef),
        ("source_sf", expected_source_sf),
        ("source_ef", expected_source_ef),
    ):
        if int(card.get(field, -1)) != expected:
            raise ValueError(
                f"字幕 {card.get('si')}.{card.get('card_index')} {field}="
                f"{card.get(field)!r}，锺点真值为 {expected}"
            )

    source_in = int(rough_row["in_f"])
    source_base_us = frame_us(source_in, fps_raw)
    if sf == tl_in:
        start = int(target_start)
    else:
        start = int(target_start) + (
            frame_us(source_in + sf - tl_in, fps_raw) - source_base_us
        )
    if ef == tl_out:
        end = int(target_start) + int(target_duration)
    else:
        end = int(target_start) + (
            frame_us(source_in + ef - tl_in, fps_raw) - source_base_us
        )

    segment_end = int(target_start) + int(target_duration)
    start = min(max(start, int(target_start)), segment_end)
    end = min(max(end, start), segment_end)
    if end <= start:
        raise ValueError(f"字幕投影后时长非正: {card.get('text')!r}")
    return {"start": start, "duration": end - start}


def card_identity(card: dict) -> tuple[int, int]:
    return int(card["si"]), int(card["card_index"])


def load_highlight_plan(work: Path, cards: list[dict], width: int, height: int) -> dict:
    """重点上色只读 P5 显式决策；P2 的 keyword 只是候选，绝不等于应上色。"""
    path = work / "jianying_packaging.json"
    if not path.is_file():
        return {"path": None, "selected": {}, "ratio": 0.0}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != "jianying-packaging@1":
        raise ValueError("jianying_packaging.json schema 必须为 jianying-packaging@1")
    if payload.get("delivery_branch") != "jianying-native":
        raise ValueError("包装计划不属于剪映原生草稿分支")
    profile = caption_profile(width, height)
    declared = payload.get("caption_profile") or {}
    exact = {
        "width": int(width),
        "height": int(height),
        "font": font_for_work(work)["name"],
        "font_size": profile["font_size"],
        "transform_x": profile["transform_x"],
        "transform_y": profile["transform_y"],
        "ordinary_color": "#FFFFFF",
    }
    for key, expected in exact.items():
        if declared.get(key) != expected:
            raise ValueError(
                f"包装计划 caption_profile.{key}={declared.get(key)!r}，"
                f"应为 {expected!r}"
            )

    by_id = {card_identity(card): card for card in cards}
    selected: dict[tuple[int, int], dict] = {}
    for pos, row in enumerate(payload.get("highlight_cards") or []):
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
        if not str(row.get("reason") or "").strip():
            raise ValueError(f"重点字幕缺少语义理由: {ident}")
        selected[ident] = {"keyword": keyword, "reason": row["reason"]}

    ratio = len(selected) / len(cards) if cards else 0.0
    if ratio > MAX_HIGHLIGHT_RATIO + 1e-12:
        raise ValueError(
            f"重点字幕密度 {ratio:.1%} 超过 {MAX_HIGHLIGHT_RATIO:.0%}；"
            "不得把 keyword 候选全部上色"
        )
    return {"path": path, "selected": selected, "ratio": ratio}
