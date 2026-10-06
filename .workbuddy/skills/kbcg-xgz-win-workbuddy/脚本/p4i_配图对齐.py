#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配图对齐：把 配图/ 文件夹里的图片按「文件名=触发词」对齐到句子，产出配图计划。

约定
----
工作区下建 `配图/` 文件夹，图片文件名就是触发词（支持 png/jpg/jpeg/webp/gif）：

- `多囊.png`        → 讲到含「多囊」的**第一句**时，图从那句的句首出现
- `检查#2.png`      → 第 2 次出现「检查」的那句话（#序号按播放顺序数）
- `多囊_2.5s.png`   → 显示 2.5 秒
- `检查#2_3s.png`   → 序号 + 时长组合写法

默认显示秒数在 config.json 的 `image_duration_s`（缺省 4.0）。
图片锚定的是**句子**（句首出图，画面先行、人声跟上），不是孤立的词；
计划里记录命中的整句话，出草稿前人工扫一眼核对命中是否正确。

防重叠内建（2026-09-16 复盘固化，不用再手工排）：

- **一句一图**：同一句话命中多张图时，只保留**触发词最长**的一张
  （最具体的匹配），其余自动让位——计划里记进 `auto_dropped` 并响亮列出，
  想保留就给别的图换触发词或加 `#序号`。
- **跨句自动压时长**：按时间线排序后，前一张的显示时长若压到后一张的
  句首，自动收窄到刚好不重叠（`auto_shortened` 标记并列出）。
  文件名 `_Ns` 仍可显式指定时长，作为压缩的上限。

对齐原理：rough_segments 的每一行就是播放顺序里的一句话（in_f/out_f 源帧 +
整句文本 + 词级 char_times），复用字幕同一套「源帧 → 成片时间线」投影
（captions_plan.segments_tl），压缩停顿、重排后的错位由既有机制兜住。

产出 `配图计划.json`（schema kbcg-xgz/jianying_image_plan@1），
由 p4j_出剪映草稿.py 在生成草稿时读取，写入「配图」画中画轨道。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCHEMA = "kbcg-xgz/jianying_image_plan@1"
EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
NAME_RE = re.compile(
    r"^(?P<trig>.+?)(?:#(?P<occ>\d+))?(?:_(?P<dur>\d+(?:\.\d+)?)s)?$"
)
DEFAULT_DURATION_S = 4.0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    return p


def parse_stem(stem: str) -> tuple[str, int, float | None]:
    """文件名 → (触发词, 第几次出现, 时长覆盖)。解析失败抛 SystemExit。"""
    m = NAME_RE.match(stem)
    if not m or not m.group("trig"):
        raise SystemExit(f"⛔ 配图文件名无法解析: {stem!r}（应为 词.png / 词#2.png / 词_2.5s.png）")
    occ = int(m.group("occ") or 1)
    if occ < 1:
        raise SystemExit(f"⛔ 配图序号从 1 起: {stem!r}")
    dur = float(m.group("dur")) if m.group("dur") else None
    if dur is not None and dur <= 0:
        raise SystemExit(f"⛔ 配图时长必须为正: {stem!r}")
    return m.group("trig"), occ, dur


def find_sentence(
    rough: list[dict], trigger: str, occurrence: int, stem: str
) -> tuple[int, dict]:
    """在播放顺序里找第 occurrence 次出现触发词的那一句。"""
    hits = 0
    for index, row in enumerate(rough):
        text = str(row.get("text") or "")
        start = 0
        while True:
            pos = text.find(trigger, start)
            if pos < 0:
                break
            hits += 1
            start = pos + len(trigger)
            if hits == occurrence:
                return index, row
    total = hits
    if total == 0:
        raise SystemExit(
            f"⛔ 配图 {stem!r} 的触发词「{trigger}」在全篇口播里一次都没出现。\n"
            "   改文件名跟口播对上，或删掉这张图。"
        )
    raise SystemExit(
        f"⛔ 配图 {stem!r}: 触发词「{trigger}」全篇只出现 {total} 次，"
        f"要不了第 {occurrence} 次。"
    )


def first_word_hit(row: dict, trigger: str) -> dict | None:
    """触发词在该句词轴里的位置（仅用于报告展示，不参与投影）。"""
    best: dict | None = None
    for word in row.get("words") or []:
        w = str(word.get("w") or "")
        if not w or (trigger not in w and w not in trigger):
            continue
            char_times = word.get("char_times") or []
            hit = {
                "text": w,
                "s": char_times[0]["s"] if char_times else word.get("s"),
                "e": char_times[-1]["e"] if char_times else word.get("e"),
            }
            if w == trigger:
                return hit
            best = best or hit
    return best


def main() -> int:
    args = parser().parse_args()
    work = args.work.expanduser().resolve()
    config = json.loads((work / "config.json").read_text(encoding="utf-8"))
    rough = json.loads((work / "rough_segments.json").read_text(encoding="utf-8"))
    captions = json.loads((work / "captions_plan.json").read_text(encoding="utf-8"))
    segments_tl = captions.get("segments_tl") or []
    if len(segments_tl) != len(rough):
        raise SystemExit("⛔ captions_plan.segments_tl 与冻结粗剪段数不一致，先重跑分卡")
    fps = float(captions.get("fps") or 0)
    if fps <= 0:
        raise SystemExit("⛔ captions_plan.fps 缺失，无法换算时间线")
    default_dur = float(config.get("image_duration_s") or DEFAULT_DURATION_S)
    total_tl_f = int(segments_tl[-1]["tl_out_f"])

    image_dir = work / "配图"
    images = sorted(
        p for p in image_dir.iterdir()
        if p.is_file() and p.suffix.lower() in EXTS
    ) if image_dir.is_dir() else []
    if not images:
        print("ℹ 配图/ 里没有图片，跳过（不产出配图计划）")
        return 0

    entries: list[dict] = []
    seen: dict[str, str] = {}
    for path in images:
        stem = path.stem
        trigger, occurrence, dur_override = parse_stem(stem)
        if trigger in seen:
            raise SystemExit(
                f"⛔ 配图触发词重复：「{trigger}」同时被 {seen[trigger]} 和 {path.name} 使用"
            )
        seen[trigger] = path.name
        si, row = find_sentence(rough, trigger, occurrence, path.name)
        dur = dur_override if dur_override is not None else default_dur
        tl_in_f = int(segments_tl[si]["tl_in_f"])
        tl_out_f = int(segments_tl[si]["tl_out_f"])
        # 时长不许越过片尾；超了就收窄并明说。
        max_f = total_tl_f - tl_in_f
        dur_f = round(dur * fps)
        clamped = dur_f > max_f
        if clamped:
            dur_f = max_f
            dur = round(dur_f / fps, 3)
        word = first_word_hit(row, trigger)
        entries.append({
            "file": path.name,
            "path": str(path.resolve()),
            "trigger": trigger,
            "occurrence": occurrence,
            "si": si,
            "sentence": str(row.get("text") or ""),
            "sentence_in_s": round(float(row["in"]), 3),
            "sentence_out_s": round(float(row["out"]), 3),
            "tl_in_f": tl_in_f,
            "tl_out_f": tl_out_f,
            "word_hit": word,
            "duration_s": dur,
            "clamped_to_end": clamped,
        })

    # ── 防重叠内建：一句一图 + 跨句自动压时长 ──────────────────────────
    # 1) 同句多图：只留触发词最长的一张（最具体），其余让位并响亮列出。
    by_si: dict[int, list[dict]] = {}
    for entry in entries:
        by_si.setdefault(entry["si"], []).append(entry)
    kept: list[dict] = []
    dropped: list[dict] = []
    for group in by_si.values():
        if len(group) == 1:
            kept.append(group[0])
            continue
        ranked = sorted(group, key=lambda e: (-len(e["trigger"]), e["file"]))
        kept.append(ranked[0])
        dropped.extend(ranked[1:])
    kept.sort(key=lambda e: int(e["tl_in_f"]))
    for entry in dropped:
        entry["dropped_reason"] = "同句多图，仅保留触发词最长的一张"
    # 2) 跨句重叠：前一张自动收窄到后一张句首为止（_Ns 指定值作为上限）。
    shortened: list[str] = []
    too_short: list[str] = []
    for prev, curr in zip(kept, kept[1:]):
        avail_f = int(curr["tl_in_f"]) - int(prev["tl_in_f"])
        want_f = round(float(prev["duration_s"]) * fps)
        if avail_f > 0 and want_f > avail_f:
            new_dur = round(avail_f / fps, 3)
            shortened.append(f"{prev['file']} {prev['duration_s']:g}s→{new_dur:g}s")
            prev["duration_s"] = new_dur
            prev["auto_shortened"] = True
            if new_dur < 0.6:
                too_short.append(prev["file"])

    plan = {
        "schema": SCHEMA,
        "fps": fps,
        "duration_default_s": default_dur,
        "images": kept,
    }
    if dropped:
        plan["auto_dropped"] = [
            {k: entry[k] for k in ("file", "trigger", "si", "sentence", "dropped_reason")}
            for entry in dropped
        ]
    out = work / "配图计划.json"
    out.write_text(
        json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"✅ 配图计划已生成: {out}（{len(kept)} 张）")
    for entry in kept:
        start_s = entry["tl_in_f"] / fps
        mm, ss = divmod(start_s, 60)
        note = "（超出片尾已收窄）" if entry["clamped_to_end"] else ""
        if entry.get("auto_shortened"):
            note += "（自动压时长防重叠）"
        word = entry.get("word_hit")
        word_note = f"，触发词「{word['text']}」人声 {word['s']:.2f}s" if word else ""
        print(
            f"   {entry['file']} → 第{entry['si'] + 1}句 "
            f"「{entry['sentence'][:24]}{'…' if len(entry['sentence']) > 24 else ''}」"
            f"句首出现 · 成片 {mm:02.0f}:{ss:05.2f} · 显示 {entry['duration_s']:g}s{note}{word_note}"
        )
    if shortened:
        print(f"   ⚠ 自动压时长 {len(shortened)} 处（前图不压到后图句首）：{'；'.join(shortened)}")
    if too_short:
        print(f"   ⚠ {len(too_short)} 张压完显示不足 0.6s（{', '.join(too_short)}），建议换触发词或删图")
    if dropped:
        print(f"   ⚠ 同句多图已去重（一句一图，保留触发词最长），{len(dropped)} 张未进计划：")
        for entry in dropped:
            print(f"      · {entry['file']}（第{entry['si'] + 1}句「{entry['sentence'][:20]}…」）"
                  f"→ 想保留请换触发词或加 #序号")
    print("   ⚠ 出草稿前扫一眼命中句子是否正确；同名不同处用 词#2.png 区分。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
