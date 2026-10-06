#!/usr/bin/env python3
"""在用户批准的多素材粗剪 FCPXML 上只增加 Basic Title 字幕。"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

from media_contract import caption_profile, fcpxml_shadow_attributes


WHITE = "1 1 1 1"
GOLD = "0.995808 0.800124 0.399987 1"
POSITION_KEY = "9999/999166631/999166633/1/100/101"


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sec(value: str) -> Fraction:
    return Fraction(value.removesuffix("s"))


def frames(value: str, frame_duration: str) -> int:
    raw = sec(value) / sec(frame_duration)
    if raw.denominator != 1:
        raise ValueError(f"时间不在项目帧网格 {frame_duration}：{value}")
    return raw.numerator


def t(value: int, frame_duration: str) -> str:
    result = value * sec(frame_duration)
    return "0s" if result == 0 else f"{result.numerator}/{result.denominator}s"


def add_title(parent, ref, offset_f, duration_f, text, keyword, profile, counter, frame_duration):
    title = ET.Element("title", {
        "ref": ref, "lane": "1",
        "offset": t(offset_f, frame_duration),
        "duration": t(duration_f, frame_duration),
    })
    ET.SubElement(title, "param", {"name": "Position", "key": POSITION_KEY, "value": f"0 {profile['xml_y']}"})
    text_node = ET.SubElement(title, "text")
    if keyword and keyword in text:
        before, _, after = text.partition(keyword)
        runs = [(before, WHITE), (keyword, GOLD), (after, WHITE)]
    else:
        runs = [(text, WHITE)]
    for value, color in runs:
        if not value:
            continue
        counter[0] += 1
        style_id = f"ts{counter[0]}"
        ET.SubElement(text_node, "text-style", {"ref": style_id}).text = value
        style_def = ET.SubElement(title, "text-style-def", {"id": style_id})
        style_attributes = {
            "font": profile["font"], "fontSize": str(profile["font_size"]),
            "fontFace": "Regular", "fontColor": color, "alignment": "center",
        }
        style_attributes.update(fcpxml_shadow_attributes(profile))
        ET.SubElement(style_def, "text-style", style_attributes)
    # asset-clip DTD 要求 anchor item（title）在 marker 之前。
    marker_pos = next((i for i, child in enumerate(list(parent)) if child.tag in {"marker", "chapter-marker", "rating", "keyword"}), len(parent))
    parent.insert(marker_pos, title)


def stripped_structure(root: ET.Element, effect_id: str, remove_effect: bool) -> bytes:
    """移除本脚本新增字幕后做结构指纹，证明其他节点没有变化。"""
    clone = copy.deepcopy(root)
    for parent in clone.iter():
        for child in list(parent):
            if child.tag == "title" and child.attrib.get("ref") == effect_id:
                parent.remove(child)
            elif remove_effect and child.tag == "effect" and child.attrib.get("id") == effect_id:
                parent.remove(child)
    for node in clone.iter():
        if node.text is not None and not node.text.strip():
            node.text = None
        if node.tail is not None and not node.tail.strip():
            node.tail = None
    return ET.tostring(clone, encoding="utf-8", short_empty_elements=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("work")
    parser.add_argument("output")
    parser.add_argument(
        "--project-name",
        help="仅为旧调用兼容保留；人工 XML 的内部项目名不会修改",
    )
    args = parser.parse_args()
    work = Path(args.work).resolve()
    output = Path(args.output).resolve()
    cfg = read_json(work / "config.json")
    baseline = read_json(work / "human_baseline.json")
    lock = read_json(work / "human_rough_lock.json")
    plan = read_json(work / "captions_plan.json")
    xml_path = Path(baseline["xml_path"])
    if sha256(xml_path) != baseline["xml_sha256"] or lock["human_xml_sha256"] != baseline["xml_sha256"]:
        sys.exit("⛔ 人工粗剪 XML 或锁哈希已漂移")
    if sha256(work / "rough_segments.json") != lock["rough_segments_sha256"]:
        sys.exit("⛔ rough_segments 已漂移")
    if sha256(work / "word_track.json") != lock["word_track_sha256"]:
        sys.exit("⛔ word_track 已漂移")
    if plan.get("total_f") != baseline["duration_f"] or len(plan.get("segments_tl", [])) != lock["clip_count"]:
        sys.exit("⛔ 字幕计划与人工粗剪长度不一致")
    profile = cfg["caption_profile"]
    if (profile.get("width"), profile.get("height")) != (baseline["width"], baseline["height"]):
        sys.exit("⛔ 字幕 profile 与人工项目画幅不一致")
    try:
        expected_profile = caption_profile(baseline["width"], baseline["height"])
    except ValueError:
        expected_profile = None
    if expected_profile is not None and profile != expected_profile:
        sys.exit(
            f"⛔ 字幕 profile 漂移：当前 {profile}，期望 {expected_profile}"
        )
    tree = ET.parse(xml_path)
    root = tree.getroot()
    resources = root.find("resources")
    sequence = root.find(".//project/sequence")
    spine = root.find(".//project/sequence/spine")
    project = root.find(".//project")
    if resources is None or sequence is None or spine is None or project is None:
        sys.exit("⛔ 人工 XML 结构不完整")
    format_id = sequence.attrib.get("format")
    fmt = root.find(f"./resources/format[@id='{format_id}']")
    if fmt is None or not fmt.attrib.get("frameDuration"):
        sys.exit("⛔ 人工 XML 缺项目 frameDuration")
    frame_duration = fmt.attrib["frameDuration"]
    project_fps = float(1 / sec(frame_duration))
    if abs(float(plan.get("fps", project_fps)) - project_fps) > 0.001:
        sys.exit("⛔ 字幕计划帧率与人工 XML 项目帧率不一致")
    original_structure = stripped_structure(root, "", False)
    clips = spine.findall("asset-clip")
    if len(clips) != lock["clip_count"]:
        sys.exit("⛔ 人工 XML 片段数已变化")
    for clip, expected in zip(clips, baseline["clips"]):
        actual = {k: clip.attrib.get(k, "") for k in ("ref", "offset", "start", "duration", "name")}
        want = {k: expected.get(k, "") for k in ("ref", "offset", "start", "duration", "name")}
        if actual != want:
            sys.exit(f"⛔ 人工片段 {expected['index']} 已变化")
    effect_id = "rHumanCaptionBasicTitle"
    effect_added = root.find(f"./resources/effect[@id='{effect_id}']") is None
    if effect_added:
        ET.SubElement(resources, "effect", {
            "id": effect_id, "name": "Basic Title",
            "uid": ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti",
        })
    cards = sorted(plan.get("normal", []) + plan.get("highlight", []), key=lambda c: (c["si"], c["sf"]))
    by_segment = {}
    for card in cards:
        by_segment.setdefault(card["si"], []).append(card)
    if set(by_segment) != set(range(len(clips))):
        sys.exit("⛔ 不是每个粗剪片段都有字幕")
    counter = [0]
    for index, (clip, st) in enumerate(zip(clips, plan["segments_tl"])):
        source_start_f = frames(clip.attrib["start"], frame_duration)
        for card in by_segment[index]:
            if not (st["tl_in_f"] <= card["sf"] < card["ef"] <= st["tl_out_f"]):
                sys.exit(f"⛔ 字幕卡越出片段 {index}")
            rel_f = card["sf"] - st["tl_in_f"]
            add_title(
                clip, effect_id, source_start_f + rel_f, card["ef"] - card["sf"],
                card["text"], card.get("keyword"), profile, counter, frame_duration,
            )
    # 人工 XML 就是已批准粗剪；内部项目名、UID、modDate 也属于只读基准。
    if stripped_structure(root, effect_id, effect_added) != original_structure:
        sys.exit("⛔ 除字幕外的人工 XML 结构发生变化，拒绝交付")
    if output.exists():
        sys.exit(f"⛔ 输出已存在，拒绝覆盖：{output}")
    ET.indent(tree, space="    ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    output.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n\n' + body + "\n", encoding="utf-8")
    dtd = Path("/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework/Versions/A/Resources") / f"FCPXMLv{root.attrib['version'].replace('.', '_')}.dtd"
    checked = subprocess.run(
        ["xmllint", "--dtdvalid", dtd.name, "--noout", str(output)],
        cwd=str(dtd.parent), capture_output=True, text=True,
    )
    if checked.returncode:
        output.unlink(missing_ok=True)
        sys.exit("⛔ DTD 验收失败：\n" + "\n".join(checked.stderr.splitlines()[:12]))
    # 最后一次独立确认：删除新增字幕后，整个 XML 结构都必须与输入一致。
    generated_root = ET.parse(output).getroot()
    if stripped_structure(generated_root, effect_id, effect_added) != original_structure:
        output.unlink(missing_ok=True)
        sys.exit("⛔ 生成后除字幕外的 XML 结构漂移")
    generated = generated_root.findall(".//project/sequence/spine/asset-clip")
    for actual, expected in zip(generated, baseline["clips"]):
        for key in ("ref", "offset", "start", "duration"):
            if actual.attrib.get(key) != expected[key]:
                output.unlink(missing_ok=True)
                sys.exit(f"⛔ 生成后片段 {expected['index']} 的 {key} 漂移")
    print(f"✅ 人工粗剪包装 XML：{output}")
    print(
        f"   只新增字幕 · 项目属性与 {len(clips)} 个视频段全部未改 · "
        f"{baseline['width']}x{baseline['height']} · {project_fps:.3f}fps · "
        f"字幕 {len(cards)} · DTD {root.attrib['version']} 通过"
    )


if __name__ == "__main__":
    main()
