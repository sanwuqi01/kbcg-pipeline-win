#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从冻结粗剪生成剪映原生草稿：真主轨、精确画幅 profile、稀疏重点字幕。"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

from media_contract import (
    analysis_to_native, assert_config_matches, probe, rate, round_fraction,
)
from jianying_contract import (
    card_identity,
    caption_profile,
    font_for_work,
    load_highlight_plan,
    project_card_timerange,
)

IMAGE_PLAN_SCHEMA = "kbcg-xgz/jianying_image_plan@1"


def load_image_plan(work: Path, segments_tl: list, rough: list) -> list[dict]:
    """读 配图计划.json（没有 配图/ 文件夹就没有这份文件，返回空表）。

    计划是按 rough + segments_tl 算的，两者任何一个重排/重切后 tl_in_f
    都会对不上 —— 这里按段逐一硬校验，过期计划响亮失败，不静默错位。
    """
    path = work / "配图计划.json"
    if not path.is_file():
        return []
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("schema") != IMAGE_PLAN_SCHEMA:
        raise SystemExit("⛔ 配图计划.json schema 不匹配，请重跑 p4i_配图对齐.py")
    entries = plan.get("images") or []
    if not entries:
        return []
    for pos, entry in enumerate(entries):
        label = f"配图[{pos}] {entry.get('file')!r}"
        si = int(entry.get("si", -1))
        if not 0 <= si < len(rough):
            raise SystemExit(f"⛔ {label}: si={si} 越出冻结粗剪段数 {len(rough)}")
        if int(entry.get("tl_in_f", -1)) != int(segments_tl[si]["tl_in_f"]):
            raise SystemExit(
                f"⛔ {label}: 时间线已变（计划 tl_in_f={entry.get('tl_in_f')}，"
                f"当前 {segments_tl[si]['tl_in_f']}）。重跑 p4i_配图对齐.py"
            )
        img = Path(str(entry.get("path") or "")).expanduser()
        if not img.is_file():
            raise SystemExit(f"⛔ {label}: 图片不存在 {img}")
    # 同一条画中画轨道不许重叠（重排序号按出现顺序）。
    fps = float(plan.get("fps") or 0)
    ordered = sorted(entries, key=lambda e: int(e["tl_in_f"]))
    for prev, curr in zip(ordered, ordered[1:]):
        prev_end_f = int(prev["tl_in_f"]) + round(float(prev["duration_s"]) * fps)
        if prev_end_f > int(curr["tl_in_f"]):
            raise SystemExit(
                f"⛔ 配图时间重叠：{prev['file']!r} 与 {curr['file']!r}。"
                "缩短其中一张的显示秒数（文件名 _2.5s）或换触发词。"
            )
    return ordered


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("--draft-root", type=Path, required=True)
    p.add_argument("--project-name")
    return p


def capcut_home() -> Path:
    candidates = []
    if os.environ.get("CAPCUT_MATE_HOME"):
        candidates.append(Path(os.environ["CAPCUT_MATE_HOME"]))
    candidates.append(Path.home() / "Developer/kbcg-xgz-capcut-mate")
    candidates.append(Path.home() / "Developer/capcut-mate")
    for path in candidates:
        if (path / "src/pyJianYingDraft").is_dir():
            return path.resolve()
    raise SystemExit(
        "⛔ 缺少 CapCutMate 环境。请让 Codex 执行本 Skill 的安装器，"
        "或设置 CAPCUT_MATE_HOME 指向 capcut-mate 仓库。"
    )


def expected_name(cfg: dict, override: str | None) -> str:
    core = cfg.get("core_expression")
    if not isinstance(core, str) or not re.search(r"[\u3400-\u9fff]", core):
        raise SystemExit("⛔ config.core_expression 必须包含中文")
    stem = f"{dt.date.today().isoformat()}_{core.strip()}_剪映草稿"
    name = override or stem
    if not re.fullmatch(re.escape(stem) + r"(?:_第(?:[2-9]|[1-9]\d+)版)?", name):
        raise SystemExit(f"⛔ 草稿名必须是「{stem}」或其递增版本")
    return name


def frame_us(frame: int, fps_raw: str) -> int:
    native = analysis_to_native(frame, fps_raw)
    return round_fraction(native * 1_000_000 / rate(fps_raw))


def frame_range_us(start_frame: int, end_frame: int, fps_raw: str) -> tuple[int, int]:
    """分别投影两端再相减，避免逐段四舍五入造成 1 微秒重叠。"""
    start = frame_us(start_frame, fps_raw)
    return start, frame_us(end_frame, fps_raw) - start


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_human_lock(work: Path, captions: dict) -> None:
    """人工粗剪工作区没有 AI decision_lock，但必须验证人工基准锁。"""
    lock_path = work / "human_rough_lock.json"
    if not lock_path.is_file():
        return
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    baseline = json.loads((work / "human_baseline.json").read_text(encoding="utf-8"))
    xml_path = Path(lock["human_xml_path"]).expanduser().resolve()
    checks = [
        (xml_path, lock["human_xml_sha256"], "人工粗剪 XML"),
        (work / "rough_segments.json", lock["rough_segments_sha256"], "rough_segments"),
        (work / "word_track.json", lock["word_track_sha256"], "word_track"),
    ]
    # 用户已经逐卡精调过的字幕是独立真值。旧锁只保护视频段和词轴，导致后续
    # P3/P6 仍能把用户字幕重新分卡或平移；新锁把字幕计划与续剪依据一起纳入。
    optional_checks = (
        ("captions_plan_sha256", work / "captions_plan.json", "captions_plan"),
        (
            "continuation_plan_sha256",
            Path(lock.get("continuation_plan_path") or ""),
            "用户续剪计划",
        ),
        (
            "manual_cards_sha256",
            Path(lock.get("manual_cards_path") or ""),
            "后半段语义分卡",
        ),
        (
            "user_refinement_regression_sha256",
            work / "user_refinement_regression.json",
            "用户精调前缀回归锁",
        ),
    )
    for field, path, label in optional_checks:
        expected = lock.get(field)
        if expected:
            checks.append((path.expanduser().resolve(), expected, label))
    for path, expected, label in checks:
        if not path.is_file() or sha256(path) != expected:
            raise SystemExit(f"⛔ {label} 已漂移，拒绝交付")
    if baseline.get("xml_sha256") != lock.get("human_xml_sha256"):
        raise SystemExit("⛔ 人工基准与粗剪锁不一致")
    expected_total = baseline.get("analysis_duration_f", baseline.get("duration_f"))
    if captions.get("total_f") != expected_total:
        raise SystemExit("⛔ 字幕计划与人工粗剪总时长不一致")


def add_keyword_style(segment, keyword: str | None, profile: dict) -> None:
    if not keyword:
        return
    for term in (part.strip() for part in keyword.split("|")):
        if not term:
            continue
        start = 0
        while True:
            start = segment.text.find(term, start)
            if start < 0:
                break
            end = start + len(term)
            segment.extra_styles.append({
                "fill": {"alpha": 1.0, "content": {"render_type": "solid", "solid": {
                    "alpha": 1.0, "color": list(profile["highlight_color"])
                }}},
                "range": [start, end],
                "size": profile["font_size"],
                "bold": False,
                "italic": False,
                "underline": False,
            })
            start = end


def source_specs(cfg: dict, rough: list[dict]) -> list[dict]:
    """返回本条时间线实际引用的原素材，顺序按首次出现。"""
    delivery_path = Path(cfg["_work"]) / "media_delivery.json" if cfg.get("_work") else None
    delivery_by_id: dict[str, Path] = {}
    if delivery_path and delivery_path.is_file():
        delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
        if delivery.get("schema") != "kbcg-xgz/jianying_media_delivery@1":
            raise SystemExit("⛔ media_delivery.json schema 不匹配")
        for row in delivery.get("sources", []):
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                delivery_by_id[row["id"]] = Path(str(row.get("delivery_path") or "")).expanduser().resolve()
    declared = cfg.get("sources")
    if isinstance(declared, list) and declared:
        by_id = {}
        for pos, row in enumerate(declared):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise SystemExit(f"⛔ config.sources[{pos}] 缺少 id")
            original = Path(str(row.get("path") or "")).expanduser().resolve()
            source = delivery_by_id.get(row["id"], original)
            if not source.is_file():
                raise SystemExit(f"⛔ 剪映交付素材不存在: {source}")
            if row["id"] in by_id:
                raise SystemExit(f"⛔ config.sources 重复 id: {row['id']}")
            by_id[row["id"]] = {"id": row["id"], "path": source, "config": row}
        used_ids = []
        for pos, item in enumerate(rough):
            source_id = item.get("source_id")
            if source_id not in by_id:
                raise SystemExit(
                    f"⛔ rough_segments[{pos}].source_id={source_id!r} 没有对应原素材"
                )
            if source_id not in used_ids:
                used_ids.append(source_id)
        return [by_id[source_id] for source_id in used_ids]

    original = Path(cfg.get("original_path") or cfg.get("proxy") or "").expanduser().resolve()
    source = delivery_by_id.get("source", original)
    if not source.is_file():
        raise SystemExit(f"⛔ 剪映交付素材不存在: {source}")
    return [{"id": "source", "path": source, "config": cfg}]


def assert_sources_compatible(specs: list[dict]) -> None:
    """多素材可合剪，但不允许在交付时静默统一技术属性。"""
    keys = (
        "width", "height", "encoded_width", "encoded_height", "rotation",
        "frame_rate_raw", "audio_sample_rate", "audio_channels",
    )
    baseline = specs[0]["actual"]
    for spec in specs[1:]:
        drift = [
            f"{key}: {baseline.get(key)!r} != {spec['actual'].get(key)!r}"
            for key in keys if baseline.get(key) != spec["actual"].get(key)
        ]
        if drift:
            raise SystemExit(
                f"⛔ 多素材属性冲突（{spec['id']}）：" + "；".join(drift)
            )


def normalize_draft_paths(draft_dir: Path, paths: list[Path]) -> None:
    """把草稿正文里的素材路径改写成剪映惯用的正斜杠。

    `VideoMaterial.__init__` 用 `os.path.abspath()` 规范化路径，Windows 上必然
    产出反斜杠；而本机 4900+ 份真实剪映草稿、以及我们写入的
    draft_meta_info.json 都用正斜杠（`E:/...`）。正文与元数据分隔符不一致时，
    剪映可能把素材判成「离线 / 需重新链接」，所以这里做一次**最小字节级改写**：
    只把 JSON 转义后的反斜杠换成正斜杠，不触碰任何版面格式。

    `draft_content.json` 与 `draft_info.json` 必须逐字节一致（p4j_登记草稿箱
    会校验），因此两者按同一份改写逻辑处理。
    """
    for name in ("draft_content.json", "draft_info.json"):
        target = draft_dir / name
        if not target.is_file():
            continue
        text = target.read_text(encoding="utf-8")
        for path in paths:
            # JSON 里一个反斜杠转义成两个，故比对/替换都用转义后的形态。
            escaped = str(path).replace("\\", "\\\\")
            if escaped in text:
                text = text.replace(escaped, path.as_posix())
        target.write_text(text, encoding="utf-8")


def update_meta(
    path: Path, name: str, duration: int, draft_id: str,
    source_materials: list[tuple[Path, object]],
    image_materials: list[tuple[Path, object]] | None = None,
) -> None:
    meta_path = path / "draft_meta_info.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["draft_name"] = name
    meta["draft_id"] = draft_id
    # 本机 256 份可读真实草稿实测：draft_fold_path 一律正斜杠（0 例外），
    # 而 draft_root_path 是反斜杠。照抄剪映自己的写法，避免下游按分隔符硬比时假失败。
    meta["draft_fold_path"] = path.as_posix()
    meta["draft_root_path"] = str(path.parent)
    meta["tm_duration"] = duration
    now = int(dt.datetime.now().timestamp() * 1_000_000)
    meta["tm_draft_create"] = now
    meta["tm_draft_modified"] = now
    library_items = [
        {
            "duration": float(material.duration),
            "extra_info": source.name,
            "file_Path": source.as_posix(),
            "height": int(material.height),
            "md5": "",
            "metetype": "video",
            "roughcut_time_range": {
                "duration": float(material.duration + 10_000), "start": 0
            },
            "type": 0,
            "width": int(material.width),
        }
        for source, material in source_materials
    ] + [
        {
            "duration": float(material.duration),
            "extra_info": source.name,
            "file_Path": source.as_posix(),
            "height": int(material.height),
            "md5": "",
            "metetype": "photo",
            "roughcut_time_range": {
                "duration": float(material.duration + 10_000), "start": 0
            },
            "type": 0,
            "width": int(material.width),
        }
        for source, material in (image_materials or [])
    ]
    groups = meta.setdefault("draft_materials", [])
    video_group = next((group for group in groups if group.get("type") == 0), None)
    if video_group is None:
        video_group = {"type": 0, "value": []}
        groups.insert(0, video_group)
    video_group["value"] = library_items
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> int:
    args = parser().parse_args()
    work = args.work.expanduser().resolve()
    cfg = json.loads((work / "config.json").read_text(encoding="utf-8"))
    cfg["_work"] = str(work)
    rough = json.loads((work / "rough_segments.json").read_text(encoding="utf-8"))
    captions = json.loads((work / "captions_plan.json").read_text(encoding="utf-8"))
    verify_human_lock(work, captions)
    if not rough:
        raise SystemExit("⛔ rough_segments.json 为空")
    image_plan = load_image_plan(
        work, captions.get("segments_tl") or [], rough
    )
    specs = source_specs(cfg, rough)
    try:
        for spec in specs:
            spec["actual"] = probe(spec["path"])
            declared = spec["config"]
            if not (declared.get("media") or {}).get("video"):
                declared = cfg
            assert_config_matches(declared, spec["actual"])
        assert_sources_compatible(specs)
    except ValueError as exc:
        raise SystemExit(f"⛔ 剪映草稿：{exc}") from exc
    actual = specs[0]["actual"]
    fps_raw = actual["frame_rate_raw"]
    project_fps = float(rate(fps_raw))

    home = capcut_home()
    sys.path.insert(0, str(home))
    import src.pyJianYingDraft as draft  # type: ignore

    # 字体来自 config.json（p0 --font 写入）。这里把字体名换成 CapCutMate
    # 的 FontType 成员，并交叉核对资源 id，防止内置字体表与 CapCutMate 漂移。
    try:
        font = font_for_work(work)
    except ValueError as exc:
        raise SystemExit(f"⛔ 剪映草稿：{exc}") from exc
    font_enum = draft.FontType.__members__.get(font["member"])
    if font_enum is None:
        try:
            font_enum = draft.FontType.from_name(font["name"])
        except ValueError:
            font_enum = None
    if font_enum is None:
        raise SystemExit(
            f"⛔ CapCutMate 字体表里没有 {font['name']!r}"
            f"（member {font['member']!r}）；请先在剪映中确认字体名"
        )
    if font_enum.value.resource_id != font["resource_id"]:
        raise SystemExit(
            f"⛔ 字体资源 id 漂移：config {font['resource_id']}，CapCutMate "
            f"{font_enum.value.resource_id}；请重跑 脚本_win/gen_font_table.py"
        )

    draft_root = args.draft_root.expanduser().resolve()
    draft_root.mkdir(parents=True, exist_ok=True)
    project_name = expected_name(cfg, args.project_name)
    output = draft_root / project_name
    if output.exists():
        raise SystemExit(f"⛔ 剪映草稿已存在，拒绝覆盖: {output}")

    width = actual["width"]
    height = actual["height"]
    try:
        profile = caption_profile(width, height)
    except ValueError as exc:
        raise SystemExit(f"⛔ 剪映草稿：{exc}") from exc
    folder = draft.DraftFolder(str(draft_root))
    script = folder.create_draft(project_name, width, height, fps=project_fps)
    draft_id = str(uuid.uuid4()).upper()
    script.content["id"] = str(uuid.uuid4()).upper()
    script.add_track(draft.TrackType.video, "main_track", relative_index=0)

    # 注意：pyJianYingDraft 的 VideoMaterial.__init__ 会执行 os.path.abspath()，
    # Windows 下把路径强制写成反斜杠，所以这里传什么分隔符都没用。
    # 正文里的路径统一由 save() 之后的 normalize_draft_paths() 改写成正斜杠。
    material_by_source_id = {
        spec["id"]: draft.VideoMaterial(str(spec["path"])) for spec in specs
    }
    source_by_id = {spec["id"]: spec for spec in specs}
    cursor = 0
    video_timeline: list[dict] = []
    for index, item in enumerate(rough):
        source_id = item.get("source_id") if len(specs) > 1 else specs[0]["id"]
        if source_id not in material_by_source_id:
            raise SystemExit(
                f"⛔ rough_segments[{index}] 无法解析素材: {source_id!r}"
            )
        spec = source_by_id[source_id]
        item_fps_raw = spec["actual"]["frame_rate_raw"]
        in_f = int(item["in_f"])
        out_f = int(item["out_f"])
        source_start, duration = frame_range_us(in_f, out_f, item_fps_raw)
        segment = draft.VideoSegment(
            material_by_source_id[source_id],
            draft.trange(start=cursor, duration=duration),
            source_timerange=draft.trange(start=source_start, duration=duration),
            volume=1.0,
        )
        script.add_segment(segment, "main_track")
        video_timeline.append({
            "start": cursor,
            "duration": duration,
            "source_id": source_id,
            "fps_raw": item_fps_raw,
        })
        cursor += duration

    script.add_track(draft.TrackType.text, "基本字幕", relative_index=0)
    cards = list(captions.get("normal", [])) + list(captions.get("highlight", []))
    cards.sort(key=lambda row: (int(row["sf"]), int(row["ef"]), row.get("text", "")))
    try:
        highlight_plan = load_highlight_plan(work, cards, width, height)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f"⛔ 剪映重点字幕计划无效：{exc}") from exc
    segments_tl = captions.get("segments_tl") or []
    if len(segments_tl) != len(rough):
        raise SystemExit("⛔ captions_plan.segments_tl 与冻结粗剪段数不一致")
    for card in cards:
        if not str(card.get("text", "")).strip():
            continue
        si = int(card.get("si", -1))
        if not 0 <= si < len(rough):
            raise SystemExit(f"⛔ 字幕缺少有效归属视频段: {card.get('text')!r}")
        try:
            target = project_card_timerange(
                card,
                segments_tl[si],
                rough[si],
                video_timeline[si]["start"],
                video_timeline[si]["duration"],
                video_timeline[si]["fps_raw"],
            )
        except ValueError as exc:
            raise SystemExit(f"⛔ 字幕时码投影失败：{exc}") from exc
        style = draft.TextStyle(
            size=profile["font_size"],
            color=profile["ordinary_color"],
            align=1,
            letter_spacing=0,
            line_spacing=3,
            auto_wrapping=True,
            max_line_width=0.90,
        )
        clip = draft.ClipSettings(
            transform_x=profile["transform_x"],
            transform_y=profile["transform_y"],
        )
        text_segment = draft.TextSegment(
            str(card["text"]).strip(),
            draft.trange(start=target["start"], duration=target["duration"]),
            font=font_enum,
            style=style,
            clip_settings=clip,
        )
        decision = highlight_plan["selected"].get(card_identity(card))
        if decision:
            add_keyword_style(text_segment, decision["keyword"], profile)
        script.add_segment(text_segment, "基本字幕")

    # 配图画中画轨：图片按「文件名=触发词」对齐到句子，句首出现。
    # 静态图素材时长是 3h，任意摆；gif 是真时长，超出就收窄并明说。
    image_materials: list[tuple[Path, object]] = []
    if image_plan:
        script.add_track(draft.TrackType.video, "配图", relative_index=1)
        for entry in image_plan:
            material = draft.VideoMaterial(str(entry["path"]))
            start_us = video_timeline[int(entry["si"])]["start"]
            duration_us = round(float(entry["duration_s"]) * 1_000_000)
            if material.duration < duration_us:
                print(
                    f"⚠ {entry['file']}: 素材时长 {material.duration / 1e6:.2f}s "
                    f"短于计划 {duration_us / 1e6:.2f}s，按素材时长收窄"
                )
                duration_us = int(material.duration)
            if start_us + duration_us > cursor:
                duration_us = cursor - start_us
            segment = draft.VideoSegment(
                material,
                draft.trange(start=start_us, duration=duration_us),
                source_timerange=draft.trange(start=0, duration=duration_us),
            )
            script.add_segment(segment, "配图")
            image_materials.append((Path(entry["path"]), material))

    script.save()
    normalize_draft_paths(
        output,
        [spec["path"] for spec in specs]
        + [path for path, _ in image_materials],
    )
    update_meta(
        output,
        project_name,
        cursor,
        draft_id,
        [(spec["path"], material_by_source_id[spec["id"]]) for spec in specs],
        image_materials,
    )
    print(f"✅ 剪映原生草稿已生成: {output}")
    print(
        f"   项目素材记录已声明 {len(specs)} 份完整原片 · 真主轨 {len(rough)} 段 · "
        f"原生字幕 {len(cards)} 张 · 重点 {len(highlight_plan['selected'])} 张"
        f"({highlight_plan['ratio']:.1%}) · 配图 {len(image_materials)} 张 · "
        f"{font['name']} · "
        f"{profile['font_size']:g} 号 · 纯白 · "
        f"{width}x{height} · {project_fps:.3f}fps"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
