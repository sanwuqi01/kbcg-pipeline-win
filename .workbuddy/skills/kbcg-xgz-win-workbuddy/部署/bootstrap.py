#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口播视频剪映出草稿（Windows 版）· 一键适配

把一台新的 Windows 机器适配到能跑本 Skill。幂等：可以反复跑，已就位的步骤会跳过。

只用标准库，所以能用机器上**任意** Python 3.11+ 执行（不需要先有管线环境）。

    bootstrap.cmd                       等价于：python bootstrap.py
    python bootstrap.py --dry-run       只看会做什么，不动手
    python bootstrap.py --skip models   跳过模型下载（最慢的一步）
    python bootstrap.py --python "C:\\Python313\\python.exe"
    python bootstrap.py --cpu           无显卡机器：装 CPU 版 torch
    python bootstrap.py --hf-mirror     国内网络慢时走 hf-mirror.com

真值来源：部署/环境契约.json（本脚本是它的可执行版本）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent
SKILL_ROOT = DEPLOY_DIR.parent
CONTRACT_PATH = DEPLOY_DIR / "环境契约.json"

REQ_PIPELINE = DEPLOY_DIR / "requirements-pipeline.txt"
REQ_CAPCUT = DEPLOY_DIR / "requirements-capcutmate.txt"

TORCH_VERSION = "2.14.0"
TORCHAUDIO_VERSION = "2.11.0"
TORCH_INDEX_CUDA = "https://download.pytorch.org/whl/cu126"
TORCH_INDEX_CPU = "https://download.pytorch.org/whl/cpu"
HF_MIRROR = "https://hf-mirror.com"

# doctor 逐项探测的模块（与 脚本_win/pipeline.py 的 _PROBE_MODULES 同源）
PIPELINE_MODULES = (
    "torch", "numpy", "silero_vad", "transformers",
    "faster_whisper", "ctranslate2", "soundfile", "jieba",
)

CAPCUT_COMMIT = "96f36486a9341ae1ad9c81d8f087d2b4caa2eac6"
CAPCUT_REPO = "https://github.com/Hommy-master/capcut-mate.git"
ASR_REPO_ID = "large-v3-turbo"
ALIGNER_MODEL_ID = "Qwen/Qwen3-ForcedAligner-0.6B-hf"


# ══════════════════════════════════════════════════════════════════════
# 输出与执行小工具
# ══════════════════════════════════════════════════════════════════════

DRY_RUN = False
ASSUME_YES = False
STEP_INDEX = 0
STEP_TOTAL = 7
FAILURES: list[str] = []


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def head(title: str) -> None:
    global STEP_INDEX
    STEP_INDEX += 1
    print()
    print(f"[{STEP_INDEX}/{STEP_TOTAL}] {title}")
    print("-" * 60)


def ok(msg: str) -> None:
    print(f"    ✓ {msg}")


def info(msg: str) -> None:
    print(f"    · {msg}")


def warn(msg: str) -> None:
    print(f"    ! {msg}")


def fail(msg: str) -> None:
    print(f"    ✗ {msg}")
    FAILURES.append(msg)


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update({k: v for k, v in extra.items() if v})
    return env


def run(cmd: list[str], *, env: dict[str, str] | None = None,
        cwd: Path | None = None, echo: bool = True) -> int:
    printable = subprocess.list2cmdline([str(c) for c in cmd])
    if echo:
        print(f"      $ {printable}")
    if DRY_RUN:
        return 0
    try:
        proc = subprocess.run(
            [str(c) for c in cmd], env=env or child_env(),
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        fail(f"命令不存在：{exc.filename}")
        return 127
    return proc.returncode


def run_capture(cmd: list[str], *, env: dict[str, str] | None = None,
                cwd: Path | None = None) -> tuple[int, str]:
    """执行并捕获输出。

    **不受 --dry-run 影响**：本函数专用于"只读探测"（查版本、探模块、git rev-parse/
    status、校验快照）。要探测就得真跑，否则 dry-run 会把"环境齐全"误报成"缺 8 个模块"。
    会改动系统状态的命令一律走 run()。
    """
    try:
        proc = subprocess.run(
            [str(c) for c in cmd], env=env or child_env(),
            cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        return 127, ""
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def confirm(question: str) -> bool:
    if ASSUME_YES or DRY_RUN:
        return True
    try:
        answer = input(f"    ? {question} [Y/n] ").strip().lower()
    except EOFError:
        return True
    return answer in ("", "y", "yes")


# ══════════════════════════════════════════════════════════════════════
# 基础探测
# ══════════════════════════════════════════════════════════════════════

def load_contract() -> dict:
    if not CONTRACT_PATH.is_file():
        return {}
    try:
        return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        warn(f"环境契约.json 解析失败（不阻塞）：{exc}")
        return {}


def find_base_python(explicit: str | None) -> Path | None:
    """找一个能用来建 venv 的 Python 3.11+。"""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    if sys.platform == "win32":
        launcher = shutil.which("py")
        if launcher:
            rc, out = run_capture([str(launcher), "-3", "-c", "import sys;print(sys.executable)"])
            if rc == 0 and out.strip():
                candidates.append(Path(out.strip().splitlines()[-1]))
    candidates.append(Path(sys.executable))

    for path in candidates:
        if not path.is_file():
            continue
        rc, out = run_capture([str(path), "-c", "import sys;print('%d.%d' % sys.version_info[:2])"])
        if rc != 0:
            continue
        try:
            major, minor = (int(x) for x in out.strip().splitlines()[-1].split("."))
        except Exception:  # noqa: BLE001
            continue
        if (major, minor) >= (3, 11):
            return path
    return None


def modules_present(python: Path, modules: tuple[str, ...]) -> dict[str, bool]:
    code = (
        "import importlib.util, json, sys\n"
        "print(json.dumps({m: importlib.util.find_spec(m) is not None for m in sys.argv[1:]}))\n"
    )
    rc, out = run_capture([str(python), "-c", code, *modules])
    if rc != 0:
        return {m: False for m in modules}
    try:
        payload = json.loads(out.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return {m: False for m in modules}
    return {m: bool(payload.get(m)) for m in modules}


def resolve_pipeline_python() -> Path | None:
    """与 脚本_win/pipeline.py 的 resolve_python() 同序探测。"""
    candidates: list[Path] = []
    explicit = os.environ.get("XGZ_PIPELINE_PY")
    if explicit:
        candidates.append(Path(explicit))
    candidates += [
        SKILL_ROOT / ".venv" / "Scripts" / "python.exe",
        SKILL_ROOT / ".venv" / "bin" / "python",
        Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "asr-win" / "Scripts" / "python.exe",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        present = modules_present(path, PIPELINE_MODULES)
        if all(present.values()):
            return path
        # 环境在、但缺包 → 也返回它，由后面的装包步骤补齐
        return path
    return None


def capcut_home() -> Path:
    override = os.environ.get("CAPCUT_MATE_HOME")
    if override:
        return Path(override)
    for cand in (
        Path.home() / "Developer" / "kbcg-xgz-capcut-mate",
        Path.home() / "Developer" / "capcut-mate",
    ):
        if cand.is_dir():
            return cand
    return Path.home() / "Developer" / "capcut-mate"


def capcut_python(home: Path) -> Path | None:
    for rel in ((".venv", "Scripts", "python.exe"), (".venv", "bin", "python")):
        cand = home.joinpath(*rel)
        if cand.is_file():
            return cand
    return None


# ══════════════════════════════════════════════════════════════════════
# 各步骤
# ══════════════════════════════════════════════════════════════════════

def step_host() -> None:
    head("检查宿主放置位置")
    print(f"    SKILL_ROOT : {SKILL_ROOT}")
    if not (SKILL_ROOT / "SKILL.md").is_file():
        fail("SKILL.md 不在预期位置，包结构可能被破坏")
        return
    ok("SKILL.md 就位")

    expected_parent = Path.home() / ".workbuddy" / "skills"
    parent = SKILL_ROOT.parent.resolve()
    if parent == expected_parent.resolve():
        ok(f"放在 WorkBuddy 用户级技能目录下：{expected_parent}")
    elif parent.name == "skills" and parent.parent.name == ".workbuddy":
        # 工作区形态（新分发包默认）：<工作区>\.workbuddy\skills\<skill>
        # 用 WorkBuddy 打开该工作区即被识别，无需拷贝到用户目录。
        ok(f"放在工作区级技能目录下（项目级 Skill）：{parent}")
        ok(f"用 WorkBuddy「打开文件夹」选择工作区根即可用：{parent.parent.parent}")
    else:
        warn("Skill 不在任何 .workbuddy\\skills\\ 下 —— WorkBuddy 可能识别不到它。")
        warn(f"  当前在：{SKILL_ROOT}")
        warn(f"  用户级应在：{expected_parent / SKILL_ROOT.name}")
        warn( "  工作区级应在：<你的工作区>\\.workbuddy\\skills\\" + SKILL_ROOT.name)

    if SKILL_ROOT.name != "kbcg-xgz-win-workbuddy":
        warn(f"文件夹名是 '{SKILL_ROOT.name}'，约定名是 'kbcg-xgz-win-workbuddy'（改名会识别不到）")
    else:
        ok("文件夹名正确")

    print()
    info("请确认这两件事（本脚本无法代你检查）：")
    info("  1) 宿主是 WorkBuddy 桌面版")
    info("  2) 会话模型是 GLM 5.3 Flash 或 DeepSeek V4.1 Flash（推荐档二选一）")
    info("  不一致时 Skill 的判断质量不可控 —— 契约测试不会报警。")


def step_env(skip: bool, explicit_python: str | None) -> Path | None:
    head("管线 Python 环境")
    if skip:
        info("按 --skip env 跳过")
        return resolve_pipeline_python()

    existing = resolve_pipeline_python()
    if existing:
        present = modules_present(existing, PIPELINE_MODULES)
        missing = [m for m in PIPELINE_MODULES if not present[m]]
        if not missing:
            ok(f"已就位：{existing}")
            return existing
        ok(f"环境存在：{existing}")
        warn(f"缺 {len(missing)} 个模块：{', '.join(missing)}")
        python = existing
    else:
        base = find_base_python(explicit_python)
        if base is None:
            fail("找不到 Python 3.11+。请先装 Python（https://www.python.org/downloads/），"
                 "或用 --python 指定路径")
            return None
        ok(f"基础解释器：{base}")
        venv_dir = SKILL_ROOT / ".venv"
        if venv_dir.is_dir():
            info(f"复用已有 venv：{venv_dir}")
        else:
            info(f"创建 venv：{venv_dir}")
            if run([str(base), "-m", "venv", str(venv_dir)]) != 0:
                fail("venv 创建失败")
                return None
        python = venv_dir / "Scripts" / "python.exe"
        if not python.is_file():
            python = venv_dir / "bin" / "python"

    if not REQ_PIPELINE.is_file():
        fail(f"缺依赖清单：{REQ_PIPELINE}")
        return python

    if not confirm("接下来会下载约 2.5 GB（torch CUDA 轮子 2.42GB + 其余），继续？"):
        warn("用户取消，环境未装完")
        return python

    extra = {"HF_ENDPOINT": HF_MIRROR} if os.environ.get("_XGZ_HF_MIRROR") else {}
    if run([str(python), "-m", "pip", "install", "--upgrade", "pip"], env=child_env(extra)) != 0:
        warn("pip 升级失败，继续用现有版本")

    info("装基础依赖（12 个包）…")
    if run([str(python), "-m", "pip", "install", "-r", str(REQ_PIPELINE)],
           env=child_env(extra)) != 0:
        fail("基础依赖安装失败，见上面的 pip 输出")
        return python

    index = TORCH_INDEX_CPU if os.environ.get("_XGZ_CPU") else TORCH_INDEX_CUDA
    info(f"装 torch（index-url={index}）…")
    if run([str(python), "-m", "pip", "install",
            f"torch=={TORCH_VERSION}", f"torchaudio=={TORCHAUDIO_VERSION}",
            "--index-url", index], env=child_env(extra)) != 0:
        fail("torch 安装失败")
        return python

    present = modules_present(python, PIPELINE_MODULES)
    missing = [m for m in PIPELINE_MODULES if not present[m]]
    if missing:
        fail(f"仍缺模块：{', '.join(missing)}")
    else:
        ok("12 个模块全部就位")
    return python


def step_tools(skip: bool) -> None:
    head("外部可执行文件 ffmpeg / ffprobe")
    if skip:
        info("按 --skip tools 跳过")
        return
    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        if found:
            ok(f"{tool}: {found}")
        else:
            fail(f"{tool} 不在 PATH —— 装一个：scoop install ffmpeg，"
                 f"或把 ffmpeg 的 bin 目录加进 PATH")


def step_models(skip: bool, python: Path | None) -> None:
    head(f"模型（ASR {ASR_REPO_ID} + 对齐器 0.6B）")
    if skip:
        info("按 --skip models 跳过")
        return
    if python is None:
        fail("没有可用的管线解释器，模型无法下载")
        return

    model_root = SKILL_ROOT / "模型" / "asr"
    dest = model_root / "mobiuslabsgmbh__faster-whisper-large-v3-turbo"
    scripts_win = str(SKILL_ROOT / "脚本_win")
    extra = {"HF_ENDPOINT": HF_MIRROR} if os.environ.get("_XGZ_HF_MIRROR") else {}
    if (dest / "model.bin").is_file() and (dest / "model.bin").stat().st_size > 0:
        ok(f"ASR 模型已就位：{dest}")
    else:
        if not confirm("下载 ASR 模型约 1546 MB，继续？"):
            warn("用户取消，ASR 模型未下载")
        else:
            info("下载 ASR 模型（实体复制，不走 symlink）…")
            code = (
                "import sys\n"
                f"sys.path.insert(0, r'{scripts_win}')\n"
                "import 转写_faster_whisper as m\n"
                "from pathlib import Path\n"
                f"print(m.fetch_model({ASR_REPO_ID!r}, Path(r'{model_root}'), False))\n"
            )
            rc = run([str(python), "-c", code], env=child_env(extra))
            if DRY_RUN:
                pass
            elif rc == 0:
                ok(f"ASR 模型就位：{dest}")
            else:
                fail("ASR 模型下载失败（网络问题？试试 --hf-mirror）")

    # 对齐器：落 HF 缓存，首次跑对齐时也会自动拉；这里预先拉一次省得临时等
    if not confirm(f"预拉对齐器模型 {ALIGNER_MODEL_ID}（约 1830 MB）？跳过也行，首次跑对齐会自动拉"):
        info("跳过对齐器预拉")
        return
    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{scripts_win}')\n"
        "from huggingface_hub import snapshot_download\n"
        f"snapshot_download({ALIGNER_MODEL_ID!r})\n"
        "import aligner_win\n"
        f"snap = aligner_win.local_snapshot({ALIGNER_MODEL_ID!r})\n"
        "print('SNAPSHOT=' + str(snap))\n"
    )
    if DRY_RUN:
        print(f"      $ <pipeline python> -c <预拉 {ALIGNER_MODEL_ID} 并校验快照>")
        return
    rc, out = run_capture([str(python), "-c", code], env=child_env(extra))
    if rc == 0 and "SNAPSHOT=" in out and "None" not in out.split("SNAPSHOT=")[-1].splitlines()[0]:
        ok(f"对齐器模型就位：{out.split('SNAPSHOT=')[-1].splitlines()[0].strip()}")
    else:
        warn("对齐器预拉未确认成功（不阻塞；首次跑对齐会自动拉）")
        warn("  若之后报 'model.bin is incomplete'：删掉 HF 缓存里 0 字节的 snapshots 目录重拉，"
             "不要急着重下几百 MB")


def step_capcut(skip: bool, explicit_python: str | None) -> tuple[Path | None, Path | None]:
    head("CapCutMate（锁提交 + venv + 依赖）")
    if skip:
        info("按 --skip capcut 跳过")
        home = capcut_home()
        return (home, capcut_python(home)) if home.is_dir() else (None, None)

    home = capcut_home()
    if (home / "src" / "pyJianYingDraft").is_dir():
        ok(f"仓库已存在：{home}")
    else:
        if shutil.which("git") is None:
            fail("找不到 git —— 请先装 Git for Windows")
            return None, None
        parent = home.parent
        if not confirm(f"clone CapCutMate 到 {home}，继续？"):
            warn("用户取消")
            return None, None
        parent.mkdir(parents=True, exist_ok=True)
        if run(["git", "clone", CAPCUT_REPO, str(home)]) != 0:
            fail("git clone 失败（网络问题？）")
            return None, None
        ok(f"已 clone：{home}")

    # 锁提交
    rc, out = run_capture(["git", "-C", str(home), "rev-parse", "HEAD"])
    head_sha = out.strip().splitlines()[-1] if out.strip() else ""
    if DRY_RUN:
        head_sha = CAPCUT_COMMIT
    if head_sha != CAPCUT_COMMIT:
        warn(f"当前提交 {head_sha[:12] or '?'} ≠ 锁定提交 {CAPCUT_COMMIT[:12]}")
        if run(["git", "-C", str(home), "checkout", CAPCUT_COMMIT]) != 0:
            fail("切到锁定提交失败")
            return home, None
    else:
        ok(f"提交正确：{CAPCUT_COMMIT[:12]}")

    # 工作树完整性：checkout 会静默留下残缺工作树
    rc, out = run_capture(["git", "-C", str(home), "status", "--porcelain"])
    dirty = [line for line in out.splitlines() if line.strip()]
    if dirty:
        warn(f"工作树有 {len(dirty)} 处改动（checkout 可能留下残缺文件）")
        if run(["git", "-C", str(home), "checkout", "-f"]) != 0:
            fail("checkout -f 失败")
        else:
            rc, out2 = run_capture(["git", "-C", str(home), "status", "--porcelain"])
            left = [line for line in out2.splitlines() if line.strip()]
            if left:
                warn(f"仍有 {len(left)} 处差异（可能无害）")
            else:
                ok("工作树已干净")
    else:
        ok("工作树干净")

    python = capcut_python(home)
    if python is None:
        base = find_base_python(explicit_python)
        if base is None:
            fail("没有可用于建 CapCutMate venv 的 Python")
            return home, None
        info(f"创建 CapCutMate venv（用 {base}）")
        if run([str(base), "-m", "venv", str(home / ".venv")]) != 0:
            fail("CapCutMate venv 创建失败")
            return home, None
        python = capcut_python(home)
    if python is None:
        fail("CapCutMate venv 建好了但找不到 python.exe")
        return home, None
    ok(f"解释器：{python}")

    if not REQ_CAPCUT.is_file():
        fail(f"缺依赖清单：{REQ_CAPCUT}")
        return home, python
    if run([str(python), "-m", "pip", "install", "-r", str(REQ_CAPCUT)]) != 0:
        fail("CapCutMate 依赖安装失败")
    else:
        ok("CapCutMate 依赖就位")

    if home != Path.home() / "Developer" / "capcut-mate":
        info("CapCutMate 不在默认位置，需要设环境变量：")
        info(f"      setx CAPCUT_MATE_HOME \"{home}\"")
        info("      （设完要新开一个命令行窗口才生效）")
    return home, python


def step_fonts(skip: bool, python: Path | None, cut_home: Path | None) -> None:
    head("剪映字体表")
    if skip:
        info("按 --skip fonts 跳过")
        return

    table = SKILL_ROOT / "字体" / "剪映字体表.json"
    if table.is_file():
        try:
            payload = json.loads(table.read_text(encoding="utf-8"))
            ok(f"已随包附带：{payload.get('count', '?')} 个字体"
               f"（免费 {payload.get('free_count', '?')} 个）")
        except Exception:  # noqa: BLE001
            warn("字体表存在但解析失败，将重建")
    if python is None:
        fail("没有可用的管线解释器，跳过字体表生成")
        return

    if cut_home is None:
        info("CapCutMate 不可用，沿用包里附带的字体表（通常没问题）")
        return

    font_meta = cut_home / "src" / "pyJianYingDraft" / "metadata" / "font_meta.py"
    if not font_meta.is_file():
        warn(f"找不到 {font_meta}，沿用包里附带的字体表")
        return
    if run([str(python), str(SKILL_ROOT / "脚本_win" / "gen_font_table.py"),
            "--font-meta", str(font_meta)]) == 0 and not DRY_RUN:
        ok("字体表已按本机 CapCutMate 重建")
    elif not DRY_RUN:
        warn("字体表重建失败，沿用包里附带的版本")


def step_doctor(skip: bool, python: Path | None) -> None:
    head("环境自检 pipeline.py doctor")
    if skip:
        info("按 --skip doctor 跳过")
        return
    if python is None:
        fail("没有可用的管线解释器，无法自检")
        return
    rc = run([str(python), str(SKILL_ROOT / "脚本_win" / "pipeline.py"), "doctor"])
    if DRY_RUN:
        return
    if rc == 0:
        ok("doctor 通过")
    else:
        fail("doctor 未通过 —— 见上面的逐项输出")


# ══════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="口播视频剪映出草稿（Windows 版）· 一键适配",
    )
    p.add_argument("--dry-run", action="store_true", help="只打印将执行的命令，不动手")
    p.add_argument("--yes", "-y", action="store_true", help="所有确认一律同意")
    p.add_argument("--python", default=None, help="指定用于建环境的 Python 3.11+")
    p.add_argument("--cpu", action="store_true", help="装 CPU 版 torch（无显卡机器）")
    p.add_argument("--hf-mirror", action="store_true", help="用 hf-mirror.com 下载模型")
    p.add_argument("--skip", default="", help="逗号分隔：env,tools,models,capcut,fonts,doctor")
    return p


def main(argv: list[str] | None = None) -> int:
    global DRY_RUN, ASSUME_YES, STEP_INDEX
    _utf8()
    args = build_parser().parse_args(argv)
    DRY_RUN = args.dry_run
    ASSUME_YES = args.yes
    if args.cpu:
        os.environ["_XGZ_CPU"] = "1"
    if args.hf_mirror:
        os.environ["_XGZ_HF_MIRROR"] = "1"
    skipped = {s.strip() for s in args.skip.split(",") if s.strip()}

    contract = load_contract()
    host = contract.get("host", {}).get("required", "WorkBuddy（桌面版）")
    model = contract.get("model", {}).get("required", "GLM 5.3 Flash 或 DeepSeek V4.1 Flash")

    print("=" * 62)
    print("口播视频剪映出草稿（Windows 版）· 一键适配")
    print("=" * 62)
    print(f"  SKILL_ROOT : {SKILL_ROOT}")
    print(f"  钉死宿主   : {host}")
    print(f"  钉死模型   : {model}")
    print(f"  模式       : {'DRY-RUN（不动手）' if DRY_RUN else '实际执行'}"
          + (f" · 跳过 {','.join(sorted(skipped))}" if skipped else ""))

    step_host()
    python = step_env("env" in skipped, args.python)
    step_tools("tools" in skipped)
    step_models("models" in skipped, python)
    cut_home, _cut_python = step_capcut("capcut" in skipped, args.python)
    step_fonts("fonts" in skipped, python, cut_home)
    step_doctor("doctor" in skipped, python)

    print()
    print("=" * 62)
    if FAILURES:
        print(f"结果：⚠ {len(FAILURES)} 项未过")
        for item in FAILURES:
            print(f"   ✗ {item}")
        print()
        print("  逐条排查见：部署/安装说明.md §6 故障排查")
    else:
        print("结果：✅ 适配完成")
        print()
        print("接下来做两件事（见 安装说明.md §5）：")
        print("  1) 跑 42 项契约回归：")
        print(f"     \"{python}\" -m unittest tests.test_learning_contracts")
        print("  2) 端到端跑一条真片子，然后在剪映里确认四件事：")
        print("     素材可见 / 无权限问题 / 出画面 / 字幕字体正确")
    if DRY_RUN:
        print()
        print("（DRY-RUN：什么都没改。）")
    print("=" * 62)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
