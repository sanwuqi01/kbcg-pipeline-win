#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并草稿 —— 同一日期文件夹的多期，整合成一个剪映草稿（多轨分层 + 接龙）。

背景：剪映一个草稿只有一条时间线（draft_content.json 的
tracks[] 分层轨道），没有多序列结构；草稿箱索引也没有文件夹字段。所以
「同工程、时间线分开几个视频」用轨道分层模拟：每个视频占一条以期名命名的
视频轨，字幕轨、配图轨跟着自己的视频走，时间上首尾相接 —— 打开草稿，
时间线像文件夹一样一层一层分开，能直接播放导出。

分工（三段编排，仿 pipeline finalize）：
  · 规划（任何 python）—— 就绪检查（除 deliver 外全部阶段完成）、逐期
    素材探测与兼容校验、媒体交付（media_delivery）、算接龙偏移量、定草稿名。
  · 建稿（capcut-mate venv 重入 `--build <计划>`）—— 逐期铺轨道，投影、
    字幕、配图全部复用 p4j_出剪映草稿 的函数。
  · 收尾（回到规划进程）—— 合并版结构验收、登记草稿箱、交付说明、给每期
    工作区写 合并交付.json 标记（剪辑.py 的 deliver 闸口认这个标记）。

触发规则：日期文件夹里 ≥2 个视频 → 该批合并出稿；只有 1 个 → 照旧单期
出草稿（intake 打「合并组」标记，≥2 才打）。本工具是**单独的一层**，
不碰单期交付流程。

用法：
  python 工具\\合并草稿.py 2026-09-15                  # 合并 输入\\2026-09-15\\ 的全部已就绪期
  python 工具\\合并草稿.py 2026-09-15 --only 期甲,期乙   # 挑选并指定接龙顺序
  python 工具\\合并草稿.py 2026-09-15 --plan-only       # 只出合并计划，不建草稿
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
BASE = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from runtime_paths import RuntimePaths, resolve_runtime_paths  # noqa: E402

RUNTIME_PATHS = resolve_runtime_paths(BASE)
WORKROOT = RUNTIME_PATHS.workroot
OUTBOX = RUNTIME_PATHS.outbox

PLAN_SCHEMA = "kbcg-xgz/merge_draft_plan@1"
MERGE_MARKER = "合并交付.json"


def echo(text: str = "") -> None:
    print(text, flush=True)


# ══════════════════════════════════════════════════════════════════════════
# 1. 纯函数（单测覆盖这一层）
# ══════════════════════════════════════════════════════════════════════════


def accumulate_offsets(durations_us: list[int]) -> list[int]:
    """接龙偏移量：第 k 期的起点 = 前面所有期时长之和。"""
    offsets: list[int] = []
    total = 0
    for d in durations_us:
        offsets.append(total)
        total += d
    return offsets


def check_no_overlap(spans: list[tuple[int, int]]) -> str | None:
    """视频段不许互相压叠（首尾相接允许）。返回错误描述或 None。"""
    ordered = sorted(spans)
    for (s1, e1), (s2, e2) in zip(ordered, ordered[1:]):
        if s2 < e1:
            return f"时间段 [{s1}, {e1}) 与 [{s2}, {e2}) 重叠"
    return None


def next_free_name(root: Path, stem: str) -> str:
    """在 root 下找第一个不存在的名字：stem、stem_第2版、stem_第3版…"""
    if not (root / stem).exists():
        return stem
    for n in range(2, 1000):
        cand = f"{stem}_第{n}版"
        if not (root / cand).exists():
            return cand
    raise SystemExit(f"⛔ {root} 下「{stem}」的版本号已经排到四位数，先人工清理")


def collect_episodes(paths: RuntimePaths | Path, date: str,
                     only: list[str] | None) -> list[dict]:
    """找出源素材位于 输入\\<日期>\\ 下的已登记期。

    返回 [{name, work, source}]，默认按素材文件名排序（=接龙顺序）。
    only 非空时按给定顺序挑选（名字必须全部命中，缺一个就响亮失败）。
    """
    # Path 参数只保留给旧调用者；入口始终传入已校验的 RuntimePaths。
    inbox = paths.inbox if isinstance(paths, RuntimePaths) else paths / "输入"
    workroot = paths.workroot if isinstance(paths, RuntimePaths) else paths / "工作区"
    inbox_date = inbox / date
    if not inbox_date.is_dir():
        raise SystemExit(f"⛔ 没有这个日期文件夹: {inbox_date}")
    found: list[dict] = []
    if workroot.is_dir():
        for entry in sorted(workroot.iterdir()):
            cfg_path = entry / "config.json"
            if not cfg_path.is_file():
                continue
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            paths = []
            declared = cfg.get("sources")
            if isinstance(declared, list):
                paths += [str(r.get("path") or "") for r in declared if isinstance(r, dict)]
            paths.append(str(cfg.get("original_path") or cfg.get("proxy") or ""))
            source = next(
                (p for p in (Path(p) for p in paths if p)
                 if p.resolve().is_relative_to(inbox_date.resolve())),
                None,
            )
            if source is not None:
                found.append({"name": entry.name, "work": entry, "source": source})
    if only is not None:
        by_name = {row["name"]: row for row in found}
        missing = [n for n in only if n not in by_name]
        if missing:
            raise SystemExit(
                f"⛔ --only 里的期不存在或不在 输入\\{date}\\ 下：{'、'.join(missing)}\n"
                f"   本批现有：{'、'.join(row['name'] for row in found) or '（空）'}"
            )
        return [by_name[n] for n in only]
    if len(found) > 1:
        found.sort(key=lambda row: row["source"].name)
    return found


# ══════════════════════════════════════════════════════════════════════════
# 2. 模块装载（剪辑工作台 + Skill 脚本 + p4j 合同层）
# ══════════════════════════════════════════════════════════════════════════


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_context() -> dict:
    """装载 剪辑（定位 Skill）与 p4j（合同层）。任何 python 都能跑，纯标准库。"""
    剪辑 = _load_module("xgz_jianji", TOOLS / "剪辑.py")
    剪辑.load_pipeline()
    scripts = Path(剪辑.SCRIPTS)
    sys.path.insert(0, str(scripts))
    p4j = _load_module("xgz_p4j_chuying", scripts / "p4j_出剪映草稿.py")
    jy = _load_module("xgz_jianying_contract", scripts / "jianying_contract.py")
    return {"剪辑": 剪辑, "p4j": p4j, "jy": jy, "SCRIPTS": scripts}


def locate_draft_box(pipeline_py: Path, scripts: Path) -> tuple[Path, Path | None]:
    """定位剪映草稿根 + 草稿箱索引（与 剪辑.py 同一个定位器）。"""
    proc = subprocess.run(
        [str(pipeline_py), str(scripts / "p4j_定位剪映草稿根.py"), "--save", "--list"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"⛔ 定位剪映草稿根失败：\n{proc.stdout}\n{proc.stderr}")
    out = proc.stdout or ""
    start = out.find("{")
    if start < 0:
        raise SystemExit(f"⛔ 定位器没有输出 JSON：\n{out}")
    data = json.loads(out[start:])
    root = Path(str(data.get("selected") or ""))
    if not root.is_dir():
        raise SystemExit(f"⛔ 剪映草稿根不存在: {root}")
    index = str(data.get("root_meta_index") or "").strip()
    return root, (Path(index) if index and Path(index).is_file() else None)


# ══════════════════════════════════════════════════════════════════════════
# 3. 规划（阶段 A）
# ══════════════════════════════════════════════════════════════════════════


def episode_ready(剪辑, work: Path) -> tuple[bool, str]:
    """除 deliver 外全部阶段完成才算就绪（已单独交付过的也算）。"""
    stages = 剪辑.build_stages()
    for stg in stages:
        if stg.key == "deliver":
            break
        try:
            finished = stg.done(work)
        except Exception:  # noqa: BLE001
            finished = False
        if not finished:
            return False, stg.label
    return True, ""


def make_plan(ctx: dict, episodes: list[dict], date: str,
              draft_root: Path, draft_name: str) -> dict:
    """阶段 A：逐期校验 + 探测素材 + 媒体交付 + 算偏移量。产出计划 dict。"""
    p4j, jy, 剪辑 = ctx["p4j"], ctx["jy"], ctx["剪辑"]
    rows: list[dict] = []
    durations: list[int] = []
    all_specs: list[dict] = []
    fonts: set[tuple] = set()
    width = height = None

    for row in episodes:
        work = Path(row["work"]).resolve()
        name = row["name"]
        echo(f"── [{name}] 就绪与素材检查")
        ok, stage_label = episode_ready(剪辑, work)
        if not ok:
            raise SystemExit(
                f"⛔ [{name}] 还没就绪（卡在：{stage_label}）。\n"
                f"   先跑 python 工具\\剪辑.py 把它推到交付闸口，再合并。"
            )
        cfg = json.loads((work / "config.json").read_text(encoding="utf-8"))
        cfg["_work"] = str(work)
        rough = json.loads((work / "rough_segments.json").read_text(encoding="utf-8"))
        captions = json.loads((work / "captions_plan.json").read_text(encoding="utf-8"))
        p4j.verify_human_lock(work, captions)
        if not rough:
            raise SystemExit(f"⛔ [{name}] rough_segments.json 为空")

        # 媒体交付（幂等；参照单期 finalize 的 p4j_media_prepare）
        rc = subprocess.run(
            [str(剪辑.PY), str(ctx["SCRIPTS"] / "p4j_准备交付素材.py"),
             str(work), "--draft-root", str(draft_root)],
        ).returncode
        if rc != 0:
            raise SystemExit(f"⛔ [{name}] 媒体交付失败（退出码 {rc}）")

        specs = p4j.source_specs(cfg, rough)
        for spec in specs:
            spec["actual"] = p4j.probe(spec["path"])
            declared = spec["config"]
            if not (declared.get("media") or {}).get("video"):
                declared = cfg
            p4j.assert_config_matches(declared, spec["actual"])
            spec["_ep"] = name
        all_specs.extend(specs)

        font = jy.font_for_work(work)
        fonts.add((font["member"], font["resource_id"]))

        if width is None:
            width, height = specs[0]["actual"]["width"], specs[0]["actual"]["height"]
        jy.caption_profile(width, height)  # 画幅必须能出字幕 profile

        # 期时长 = 冻结粗剪各段投影之和（与 p4j 的 cursor 同一算法）
        material_fps = {spec["id"]: spec["actual"]["frame_rate_raw"] for spec in specs}
        duration = 0
        for item in rough:
            fps_raw = material_fps.get(item.get("source_id"), specs[0]["actual"]["frame_rate_raw"]) \
                if len(specs) > 1 else specs[0]["actual"]["frame_rate_raw"]
            duration += p4j.frame_range_us(int(item["in_f"]), int(item["out_f"]), fps_raw)[1]
        durations.append(duration)
        rows.append({
            "name": name,
            "work": str(work),
            "rough_sha256": p4j.sha256(work / "rough_segments.json"),
            "captions_sha256": p4j.sha256(work / "captions_plan.json"),
            "duration_us": duration,
            "font": font,
        })
        echo(f"   ✅ 时长 {duration / 1e6:.2f}s · 素材 {[Path(s['path']).name for s in specs]}")

    # 跨期素材兼容：同一天的多条口播一般同机同参，属性漂移必须响亮失败。
    # 同一物理文件跨期重复出现只比对一次（按解析后的路径去重）。
    # 手机 VFR 素材的容器平均帧率分数逐文件不同（实测五条
    # 29.947~29.955fps，差 ≤0.03%，是同一标称帧率的不同分数表示），
    # 严格字符串比较会误报。frame_rate_raw 改为浮点容差比较（0.2%），
    # 真正的帧率档位差异（如 30 vs 25）和其余属性仍然严格一致。
    # VFR 丢帧会进一步拉低容器平均帧率（实测素材标称 30000/1001 与
    # 其余几条完全相同，但丢 23 帧后 avg=29.70，与 29.96 差 0.86%）。
    # 0.2% 容差不够，放宽到 1.0%——仍远小于
    # 真档位差异（30 vs 25 = 16.7%），档位混淆依然会响亮失败。
    # VFR 丢帧可以更狠（同标称 30000/1001，1987 帧/67.92s ≈ 29.21，
    # 丢帧 2.4%）。1.0% 也不够，放宽到 3.0%
    # ——真档位差最近的也有 16.7%（30 vs 25），档位混淆依然会响亮失败。
    union: dict[str, dict] = {}
    for spec in all_specs:
        key = str(Path(spec["path"]).resolve())
        union.setdefault(key, spec)
    specs_list = list(union.values())
    if len(specs_list) > 1:
        keys = (
            "width", "height", "encoded_width", "encoded_height", "rotation",
            "audio_sample_rate", "audio_channels",
        )
        baseline = specs_list[0]["actual"]

        def _fps(raw):
            from fractions import Fraction
            return float(Fraction(raw))

        for spec in specs_list[1:]:
            drift = [
                f"{key}: {baseline.get(key)!r} != {spec['actual'].get(key)!r}"
                for key in keys if baseline.get(key) != spec["actual"].get(key)
            ]
            fb, fs = _fps(baseline["frame_rate_raw"]), _fps(spec["actual"]["frame_rate_raw"])
            if abs(fs - fb) / fb > 0.030:
                drift.append(
                    f"frame_rate_raw: {baseline['frame_rate_raw']!r} != "
                    f"{spec['actual']['frame_rate_raw']!r}"
                )
            if drift:
                raise SystemExit(
                    "⛔ 多素材属性冲突（%s）：%s" % (spec["id"], "；".join(drift))
                )

    if len(fonts) != 1:
        raise SystemExit(f"⛔ 各期字幕字体不一致（{fonts}），合并前先统一 config.font")
    offsets = accumulate_offsets(durations)
    for row, offset in zip(rows, offsets):
        row["offset_us"] = offset
    total = sum(durations)
    return {
        "schema": PLAN_SCHEMA,
        "date": date,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "draft_root": str(draft_root),
        "draft_name": draft_name,
        "project": {"width": width, "height": height},
        "total_duration_us": total,
        "episodes": rows,
    }


# ══════════════════════════════════════════════════════════════════════════
# 4. 建稿（阶段 B，capcut-mate venv 里跑）
# ══════════════════════════════════════════════════════════════════════════


def build_from_plan(plan: dict, scripts: Path) -> None:
    """按计划逐期铺轨道。轨道名：视频=期名、字幕=字幕_期名、配图=配图_期名。"""
    p4j = _load_module("xgz_p4j_chuying", scripts / "p4j_出剪映草稿.py")
    jy = _load_module("xgz_jianying_contract", scripts / "jianying_contract.py")

    home = p4j.capcut_home()
    sys.path.insert(0, str(home))
    import src.pyJianYingDraft as draft  # type: ignore

    draft_root = Path(plan["draft_root"])
    output = draft_root / plan["draft_name"]
    if output.exists():
        raise SystemExit(f"⛔ 剪映草稿已存在，拒绝覆盖: {output}")

    eps = plan["episodes"]
    n_eps = len(eps)
    width = int(plan["project"]["width"])
    height = int(plan["project"]["height"])

    # 先逐期把素材/计划全部准备好，任何一期有问题都在建轨之前失败
    prepared: list[dict] = []
    for row in eps:
        work = Path(row["work"])
        if p4j.sha256(work / "rough_segments.json") != row["rough_sha256"]:
            raise SystemExit(f"⛔ [{row['name']}] rough_segments 在规划后被改动，重新跑合并")
        if p4j.sha256(work / "captions_plan.json") != row["captions_sha256"]:
            raise SystemExit(f"⛔ [{row['name']}] captions_plan 在规划后被改动，重新跑合并")
        cfg = json.loads((work / "config.json").read_text(encoding="utf-8"))
        cfg["_work"] = str(work)
        rough = json.loads((work / "rough_segments.json").read_text(encoding="utf-8"))
        captions = json.loads((work / "captions_plan.json").read_text(encoding="utf-8"))
        image_plan = p4j.load_image_plan(work, captions.get("segments_tl") or [], rough)
        specs = p4j.source_specs(cfg, rough)
        for spec in specs:
            spec["actual"] = p4j.probe(spec["path"])
            declared = spec["config"]
            if not (declared.get("media") or {}).get("video"):
                declared = cfg
            p4j.assert_config_matches(declared, spec["actual"])
        font = jy.font_for_work(work)
        prepared.append({
            "row": row, "cfg": cfg, "rough": rough, "captions": captions,
            "image_plan": image_plan, "specs": specs, "font": font,
        })

    font_member = prepared[0]["font"]["member"]
    font_enum = draft.FontType.__members__.get(font_member)
    if font_enum is None:
        raise SystemExit(f"⛔ CapCutMate 字体表里没有 {font_member!r}")
    profile = jy.caption_profile(width, height)
    project_fps = float(p4j.rate(prepared[0]["specs"][0]["actual"]["frame_rate_raw"]))

    folder = draft.DraftFolder(str(draft_root))
    script = folder.create_draft(plan["draft_name"], width, height, fps=project_fps)
    script.content["id"] = str(uuid.uuid4()).upper()

    # 轨道骨架：视频轨 render_index 0..N-1（第一期在最底层、从 0s 起），
    # 配图轨在其上，字幕轨 type=text 基准 15000 天然压顶。
    for k, prep in enumerate(prepared):
        script.add_track(draft.TrackType.video, prep["row"]["name"], relative_index=k)
    for k, prep in enumerate(prepared):
        if prep["image_plan"]:
            script.add_track(draft.TrackType.video, f"配图_{prep['row']['name']}",
                             relative_index=n_eps + k)
    for prep in prepared:
        script.add_track(draft.TrackType.text, f"字幕_{prep['row']['name']}", relative_index=0)

    # 素材缓存：同一物理文件跨期共用一个 VideoMaterial
    material_by_path: dict[str, object] = {}
    source_materials: list[tuple[Path, object]] = []
    image_materials: list[tuple[Path, object]] = []

    def material_for(path: str):
        key = str(Path(path).resolve())
        if key not in material_by_path:
            material_by_path[key] = draft.VideoMaterial(path)
        return material_by_path[key]

    cursor = 0
    for k, prep in enumerate(prepared):
        row, rough, captions = prep["row"], prep["rough"], prep["captions"]
        specs = prep["specs"]
        offset = int(row["offset_us"])
        material_by_id = {spec["id"]: material_for(spec["path"]) for spec in specs}
        source_by_id = {spec["id"]: spec for spec in specs}
        for spec in specs:
            source_materials.append((Path(spec["path"]), material_by_id[spec["id"]]))

        video_timeline: list[dict] = []
        ep_cursor = 0
        for index, item in enumerate(rough):
            source_id = item.get("source_id") if len(specs) > 1 else specs[0]["id"]
            spec = source_by_id.get(source_id)
            if spec is None:
                raise SystemExit(f"⛔ [{row['name']}] rough[{index}] 无法解析素材: {source_id!r}")
            fps_raw = spec["actual"]["frame_rate_raw"]
            source_start, duration = p4j.frame_range_us(int(item["in_f"]), int(item["out_f"]), fps_raw)
            segment = draft.VideoSegment(
                material_by_id[source_id],
                draft.trange(start=offset + ep_cursor, duration=duration),
                source_timerange=draft.trange(start=source_start, duration=duration),
                volume=1.0,
            )
            script.add_segment(segment, row["name"])
            video_timeline.append({"start": ep_cursor, "duration": duration, "fps_raw": fps_raw})
            ep_cursor += duration
        if ep_cursor != int(row["duration_us"]):
            raise SystemExit(f"⛔ [{row['name']}] 期时长与计划不符（{ep_cursor} ≠ {row['duration_us']}）")

        # 字幕：单期投影 + 全局偏移
        cards = list(captions.get("normal", [])) + list(captions.get("highlight", []))
        cards.sort(key=lambda r: (int(r["sf"]), int(r["ef"]), r.get("text", "")))
        highlight_plan = p4j.load_highlight_plan(Path(row["work"]), cards, width, height)
        segments_tl = captions.get("segments_tl") or []
        if len(segments_tl) != len(rough):
            raise SystemExit(f"⛔ [{row['name']}] captions_plan.segments_tl 与粗剪段数不一致")
        for card in cards:
            if not str(card.get("text", "")).strip():
                continue
            si = int(card.get("si", -1))
            if not 0 <= si < len(rough):
                raise SystemExit(f"⛔ [{row['name']}] 字幕缺有效归属段: {card.get('text')!r}")
            target = p4j.project_card_timerange(
                card, segments_tl[si], rough[si],
                video_timeline[si]["start"], video_timeline[si]["duration"],
                video_timeline[si]["fps_raw"],
            )
            text_segment = draft.TextSegment(
                str(card["text"]).strip(),
                draft.trange(start=offset + int(target["start"]), duration=int(target["duration"])),
                font=font_enum,
                style=draft.TextStyle(
                    size=profile["font_size"], color=profile["ordinary_color"], align=1,
                    letter_spacing=0, line_spacing=3, auto_wrapping=True, max_line_width=0.90,
                ),
                clip_settings=draft.ClipSettings(
                    transform_x=profile["transform_x"], transform_y=profile["transform_y"],
                ),
            )
            decision = highlight_plan["selected"].get(p4j.card_identity(card))
            if decision:
                p4j.add_keyword_style(text_segment, decision["keyword"], profile)
            script.add_segment(text_segment, f"字幕_{row['name']}")

        # 配图画中画（该期有图才有轨）
        # p4i 的防重叠按 60fps 帧数学收窄，而这里摆段用素材
        # fps_raw 逐段累积 µs——VFR 素材上两条时间轴有逐段舍入漂移
        # （实测每段十几 ms），图一多必叠（SegmentOverlap 响亮失败）。
        # 合并端钳制：后一张起点不得早于同轨前一张终点；贴片尾时收窄，
        # 收窄后不足 0.1s 的整张跳过（配图少 0.1s 无感，叠轨必崩）。
        pip_last_end = 0
        for entry in prep["image_plan"]:
            material = material_for(str(entry["path"]))
            start_us = offset + int(video_timeline[int(entry["si"])]["start"])
            duration_us = round(float(entry["duration_s"]) * 1_000_000)
            if material.duration < duration_us:
                print(f"⚠ [{row['name']}] {entry['file']}: 素材时长短于计划，按素材时长收窄")
                duration_us = int(material.duration)
            if start_us < pip_last_end:
                start_us = pip_last_end
            if start_us + duration_us > offset + ep_cursor:
                duration_us = offset + ep_cursor - start_us
            if duration_us <= 100_000:
                print(f"⚠ [{row['name']}] {entry['file']}: 钳制后不足 0.1s，本张跳过")
                continue
            segment = draft.VideoSegment(
                material,
                draft.trange(start=start_us, duration=duration_us),
                source_timerange=draft.trange(start=0, duration=duration_us),
            )
            script.add_segment(segment, f"配图_{row['name']}")
            pip_last_end = start_us + duration_us
            image_materials.append((Path(entry["path"]), material))
        cursor = offset + ep_cursor

    script.save()
    p4j.normalize_draft_paths(
        output,
        [Path(p) for p in material_by_path],
    )
    p4j.update_meta(
        output, plan["draft_name"], cursor, str(uuid.uuid4()).upper(),
        source_materials, image_materials,
    )
    print(f"✅ 合并草稿已生成: {output}")
    print(f"   {n_eps} 期接龙 · 总时长 {cursor / 1e6:.2f}s · "
          f"视频轨 {n_eps} 条 · 字幕轨 {n_eps} 条 · "
          f"配图轨 {sum(1 for p in prepared if p['image_plan'])} 条")


# ══════════════════════════════════════════════════════════════════════════
# 5. 验收 + 登记 + 交付说明（阶段 C）
# ══════════════════════════════════════════════════════════════════════════


def verify_merged_draft(plan: dict) -> dict:
    """合并版结构验收：逐轨道数、时间不压叠、素材齐全、底层从 0s 起。"""
    draft_dir = Path(plan["draft_root"]) / plan["draft_name"]
    b = json.loads((draft_dir / "draft_content.json").read_text(encoding="utf-8"))
    tracks = b.get("tracks", [])
    by_name = {t.get("name"): t for t in tracks}
    fails: list[str] = []

    spans: list[tuple[int, int]] = []
    total = int(plan["total_duration_us"])
    for k, row in enumerate(plan["episodes"]):
        name = row["name"]
        track = by_name.get(name)
        if track is None:
            fails.append(f"[{name}] 视频轨缺失")
            continue
        segs = track.get("segments", [])
        if len(segs) != len(json.loads((Path(row["work"]) / "rough_segments.json").read_text(encoding="utf-8"))):
            fails.append(f"[{name}] 视频段数不符（轨上 {len(segs)}）")
        for seg in segs:
            tr = seg.get("target_timerange") or {}
            s, d = int(tr.get("start", 0)), int(tr.get("duration", 0))
            spans.append((s, s + d))
            if seg.get("render_index") != k:
                fails.append(f"[{name}] 视频段 render_index 应为 {k}，实际 {seg.get('render_index')}")
        text_track = by_name.get(f"字幕_{name}")
        if text_track is None:
            fails.append(f"[{name}] 字幕轨缺失")

    overlap = check_no_overlap(spans)
    if overlap:
        fails.append(f"视频段压叠：{overlap}")
    if spans and min(s for s, _ in spans) != 0:
        fails.append("底层视频轨没有从 0s 开始")
    if spans and max(e for _, e in spans) != total:
        fails.append(f"时间线终点 {max(e for _, e in spans)} ≠ 计划总时长 {total}")

    # 素材登记与文件存在性（视频/图片在 materials.videos，文字在 materials.texts）
    paths: dict[str, str | None] = {}
    for group in ("videos", "texts"):
        for m in b.get("materials", {}).get(group, []):
            if isinstance(m, dict) and m.get("id"):
                paths[m["id"]] = m.get("path")
    for track in tracks:
        for seg in track.get("segments", []):
            mid = seg.get("material_id")
            if mid not in paths:
                fails.append(f"轨道 {track.get('name')!r} 有素材未登记: {mid}")
            elif paths[mid] and not Path(str(paths[mid])).is_file():
                fails.append(f"素材文件不存在: {paths[mid]}")

    report = {
        "schema": "kbcg-xgz/merge_draft_verify@1",
        "draft_name": plan["draft_name"],
        "draft_path": str(draft_dir),
        "episodes": [row["name"] for row in plan["episodes"]],
        "total_duration_us": total,
        "structural_verified": not fails,
        "failures": fails,
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    return report


def write_delivery_note(plan: dict, report: dict, draft_dir: Path) -> Path:
    folder = OUTBOX / plan["draft_name"]
    folder.mkdir(parents=True, exist_ok=True)
    rows = [
        f"# {plan['draft_name']} · 交付说明",
        "",
        f"- 合并日期：{plan['date']}",
        f"- 期数：{len(plan['episodes'])} · 总时长：{plan['total_duration_us'] / 1e6:.2f} 秒",
        f"- 验收状态：`{'通过' if report['structural_verified'] else '失败'}"
        f"（{report['checked_at']}）`",
        "",
        "## 剪映草稿",
        "",
        f"`{draft_dir}`",
        "",
        "时间线结构：每个视频一条轨道（轨道名=期名），字幕/配图轨跟着自己的视频，"
        "首尾相接，播放导出就是一条连续片。",
        "",
        "| 接龙 | 期 | 起点 | 时长 |",
        "|---|---|---|---|",
    ]
    for k, row in enumerate(plan["episodes"], 1):
        rows.append(
            f"| {k} | {row['name']} | {row['offset_us'] / 1e6:.2f}s "
            f"| {row['duration_us'] / 1e6:.2f}s |"
        )
    rows += [
        "",
        "## 各期中间产物",
        "",
    ]
    for row in plan["episodes"]:
        rows.append(f"- `{row['name']}`：`{row['work']}`")
    rows += [
        "",
        "要改某一期的内容，回它的工作区改 JSON 重跑；改完再跑一次合并草稿。",
        "",
    ]
    note = folder / "交付说明.md"
    note.write_text("\n".join(rows) + "\n", encoding="utf-8")
    cmd = folder / "打开草稿.cmd"
    cmd.write_text(
        "@echo off\r\nchcp 65001 >nul\r\n"
        f'explorer "{draft_dir}"\r\n',
        encoding="utf-8",
    )
    return note


# ══════════════════════════════════════════════════════════════════════════
# 6. 入口
# ══════════════════════════════════════════════════════════════════════════


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="合并草稿.py",
        description="把同一天的多期合并成一个剪映草稿（多轨分层 + 接龙）",
    )
    p.add_argument("date", nargs="?", default=None, help="日期文件夹名，如 2026-09-15")
    p.add_argument("--only", default=None, help="只合并这些期（逗号分隔，顺序=接龙顺序）")
    p.add_argument("--plan-only", action="store_true", help="只出合并计划，不建草稿")
    p.add_argument("--build", default=None, help=argparse.SUPPRESS)  # 阶段 B 内部用
    return p


def capcut_python(home: Path) -> Path:
    for rel in ((".venv", "Scripts", "python.exe"), (".venv", "bin", "python")):
        cand = home.joinpath(*rel)
        if cand.is_file():
            return cand
    raise SystemExit(f"⛔ CapCutMate venv 解释器不存在: {home / '.venv'}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)

    # ── 阶段 B：在 capcut venv 里重建草稿 ──────────────────────────────
    if args.build:
        plan = json.loads(Path(args.build).read_text(encoding="utf-8"))
        if plan.get("schema") != PLAN_SCHEMA:
            raise SystemExit(f"⛔ 合并计划 schema 不匹配: {plan.get('schema')!r}")
        ctx = load_context()
        build_from_plan(plan, ctx["SCRIPTS"])
        return 0

    # ── 阶段 A：规划 ──────────────────────────────────────────────────
    if not args.date:
        raise SystemExit("⛔ 用法: 合并草稿.py <日期文件夹> [--only 期甲,期乙] [--plan-only]")
    ctx = load_context()
    剪辑 = ctx["剪辑"]
    only = [x.strip() for x in args.only.split(",") if x.strip()] if args.only else None
    episodes = collect_episodes(RUNTIME_PATHS, args.date, only)
    if len(episodes) < 2:
        names = "、".join(row["name"] for row in episodes) or "（没有已登记的期）"
        raise SystemExit(
            f"⛔ 这个日期文件夹只凑出 {len(episodes)} 期（{names}）。\n"
            f"   单个视频不用合并 —— 照旧跑 python 工具\\剪辑.py 单期出草稿。"
        )
    echo(f"合并组「{args.date}」共 {len(episodes)} 期（接龙顺序=素材文件名顺序）：")
    for row in episodes:
        echo(f"   · {row['name']}")

    draft_root, _ = locate_draft_box(剪辑.PY, Path(剪辑.SCRIPTS))
    draft_name = next_free_name(draft_root, f"{args.date}_合并剪映草稿")
    plan = make_plan(ctx, episodes, args.date, draft_root, draft_name)

    out_dir = OUTBOX / draft_name
    out_dir.mkdir(parents=True, exist_ok=True)
    plan_path = out_dir / "合并计划.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    echo(f"📋 合并计划：{plan_path}")
    for row in plan["episodes"]:
        echo(f"   {row['offset_us'] / 1e6:8.2f}s → {row['name']} "
             f"（{row['duration_us'] / 1e6:.2f}s）")
    echo(f"   总时长 {plan['total_duration_us'] / 1e6:.2f}s · 草稿名 {draft_name}")
    if args.plan_only:
        echo("（--plan-only：到此为止，不建草稿）")
        return 0

    # ── 阶段 B：capcut venv 重入 ─────────────────────────────────────
    cap_py = capcut_python(Path(ctx["p4j"].capcut_home()))
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [str(cap_py), str(Path(__file__).resolve()), "--build", str(plan_path)],
        env=env,
    )
    if proc.returncode != 0:
        raise SystemExit(f"⛔ 合并建稿失败（退出码 {proc.returncode}），计划在 {plan_path}")

    # ── 阶段 C：验收 + 登记 + 交付说明 + 标记 ────────────────────────
    report = verify_merged_draft(plan)
    draft_dir = Path(plan["draft_root"]) / plan["draft_name"]
    (out_dir / "合并验收.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not report["structural_verified"]:
        for f in report["failures"]:
            echo(f"   ⛔ {f}")
        raise SystemExit("⛔ 合并草稿结构验收未通过（明细见 合并验收.json），不登记")

    _, meta_path = locate_draft_box(剪辑.PY, Path(剪辑.SCRIPTS))
    if meta_path is None:
        raise SystemExit("⛔ 草稿箱索引文件（root_meta_info.json）不存在，无法登记")
    reg = subprocess.run(
        [str(剪辑.PY), str(Path(剪辑.SCRIPTS) / "p4j_登记草稿箱.py"), str(draft_dir),
         "--root-meta", str(meta_path)],
    )
    if reg.returncode != 0:
        raise SystemExit(f"⛔ 登记草稿箱失败（退出码 {reg.returncode}）")

    note = write_delivery_note(plan, report, draft_dir)
    for row in plan["episodes"]:
        marker = {
            "schema": "kbcg-xgz/merge_delivery@1",
            "date": plan["date"],
            "draft_name": plan["draft_name"],
            "draft_path": str(draft_dir),
            "merged_at": plan["created_at"],
        }
        (Path(row["work"]) / MERGE_MARKER).write_text(
            json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    echo("")
    echo(f"🎉 合并草稿交付完成：{draft_name}")
    echo(f"   交付说明：{note}")
    echo("   打开剪映专业版即可在草稿列表看到；每期工作区已写 合并交付.json 标记，")
    echo("   剪辑.py 不再对它们单独出稿。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
