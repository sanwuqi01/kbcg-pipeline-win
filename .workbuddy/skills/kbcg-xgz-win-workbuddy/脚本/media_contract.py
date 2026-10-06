#!/usr/bin/env python3
"""素材技术属性与 FCPXML 时间换算的单一契约。"""
from __future__ import annotations

import json
import hashlib
import re
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path


ANALYSIS_FPS = 60


_FCPXML_FORMAT_PREFIXES = {
    (1280, 720): "FFVideoFormat720p",
    (1920, 1080): "FFVideoFormat1080p",
    (3840, 2160): "FFVideoFormat3840x2160p",
}

_FCPXML_RATE_SUFFIXES = {
    Fraction(24000, 1001): "2398",
    Fraction(24, 1): "24",
    Fraction(25, 1): "25",
    Fraction(30000, 1001): "2997",
    Fraction(30, 1): "30",
    Fraction(50, 1): "50",
    Fraction(60000, 1001): "5994",
    Fraction(60, 1): "60",
}

_TIMECODE_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})([:;])(\d{2})$")


def rate(raw: str) -> Fraction:
    try:
        value = Fraction(str(raw))
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"非法帧率：{raw!r}") from exc
    if value <= 0:
        raise ValueError(f"非法帧率：{raw!r}")
    return value


def round_fraction(value: Fraction) -> int:
    """正数四舍五入，避免 Python round 的银行家舍入。"""
    if value < 0:
        raise ValueError("时间帧不能为负")
    return (value.numerator * 2 + value.denominator) // (2 * value.denominator)


def format_time(value: Fraction) -> str:
    if value == 0:
        return "0s"
    return f"{value.numerator}/{value.denominator}s"


def frame_duration(raw: str) -> str:
    return format_time(1 / rate(raw))


def fcpxml_format_name(width: int, height: int, raw: str) -> str | None:
    """Return Final Cut's canonical name for a known standard progressive format.

    Portrait and other custom rasters intentionally return ``None``: Final Cut
    accepts those as custom formats, whereas inventing a standard format name can
    make the sequence semantically invalid even when the XML passes the DTD.
    """
    prefix = _FCPXML_FORMAT_PREFIXES.get((width, height))
    suffix = _FCPXML_RATE_SUFFIXES.get(rate(raw))
    return prefix + suffix if prefix and suffix else None


COLOR_CONTRACT_SCHEMA = "kbcg-xgz/color_contract@1"


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def file_uri_to_path(src: str) -> Path:
    """把 FCPXML ``media-rep@src`` 的 file URI 还原成本机路径。

    POSIX 上 ``urlparse`` 得到 ``/Users/...``，直接构造 Path 即正确。
    Windows 上同样得到 ``/C:/Users/...``，但 ``Path('/C:/...')`` 会把盘符当成
    目录名，丢掉分隔冒号（变成 ``C:Users...`` 这种盘符相对路径）。``url2pathname``
    在 Windows 会去掉盘符前的前导斜杠，在 POSIX 上等价于 unquote，因此对两端都正确。
    """
    return Path(urllib.request.url2pathname(urllib.parse.urlparse(src).path))


def _source_fingerprint(actual: dict) -> dict:
    return {
        "path": actual["path"],
        "size_bytes": actual["size_bytes"],
        "mtime_ns": actual["mtime_ns"],
        "duration": str(actual["duration"]),
        "width": actual["width"],
        "height": actual["height"],
        "rotation": actual["rotation"],
        "frame_rate_raw": actual["frame_rate_raw"],
        "audio_sample_rate": actual["audio_sample_rate"],
        "audio_channels": actual["audio_channels"],
    }


def build_final_cut_color_contract(reference_xml: str | Path, actual: dict) -> dict:
    """从 Final Cut 实机导出中只借用已观测的 colorSpace，其他属性必须全部同源。"""
    path = Path(reference_xml).expanduser().resolve()
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"Final Cut 色彩参考 XML 无法读取：{path}") from exc
    source_path = Path(actual["path"]).resolve()
    matches: list[tuple[ET.Element, ET.Element]] = []
    for asset in root.findall("./resources/asset"):
        rep = asset.find("media-rep")
        src = rep.get("src") if rep is not None else None
        if not src:
            continue
        observed = file_uri_to_path(src).resolve()
        if observed == source_path:
            fmt = root.find(f"./resources/format[@id='{asset.get('format')}']")
            if fmt is not None:
                matches.append((asset, fmt))
    if len(matches) != 1:
        raise ValueError(f"色彩参考 XML 必须唯一引用当前原素材，实际匹配 {len(matches)} 条")
    asset, fmt = matches[0]
    expected_fd = seconds(frame_duration(actual["frame_rate_raw"]))
    checks = {
        "width": (fmt.get("width"), str(actual["width"])),
        "height": (fmt.get("height"), str(actual["height"])),
        "audioRate": (asset.get("audioRate"), str(actual["audio_sample_rate"])),
        "audioChannels": (asset.get("audioChannels"), str(actual["audio_channels"])),
    }
    drift = [name for name, (found, expected) in checks.items() if found != expected]
    try:
        reference_fd = seconds(fmt.get("frameDuration"))
    except (TypeError, ValueError, ZeroDivisionError):
        drift.append("frameDuration")
    else:
        if reference_fd != expected_fd:
            drift.append("frameDuration")
    if drift:
        raise ValueError("色彩参考 XML 与原素材属性不同源：" + ", ".join(drift))
    asset_duration = seconds(asset.get("duration"))
    if abs(asset_duration - actual["duration"]) > expected_fd:
        raise ValueError("色彩参考 XML 的 asset duration 与原素材相差超过一帧")
    observed_color = fmt.get("colorSpace")
    if not isinstance(observed_color, str) or not observed_color.strip():
        raise ValueError("色彩参考 XML 没有 format@colorSpace")
    return {
        "schema": COLOR_CONTRACT_SCHEMA,
        "state": "final_cut_observed",
        "fcpxml_color_space": observed_color,
        "source_fingerprint": _source_fingerprint(actual),
        "reference_xml": {"path": str(path), "sha256": sha256_file(path)},
        "provenance": "Final Cut 实机导出；只借用 colorSpace，不继承项目帧率或其他属性",
    }


def _validated_observed_color(actual: dict, contract: dict | None) -> str | None:
    if not isinstance(contract, dict) or contract.get("schema") != COLOR_CONTRACT_SCHEMA:
        return None
    if contract.get("state") != "final_cut_observed":
        raise ValueError("color_contract.state 非法")
    if contract.get("source_fingerprint") != _source_fingerprint(actual):
        raise ValueError("Final Cut 色彩观测证据与当前素材指纹不一致")
    reference = contract.get("reference_xml")
    if not isinstance(reference, dict):
        raise ValueError("color_contract.reference_xml 缺失")
    reference_path = Path(reference.get("path") or "").expanduser().resolve()
    if not reference_path.is_file() or sha256_file(reference_path) != reference.get("sha256"):
        raise ValueError("Final Cut 色彩观测 XML 缺失或哈希已变")
    # 重新解析参考，防止 config 中的字符串被手改。
    rebuilt = build_final_cut_color_contract(reference_path, actual)
    if rebuilt["fcpxml_color_space"] != contract.get("fcpxml_color_space"):
        raise ValueError("color_contract.fcpxml_color_space 与参考 XML 不一致")
    return rebuilt["fcpxml_color_space"]


def fcpxml_color_space(actual: dict, contract: dict | None = None) -> str:
    """优先使用原素材明确元数据；缺失时只接受严格同源的 Final Cut 实机观测。"""
    key = (
        actual.get("color_primaries"),
        actual.get("color_transfer"),
        actual.get("color_space"),
    )
    if key == ("bt709", "bt709", "bt709"):
        return "1-1-1 (Rec. 709)"
    observed = _validated_observed_color(actual, contract)
    if observed:
        return observed
    raise ValueError(
        "Final Cut 色彩空间尚未建立经验证的映射："
        f"primaries={key[0]!r}, transfer={key[1]!r}, matrix={key[2]!r}"
    )


def fcpxml_source_timecode(actual: dict) -> tuple[int, str]:
    """Return the asset's FCP native-frame origin and DF/NDF display mode.

    Final Cut schedules asset edits in the media asset's local timeline. Camera
    files with embedded timecode therefore cannot be treated as if they started
    at zero. The 59.94 DJI samples in the user's Final Cut exports use a 29.97
    timecode counter doubled onto the 59.94 media grid; preserve that convention.
    """
    raw = actual.get("frame_rate_raw")
    source_rate = rate(str(raw))
    value = actual.get("timecode")
    if not value:
        return 0, "NDF"
    match = _TIMECODE_RE.fullmatch(str(value))
    if not match:
        raise ValueError(f"Final Cut 无法解析素材时间码：{value!r}")
    hour, minute, second, separator, frame = match.groups()
    hour_i, minute_i, second_i, frame_i = map(
        int, (hour, minute, second, frame)
    )
    if minute_i >= 60 or second_i >= 60:
        raise ValueError(f"素材时间码越界：{value!r}")

    if source_rate == Fraction(30000, 1001):
        counter_rate, multiplier = 30, 1
    elif source_rate == Fraction(60000, 1001):
        # 已用 Final Cut 导出的 DJI 59.94 DF 素材逐项验证。
        counter_rate, multiplier = 30, 2
    elif source_rate == Fraction(24000, 1001):
        counter_rate, multiplier = 24, 1
    elif source_rate.denominator == 1 and source_rate in {
        Fraction(24), Fraction(25), Fraction(30)
    }:
        counter_rate, multiplier = int(source_rate), 1
    else:
        raise ValueError(
            f"素材带时间码 {value}，但帧率 {raw} 没有经 Final Cut 样本验证"
        )
    if frame_i >= counter_rate:
        raise ValueError(f"素材时间码帧号越界：{value!r}")

    elapsed_seconds = hour_i * 3600 + minute_i * 60 + second_i
    base_frames = elapsed_seconds * counter_rate + frame_i
    tc_format = "DF" if separator == ";" else "NDF"
    if tc_format == "DF":
        if source_rate not in {
            Fraction(30000, 1001), Fraction(60000, 1001)
        }:
            raise ValueError(f"非 NTSC 帧率不得使用丢帧时间码：{value!r}")
        total_minutes = hour_i * 60 + minute_i
        base_frames -= 2 * (total_minutes - total_minutes // 10)
    return base_frames * multiplier, tc_format


def analysis_to_native(frame: int, raw: str) -> int:
    if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
        raise ValueError(f"非法分析帧：{frame!r}")
    return round_fraction(Fraction(frame, ANALYSIS_FPS) * rate(raw))


def native_time(frame: int, raw: str) -> str:
    if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
        raise ValueError(f"非法原生帧：{frame!r}")
    return format_time(Fraction(frame, 1) / rate(raw))


def analysis_point_time(frame: int, raw: str) -> str:
    return native_time(analysis_to_native(frame, raw), raw)


def analysis_range_native(start: int, end: int, raw: str) -> tuple[int, int]:
    if end <= start:
        raise ValueError(f"非法分析区间：{start}→{end}")
    native_start = analysis_to_native(start, raw)
    native_end = analysis_to_native(end, raw)
    if native_end <= native_start:
        raise ValueError(f"区间量化后不足一帧：{start}→{end}")
    return native_start, native_end - native_start


def seconds(value: str) -> Fraction:
    if not isinstance(value, str) or not value.endswith("s"):
        raise ValueError(f"非法 FCPXML 时间：{value!r}")
    return Fraction(value[:-1])


def frames_on_grid(value: str, duration: str) -> int:
    raw = seconds(value) / seconds(duration)
    if raw.denominator != 1:
        raise ValueError(f"时间 {value} 不在 {duration} 帧网格")
    return raw.numerator


def probe(source: str | Path) -> dict:
    path = Path(source).expanduser().resolve()
    try:
        run = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_format", "-show_streams",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, check=True, timeout=60,
        )
        payload = json.loads(run.stdout)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ValueError(f"ffprobe 无法读取素材：{path}") from exc
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    if not video:
        raise ValueError(f"素材没有视频流：{path}")
    rotation = 0
    for item in video.get("side_data_list") or []:
        if isinstance(item, dict) and item.get("rotation") is not None:
            rotation = int(round(float(item["rotation"]))) % 360
            break
    encoded_width = int(video["width"])
    encoded_height = int(video["height"])
    if rotation in {90, 270}:
        width, height = encoded_height, encoded_width
    else:
        width, height = encoded_width, encoded_height
    fps_raw = video.get("avg_frame_rate") or video.get("r_frame_rate")
    rate(str(fps_raw))
    duration = Fraction(str((payload.get("format") or {}).get("duration")))
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "width": width,
        "height": height,
        "encoded_width": encoded_width,
        "encoded_height": encoded_height,
        "rotation": rotation,
        "frame_rate_raw": str(fps_raw),
        "fps": float(rate(str(fps_raw))),
        "color_primaries": video.get("color_primaries"),
        "color_transfer": video.get("color_transfer"),
        "color_space": video.get("color_space"),
        "timecode": (video.get("tags") or {}).get("timecode"),
        "duration": duration,
        "audio_sample_rate": int(audio["sample_rate"])
        if str(audio.get("sample_rate", "")).isdigit() else None,
        "audio_channels": int(audio["channels"])
        if str(audio.get("channels", "")).isdigit() else None,
    }


def assert_config_matches(config: dict, actual: dict) -> None:
    media = config.get("media") or {}
    video = media.get("video") or {}
    audio = media.get("audio") or {}
    checks = {
        "显示宽度": (video.get("width"), actual["width"]),
        "显示高度": (video.get("height"), actual["height"]),
        "编码宽度": (video.get("encoded_width"), actual["encoded_width"]),
        "编码高度": (video.get("encoded_height"), actual["encoded_height"]),
        "旋转": (video.get("rotation"), actual["rotation"]),
        "原始帧率": (video.get("frame_rate_raw"), actual["frame_rate_raw"]),
        "音频采样率": (audio.get("sample_rate"), actual["audio_sample_rate"]),
        "音频声道": (audio.get("channels"), actual["audio_channels"]),
    }
    # 旧工作区可能没有 encoded_width/rotation 等新增字段；ffprobe 才是当前真值。
    # 已经声明的字段必须一致，未声明字段由生成器使用本次探测值，不回写、不猜测。
    drift = [
        f"{name} config={declared!r} 实际={found!r}"
        for name, (declared, found) in checks.items()
        if declared is not None and str(declared) != str(found)
    ]
    if drift:
        raise ValueError("素材属性已漂移：" + "；".join(drift))


def caption_profile(width: int, height: int) -> dict:
    """只返回用户在 Final Cut 实机确认过的精确画幅 profile。

    Final Cut 的 Basic Title 字号与 Position 不能按像素宽度线性缩放。
    未确认画幅必须停止，由用户在实际工程中校正后再登记。
    """
    confirmed = {
        (1080, 1920): {
            "name": "portrait_1080x1920",
            "font_size": 50,
            "xml_y": -220,
        },
        (1920, 1080): {
            "name": "landscape_1920x1080",
            "font_size": 118,
            "xml_y": -470,
        },
        (3840, 2160): {
            "name": "landscape_3840x2160",
            "font_size": 120,
            "xml_y": -478.53,
        },
        (2160, 3840): {
            "name": "portrait_2160x3840",
            "font_size": 52,
            "xml_y": -190,
            "shadow": {
                "color": "0 0 0",
                "opacity": 0.75,
                "blur": 0.63,
                "distance": 2,
                "angle": 315,
            },
        },
    }
    selected = confirmed.get((width, height))
    if selected is None:
        raise ValueError(
            f"画幅 {width}x{height} 没有经用户在 Final Cut 实机确认；"
            "必须人工给字幕 profile，不得按分辨率推算"
        )
    profile = {
        "name": selected["name"],
        "width": width,
        "height": height,
        "font": "XQjuzhentiZJSY",
        "font_size": selected["font_size"],
        "xml_y": selected["xml_y"],
    }
    if "shadow" in selected:
        profile["shadow"] = dict(selected["shadow"])
    return profile


def fcpxml_shadow_attributes(profile: dict) -> dict[str, str]:
    """Return validated FCPXML text-style shadow attributes for one profile."""
    shadow = profile.get("shadow")
    if shadow is None:
        return {}
    required = {"color", "opacity", "blur", "distance", "angle"}
    if not isinstance(shadow, dict) or set(shadow) != required:
        raise ValueError("caption_profile.shadow 字段不完整")
    color = shadow.get("color")
    try:
        opacity = float(shadow["opacity"])
        blur = float(shadow["blur"])
        distance = float(shadow["distance"])
        angle = float(shadow["angle"])
    except (TypeError, ValueError) as exc:
        raise ValueError("caption_profile.shadow 数值非法") from exc
    if color != "0 0 0" or not 0 <= opacity <= 1 or blur < 0 or distance < 0:
        raise ValueError("caption_profile.shadow 颜色、不透明度、模糊或距离非法")
    if not 0 <= angle < 360:
        raise ValueError("caption_profile.shadow 角度必须在 [0, 360) 内")

    def number(value: float) -> str:
        return str(int(value)) if value.is_integer() else format(value, ".12g")

    return {
        "shadowColor": f"{color} {number(opacity)}",
        "shadowOffset": f"{number(distance)} {number(angle)}",
        "shadowBlurRadius": number(blur),
    }
