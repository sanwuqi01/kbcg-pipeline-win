#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工序⓪：建立/更新工作区。

用法：
  p0_建工作区.py <视频路径> [片名]
      [--material-type single_speaker|single_subject_interview|multi_speaker|unknown]
      [--core-expression 中文核心表达]
      [--font 剪映字体名 | 字体资源id]

本步只建配置并给出真实转写命令；不会跳过对齐和决策阶段直接 finalize。
已有 config.json 采用合并更新，不静默删除审阅字段或未知扩展字段。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from media_contract import build_final_cut_color_contract, probe
from jianying_contract import DEFAULT_FONT_NAME, resolve_font


MATERIAL_TYPES = (
    "single_speaker",
    "single_subject_interview",
    "multi_speaker",
    "unknown",
)
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")
UNSAFE_FILENAME_RE = re.compile(r"[\\/:*?\"<>|\r\n]+")


def atomic_json(path: Path, data: dict[str, Any]) -> None:
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


def rational(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        if "/" in value:
            top, bottom = value.split("/", 1)
            return round(float(top) / float(bottom), 6) if float(bottom) else None
        return round(float(value), 6)
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(source: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(source),
    ]
    try:
        run = subprocess.run(command, capture_output=True, text=True, check=True)
        payload = json.loads(run.stdout)
    except FileNotFoundError as exc:
        raise SystemExit("⛔ 找不到 ffprobe，无法核实素材元数据") from exc
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise SystemExit(f"⛔ ffprobe 无法读取素材: {exc}") from exc

    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    side_data = video.get("side_data_list") or []
    rotation = next(
        (
            item.get("rotation")
            for item in side_data
            if isinstance(item, dict) and item.get("rotation") is not None
        ),
        0,
    )
    try:
        rotation = int(round(float(rotation))) % 360
    except (TypeError, ValueError):
        rotation = 0
    encoded_width = video.get("width")
    encoded_height = video.get("height")
    frame_rate_raw = video.get("avg_frame_rate") or video.get("r_frame_rate")
    if not encoded_width or not encoded_height or not rational(frame_rate_raw):
        raise SystemExit("⛔ 无法确认原素材分辨率或真实帧率，禁止按默认值建项目")
    if not str(audio.get("sample_rate", "")).isdigit() or not audio.get("channels"):
        raise SystemExit("⛔ 无法确认原素材音频采样率或声道，禁止静默猜测")
    if rotation in {90, 270}:
        display_width, display_height = encoded_height, encoded_width
    else:
        display_width, display_height = encoded_width, encoded_height
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError) as exc:
        raise SystemExit("⛔ ffprobe 读不到有效时长") from exc
    stat = source.stat()
    fps = rational(frame_rate_raw)
    return {
        "path": str(source),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "duration_s": round(duration, 6),
        "duration_us": int(round(duration * 1_000_000)),
        "container": fmt.get("format_name"),
        "bit_rate": int(fmt["bit_rate"]) if str(fmt.get("bit_rate", "")).isdigit() else None,
        "video": {
            "codec": video.get("codec_name"),
            # width / height 一律表示用户在剪辑软件中看到的显示方向。
            # 手机竖拍常以 1920x1080 编码并通过 display matrix 旋转；若忽略
            # 该信息，后续 FCPXML 会被错误创建成横屏工程。
            "width": display_width,
            "height": display_height,
            "encoded_width": encoded_width,
            "encoded_height": encoded_height,
            "rotation": rotation,
            "fps": fps,
            "frame_rate_raw": frame_rate_raw,
            "pix_fmt": video.get("pix_fmt"),
            "color_range": video.get("color_range"),
            "color_space": video.get("color_space"),
            "color_transfer": video.get("color_transfer"),
            "color_primaries": video.get("color_primaries"),
        },
        "audio": {
            "codec": audio.get("codec_name"),
            "sample_rate": int(audio["sample_rate"])
            if str(audio.get("sample_rate", "")).isdigit()
            else None,
            "channels": audio.get("channels"),
            "channel_layout": audio.get("channel_layout"),
        },
    }


def normalize_core_expression(value: str) -> str:
    value = UNSAFE_FILENAME_RE.sub("", value).strip().strip(". ")
    if len(CHINESE_RE.findall(value)) < 2:
        raise SystemExit("⛔ core-expression 必须是至少含两个汉字的中文核心表达")
    if len(value) > 36:
        raise SystemExit("⛔ core-expression 最长 36 字，请保留真正的核心表达")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("name", nargs="?")
    parser.add_argument("--material-type", choices=MATERIAL_TYPES)
    parser.add_argument(
        "--core-expression",
        help="最终中文片名的核心表达；交付名自动加当日 YYYY-MM-DD 前缀",
    )
    parser.add_argument(
        "--color-reference-xml",
        type=Path,
        help="ffprobe 色彩元数据缺失时，提供一份同素材的 Final Cut 实机导出 XML",
    )
    parser.add_argument(
        "--font",
        help=f"剪映字幕字体名（默认 {DEFAULT_FONT_NAME}），也可直接给字体资源 id；"
        "想换字体只需改 config.json 的 font 字段后重跑 F1",
    )
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if not source.is_file():
        parser.error(f"素材不存在: {source}")
    name = args.name or source.stem
    work = source.parent / ".工作区" / name
    work.mkdir(parents=True, exist_ok=True)
    config_path = work / "config.json"

    existing: dict[str, Any] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"⛔ 已有 config.json 无法读取，拒绝覆盖: {exc}") from exc
        if not isinstance(loaded, dict):
            raise SystemExit("⛔ 已有 config.json 不是对象，拒绝覆盖")
        existing = loaded

    media = probe_media(source)
    material_type = args.material_type or existing.get("material_type") or "unknown"
    if material_type not in MATERIAL_TYPES:
        raise SystemExit(
            f"⛔ 已有 material_type={material_type!r} 不合法；"
            "请显式使用 --material-type 迁移"
        )

    # 字体是样式决策：--font 优先，其次沿用已有 config，最后回落默认字体。
    # 解析结果（名字 + 资源 id）一并落盘，生成与验收从此读同一份真值。
    try:
        font = resolve_font(args.font or existing.get("font"))
    except ValueError as exc:
        raise SystemExit(f"⛔ {exc}") from exc

    # 先复制旧配置，只覆盖本步可从当前素材确定的字段。
    config = dict(existing)
    config.update(
        {
            "name": name,
            "cname": existing.get("cname") or name,
            "full_us": media["duration_us"],
            "proxy": str(source),
            "original_path": str(source),
            "material_type": material_type,
            "font": {
                "name": font["name"],
                "resource_id": font["resource_id"],
                "is_vip": font["is_vip"],
            },
            "media": media,
        }
    )
    if args.color_reference_xml:
        actual = probe(source)
        config["color_contract"] = build_final_cut_color_contract(
            args.color_reference_xml, actual
        )
    elif config.get("color_contract"):
        # 已有证据交由交付层每次严格重验，p0 不静默删除。
        pass
    requested_expression = args.core_expression or existing.get("core_expression")
    if requested_expression:
        config["core_expression"] = normalize_core_expression(requested_expression)
        config.pop("_core_expression待定", None)
    else:
        config["_core_expression待定"] = (
            "完成 content_plan.json 后，将一句中文核心表达写入 core_expression；"
            "未设置时禁止最终交付。"
        )

    atomic_json(config_path, config)

    video = media["video"]
    dimensions = (
        f"{video.get('width')}x{video.get('height')}"
        if video.get("width") and video.get("height")
        else "?"
    )
    print(f"✅ 工作区就绪: {work}")
    print(
        f"   素材 {source.name} · {media['duration_s']:.1f}s · {dimensions} · "
        f"{video.get('fps') or '?'}fps · 编码 {video.get('encoded_width')}x"
        f"{video.get('encoded_height')} · 旋转 {video.get('rotation')}°"
    )
    print(f"   material_type: {material_type}")
    print(
        f"   字幕字体: {font['name']}（资源 id {font['resource_id']}"
        f"{' · VIP' if font['is_vip'] else ''}）"
    )
    if config.get("core_expression"):
        print(f"   中文核心表达: {config['core_expression']}（交付时自动加当日日期）")
    else:
        print("   ⚠ 中文核心表达待定：先做 content_plan，再写入 config.core_expression")
    if material_type not in {"single_speaker", "single_subject_interview"}:
        print(
            "   ⛔ 当前流水线只支持 single_speaker / "
            "single_subject_interview；"
            "multi_speaker/unknown 已记录但必须停止，不会默认按口播继续。"
        )
    elif material_type == "single_subject_interview":
        print(
            "   • 采访模式：写 speaker_turns.json，并将所有 interviewer 词纳入 keep.drop"
        )
    words = sorted(work.glob("*_words.json"))
    if words:
        print(f"   转写已有: {words[0].name}")
    elif os.name == "nt":
        # Windows 分支：MLX 无 Windows 轮子，转写改由 CTranslate2 版
        # faster-whisper 承担（同款 large-v3-turbo 权重）。参数在适配器内
        # 已逐项对齐 SKILL.md:113-117，故此处只给命令、不复述参数。
        transcriber = Path(__file__).resolve().parent.parent / "脚本_win" / "转写_faster_whisper.py"
        print("   ⚠ 尚无 *_words.json。请使用以下完整 ASR 命令：")
        print(f'     "{sys.executable}" "{transcriber}" "{source}" \\')
        print(f'       --outdir "{work}" --model large-v3-turbo --language zh')
        print("     参数已对齐 Mac 侧 mlx_whisper 基线"
              "（condition_on_previous_text=False / 贪心 / 不做 VAD 过滤）")
        print(f"     将输出规范为 {name}_words.json")
    else:
        print("   ⚠ 尚无 *_words.json。请使用以下完整 ASR 命令：")
        asr_bin = Path.home() / ".local/bin/mlx_whisper"
        print(
            f'     "{asr_bin}" "{source}" --model mlx-community/whisper-large-v3-turbo \\'
        )
        print(
            "       --language zh --condition-on-previous-text False "
            "--word-timestamps True \\"
        )
        print(f'       --output-format json --output-dir "{work}"')
        print(f"     将输出规范为 {name}_words.json")
    print(
        "   下一阶段：p1a 建词轴 → 按素材类型读取粗剪子技能 → content_plan 全素材内容地图/开头候选 → "
        "审阅 keep → validate-keep → "
        "p1a2 对齐 → p1c 表达候选逐项裁决 → p1 建段 → structure → p2 rough_segments → "
        "p2b 切口清单/逐项听审 → F1 独立声学验收/冻结 → 审阅 cards → finalize"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
