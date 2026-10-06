#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口播出稿 · 剪辑工作台（Windows 一键入口）

把原素材丢进 `输入\\日期文件夹\\`（如 `输入\\2026-09-15\\`），然后：

    剪辑.py                接单 + 自动推进（默认，最常用）
    剪辑.py status         看每一期走到哪一步
    剪辑.py deliver [片名]  交付到剪映
    剪辑.py doctor         环境自检
    剪辑.py inputs         只列输入里有哪些素材

设计原则
--------
**机械步骤全自动，判断步骤一律停下等人。**

管线的脚本分两类：

  · 机械步骤 —— 转写、建词轴、声学对齐、表达审查候选、建段、结构重排、
    切口清单、帧级验收、冻结、生成草稿、登记草稿箱。输入确定、输出确定，
    本程序按顺序自动跑完，不用人管。
  · 判断步骤 —— `content_plan` / `keep` / `expression_review` /
    `structure` / `cards` / `cut_review`。这六个 JSON 记的是「已经做完的
    编辑判断」，只能由人或 AI 写出来；脚本代劳就是伪造。
    跑到这里本程序**停住**，告诉你要写哪个文件、有哪些字段、样例在哪。

所以完整用法是一个循环：**跑 → 补齐它要的那一个 JSON → 再跑**，直到出草稿。

幂等
----
每一步都拿「产物文件在不在 + 是否仍然新鲜」判断完成没完成，已完成的一律跳过。
重复运行安全，只有删掉对应产物才会重跑那一步（例：删掉 `cards.json`
就只重跑分卡之后的环节，不会重新转写）。

「新鲜」是后补的半边：字缝吸附（p1a3）这类后处理会在**两次运行之间**
改写上游产物（word_track 的时间字段），光看下游文件在不在，会把旧字轴算出来的
候选表、分段、切口一路静默用到出草稿。所以机械步骤额外比对新旧（哈希或修改
时间），上游变过就自动重跑；人工裁决类产物不会被自动覆盖 —— 表达审查按候选
指纹合并继承（指纹没变的裁决保留，变了的回到待裁决），切口听审则重新打开闸口
等你复核。已交付的期整体视为成品，不再补跑字缝吸附（那是一次有意的重剪）。
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# ══════════════════════════════════════════════════════════════════════════
# 0. 自举：强制 UTF-8（子进程也必须继承，所以重启自己一次）
# ══════════════════════════════════════════════════════════════════════════


def _bootstrap_utf8() -> None:
    """未开 UTF-8 模式则以 PYTHONUTF8=1 重启自己，并兜底重配 stdio。

    「重启自己」而不是只重配 stdio：子进程也必须继承 UTF-8 模式，
    而 PYTHONUTF8 只有在**新进程**的环境里才生效。
    不用 os.execve：Windows 的 CPython 上 execve 带环境参数会以
    0xC0000005 崩掉（实测 3.14.4；execv 不带 env 反而正常）。
    换成 subprocess 重启 + sys.exit，语义等价且跨版本稳。
    """
    if os.environ.get("PYTHONUTF8") != "1":
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run([sys.executable, *sys.argv], env=env)
        raise SystemExit(proc.returncode)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


_bootstrap_utf8()


# ══════════════════════════════════════════════════════════════════════════
# 1. 工作台路径
# ══════════════════════════════════════════════════════════════════════════

TOOLS = Path(__file__).resolve().parent
BASE = TOOLS.parent
INBOX = BASE / "输入"
OUTBOX = BASE / "输出"
WORKROOT = BASE / "工作区"
LOGROOT = BASE / "运行记录"
SETTINGS = BASE / "配置" / "设置.json"
TEMPLATES = BASE / "说明书" / "待填模板"
FACTORY = BASE / "配图工厂" / "工具" / "图标工厂.py"

VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".m4v", ".avi", ".mts", ".m2ts",
    ".mpg", ".mpeg", ".webm", ".flv", ".wmv", ".3gp",
}
SKIP_DIRS = {".工作区", "__pycache__", ".git", ".idea", ".vscode"}

VERBOSE = False


# ══════════════════════════════════════════════════════════════════════════
# 2. 小工具
# ══════════════════════════════════════════════════════════════════════════


def echo(text: str = "") -> None:
    print(text, flush=True)


def head(title: str) -> None:
    echo("")
    echo("═" * 68)
    echo(f"  {title}")
    echo("═" * 68)


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: Path, payload) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def call(cmd: list, *, capture: bool = False) -> tuple[int, str]:
    """执行子进程。capture=True 时把输出抓回来（用于解析），否则直通终端。"""
    cmd = [str(c) for c in cmd]
    if VERBOSE:
        echo("   $ " + subprocess.list2cmdline(cmd))
    if capture:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", env=child_env(),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, out
    proc = subprocess.run(cmd, env=child_env())
    return proc.returncode, ""


def call_tee(cmd: list, log_path: Path) -> tuple[int, str]:
    """实时回显 + 追加落日志 + 返回 (退出码, 输出尾部)。"""
    cmd = [str(c) for c in cmd]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    shown = subprocess.list2cmdline(cmd)
    if VERBOSE:
        echo("   $ " + shown)
    tail: collections.deque = collections.deque(maxlen=40)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] $ {shown}\n")
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=child_env(), bufsize=1,
        )
        assert proc.stdout is not None
        for raw in proc.stdout:
            sys.stdout.write(raw)
            sys.stdout.flush()
            handle.write(raw)
            tail.append(raw.rstrip("\n"))
        proc.wait()
    return proc.returncode, "\n".join(tail)


def probe(cmd: list) -> int:
    """静默执行，只要退出码（用于「校验型」阶段的完成判定）。"""
    proc = subprocess.run(
        [str(c) for c in cmd], capture_output=True,
        text=True, encoding="utf-8", errors="replace", env=child_env(),
    )
    return proc.returncode


# ══════════════════════════════════════════════════════════════════════════
# 3. 管线定位（skill + 解释器）
# ══════════════════════════════════════════════════════════════════════════

SKILL: Path = Path()
PY: Path = Path()
PIPELINE: Path = Path()
SCRIPTS: Path = Path()
SCRIPTS_WIN: Path = Path()


def settings() -> dict:
    try:
        data = read_json(SETTINGS)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def resolve_skill() -> Path:
    """定位 Skill 本体（含 脚本_win/pipeline.py 的那个目录）。

    按下面的顺序找，第一个命中的就用。前两条是"用户/环境显式指定"，后几条是
    "自己找上门"——目的是让同一份工作台在三种摆放形态下都能开箱即用：

      A. 工作区形态（新分发包）—— 工作台在工作区根，Skill 是项目级技能
         （<工作区>\\.workbuddy\\skills\\kbcg-xgz-win-workbuddy\\）：第 ④ 条命中。
      B. 旧分发包形态 —— 工作台就在 Skill 目录里面（<skill>\\剪辑工作台\\工具\\）：
         此时 BASE.parent 正好是 <skill>，第 ③ 条命中。
      C. 本机形态 —— 工作台在项目根，Skill 装在 ~\\.workbuddy\\skills\\ 或源码
         win-port\\：第 ⑤ / ⑥ 条命中。

    ③ ④ 放在 ⑤ 前面是有意的：谁把我放在身边，就先认身边的那个，而不是去猜
    用户主目录里是不是还装着另一个版本。
    """
    cands: list[Path] = []
    env = os.environ.get("XGZ_SKILL_ROOT")
    if env:
        cands.append(Path(env))
    configured = str(settings().get("skill_root") or "").strip()
    if configured:
        cands.append(Path(configured))
    cands.append(BASE.parent)                                    # B 自身所在目录
    cands.append(BASE.parent / ".workbuddy" / "skills" / "kbcg-xgz-win-workbuddy")  # A 工作区级
    cands.append(Path.home() / ".workbuddy" / "skills" / "kbcg-xgz-win-workbuddy")
    cands.append(BASE.parent / "03_Windows迁移分析" / "win-port")  # C 本机布局
    for cand in cands:
        cand = cand.expanduser()
        if (cand / "脚本_win" / "pipeline.py").is_file():
            return cand.resolve()
    raise SystemExit(
        "⛔ 找不到口播出稿的 Skill 本体。\n"
        "   依次找过：\n"
        + "\n".join(f"     · {c.expanduser()}" for c in cands)
        + "\n   请在 配置\\设置.json 的 \"skill_root\" 里写明确路径。"
    )


def resolve_python() -> Path:
    cands: list[Path] = []
    env = os.environ.get("XGZ_PIPELINE_PY")
    if env:
        cands.append(Path(env))
    cands += [
        SKILL / ".venv" / "Scripts" / "python.exe",
        SKILL / ".venv" / "bin" / "python",
        Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "asr-win" / "Scripts" / "python.exe",
        Path(sys.executable),
    ]
    for cand in cands:
        if cand.is_file():
            return cand
    raise SystemExit(
        "⛔ 找不到管线解释器（需要装好 torch / faster_whisper / silero_vad / transformers 的那个 python）。\n"
        "   排查顺序：\n"
        "   1. 设置环境变量 XGZ_PIPELINE_PY 指向该 python.exe；\n"
        "   2. 或在 Skill 目录下建 .venv（对话里说「安装口播出稿」由 AI 自动完成）；\n"
        f"   3. 或用受管环境 {Path.home() / '.workbuddy' / 'binaries' / 'python' / 'envs' / 'asr-win' / 'Scripts' / 'python.exe'}\n"
        "   已依次尝试：\n" + "\n".join(f"     · {c.expanduser()}" for c in cands)
    )


def load_pipeline() -> None:
    global SKILL, PY, PIPELINE, SCRIPTS, SCRIPTS_WIN
    SKILL = resolve_skill()
    PY = resolve_python()
    PIPELINE = SKILL / "脚本_win" / "pipeline.py"
    SCRIPTS = SKILL / "脚本"
    SCRIPTS_WIN = SKILL / "脚本_win"


# ══════════════════════════════════════════════════════════════════════════
# 4. 素材扫描与接单
# ══════════════════════════════════════════════════════════════════════════


def scan_inputs() -> list[Path]:
    """递归找 输入\\ 下的视频；跳过 .工作区 这类内部目录和隐藏项。

    约定：素材按天放进日期文件夹（输入\\2026-09-15\\xx.mp4），
    直接丢在 输入\\ 根下的也能接单，但会提醒挪进去 —— 不然堆多了分不清哪天来的。
    """
    found: list[Path] = []
    if not INBOX.is_dir():
        return found
    for current, dirs, files in os.walk(INBOX):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = Path(current) / name
            if path.suffix.lower() in VIDEO_EXTS:
                found.append(path)
    return found


def _loose_in_inbox(videos: list[Path]) -> list[Path]:
    """直接躺在 输入\\ 根下（不在日期文件夹里）的素材。"""
    return [v for v in videos if v.parent == INBOX]


CORE_BAD = re.compile(r"[\\/:\r\n\t]")
CORE_CJK = re.compile(r"[\u3400-\u9fff]")
CORE_NUMBER_ONLY = re.compile(r"[Cc]\d+")


def valid_core(text: str) -> bool:
    """与 脚本/p0_建工作区.py 的 --core-expression 校验保持一致。"""
    if not text or not text.strip():
        return False
    text = text.strip()
    if len(text) > 36 or CORE_BAD.search(text):
        return False
    if not CORE_CJK.search(text):
        return False
    if CORE_NUMBER_ONLY.fullmatch(text):
        return False
    return True


def core_expression_for(video: Path) -> tuple[str, str]:
    """返回 (核心表达, 来源说明)。优先级：同名 .txt 首行 → 文件名。"""
    sidecar = video.with_suffix(".txt")
    if sidecar.is_file():
        try:
            for line in sidecar.read_text(encoding="utf-8", errors="replace").splitlines():
                cand = line.strip()
                if not cand or cand.startswith("#"):
                    continue
                if valid_core(cand):
                    return cand, f"取自 {sidecar.name} 的第一行"
                return "", f"{sidecar.name} 的第一行不合规（须含中文、≤36 字、不能只是编号）"
        except OSError as exc:
            return "", f"读 {sidecar.name} 失败：{exc}"
    if valid_core(video.stem):
        return video.stem.strip(), "取自文件名"
    return "", f"文件名「{video.stem}」不能当核心表达（须含中文、≤36 字、不能只是编号）"


def _tag_merge_groups() -> None:
    """日期文件夹里 ≥2 个素材 → 给对应已登记期打「合并组」标记。

    合并组：同一天丢多个视频时，交付层由 工具\\合并草稿.py
    把整批合成一个剪映草稿（多轨分层+接龙）；只有一个视频的文件夹不标记，
    照旧单期出稿。只加标记不改任何决策产物。
    """
    by_folder: dict[Path, list[Path]] = {}
    for video in scan_inputs():
        if video.parent != INBOX:
            by_folder.setdefault(video.parent, []).append(video)
    for folder, videos in sorted(by_folder.items()):
        if len(videos) < 2:
            continue
        for video in videos:
            cfg_path = WORKROOT / video.stem / "config.json"
            if not cfg_path.is_file():
                continue
            try:
                cfg = read_json(cfg_path)
            except Exception:  # noqa: BLE001
                continue
            if cfg.get("合并组") == folder.name:
                continue
            cfg["合并组"] = folder.name
            write_json(cfg_path, cfg)
            echo(f"   📦 [{video.stem}] 同批 {len(videos)} 个素材 → 标记合并组"
                 f"「{folder.name}」，交付时合并成一个草稿")


def intake(dry: bool) -> list[tuple[str, str, Path]]:
    """把 输入\\ 里还没登记过的素材建成工作区。返回 [(片名, 结果, 工作区)]。"""
    results: list[tuple[str, str, Path]] = []
    conf = settings()
    material_type = str(conf.get("素材类型") or "single_speaker").strip()
    font = str(conf.get("字幕字体") or "").strip()

    videos = scan_inputs()
    if not videos:
        today = f"{dt.date.today():%Y-%m-%d}"
        echo(f"（输入\\ 里没有视频文件。把 mp4 丢进 {INBOX}\\{today}\\ 再跑）")
        return results

    loose = _loose_in_inbox(videos)
    if loose:
        today = f"{dt.date.today():%Y-%m-%d}"
        echo(f"⚠ 有 {len(loose)} 个素材直接躺在 输入\\ 根下，建议按天归档："
             f"挪到 输入\\{today}\\ 里（不影响本次接单）")

    for video in videos:
        name = video.stem
        target = WORKROOT / name
        if (target / "config.json").is_file():
            results.append((name, "已登记，跳过", target))
            continue

        core, why = core_expression_for(video)
        echo(f"\n── 接单：{video.name}")
        echo(f"   片名：{name}   核心表达：{core or '（未定，稍后补）'}   [{why}]")

        cmd = [
            PY, PIPELINE, "p0", str(video), name,
            "--material-type", material_type,
            "--no-transcribe",
        ]
        if font:
            cmd += ["--font", font]
        if core:
            cmd += ["--core-expression", core]

        if dry:
            echo("   （演练）将执行：" + subprocess.list2cmdline([str(c) for c in cmd]))
            results.append((name, "（演练）", target))
            continue

        rc, out = call(cmd, capture=True)
        tail = "\n".join(out.strip().splitlines()[-6:])
        if rc != 0:
            echo("   ⛔ 建工作区失败：")
            for line in out.strip().splitlines()[-12:]:
                echo("      " + line)
            results.append((name, "失败", target))
            continue

        produced = None
        for line in out.splitlines():
            if "工作区就绪:" in line:
                produced = Path(line.split("工作区就绪:", 1)[1].strip())
        if produced is None or not produced.is_dir():
            echo("   ⛔ 没能解析出工作区路径，输出尾部：")
            echo("      " + tail)
            results.append((name, "失败", target))
            continue

        # p0 把工作区建在「素材所在目录\.工作区\片名」，挪到工作台统一位置。
        if produced.resolve() != target.resolve():
            WORKROOT.mkdir(parents=True, exist_ok=True)
            if target.exists():
                echo(f"   ⛔ 工作区\\{name} 已存在，不敢覆盖；先手动处理")
                results.append((name, "冲突", target))
                continue
            shutil.move(str(produced), str(target))
            leftover = produced.parent
            try:
                if leftover.is_dir() and not any(leftover.iterdir()):
                    leftover.rmdir()
            except OSError:
                pass
        echo(f"   ✅ 工作区就绪 → 工作区\\{name}")
        results.append((name, "已登记", target))
    if not dry:
        _tag_merge_groups()
    return results


# ══════════════════════════════════════════════════════════════════════════
# 5. 阶段表：机械步骤自动跑，判断步骤停下等人
# ══════════════════════════════════════════════════════════════════════════

SCRIPTS_HELP = "脚本\\查词.py 查下标 · 脚本\\核专名.py 查英文缩写与专名"


def source_of(work: Path) -> str:
    conf = read_json(work / "config.json")
    return str(conf.get("original_path") or conf.get("proxy") or "")


def tpl(name: str) -> str:
    return str(TEMPLATES / name)


def _hint_content_plan(work: Path) -> str:
    return (
        f"写 {work / 'content_plan.json'}\n"
        "      先读：references/material-routing.md（素材路由）→ 子技能/粗剪/单人口播.md\n"
        "      字段：goal / audience / main_claim / topic_blocks（必须覆盖全词轴、不得留空档）\n"
        "            selected_story / hook_candidates / selected_hook / review\n"
        f"      样例：{tpl('content_plan.样例.json')}（学字段，别抄内容 —— 那是别的片子）"
    )


def _hint_keep(work: Path) -> str:
    return (
        f"写 {work / 'keep.json'}\n"
        "      字段：drop（删声音）/ fix（只修 ASR 文本）/ retain（有意保留）/\n"
        "            split_after（两侧都留但建立结构边界）/ review\n"
        f"      辅助：{SCRIPTS_HELP}\n"
        f"      样例：{tpl('keep.样例.json')}\n"
        "      写完后本程序会自动跑 validate-keep 自检并报错到下通过为止"
    )


def _hint_expr(work: Path) -> str:
    counts = ""
    try:
        data = read_json(work / "expression_review.json")
        counts = (
            f"\n      候选规模：口语 {len(data.get('micro_candidates') or [])} 条 · "
            f"词轴外残声 {len(data.get('residual_candidates') or [])} 条 · "
            f"停顿 {len(data.get('pause_candidates') or [])} 条"
        )
    except Exception:  # noqa: BLE001
        pass
    return (
        f"逐项裁决 {work / 'expression_review.json'}{counts}\n"
        "      micro_candidates[].decision   ∈ drop | retain | not_micro\n"
        "      residual_candidates[].decision ∈ filler_drop | breath_noise | restore_text\n"
        "      pause_candidates[].decision    ∈ compress | retain\n"
        "        （compress 必须 function=ineffective_wait；思考/强调/换气/句间气口一律 retain）\n"
        "      每一项都要听原音后填 decision / function / caption_action / reason / acoustic_review\n"
        "      若候选刚被自动重建过（字缝吸附改了字轴会触发），上下文没变的裁决已继承，\n"
        "        只需把 decision=pending 的项重新听审补齐\n"
        f"      样例：{tpl('expression_review.样例.json')}"
    )


def _hint_structure(work: Path) -> str:
    return (
        f"写 {work / 'structure.json'}\n"
        "      字段：goal / audience / main_claim / hook_candidates / selected_hook /\n"
        "            intentional_repeats / sequence（按最终播放顺序列全部段）\n"
        "      ⚠ 前三项与 selected_hook 必须与 content_plan.json **逐字一致**，否则冻结会失败\n"
        f"      样例：{tpl('structure.样例.json')}"
    )


def _hint_cut(work: Path) -> str:
    counts = ""
    stale_note = ""
    try:
        data = read_json(work / "cut_review.json")
        counts = (
            f"\n      待听审：{len(data.get('segments') or [])} 个段头尾 · "
            f"{len(data.get('cuts') or [])} 个真实切口"
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        rough_m = (work / "rough_segments.json").stat().st_mtime
        cut_m = (work / "cut_review.json").stat().st_mtime
        if cut_m < rough_m - 1e-6:
            stale_note = (
                "\n      ⚠ cut_review.json 比 rough_segments.json 旧：上游重跑过"
                "（如字缝吸附改了字轴），切口位置已变。\n"
                "        旧听审不能默认沿用：把改动过的切口重新听一遍；"
                "结论变了就改对应字段，没变就确认 review 说明并保持通过状态。"
            )
    except OSError:  # noqa: BLE001
        pass
    return (
        f"逐段、逐切口听审 {work / 'cut_review.json'}{counts}{stale_note}\n"
        "      segments[]：填 head_complete / tail_complete / head_note / tail_note，\n"
        "        低于目标帧数（头 2 帧 / 尾 11 帧）必须在 *_margin_exception 写安全上界\n"
        "      cuts[]：填 no_deleted_audio / semantic_natural / joined_playback_checked / joined_note\n"
        "      review.status 从 pending 改成已审状态（不改它就视为没审）\n"
        f"      样例：{tpl('cut_review.样例.json')}"
    )


def _hint_cards(work: Path) -> str:
    keys = ""
    try:
        rough = read_json(work / "rough_segments.json")
        keys = "、".join(
            str((row.get("_rough_structure") or {}).get("playback_key") or "?")
            for row in rough[:6]
        )
    except Exception:  # noqa: BLE001
        pass
    return (
        f"写 {work / 'cards.json'}\n"
        "      结构：{ 段键: [ [卡文本, 关键词], ... ], ... }，段键必须与 rough_segments 完全一致"
        + (f"\n      本片段键形如：{keys} …" if keys else "") +
        "\n      竖版每张卡 ≤ 10 全角字宽；不得在多字词或英文 token 内部切卡；\n"
        "      关键词必须是该卡文本的子串（它是 P5 上色候选，P2 不上色）\n"
        f"      样例：{tpl('cards.样例.json')}"
    )


@dataclass
class Stage:
    key: str
    label: str
    done: Callable[[Path], bool]
    gate: bool = False
    cmd: Callable[[Path], list] | None = None
    hint: Callable[[Path], str] | None = None


# ── 新鲜度判定 ────────────────────────────────────────────────────────────────
# 背景：字缝吸附（p1a3）作为后处理接入后，「上游产物在两次运行之间
# 被改写、下游产物还是旧数据」第一次成为自动会发生的场景。只看下游文件在不在，
# 会把旧字轴算出来的候选表/分段/切口一路静默用到冻结（管线侧的哈希护栏会在最后
# 响亮失败，但那时已经白走十几步）。


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _up_to_date(artifact: Path, *upstreams: Path) -> bool:
    """产物存在，且不早于任何上游输入的修改时间。

    上游文件不存在时不算过期——缺上游会让本步骤的命令自己报错，报错更清楚。
    """
    if not artifact.is_file():
        return False
    try:
        stamp = artifact.stat().st_mtime
    except OSError:
        return False
    for up in upstreams:
        try:
            if up.is_file() and up.stat().st_mtime > stamp + 1e-6:
                return False
        except OSError:
            continue
    return True


def _expr_candidates_fresh(work: Path) -> bool:
    """expression_review.json 的候选必须仍绑定当前 word_track / keep。

    与 脚本/expression_review_contract.py 的校验同一口径：候选表里记了生成时的
    `word_track_sha256` / `keep_sha256`，对不上就是旧词轴的候选，视为未完成，
    让 p1c 带着 --force 重跑（脚本会按候选指纹合并继承旧裁决）。
    """
    path = work / "expression_review.json"
    if not path.is_file():
        return False
    try:
        data = read_json(path)
        return (
            data.get("word_track_sha256") == _sha256_file(work / "word_track.json")
            and data.get("keep_sha256") == _sha256_file(work / "keep.json")
        )
    except Exception:  # noqa: BLE001
        return False


def _char_snapped(work: Path) -> bool:
    """p1a3 完成判定。已交付的期整体跳过：草稿已经是成品，那份 word_track 是
    已交付草稿的历史依据，补吸附会让整个下游全部过期、逼着重新听审重交付——
    那是一次有意的重剪，不该由幂等重跑顺手触发。"""
    if (work / "char_snap_report.json").is_file():
        return True
    try:
        if read_json(work / "word_track.json").get("char_snap"):
            return True
    except Exception:  # noqa: BLE001
        pass
    return _delivered(work)


def _icon_batch(work: Path) -> Path | None:
    """期 config.json 的 icon_batch 字段 → 配图工厂批次目录；未配置返回 None。"""
    try:
        cfg = read_json(work / "config.json")
    except Exception:  # noqa: BLE001
        return None
    raw = str(cfg.get("icon_batch") or "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else (work / raw)


def _icon_batch_fresh(work: Path) -> bool:
    """p4g 完成判定：未启用配图工厂=无事可做；启用则 期/配图 必须比批次上游新。"""
    batch = _icon_batch(work)
    if batch is None:
        return True
    if not batch.is_dir():
        return False
    dest = work / "配图"
    if not dest.is_dir() or not any(dest.iterdir()):
        return False
    dest_mtime = max(p.stat().st_mtime for p in dest.iterdir() if p.is_file())
    newest = 0.0
    for name in ("提示词.json", "生图"):
        u = batch / name
        if u.is_file():
            newest = max(newest, u.stat().st_mtime)
        elif u.is_dir():
            files = [p for p in u.rglob("*") if p.is_file()]
            if files:
                newest = max(newest, max(p.stat().st_mtime for p in files))
    return dest_mtime >= newest


def _image_plan_fresh(work: Path) -> bool:
    """配图对齐完成判定：没放图=无事可做；放了图则计划必须比图和粗剪都新。"""
    image_dir = work / "配图"
    images = [
        p for p in image_dir.iterdir()
        if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    ] if image_dir.is_dir() else []
    if not images:
        return True
    plan = work / "配图计划.json"
    if not plan.is_file():
        return False
    plan_mtime = plan.stat().st_mtime
    newest = max([p.stat().st_mtime for p in images] + (
        [(work / "rough_segments.json").stat().st_mtime]
        if (work / "rough_segments.json").is_file() else []
    ))
    return plan_mtime >= newest


def _asr_cmd(w: Path) -> list:
    """转写命令。设置.json 的「转写热词」非空时透传给 --hotwords。

    IP 人名（设「转写热词」前）曾被 ASR 听成同音字，靠下游人工通读
    才兜住；在这里提前喂热词，比事后核专名省一轮返工。热词支持空格或
    逗号分隔多个（转写器的参数契约）。
    """
    cmd = [
        PY, SCRIPTS_WIN / "转写_faster_whisper.py", source_of(w),
        "--outdir", w, "--model", "large-v3-turbo", "--language", "zh",
    ]
    hotwords = str(settings().get("转写热词") or "").strip()
    if hotwords:
        cmd += ["--hotwords", hotwords]
    return cmd


def build_stages() -> list[Stage]:
    return [
        # ── 机械步骤 ────────────────────────────────────────────────────
        Stage(
            "asr", "语音转写（faster-whisper large-v3-turbo）",
            done=lambda w: bool(list(w.glob("*_words.json"))),
            cmd=lambda w: _asr_cmd(w),
        ),
        Stage(
            "p1a", "建统一词轴",
            done=lambda w: (w / "word_track.json").is_file(),
            cmd=lambda w: [PY, SCRIPTS / "p1a_建词轴.py", w],
        ),
        # ── 判断：内容计划 ──────────────────────────────────────────────
        Stage(
            "content_plan", "内容计划 content_plan.json",
            done=lambda w: (w / "content_plan.json").is_file(),
            gate=True, hint=_hint_content_plan,
        ),
        # ── 判断：原声留删 ──────────────────────────────────────────────
        Stage(
            "keep", "原声留删 keep.json",
            done=lambda w: (w / "keep.json").is_file(),
            gate=True, hint=_hint_keep,
        ),
        Stage(
            "validate_keep", "留删自检（validate-keep）",
            done=lambda w: probe([PY, SCRIPTS / "decision_contract.py", "validate-keep", w]) == 0,
            cmd=lambda w: [PY, SCRIPTS / "decision_contract.py", "validate-keep", w],
        ),
        Stage(
            "p1a2", "词轴声学对齐",
            done=lambda w: (w / "word_track.whisper备份.json").is_file(),
            cmd=lambda w: [PY, SCRIPTS / "p1a2_对齐词轴.py", w],
        ),
        # 字缝吸附：对齐器给的是「字大致在哪一段」，连读处的字缝会偏 55-190ms
        # （实测「第三步」的「三」被标在「第」字内部，用户听成「第三」）。
        # 这一步纯后处理，把每个字缝挪到声学能量谷上，不重跑对齐模型。
        Stage(
            "p1a3", "字缝吸附（字级时间精修）",
            done=_char_snapped,
            cmd=lambda w: [PY, SCRIPTS / "p1a3_字缝吸附.py", w],
        ),
        # 候选表过期（字缝吸附改了字轴 / keep 改过）时判为未完成，带 --force 重跑：
        # p1c 会按「候选 ID + 上下文指纹」合并继承旧裁决，指纹没变的判断不会丢。
        Stage(
            "p1c", "生成表达审查候选",
            done=_expr_candidates_fresh,
            cmd=lambda w: [PY, SCRIPTS / "p1c_生成表达审查.py", w]
            + (["--force"] if (w / "expression_review.json").is_file() else []),
        ),
        # ── 判断：表达审查逐项裁决 ──────────────────────────────────────
        Stage(
            "expr_fill", "表达审查逐项裁决 expression_review.json",
            done=lambda w: probe([PY, SCRIPTS / "p1c_生成表达审查.py", w, "--check"]) == 0,
            gate=True, hint=_hint_expr,
        ),
        Stage(
            "p1", "建段 segments.json",
            done=lambda w: _up_to_date(
                w / "segments.json",
                w / "word_track.json", w / "keep.json", w / "expression_review.json",
            ),
            cmd=lambda w: [PY, SCRIPTS / "p1_建段.py", w],
        ),
        # ── 判断：结构编排 ──────────────────────────────────────────────
        Stage(
            "structure", "结构编排 structure.json",
            done=lambda w: (w / "structure.json").is_file(),
            gate=True, hint=_hint_structure,
        ),
        Stage(
            "p2", "结构重排 rough_segments.json",
            done=lambda w: _up_to_date(
                w / "rough_segments.json", w / "segments.json", w / "structure.json",
            ),
            cmd=lambda w: [PY, SCRIPTS / "p2_结构编排.py", w],
        ),
        Stage(
            "p2b", "生成切口清单",
            done=lambda w: (w / "cut_review.json").is_file(),
            cmd=lambda w: [PY, SCRIPTS / "p2b_生成切口清单.py", w],
        ),
        # ── 判断：逐切口听审 ────────────────────────────────────────────
        Stage(
            "cut_fill", "逐切口听审 cut_review.json",
            done=lambda w: _cut_reviewed(w),
            gate=True, hint=_hint_cut,
        ),
        Stage(
            "f1", "F1 内部验收并冻结决策锁",
            done=lambda w: (w / "decision_lock.json").is_file(),
            cmd=lambda w: [PY, PIPELINE, "f1", w],
        ),
        # ── 判断：字幕分卡 ──────────────────────────────────────────────
        Stage(
            "cards", "字幕分卡 cards.json",
            done=lambda w: (w / "cards.json").is_file(),
            gate=True, hint=_hint_cards,
        ),
        # 字幕时间线投影（captions_plan.json）：p4i 配图对齐和交付都吃它。
        # 踩过的坑：新期首跑时它不存在，p4i 直接崩 ——
        # 早期测试时文件恰好已在，缺口被掩盖。完成判定=比 cards.json 新。
        Stage(
            "p3captions", "字幕时间线投影 captions_plan.json",
            done=lambda w: (w / "captions_plan.json").is_file()
            and (w / "captions_plan.json").stat().st_mtime
            >= (w / "cards.json").stat().st_mtime,
            cmd=lambda w: [PY, SCRIPTS / "p3_分卡.py", w],
        ),
        # 配图：图片文件名=触发词（热身.png → 讲到含「热身」那句时句首出现），
        # 显示秒数 config.image_duration_s（默认 4s，文件名 _2.5s 可覆盖）。
        # 配图工厂批次（config.json 的 icon_batch）：没配置自动跳过；
        # 批次提示词或生图更新后，期/配图 自动重同步。
        Stage(
            "p4g", "配图工厂同步（文稿图标 → 触发词.png）",
            done=_icon_batch_fresh,
            cmd=lambda w: [
                PY, FACTORY, "sync", str(_icon_batch(w)), "--out", str(w / "配图"),
            ],
        ),
        # 没放配图自动跳过；换图或重切后计划自动重算。
        Stage(
            "p4i", "配图对齐（文件名=触发词，句首出现）",
            done=_image_plan_fresh,
            cmd=lambda w: [PY, SCRIPTS / "p4i_配图对齐.py", w],
        ),
        Stage(
            "deliver", "生成剪映草稿并登记草稿箱",
            done=lambda w: (_delivered(w) or _merged_delivered(w))
            and _delivery_report_fresh(w),
            cmd=lambda w: [PY, PIPELINE, "finalize", w, "jianying"],
        ),
    ]


def _delivery_report_fresh(work: Path) -> bool:
    """交付报告必须比所有字幕决策产物新，否则视为过期件。

    实测踩坑（2026-09-29）：重分 cards.json 后，_delivered 仍看到旧的
    结构验收报告 + 草稿目录 + 登记条目，三件事全成立 → deliver 阶段被判
    「已完成」，finalize 被跳过，新分卡永远到不了草稿。报告的 mtime 必须
    晚于 cards.json / captions_plan.json，否则要求重出。
    """
    report = work / "jianying_structural_verification.json"
    if not report.is_file():
        return False
    try:
        report_mtime = report.stat().st_mtime
        for name in ("cards.json", "captions_plan.json"):
            upstream = work / name
            if upstream.is_file() and upstream.stat().st_mtime > report_mtime:
                return False
    except OSError:
        return False
    return True


# ── deliver 阶段：「已交付」到底怎么判定 ─────────────────────────────────────
# 踩过的坑：原来只查 jianying_structural_verification.json 在不在，
# 结果那份报告指向的草稿早已被归档 → 报告成了「过期件」，工作台却报「已出草稿」，
# 直接把这一步跳过了。判定必须落到磁盘和草稿箱索引上。

_ROOT_META_MEMO: dict = {}


def _root_meta_index(draft: Path) -> Path | None:
    """剪映草稿箱索引 root_meta_info.json 的真实路径。

    Windows 上剪映可以把「草稿实体」放在自定义目录（如 D:\\JianyingPro Drafts），
    而索引仍留在默认目录 —— 所以不能想当然用 draft.parent/root_meta_info.json。
    先看草稿实体目录里有没有；没有就问定位器要 root_meta_index。结果按草稿根缓存，
    免得 advance() 每轮扫描都起一次子进程。
    """
    key = str(draft.parent).lower()
    if key in _ROOT_META_MEMO:
        return _ROOT_META_MEMO[key]

    found: Path | None = None
    beside = draft.parent / "root_meta_info.json"
    if beside.is_file():
        found = beside
    else:
        proc = subprocess.run(
            [str(PY), str(SCRIPTS / "p4j_定位剪映草稿根.py"), "--save", "--list"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=child_env(),
        )
        if proc.returncode == 0:
            out = proc.stdout or ""
            start = out.find("{")
            if start >= 0:
                try:
                    idx = str(json.loads(out[start:]).get("root_meta_index") or "").strip()
                except Exception:  # noqa: BLE001
                    idx = ""
                if idx and Path(idx).is_file():
                    found = Path(idx)

    _ROOT_META_MEMO[key] = found
    return found


def _registered_names(index: Path) -> set[str]:
    """草稿箱索引里「目录真实存在」的草稿名集合。悬空条目（索引里有、磁盘上没了）剔掉。"""
    try:
        rows = read_json(index).get("all_draft_store") or []
    except Exception:  # noqa: BLE001
        return set()
    names: set[str] = set()
    for row in rows:
        name = row.get("draft_name")
        fold = row.get("draft_fold_path")
        if not isinstance(name, str) or not name:
            continue
        if isinstance(fold, str) and fold and not Path(os.path.normpath(fold)).is_dir():
            continue
        names.add(name)
    return names


def _delivered(work: Path) -> bool:
    """三件事同时成立才算交付完成：报告在 + 草稿目录在 + 已登记进草稿箱索引。

    单看报告会骗人（报告可能是过期件）；单看草稿目录也不够（没登记的话用户在
    剪映里根本看不到）。而登记条目和草稿目录都是持久事实，即使用户打开过剪映
    （剪映会把 draft_meta_info.json 改写成加密串）也依然成立。
    """
    report = work / "jianying_structural_verification.json"
    if not report.is_file():
        return False
    try:
        data = read_json(report)
    except Exception:  # noqa: BLE001
        return False
    raw = str(data.get("draft_path") or "").strip()
    if not raw:
        return False
    draft = Path(os.path.normpath(raw))
    if not (draft / "draft_info.json").is_file():
        return False  # 报告指向的草稿已经不在了 → 过期件，重跑
    index = _root_meta_index(draft)
    if index is None:
        return bool(data.get("structural_verified"))  # 拿不到索引，退一步看报告结论
    return draft.name in _registered_names(index)


def _merge_group(work: Path) -> str:
    """期的「合并组」标记（=输入里的日期文件夹名）。空串=走单期交付。"""
    try:
        return str(read_json(work / "config.json").get("合并组") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _merged_delivered(work: Path) -> bool:
    """合并交付判定：合并草稿.py 写的 合并交付.json 指向的草稿仍已登记。

    有标记的期不再单独出稿 —— deliver 闸口直接放行，状态显示「已合并交付」。
    """
    marker = work / "合并交付.json"
    if not marker.is_file():
        return False
    try:
        data = read_json(marker)
        name = str(data.get("draft_name") or "")
        draft = Path(os.path.normpath(str(data.get("draft_path") or "")))
        if not name or not (draft / "draft_info.json").is_file():
            return False
        index = _root_meta_index(draft)
        if index is None:
            return True
        return name in _registered_names(index)
    except Exception:  # noqa: BLE001
        return False


def _cut_reviewed(work: Path) -> bool:
    try:
        data = read_json(work / "cut_review.json")
    except Exception:  # noqa: BLE001
        return False
    review = data.get("review")
    if not isinstance(review, dict):
        return False
    if str(review.get("status") or "") in ("", "pending"):
        return False
    # 上游重跑过（字缝吸附改了字轴 → segments/rough_segments 重建）的话，
    # 切口位置已经变了，旧听审记录不能默认沿用——重新打开闸口让人复核。
    return _up_to_date(work / "cut_review.json", work / "rough_segments.json")


# ══════════════════════════════════════════════════════════════════════════
# 6. 推进
# ══════════════════════════════════════════════════════════════════════════


def list_episodes() -> list[Path]:
    if not WORKROOT.is_dir():
        return []
    out = []
    for entry in sorted(WORKROOT.iterdir()):
        if entry.is_dir() and (entry / "config.json").is_file():
            out.append(entry)
    return out


def find_episode(token: str) -> Path:
    eps = list_episodes()
    exact = [e for e in eps if e.name == token]
    if exact:
        return exact[0]
    partial = [e for e in eps if token in e.name]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise SystemExit(f"⛔ 工作区里没有叫「{token}」的期。现有：{', '.join(e.name for e in eps) or '（空）'}")
    raise SystemExit(f"⛔「{token}」匹配到多期：{', '.join(e.name for e in partial)}")


def stage_state(work: Path) -> tuple[int, int, Stage | None]:
    """返回 (已完成阶段数, 总阶段数, 第一个未完成的阶段)。"""
    stages = build_stages()
    for idx, stg in enumerate(stages):
        try:
            if stg.done(work):
                continue
        except Exception:  # noqa: BLE001
            pass
        return idx, len(stages), stg
    return len(stages), len(stages), None


def advance(work: Path, *, dry: bool = False) -> tuple[bool, str]:
    """一路推进到卡住或完成。返回 (是否全部完成, 说明)。"""
    stages = build_stages()
    log_path = LOGROOT / f"{work.name}.log"
    while True:
        current = None
        for stg in stages:
            try:
                finished = stg.done(work)
            except Exception:  # noqa: BLE001
                finished = False
            if not finished:
                current = stg
                break
        if current is None:
            return True, "全部环节已完成"

        head(f"[{work.name}] {current.label}")
        # 合并组：deliver 不单独出稿，等合并草稿统一交付（新层）
        if current.key == "deliver" and not dry:
            group = _merge_group(work)
            if group and not _merged_delivered(work):
                return False, (
                    f"【等你】[{work.name}] 属于合并组「{group}」（同一天丢了多个视频），"
                    f"由合并草稿统一出稿：\n"
                    f"      python 工具\\合并草稿.py {group}"
                )
        if current.gate:
            return False, f"【等你】{current.label}\n      {current.hint(work) if current.hint else ''}"
        if dry:
            return True, f"（演练）下一步会执行：{current.label}"
        assert current.cmd is not None
        rc, tail = call_tee(current.cmd(work), log_path)
        if rc != 0:
            return False, f"【失败】{current.label}（退出码 {rc}）\n      " + \
                (tail.replace("\n", "\n      ") if tail else "（无输出，见运行记录）")
        echo(f"   ✅ {current.label}")


# ══════════════════════════════════════════════════════════════════════════
# 7. 交付台账
# ══════════════════════════════════════════════════════════════════════════


def write_delivery_note(work: Path) -> Path | None:
    try:
        conf = read_json(work / "config.json")
        struct = read_json(work / "jianying_structural_verification.json")
    except Exception as exc:  # noqa: BLE001
        echo(f"   ⚠ 台账生成跳过：{exc}")
        return None

    draft = Path(str(struct.get("draft_path") or ""))
    core = conf.get("core_expression") or conf.get("name") or work.name
    font = conf.get("font") or {}
    media = conf.get("media") or {}
    stamp = dt.date.today().isoformat()
    # 正常情况工作区名就是核心表达（接单时按核心表达建），目录就叫「日期_核心表达」——
    # 好看也好找。但同一个核心表达可能对应两个工作区（比如换台机器重跑、或拿旧
    # 测试工作区做对照），那种情况必须带上工作区名，否则两期的交付说明会互相覆盖。
    folder_name = f"{stamp}_{core}"
    if work.name != core:
        folder_name = f"{stamp}_{core}（{work.name}）"
    folder = OUTBOX / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    def count_list(path: Path):
        try:
            data = read_json(path)
            return len(data) if isinstance(data, list) else "?"
        except Exception:  # noqa: BLE001
            return "?"

    segs = count_list(work / "rough_segments.json")
    try:
        cards_data = read_json(work / "cards.json")
    except Exception:  # noqa: BLE001
        cards_data = {}
    if not isinstance(cards_data, dict):
        cards_data = {}
    # 卡数 = 所有段键下的卡加起来；段键数 = dict 的键个数。
    # 注意别把 cards_data 本身写进正文（踩过：整份 dict 被格式化进交付说明里）
    card_total = sum(len(v) for v in cards_data.values() if isinstance(v, list))
    card_groups = len(cards_data)

    runtime = None
    runtime_path = work / "jianying_runtime_verification.json"
    if runtime_path.is_file():
        try:
            runtime = read_json(runtime_path)
        except Exception:  # noqa: BLE001
            runtime = None

    state = "complete_runtime_verified" if runtime else "complete_structural"
    duration = media.get("duration_s")
    video = media.get("video") or {}

    lines = [
        f"# {core} · 交付说明",
        "",
        f"- 交付日期：{stamp}",
        f"- 核心表达：{core}",
        f"- 原素材：`{conf.get('original_path') or conf.get('proxy')}`",
        f"- 时长：{duration:.2f} 秒" if isinstance(duration, (int, float)) else "- 时长：—",
        f"- 画幅：{video.get('width')}×{video.get('height')} @ {video.get('fps')}fps",
        f"- 视频段：{segs} 段 · 字幕：{card_total} 张（{card_groups} 个段键）",
        f"- 字幕字体：{font.get('name')}（资源 id {font.get('resource_id')}）",
        f"- 交付状态：`{state}`",
        "",
        "## 剪映草稿",
        "",
        f"`{draft}`",
        "",
        "打开剪映专业版即可在草稿列表里看到它（已登记草稿箱）。目录里也放了一个",
        "`打开草稿.cmd`，双击直接跳转到草稿文件夹。",
        "",
    ]
    # 已经实机确认过的期，就别再叫人家"去确认三项"了 —— 正文自相矛盾。
    if runtime:
        lines += [
            "## 实机验收（已完成）",
            "",
            f"- 检查时间：{runtime.get('checked_at')}",
            f"- 素材可见：{runtime.get('material_bin_visible')} · "
            f"时间线可读：{runtime.get('timeline_media_accessible')} · "
            f"出画面：{runtime.get('preview_frame_visible')}",
            f"- 说明：{runtime.get('evidence_note')}",
            "",
        ]
    else:
        lines += [
            "## 请你实机确认三项",
            "",
            "1. 左上角项目素材可见",
            "2. 时间线没有「无访问权限」",
            "3. 播放器能出画面（字幕字体也顺便看一眼）",
            "",
            "三项都正常后，运行下面这条把状态推到「已实机确认」：",
            "",
            "```bat",
            f'"{PY}" "{SCRIPTS / "p4j_记录剪映实机验收.py"}" "{work}" ^',
            "  --material-bin-visible yes --timeline-media-accessible yes ^",
            "  --preview-frame-visible yes --evidence-note \"<你怎么检查的>\"",
            "```",
            "",
            "有任何一项不对：把现象说清楚，不要直接改 JSON 冒充通过。",
            "",
        ]
    lines += [
        "## 中间产物",
        "",
        f"`{work}`",
        "",
        "决策类 JSON（content_plan / keep / expression_review / structure /",
        "cards / cut_review）与决策锁 `decision_lock.json` 都在这里，",
        "要改内容就回这里改，然后按工序隔离锁重跑（改上游会让冻结失效）。",
        "",
    ]
    note = folder / "交付说明.md"
    note.write_text("\n".join(lines), encoding="utf-8")

    cmd = folder / "打开草稿.cmd"
    cmd.write_text(
        "@echo off\r\nchcp 65001 >nul\r\n"
        f'explorer "{draft}"\r\n',
        encoding="utf-8",
    )

    # 总台账：以「交付日期 + 期（工作区名）」为键，同一期再交付就**整行替换**成最新的。
    # 两个坑：① 原来发现键已存在就跳过不写，于是返工后重出草稿时，台账里留的还是
    # 旧那一版草稿的路径和旧状态；② 原来键只用（日期 + 核心表达），两期同一个内容
    # 会挤成一行、互相看不见。所以键加上期名，列也把期名露出来。
    ledger = OUTBOX / "全部交付.md"
    entry = (f"| {stamp} | {work.name} | {core} | {card_total} 张字幕 "
             f"| `{draft}` | {state} |")
    if not ledger.is_file():
        ledger.write_text(
            "# 全部交付\n\n| 日期 | 期 | 核心表达 | 字幕 | 剪映草稿 | 状态 |\n"
            "|---|---|---|---|---|---|\n" + entry + "\n",
            encoding="utf-8",
        )
    else:
        marker = f"| {stamp} | {work.name} |"
        text = ledger.read_text(encoding="utf-8")
        if marker in text:
            kept = [entry if ln.startswith(marker) else ln
                    for ln in text.splitlines()]
            ledger.write_text("\n".join(kept) + "\n", encoding="utf-8")
        else:
            ledger.write_text(text.rstrip("\n") + "\n" + entry + "\n",
                              encoding="utf-8")
    return note


# ══════════════════════════════════════════════════════════════════════════
# 8. 命令
# ══════════════════════════════════════════════════════════════════════════


def cmd_run(args) -> int:
    echo(f"Skill 本体：{SKILL}")
    echo(f"管线解释器：{PY}")
    head("第 1 步 · 接单（看 输入\\ 里有什么）")
    intake(args.dry)

    episodes = list_episodes()
    if args.only:
        episodes = [find_episode(args.only)]
    if not episodes:
        echo("")
        echo(f"没有可推进的期。把素材丢进 {INBOX}\\<日期>\\ 再跑一次。")
        return 0

    pending: list[str] = []
    for work in episodes:
        done, message = advance(work, dry=args.dry)
        if done:
            # 和 deliver 阶段的完成判定同一个口径：报告在 + 草稿在 + 已登记。
            # 光看报告文件在不在，会在「报告是过期件」时写出指向别处的交付说明。
            if _merged_delivered(work) and not _delivered(work):
                try:
                    marker = read_json(work / "合并交付.json")
                    echo("")
                    echo(f"🎉 [{work.name}] 已由合并草稿统一交付。")
                    echo(f"   草稿：{marker.get('draft_path')}")
                except Exception:  # noqa: BLE001
                    echo(f"🎉 [{work.name}] 已由合并草稿统一交付。")
            elif _delivered(work):
                note = write_delivery_note(work)
                echo("")
                echo(f"🎉 [{work.name}] 已经出了剪映草稿。")
                if note:
                    echo(f"   交付说明：{note}")
                verified = (work / "jianying_runtime_verification.json").is_file()
                echo("   已实机确认。" if verified
                     else "   打开剪映验收三项；确认后我来记实机状态。")
            else:
                echo(f"\n[{work.name}] {message}")
        else:
            pending.append(f"[{work.name}]\n      {message}")

    if pending:
        head("卡在这里了 —— 需要你（或 AI）补一个文件")
        for item in pending:
            echo(item)
            echo("")
        echo("补完再跑一次：  python 工具\\剪辑.py")
    return 0


def cmd_status(_args) -> int:
    episodes = list_episodes()
    if not episodes:
        echo(f"工作区\\ 还是空的。把素材丢进 {INBOX}，然后跑 剪辑.py。")
        return 0
    echo("")
    echo(f"{'期名':<26}{'进度':<9}当前环节")
    echo("─" * 96)
    for work in episodes:
        done, total, stage = stage_state(work)
        if stage is None:
            if _merged_delivered(work) and not _delivered(work):
                where = "✅ 已合并交付"
            elif (work / "jianying_runtime_verification.json").is_file():
                where = "✅ 已实机确认"
            else:
                where = "✅ 已出草稿（待你在剪映里确认三项）"
        elif stage.key == "deliver" and _merge_group(work):
            if _merged_delivered(work):
                where = "✅ 已合并交付"
            else:
                where = f"等合并：python 工具\\合并草稿.py {_merge_group(work)}"
        else:
            where = ("等你：" if stage.gate else "自动：") + stage.label
        echo(f"{work.name:<26}{f'{done}/{total}':<9}{where}")
    echo("")
    return 0


def cmd_deliver(args) -> int:
    episodes = list_episodes()
    if args.only:
        episodes = [find_episode(args.only)]
    if len(episodes) != 1 and not args.only:
        if not episodes:
            echo("工作区\\ 里没有可交付的期。")
            return 0
        echo("有多期，请指明片名：")
        for work in episodes:
            echo(f"    python 工具\\剪辑.py deliver --only \"{work.name}\"")
        return 0
    work = episodes[0]
    if not (work / "decision_lock.json").is_file():
        echo(f"⛔ [{work.name}] 还没冻结（缺 decision_lock.json），不能交付。")
        echo("   先跑  python 工具\\剪辑.py 把它推到冻结。")
        return 1
    if not (work / "cards.json").is_file():
        done, total, stage = stage_state(work)
        echo(f"⛔ [{work.name}] 还没分卡（缺 cards.json），不能交付。")
        echo(f"   当前卡在：{stage.label if stage else '?'}（进度 {done}/{total}）")
        return 1
    done, message = advance(work, dry=args.dry)
    if done:
        note = write_delivery_note(work)
        echo("")
        echo(f"🎉 [{work.name}] 交付完成。")
        if note:
            echo(f"   交付说明：{note}")
        return 0
    echo("")
    echo(f"⚠ [{work.name}] 没能交付：\n      {message}")
    return 1


def cmd_inputs(_args) -> int:
    videos = scan_inputs()
    if not videos:
        echo(f"输入\\ 里没有视频。丢 mp4 到：{INBOX}\\<日期>\\（如 {INBOX / '2026-09-15'}）")
        return 0
    echo("")
    for video in videos:
        target = WORKROOT / video.stem
        state = "已登记" if (target / "config.json").is_file() else "未登记"
        core, why = core_expression_for(video)
        echo(f"  [{state}] {video}")
        echo(f"            核心表达：{core or '（缺）'}  [{why}]")
    loose = _loose_in_inbox(videos)
    if loose:
        echo(f"⚠ 以上 {len(loose)} 个直接放在 输入\\ 根下，建议挪进 日期文件夹（输入\\2026-09-15\\ 这种）")
    echo("")
    return 0


def cmd_doctor(_args) -> int:
    head("工作台自检")
    echo(f"工作台根目录 : {BASE}")
    echo(f"输入          : {INBOX}  {'✓' if INBOX.is_dir() else '✗ 缺'}")
    echo(f"输出          : {OUTBOX}  {'✓' if OUTBOX.is_dir() else '✗ 缺'}")
    echo(f"工作区        : {WORKROOT}  {'✓' if WORKROOT.is_dir() else '✗ 缺'}")
    echo(f"设置          : {SETTINGS}  {'✓' if SETTINGS.is_file() else '✗ 缺'}")
    echo("")
    echo(f"Skill 本体    : {SKILL}")
    echo(f"管线解释器    : {PY}")
    if not SKILL.is_dir() or not PY.is_file():
        echo("✗ 上面两项有一项不可用，后面的检查跳过")
        return 1

    conf = settings()
    echo("")
    echo("--- 设置 ---")
    for key in ("素材类型", "字幕字体", "skill_root", "剪映草稿根", "转写热词"):
        echo(f"   {key} = {conf.get(key) or '（空，自动）'}")
    echo("   （「剪映草稿根」只是记录用备忘；草稿根实际由定位器自动探测，")
    echo("     固定位置请设环境变量 JIANYING_DRAFT_ROOT，写设置.json 不生效）")

    echo("")
    echo("--- 管线环境自检 ---")
    rc, _ = call_tee([PY, PIPELINE, "doctor"], LOGROOT / "_doctor.log")
    echo("")
    echo("工作台结论: " + ("✅ 就绪" if rc == 0 else "⚠ 管线环境有缺件（见上）"))
    return rc


# ══════════════════════════════════════════════════════════════════════════
# 9. 入口
# ══════════════════════════════════════════════════════════════════════════


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="剪辑.py",
        description="口播出稿 · 剪辑工作台（把素材丢进 输入\\，跑本程序，出剪映草稿）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用：\n"
            "  剪辑.py                    接单 + 一路推进（卡住会告诉你该写哪个文件）\n"
            "  剪辑.py status             看每一期走到哪一步\n"
            "  剪辑.py deliver --only 片名  只交付某一期\n"
            "  剪辑.py doctor             环境自检\n"
        ),
    )
    p.add_argument("command", nargs="?", default="run",
                   choices=["run", "status", "deliver", "doctor", "inputs"],
                   help="默认 run")
    p.add_argument("--only", default=None, help="只处理片名里含这个字符串的那一期")
    p.add_argument("--dry", action="store_true", help="只报告要做什么，不实际执行")
    p.add_argument("-v", "--verbose", action="store_true", help="回显每一条子命令")
    return p


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    args = parser().parse_args(argv)
    VERBOSE = bool(args.verbose)

    # doctor 之外都要先定位管线
    if args.command != "doctor":
        try:
            load_pipeline()
        except SystemExit as exc:
            echo(str(exc))
            return 1
    else:
        try:
            load_pipeline()
        except SystemExit as exc:
            head("工作台自检")
            echo(f"工作台根目录 : {BASE}")
            echo(f"✗ {exc}")
            return 1

    return {
        "run": cmd_run,
        "status": cmd_status,
        "deliver": cmd_deliver,
        "doctor": cmd_doctor,
        "inputs": cmd_inputs,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
