#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把冻结后的 FCP 序列化结果转换为剪映可识别的原生字幕 FCPXML。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.dom import Node, minidom


# 剪映把 FCPXML 的 118 映射为界面约 5 号；实机标定 188 对应约 8 号。
# 只改剪映分支，Final Cut 的 118 与 Y=-470 固化值保持不变。
JIANying_FCPXML_FONT_SIZE = "188"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--project-name")
    p.add_argument("--verify-only", action="store_true")
    return p


def expected_name(work: Path, override: str | None) -> str:
    cfg = json.loads((work / "config.json").read_text(encoding="utf-8"))
    core = cfg.get("core_expression")
    if not isinstance(core, str) or not core.strip() or not re.search(r"[\u3400-\u9fff]", core):
        raise SystemExit("⛔ p4j: config.core_expression 必须是非空中文核心表达")
    core = core.strip()
    if re.search(r"[\\/:\0\r\n\t]", core):
        raise SystemExit("⛔ p4j: 中文核心表达含路径分隔符或控制字符")
    stem = f"{dt.date.today().isoformat()}_{core}_剪映"
    name = override or stem
    if not re.fullmatch(re.escape(stem) + r"(?:_第(?:[2-9]|[1-9]\d+)版)?", name):
        raise SystemExit(f"⛔ p4j: project name 必须是「{stem}」或其递增版本")
    return name


def transform(work: Path, project_name: str) -> bytes:
    script_dir = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="kbcg-xgz-jianying-") as tmp:
        base_xml = Path(tmp) / "base.fcpxml"
        # 复用已验证的 Final Cut 序列化器，确保视频主故事线、源时码和字幕时码不分叉。
        subprocess.run(
            [sys.executable, str(script_dir / "p4f_出FCPXML.py"), str(work), str(base_xml)],
            check=True,
        )
        doc = minidom.parse(str(base_xml))

    projects = doc.getElementsByTagName("project")
    if len(projects) != 1:
        raise SystemExit(f"⛔ p4j: 期望唯一 project，实际 {len(projects)}")
    projects[0].setAttribute("name", project_name)

    converted = 0
    for title in list(doc.getElementsByTagName("title")):
        if title.getAttribute("lane") != "1":
            continue
        caption = doc.createElement("caption")
        for key in ("lane", "offset", "duration", "enabled"):
            if title.hasAttribute(key):
                caption.setAttribute(key, title.getAttribute(key))
        caption.setAttribute("role", "caption.ITT.zh")

        for child in list(title.childNodes):
            if child.nodeType != Node.ELEMENT_NODE or child.tagName not in {"text", "text-style-def"}:
                continue
            clone = child.cloneNode(deep=True)
            if clone.tagName == "text":
                # 让剪映按原生字幕的底部锚点解释位置；不携带 Final Cut 私有 Position 参数。
                clone.setAttribute("placement", "bottom")
            elif clone.tagName == "text-style-def":
                for style in clone.getElementsByTagName("text-style"):
                    style.setAttribute("fontSize", JIANying_FCPXML_FONT_SIZE)
            caption.appendChild(clone)
        title.parentNode.replaceChild(caption, title)
        converted += 1

    if converted == 0:
        raise SystemExit("⛔ p4j: 没有可转换的主字幕 title")

    # 没有顶部标题时，Basic Title 资源已不再被引用，删除可避免剪映误判普通文本包装。
    if not doc.getElementsByTagName("title"):
        for effect in list(doc.getElementsByTagName("effect")):
            if effect.getAttribute("id") == "rTitle":
                effect.parentNode.removeChild(effect)

    return doc.toxml(encoding="UTF-8")


def main() -> int:
    args = parser().parse_args()
    work = args.work.expanduser().resolve()
    output = args.output.expanduser().resolve()
    project_name = expected_name(work, args.project_name)
    rendered = transform(work, project_name)

    if args.verify_only:
        if not output.is_file():
            raise SystemExit(f"⛔ p4j verify: XML 不存在: {output}")
        if output.read_bytes() != rendered:
            raise SystemExit("⛔ p4j verify: 现有 XML 与冻结输入重新序列化结果不一致")
        print("✅ 剪映 XML 可重复序列化校验通过")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(rendered)
    print(f"✅ 剪映原生字幕 XML 已生成: {output}")
    print(f"   项目 {project_name} · 主故事线视频不变 · 主字幕已转为 caption")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
