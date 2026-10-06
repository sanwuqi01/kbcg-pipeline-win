#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 CapCutMate 的 pyJianYingDraft 字体元数据生成 skill 内置字体表。

为什么要有这一步：
  剪映字幕的字体在草稿里存的是**资源 id**，不是字体名。生成草稿时用
  CapCutMate（pyJianYingDraft）把字体名换成 id；但验收脚本和建工作区
  脚本跑在管线解释器里，装的是 faster-whisper，不能 import CapCutMate。
  所以把字体表固化成一份 JSON 随 skill 走，两处引用同一份真值。

用法：
  python gen_font_table.py [--font-meta <font_meta.py 路径>] [--out <输出 JSON>]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_FONT_META = (
    Path.home() / "Developer" / "capcut-mate" / "src"
    / "pyJianYingDraft" / "metadata" / "font_meta.py"
)

# 形如：    Alice_Regular        = EffectMeta("Alice-Regular", False, "7238…", "14443129", "2d35…", [])
ENTRY_RE = re.compile(
    r'^\s*(?P<member>[A-Za-z0-9_\u3400-\u9fff]+)\s*=\s*EffectMeta\('
    r'\s*"(?P<display>(?:[^"\\]|\\.)*)"\s*,\s*(?P<vip>True|False)\s*,'
    r'\s*"(?P<resource_id>[0-9]+)"\s*,\s*"(?P<effect_id>[0-9]+)"\s*,'
    r'\s*"(?P<md5>[0-9a-fA-F]+)"\s*,\s*\[\s*\]\s*\)\s*$'
)


def parse_font_meta(path: Path) -> tuple[dict, list[str]]:
    fonts: dict[str, dict] = {}
    conflicts: list[str] = []
    text = path.read_text(encoding="utf-8")
    for number, line in enumerate(text.splitlines(), start=1):
        match = ENTRY_RE.match(line)
        if not match:
            continue
        row = match.groupdict()
        name = row["display"]
        entry = {
            "member": row["member"],
            "resource_id": row["resource_id"],
            "is_vip": row["vip"] == "True",
        }
        if name in fonts and fonts[name]["resource_id"] != entry["resource_id"]:
            conflicts.append(
                f"第{number}行 字体名重复且 id 不同: {name} "
                f"({fonts[name]['resource_id']} vs {entry['resource_id']})"
            )
        fonts[name] = entry
    return fonts, conflicts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font-meta", type=Path, default=DEFAULT_FONT_META)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "字体" / "剪映字体表.json",
    )
    args = parser.parse_args()

    font_meta = args.font_meta.expanduser().resolve()
    if not font_meta.is_file():
        raise SystemExit(f"⛔ 找不到 font_meta.py: {font_meta}")

    fonts, conflicts = parse_font_meta(font_meta)
    if not fonts:
        raise SystemExit(f"⛔ 没能从 {font_meta} 解析出任何字体条目")

    payload = {
        "schema": "jianying-font-table@1",
        "source": str(font_meta),
        "count": len(fonts),
        "free_count": sum(1 for row in fonts.values() if not row["is_vip"]),
        "fonts": dict(sorted(fonts.items())),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )

    print(f"✅ 字体表已生成: {args.out}")
    print(f"   共 {payload['count']} 个字体（免费 {payload['free_count']} 个）")
    for name in ("新青年体", "聚珍体"):
        row = fonts.get(name)
        print(f"   {name}: {row['resource_id'] if row else '未找到'}"
              f"{' · VIP' if row and row['is_vip'] else ''}")
    if conflicts:
        print(f"   ⚠ 字体名冲突 {len(conflicts)} 条（已取后者）：")
        for item in conflicts[:5]:
            print("     ", item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
