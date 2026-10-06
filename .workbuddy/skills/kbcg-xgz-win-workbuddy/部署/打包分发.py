#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""口播视频剪映出草稿（Windows 版）· 打包分发

把当前 Skill 打成可以直接交给下一个用户的「工作区式」分发 zip，两个变体一次产齐：

    python 打包分发.py                       # 完整版 + 标准版（默认）
    python 打包分发.py --variant full        # 只打完整版（含配图工厂）
    python 打包分发.py --variant lite        # 只打标准版（无配图工厂）
    python 打包分发.py --no-tests            # 跳过契约回归（快，但别常用）
    python 打包分发.py --out "D:\分享"        # 指定输出目录
    python 打包分发.py --workbench "D:\剪辑工作台"   # 指定工作台位置（默认自动找）

产物布局（zip 里只有一个顶层文件夹，解压出来就是一个 WorkBuddy 工作区）：

    口播视频剪映出草稿-xgz-win-workbuddy-完整版\
    ├─ README.md                              ← 快速上手（由 部署\工作区README.md 模板生成）
    ├─ .workbuddy\skills\kbcg-xgz-win-workbuddy\   ← Skill 本体（项目级，打开工作区即被识别）
    │   ├─ SKILL.md / 脚本\ / 脚本_win\ / references\ / 子技能\ / 知识库\
    │   ├─ 样本库\ / 字体\ / tests\ / 部署\ / agents\
    │   └─ 模型\.gitkeep                       ← 空壳占位，模型由 bootstrap 拉（3.4 GB 不打进包）
    └─ 剪辑工作台\                              ← 操作面：输入/输出/工作区/运行记录（空壳）+ 工具/配置/说明书
        └─ 配图工厂\                            ← 仅完整版；标准版整个剔除

两个变体的差别**只有**配图工厂（图标配图线：文稿 → 提示词 → AI 生图 → 抠图 → 触发词.png）。
剪辑主链（粗剪 → 字幕 → 剪映草稿）两版完全一样：p4i_配图对齐仍在标准版里，
用户手工把「触发词.png」放进期的 配图\ 目录照样能对齐，只是没有生产图标的工厂。

会做五件事：
  1. 校验必需文件齐全（缺一个就中止，不产坏包）
  2. 收集文件并打 zip —— 排除 模型/ 实体、__pycache__、.venv、.git、诊断残留
  3. 把 剪辑工作台 并入（运行时目录只带空壳，输入不带素材，批次只带跑通样例）
  4. 跑契约回归（在打包源上，确认没装坏）
  5. 校验 zip 内容（变体该有/不该有的都查一遍），打印体积报告与交付四步
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent
SKILL_ROOT = DEPLOY_DIR.parent
SKILL_NAME = "kbcg-xgz-win-workbuddy"

# zip 内的固定路径
SKILL_ARC = f".workbuddy/skills/{SKILL_NAME}"   # Skill 本体落点（项目级技能位）
WB_ARC = "剪辑工作台"                            # 工作台落点（工作区根）

VARIANTS = {
    "full": {"title": "完整版", "factory": True},
    "lite": {"title": "标准版", "factory": False},
}

# 目录名（任意层级出现即排除）
EXCLUDE_DIRS = {
    "模型",             # 3.4GB 实体，单独拉
    "__pycache__",
    ".venv",
    ".git",
    ".工作区",
    ".spike_tmp",
    "_archive",
    "_diag",
    ".mypy_cache",
    ".pytest_cache",
}

# 文件名后缀排除
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log", ".tmp", ".bak", ".zip")

# ── 剪辑工作台（第二打包源）──────────────────────────────────────────────
WORKBENCH_DIRNAME = "剪辑工作台"

# 工作台里的「运行时目录」——只带空壳，不带里面的历史数据。
WORKBENCH_RUNTIME_DIRS = {"工作区", "输出", "运行记录", "_历史归档"}

# 工作台的 输入\ 只带说明文件，不带任何素材（素材是用户的，不该被打包带走）
WORKBENCH_INBOX_EXTS = {".md", ".txt", ".gitkeep"}

# 工作台被单独收集，Skill 那趟 walk 要跳过它，否则会重复且绕过上面两条过滤
SKILL_WALK_SKIP = EXCLUDE_DIRS | {WORKBENCH_DIRNAME}

# 工作台的运行时目录（zip 不存空目录，用占位文件撑住结构 + 一句说明）
WORKBENCH_RUNTIME_NOTES = (
    ("输入", "把原素材（视频）丢进这个目录，然后回 WorkBuddy 对话里说「用口播出稿，跑一下输入」，由 AI 引导推进。不要双击 工具\\剪辑.cmd——没有 AI 在场，没人审错误、没人盯闸口。\n"),
    ("输出", "每次交付的说明文件与总台账落在这里。\n"),
    ("工作区", "每期的中间产物（含六个决策 JSON）落在这里。\n"),
    ("运行记录", "完整运行日志落在这里。\n"),
)

# 工作台里打包后必须存在的文件（相对 剪辑工作台\）
REQUIRED_WORKBENCH_BASE = [
    "工具/剪辑.py",
    "工具/剪辑.cmd",
    "工具/合并草稿.py",
    "开始在这里.md",
    "配置/设置.json",
    "说明书/工序与闸口.md",
    "说明书/AGENT开工反问清单.md",
    "说明书/AGENT运行守则.md",
]
REQUIRED_WORKBENCH_FACTORY = [
    "配图工厂/工具/图标工厂.py",
    "配图工厂/工具/抠图内核.py",
    "配图工厂/说明书.md",
]

# 配图工厂\批次\ 只带"跑通的完整样例"（新用户照着学），其余生产批次是用户自己的数据
WORKBENCH_BATCH_SAMPLES = {"2026-09-15_两种白带"}

# 打包后必须存在的文件（相对 SKILL_ROOT）
REQUIRED = [
    "SKILL.md",
    "部署/安装说明.md",
    "部署/工作区README.md",
    "部署/环境契约.json",
    "部署/bootstrap.py",
    "部署/bootstrap.cmd",
    "部署/requirements-pipeline.txt",
    "部署/requirements-capcutmate.txt",
    "部署/打包分发.py",
    "脚本/p0_建工作区.py",
    "脚本/jianying_contract.py",
    "脚本/p1a3_字缝吸附.py",
    "脚本/p4i_配图对齐.py",
    "脚本/p4j_出剪映草稿.py",
    "脚本_win/pipeline.py",
    "脚本_win/转写_faster_whisper.py",
    "脚本_win/gen_font_table.py",
    "字体/剪映字体表.json",
    "样本库/成对样本清单.json",
    "tests/test_learning_contracts.py",
]

# 标准版额外剔除的 Skill 文件（配图工厂的契约测试，没有工厂就没意义）
LITE_EXCLUDE_SKILL_FILES = {"tests/test_icon_factory.py"}


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def source_has_factory(workbench: Path | None) -> bool:
    """源里是否真的带配图工厂（标准版源天然没有，打 lite 不受影响；打 full 的前提）。"""
    return workbench is not None and (workbench / "配图工厂" / "工具" / "图标工厂.py").is_file()


def check_required(workbench: Path | None, factory: bool) -> list[str]:
    missing = [rel for rel in REQUIRED if not (SKILL_ROOT / rel).is_file()]
    if workbench is not None:
        # 配图工厂按源实际形态查：源里有才要求齐，没有就不查（标准版源）
        want = REQUIRED_WORKBENCH_BASE + (
            REQUIRED_WORKBENCH_FACTORY if factory and source_has_factory(workbench) else []
        )
        missing += [
            f"{WORKBENCH_DIRNAME}/{rel}"
            for rel in want
            if not (workbench / rel).is_file()
        ]
    return missing


def find_workbench(explicit: str | None = None) -> Path | None:
    """定位 剪辑工作台\\。

    两种摆放都认：
      · 本机维护姿态 —— 工作台在项目根，而 SKILL_ROOT 在 <项目根>\\03_Windows迁移分析\\win-port
      · 旧分发姿态   —— 工作台与 SKILL.md 同级，位于 <skill>\\剪辑工作台\\

    认不出来就只打 Skill 包（不是错误，只是少了那个操作面）。
    """
    if explicit:
        cand = Path(explicit)
        return cand.resolve() if (cand / "工具" / "剪辑.py").is_file() else None
    cands = [
        SKILL_ROOT / WORKBENCH_DIRNAME,
        SKILL_ROOT.parent / WORKBENCH_DIRNAME,
        SKILL_ROOT.parent.parent / WORKBENCH_DIRNAME,
        # 分发工作区姿态：部署→skill→skills→.workbuddy→工作区根（比维护源深一级）
        SKILL_ROOT.parent.parent.parent / WORKBENCH_DIRNAME,
        Path.home() / ".workbuddy" / "skills" / SKILL_NAME / WORKBENCH_DIRNAME,
    ]
    for cand in cands:
        if (cand / "工具" / "剪辑.py").is_file():
            return cand.resolve()
    return None


def check_skill_md() -> list[str]:
    """frontmatter 必须有 name + description，否则 WorkBuddy 识别不到。"""
    problems: list[str] = []
    path = SKILL_ROOT / "SKILL.md"
    if not path.is_file():
        return ["SKILL.md 不存在"]
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        problems.append("SKILL.md 开头不是 frontmatter（---）")
        return problems
    end = text.find("\n---", 3)
    front = text[3:end] if end > 0 else text[3:]
    for key in ("name:", "description:"):
        if key not in front:
            problems.append(f"SKILL.md frontmatter 缺 {key}")
    return problems


def _keep_name(name: str) -> bool:
    if name.startswith("_") and name.endswith((".txt", ".log", ".json")):
        return False                      # 诊断残留
    return not name.lower().endswith(EXCLUDE_SUFFIXES)


def collect_workbench(wb: Path, factory: bool) -> list[tuple[Path, str]]:
    """收工作台：带 工具/配置/说明书/开始在这里.md，运行时目录只留空壳。

    三条有意为之的过滤：
      · 工作区\\输出\\运行记录 —— 是跑过的痕迹，带进包会让新用户看到别人的片子；
      · 输入\\ 下的素材 —— 是用户的东西，不替他做主带走；
      · 输入\\ 下的 .md/.txt 说明 —— 保留，新用户得知道往哪儿放。
    标准版（factory=False）再加一条：配图工厂\\ 整个剔除。
    """
    items: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(wb):
        here = Path(dirpath)
        rel = here.relative_to(wb)
        top = rel.parts[0] if rel.parts else ""
        if top in WORKBENCH_RUNTIME_DIRS:
            dirnames[:] = []
            continue
        if not factory and top == "配图工厂":
            dirnames[:] = []
            continue
        # 配图工厂\批次\：只走进样例批次，生产批次整个剪掉
        if (rel.parts[:2] == ("配图工厂", "批次") and len(rel.parts) > 2
                and rel.parts[2] not in WORKBENCH_BATCH_SAMPLES):
            dirnames[:] = []
            continue
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in WORKBENCH_RUNTIME_DIRS and d != "__pycache__"
            and (factory or d != "配图工厂")
        )
        for name in sorted(filenames):
            if top == "输入" and Path(name).suffix.lower() not in WORKBENCH_INBOX_EXTS:
                continue
            if (rel.parts[:2] == ("配图工厂", "批次") and len(rel.parts) > 2
                    and rel.parts[2] not in WORKBENCH_BATCH_SAMPLES):
                continue
            if not _keep_name(name):
                continue
            path = here / name
            items.append((path, f"{WB_ARC}/{path.relative_to(wb).as_posix()}"))
    return items


def collect_files(workbench: Path | None, factory: bool) -> list[tuple[Path, str]]:
    """收集要打进包的文件 -> [(磁盘路径, zip 内相对路径)]。"""
    items: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(SKILL_ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in SKILL_WALK_SKIP)
        for name in sorted(filenames):
            if not _keep_name(name):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(SKILL_ROOT).as_posix()
            if not factory and rel in LITE_EXCLUDE_SKILL_FILES:
                continue
            items.append((path, f"{SKILL_ARC}/{rel}"))
    if workbench is not None:
        items += collect_workbench(workbench, factory)
    return items


def run_contract_tests(python: Path) -> bool:
    modules = ["tests.test_learning_contracts", "tests.test_p1a3_char_snap",
               "tests.test_p4i_image_plan"]
    # 配图工厂测试只在源里真实存在时才跑（标准版源没有）
    if (SKILL_ROOT / "tests" / "test_icon_factory.py").is_file():
        modules.append("tests.test_icon_factory")
    print(f"  · 跑契约回归：{len(modules)} 个模块（在打包源上）…")
    try:
        proc = subprocess.run(
            [str(python), "-m", "unittest", *modules],
            cwd=str(SKILL_ROOT),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("    ! 找不到 Python，跳过回归")
        return True
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    for line in tail[-3:]:
        print(f"    {line.strip()}")
    if proc.returncode != 0:
        print("    ✗ 契约回归未通过 —— 不产包")
        return False
    return True


def render_readme(variant: str, stamp: str) -> str:
    """用 部署\\工作区README.md 模板渲染 zip 根的 README。"""
    template = (DEPLOY_DIR / "工作区README.md").read_text(encoding="utf-8")
    info = VARIANTS[variant]
    if info["factory"]:
        factory_block = (
            "- **给片子加小图标配图** → 本版含 `剪辑工作台\\配图工厂\\`："
            "文稿 → 图标提示词 → AI 生图（洋红 sprite sheet）→ 抠透明 PNG → 触发词.png，"
            "全流程见 `配图工厂\\说明书.md`。\n"
            "  铁律：洋红底的 sprite sheet **不许直接丢进期的 `配图\\` 目录**，"
            "必须走配图工厂抠成透明 PNG 再 sync 进去，否则洋红背景会整张进片。"
        )
        variant_note = "含配图工厂（图标配图线）。"
    else:
        factory_block = (
            "- **本版不含配图工厂**（图标配图线已剔除）。剪辑主链不受影响；"
            "需要给片子加图标时，换用完整版，或手工把「触发词.png」放进期的 `配图\\` 目录 "
            "（p4i 配图对齐仍在，文件名即触发词）。"
        )
        variant_note = "不含配图工厂；剪辑主链与完整版完全一致。"
    return (template
            .replace("{{VARIANT}}", info["title"])
            .replace("{{VARIANT_NOTE}}", variant_note)
            .replace("{{FACTORY_BLOCK}}", factory_block)
            .replace("{{DATE}}", stamp))


def build_zip(items: list[tuple[Path, str]], out_zip: Path, top: str,
              workbench: Path | None, variant: str, stamp: str) -> tuple[int, int]:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    total_raw = 0
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, rel in items:
            zf.write(path, f"{top}/{rel}")
            total_raw += path.stat().st_size
        # zip 根放一份渲染好的 README，解压第一眼就能看到
        zf.writestr(f"{top}/README.md", render_readme(variant, stamp))
        # 保住 模型/ 目录结构（空壳）
        zf.writestr(
            f"{top}/{SKILL_ARC}/模型/.gitkeep",
            "此目录留空。模型不打进包，装完跑 部署/bootstrap.cmd 自动拉（约 3.4 GB）。\n",
        )
        # 工作台的运行时目录占位
        if workbench is not None:
            written = {rel for _, rel in items}
            for sub, note in WORKBENCH_RUNTIME_NOTES:
                arc = f"{WB_ARC}/{sub}/.gitkeep"
                if arc not in written:
                    zf.writestr(f"{top}/{arc}", note)
    return total_raw, out_zip.stat().st_size


def verify_zip(out_zip: Path, top: str, workbench: Path | None, factory: bool) -> list[str]:
    """zip 内容核验：必需的在、变体该剔除的不在。"""
    problems: list[str] = []
    with zipfile.ZipFile(out_zip) as zf:
        names = set(zf.namelist())
    must = [f"{top}/README.md", f"{top}/{SKILL_ARC}/SKILL.md",
            f"{top}/{SKILL_ARC}/模型/.gitkeep"]
    must += [f"{top}/{SKILL_ARC}/{rel}" for rel in REQUIRED]
    if workbench is not None:
        want = REQUIRED_WORKBENCH_BASE + (REQUIRED_WORKBENCH_FACTORY if factory else [])
        must += [f"{top}/{WB_ARC}/{rel}" for rel in want]
        must += [f"{top}/{WB_ARC}/{sub}/.gitkeep" for sub, _ in WORKBENCH_RUNTIME_NOTES]
    for arc in must:
        if arc not in names:
            problems.append(f"缺 {arc}")
    if not factory:
        banned = [n for n in names if f"{WB_ARC}/配图工厂/" in n
                  or n.endswith("tests/test_icon_factory.py")]
        if banned:
            problems.append(f"标准版混入了配图工厂内容：{banned[0]} 等 {len(banned)} 项")
    # 不该出现的：缓存 / 模型实体 / 旧诊断
    bad = [n for n in names if "__pycache__" in n or "/.venv/" in n
           or n.endswith((".pyc", ".log", ".tmp", ".bak"))]
    if bad:
        problems.append(f"混入缓存/诊断残留：{bad[0]} 等 {len(bad)} 项")
    # 不该出现的：工作区记忆（打包机的会话痕迹，接收方不该看到别人的 memory）
    bad_mem = [n for n in names
               if n.startswith(".workbuddy/") and not n.startswith(f"{SKILL_ARC}/")
               or "/.workbuddy/memory/" in n]
    if bad_mem:
        problems.append(f"混入工作区记忆残留：{bad_mem[0]} 等 {len(bad_mem)} 项")
    # 不该出现的：打包机用户名硬编码（隐私）
    home_name = Path.home().name
    leak = []
    with zipfile.ZipFile(out_zip) as zf:
        for n in names:
            if n.endswith((".md", ".json", ".py", ".txt", ".cmd", ".bat", ".yaml", ".sh")):
                if home_name in zf.read(n).decode("utf-8", errors="ignore"):
                    leak.append(n)
    if leak:
        problems.append(f"混入打包机用户名（{home_name}）：{leak[0]} 等 {len(leak)} 项")
    return problems


def find_python(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    for cand in (
        SKILL_ROOT / ".venv" / "Scripts" / "python.exe",
        Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "asr-win" / "Scripts" / "python.exe",
    ):
        if cand.is_file():
            return cand
    return Path(sys.executable)


def main(argv: list[str] | None = None) -> int:
    _utf8()
    parser = argparse.ArgumentParser(prog="打包分发.py", description=__doc__)
    parser.add_argument("--variant", choices=["full", "lite", "both"], default="both",
                        help="打哪个变体：full=完整版（含配图工厂）/ lite=标准版 / both=两个都打（默认）")
    parser.add_argument("--out", default=None, help="输出目录（默认 <迁移分析目录>/分发包）")
    parser.add_argument("--no-tests", action="store_true", help="跳过契约回归")
    parser.add_argument("--python", default=None, help="跑回归用的解释器")
    parser.add_argument("--workbench", default=None, help="指定 剪辑工作台\\ 位置（默认自动找）")
    args = parser.parse_args(argv)

    print("=" * 62)
    print("口播视频剪映出草稿（Windows 版）· 打包分发（工作区布局）")
    print("=" * 62)
    print(f"  Skill 源 : {SKILL_ROOT}")

    workbench = find_workbench(args.workbench)
    print(f"  工作台源 : {workbench if workbench else '（未找到 —— 本次只打 Skill 包）'}")

    print()
    print("[1/5] 校验必需文件")
    missing = check_required(workbench, factory=True)  # 源必须全齐（标准版是从全量源剔除出来的）
    problems = check_skill_md()
    if missing:
        print(f"    ✗ 缺 {len(missing)} 个必需文件：")
        for rel in missing:
            print(f"       - {rel}")
        print("    中止，不产包。")
        return 1
    if problems:
        print("    ✗ SKILL.md 有问题：")
        for item in problems:
            print(f"       - {item}")
        return 1
    print(f"    ✓ {len(REQUIRED) + len(REQUIRED_WORKBENCH_BASE) + (len(REQUIRED_WORKBENCH_FACTORY) if source_has_factory(workbench) else 0)} 个必需文件齐全")
    print("    ✓ SKILL.md frontmatter 完整")

    print()
    print("[2/5] 契约回归")
    if args.no_tests:
        print("    · 按 --no-tests 跳过")
    elif not run_contract_tests(find_python(args.python)):
        return 1

    stamp = datetime.date.today().strftime("%Y%m%d")
    out_dir = Path(args.out) if args.out else (SKILL_ROOT.parent / "分发包")
    variants = ["full", "lite"] if args.variant == "both" else [args.variant]
    if any(v == "full" for v in variants) and not source_has_factory(workbench):
        print("    ✗ 本源不含配图工厂（标准版源），打不了完整版——改用 --variant lite。")
        return 1
    made: list[Path] = []

    for variant in variants:
        info = VARIANTS[variant]
        top = f"口播视频剪映出草稿-xgz-win-workbuddy-{info['title']}"
        print()
        print(f"[3/5] 收集文件（{info['title']}）")
        items = collect_files(workbench, info["factory"])
        raw = sum(p.stat().st_size for p, _ in items)
        print(f"    ✓ {len(items)} 个文件 / {human(raw)}（未压缩）")
        print("    · 已排除：模型/ 实体、__pycache__、.venv、.git、诊断残留")
        if workbench is not None:
            print("    · 工作台已并入：工作区\\输出\\运行记录 只留空壳，输入\\ 不带素材，批次只带样例")
        if not info["factory"]:
            print("    · 标准版：配图工厂\\ 与 tests/test_icon_factory.py 已剔除")

        print(f"[4/5] 打包（{info['title']}）")
        out_zip = out_dir / f"{top}-{stamp}.zip"
        total_raw, total_zip = build_zip(items, out_zip, top, workbench, variant, stamp)
        print(f"    ✓ {out_zip}")
        print(f"      未压缩 {human(total_raw)} → 压缩后 {human(total_zip)}"
              f"（{total_zip / total_raw * 100:.1f}%）")

        print(f"[5/5] zip 内容核验（{info['title']}）")
        zip_problems = verify_zip(out_zip, top, workbench, info["factory"])
        if zip_problems:
            for item in zip_problems:
                print(f"    ✗ {item}")
            print("    包不合格，已产出但请不要分发。")
            return 1
        print("    ✓ 必需文件齐、剔除项为零、无缓存残留")
        made.append(out_zip)

    print()
    print("=" * 62)
    print("✅ 分发包已生成")
    print("=" * 62)
    for z in made:
        print(f"  {z}")
    print()
    print("  交给下一个用户时，让他：")
    print("    1) 解压，用 WorkBuddy「打开文件夹」选解压出来的 口播视频剪映出草稿-xgz-win-workbuddy-* 目录")
    print("       （Skill 是项目级的，打开工作区自动识别，不用拷到用户目录）")
    print("    2) 模型选推荐档（GLM 5.3 Flash 或 DeepSeek V4.1 Flash），")
    print("       对话里说「安装口播出稿」——AI 自动跑 bootstrap，全程不用碰命令行")
    print("    3) 按 README.md 或 部署\\安装说明.md §5 验收（doctor + 契约回归 + 一条真片子）")
    if workbench is not None:
        print()
        print("  装好后怎么开工（唯一入口——AI 对话式）：")
        print("    · 视频丢 剪辑工作台\\输入\\，说「用口播出稿，跑一下输入」")
        print("    · 剪辑.cmd 只是备用启动器，别直接双击——没有 AI 在场，没人审错误、没人盯闸口")
    print()
    print("  钉死项（改了就重跑全链才算数）：")
    try:
        contract = json.loads((DEPLOY_DIR / "环境契约.json").read_text(encoding="utf-8"))
        print(f"    宿主 : {contract.get('host', {}).get('required')}")
        print(f"    模型 : {contract.get('model', {}).get('required')}")
        print(f"    系统 : {contract.get('os', {}).get('required')}")
    except Exception:  # noqa: BLE001
        print("    （环境契约.json 读取失败）")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
