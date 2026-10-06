#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""口播出稿 · Windows 编排层（4 个 zsh 的 Python 等价物）。

    原版                                本文件
    ----------------------------------  -----------------------------------------
    脚本/f1_粗剪验收冻结.sh             pipeline.py f1       <工作目录> [--force]
    脚本/finalize.sh                    pipeline.py finalize <工作目录> [目标]
    脚本/finalize_jianying.sh           pipeline.py finalize <工作目录> jianying
    脚本/finalize_fcp.sh                pipeline.py finalize <工作目录> final-cut
    （原版无此入口，SKILL.md 分步描述）  pipeline.py p0       <素材> [片名] ...
    （新增运维入口）                     pipeline.py doctor

硬约束（照抄原 zsh，不得改动）
    · 只按序调脚本 + 前置文件存在性检查 + pipeline_manifest 记账；
      **不重跑 p1a / p1a2 / p1 / p2 / p1q**——粗剪已冻结，终结器只做包装投影与验收。
    · 单步失败立即停止且不交付；失败时撤回本轮新建的交付副本、向 manifest 写 abort。
    · `_run` 语义逐条保留：begin 在命令执行前对**输入**取 sha256，命令成功后才 end 取**输出**哈希。

平台适配（仅这三类，逐条登记在 03_Windows迁移分析/02_实测记录.md）
    1. 解释器：Mac 的 PYMLX / PYALIGN / python3 三套，Windows 统一为一个「管线解释器」
       （需 torch / numpy / silero_vad / transformers / faster_whisper / jieba）。见 resolve_python()。
       唯一例外是 p4j_出剪映草稿.py，它跑在 CapCutMate 自己的 venv 里（见 resolve_capcut_mate()）。
    2. 强制 UTF-8：原脚本大量 ⛔✅ emoji，GBK 控制台会 UnicodeEncodeError 崩在半路。
    3. 命令替换：cp→shutil.copy2、find→os.walk、date +%F→datetime.date.today()、
       进程内 zsh 变量展开→Python 变量。**语义不变**。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ══════════════════════════════════════════════════════════════════════════
# 0. 自举：强制 UTF-8
# ══════════════════════════════════════════════════════════════════════════

def _bootstrap_utf8() -> None:
    """未开 UTF-8 模式则以 PYTHONUTF8=1 重启自己，并兜底重配 stdio。

    「重启自己」而不是只重配 stdio：子进程也必须继承 UTF-8 模式，
    而 PYTHONUTF8 只有在**新进程**的环境里才生效。
    不用 os.execve：Windows 的 CPython 上 execve 带环境参数会以
    0xC0000005 崩掉（实测 3.14.4）。换 subprocess 重启 + sys.exit，
    语义等价且跨版本稳。
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


SKILL_ROOT = Path(__file__).resolve().parent.parent      # .../win-port
SCRIPTS = SKILL_ROOT / "脚本"
SCRIPTS_WIN = SKILL_ROOT / "脚本_win"


# ══════════════════════════════════════════════════════════════════════════
# 1. 解释器解析
# ══════════════════════════════════════════════════════════════════════════

_PROBE_MODULES = (
    "torch", "numpy", "silero_vad", "transformers",
    "faster_whisper", "ctranslate2", "soundfile", "jieba",
)
_probe_cache: dict[Path, dict[str, bool]] = {}


def resolve_python() -> Path:
    """管线解释器：Windows 上统一承担 Mac 侧 PYMLX / PYALIGN / python3 三个角色。

    优先级：环境变量 XGZ_PIPELINE_PY → win-port/.venv → 受管 asr-win → 当前解释器。
    选 asr-win 是因为 AGENT.md §3 D1 已把它定位为「Windows 统一管线环境」。
    """
    candidates: list[Path] = []
    explicit = os.environ.get("XGZ_PIPELINE_PY")
    if explicit:
        candidates.append(Path(explicit))
    candidates += [
        SKILL_ROOT / ".venv" / "Scripts" / "python.exe",
        SKILL_ROOT / ".venv" / "bin" / "python",
        Path.home() / ".workbuddy" / "binaries" / "python" / "envs" / "asr-win" / "Scripts" / "python.exe",
        Path(sys.executable),
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit(
        "⛔ 找不到 Windows 管线解释器。\n"
        "   请设置环境变量 XGZ_PIPELINE_PY 指向一个装有\n"
        "   torch / numpy / silero_vad / transformers / faster_whisper / jieba 的 python.exe，\n"
        "   或在 win-port/ 下创建 .venv，或改用受管 asr-win 环境。"
    )


def probe_modules(py: Path) -> dict[str, bool]:
    """用 find_spec 探测模块可用性（**不 import**，所以不会付 torch 的加载代价）。"""
    if py in _probe_cache:
        return _probe_cache[py]
    code = (
        "import importlib.util, json, sys\n"
        "print(json.dumps({m: importlib.util.find_spec(m) is not None for m in sys.argv[1:]}))\n"
    )
    try:
        result = subprocess.run(
            [str(py), "-c", code, *_PROBE_MODULES],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        available = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 else {}
    except Exception:  # noqa: BLE001
        available = {}
    if not isinstance(available, dict):
        available = {}
    _probe_cache[py] = {m: bool(available.get(m)) for m in _PROBE_MODULES}
    return _probe_cache[py]


def require(py: Path, *modules: str) -> None:
    """前置能力检查。缺件时给出可直接照抄的 pip 命令，而不是让子进程崩在中途。"""
    if os.environ.get("XGZ_SKIP_PREFLIGHT") == "1":
        return
    available = probe_modules(py)
    missing = [m for m in modules if not available.get(m)]
    if not missing:
        return
    raise SystemExit(
        f"⛔ 管线解释器缺少依赖 {missing}：\n"
        f"   {py}\n"
        f"   安装：\"{py}\" -m pip install {' '.join(missing)}\n"
        f"   （也可用 doctor 子命令查看完整环境报告）"
    )


def resolve_capcut_mate() -> tuple[Path, Path]:
    """返回 (CAPCUT_MATE_HOME, CAPCUT_PY)。对应 finalize_jianying.sh:12-24。

    原版默认 ~/Developer/kbcg-xgz-capcut-mate 或 ~/Developer/capcut-mate；
    Windows 上 .venv 的布局是 Scripts/python.exe 而非 bin/python。
    """
    home_env = os.environ.get("CAPCUT_MATE_HOME")
    home: Path | None = Path(home_env) if home_env else None
    if home is None:
        for cand in (
            Path.home() / "Developer" / "kbcg-xgz-capcut-mate",
            Path.home() / "Developer" / "capcut-mate",
        ):
            if cand.is_dir():
                home = cand
                break
    if home is None:
        home = Path.home() / "Developer" / "capcut-mate"       # 供报错时显示
    for rel in ((".venv", "Scripts", "python.exe"), (".venv", "bin", "python")):
        candidate = home.joinpath(*rel)
        if candidate.is_file():
            return home, candidate
    raise SystemExit(
        "⛔ 缺少 CapCutMate Python 环境:\n"
        f"   {home / '.venv' / 'Scripts' / 'python.exe'}\n"
        "   请先安装 CapCutMate 并设置 CAPCUT_MATE_HOME（见 AGENT.md §3 D4）后重试。"
    )


def child_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update({k: v for k, v in extra.items() if v})
    return env


# ══════════════════════════════════════════════════════════════════════════
# 2. 进程与 manifest 小工具
# ══════════════════════════════════════════════════════════════════════════

VERBOSE = False


class StepFailed(SystemExit):
    """单步命令非零退出。携带 rc，供外层写 abort 与撤回交付副本。"""

    def __init__(self, step: str, rc: int) -> None:
        super().__init__(rc)
        self.step = step
        self.rc = rc


def show(cmd: list[str]) -> None:
    if VERBOSE:
        print("      $ " + subprocess.list2cmdline([str(c) for c in cmd]))


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> int:
    show(cmd)
    return subprocess.run([str(c) for c in cmd], env=env or child_env()).returncode


def run_capture(
    cmd: list[str], *, env: dict[str, str] | None = None, echo: bool = True
) -> tuple[int, str, str]:
    """执行子进程并回显（可选）+ 捕获。返回 (rc, stdout, stderr)。

    分开返回 stdout/stderr 而不是合并：调用方要拿 stdout 做机器解析（p0 的工作目录、
    定位器的 JSON），stderr 里混进任何一行警告都会污染解析。
    """
    show(cmd)
    proc = subprocess.run(
        [str(c) for c in cmd], env=env or child_env(),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if echo:
        for text in (proc.stdout, proc.stderr):
            if text:
                sys.stdout.write(text)
        sys.stdout.flush()
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def echo_block(label: str, text: str) -> None:
    for line in text.splitlines():
        print(f"     {label}{line}")


def manifest(py: Path, *args: object) -> int:
    return run([py, SCRIPTS / "pipeline_manifest.py", *args])


def _digest(py: Path, work: Path, field: str) -> str:
    """从 manifest 里取一个 run_inputs 指纹（只用于日志，不做判定）。"""
    try:
        payload = json.loads((Path(work) / "run_manifest_jianying.json").read_text(encoding="utf-8"))
        return str(payload.get("run_inputs", {}).get(field, {}).get("sha256", "?")[:12])
    except Exception:  # noqa: BLE001
        return "?"


# ══════════════════════════════════════════════════════════════════════════
# 3. config 读取（照抄两个 zsh 里的 heredoc python）
# ══════════════════════════════════════════════════════════════════════════

_CJK = re.compile(r"[\u3400-\u9fff]")
_C_NUMBER = re.compile(r"[Cc]\d+", re.I)
_PATH_CHARS = re.compile(r"[\\/:\0\r\n\t]")
_ALLOWED_MATERIAL = ("single_speaker", "single_subject_interview")


def read_config_header(work: Path, *, allow_sources_fallback: bool) -> tuple[str, str, str, str]:
    """返回 (name, source, core_expression, material_type)。

    对应 finalize_jianying.sh:65-88（allow_sources_fallback=True）
    与   finalize_fcp.sh:46-65（allow_sources_fallback=False）。
    """
    config = json.loads((work / "config.json").read_text(encoding="utf-8"))
    source = config.get("original_path") or config.get("proxy")
    if not source and allow_sources_fallback:
        sources = config.get("sources")
        if not isinstance(sources, list) or not sources:
            raise SystemExit("⛔ config 既没有 original_path/proxy，也没有 sources")
        source = sources[0].get("path")
    vals = [config.get("name"), source, config.get("core_expression"), config.get("material_type")]
    if any(not isinstance(x, str) or not x for x in vals):
        raise SystemExit("⛔ config 缺少 name/original_path/core_expression/material_type")
    if any("\t" in x or "\n" in x for x in vals):
        raise SystemExit("⛔ config 路径/名称不得包含制表符或换行")
    core = vals[2].strip()
    if not _CJK.search(core) or _C_NUMBER.fullmatch(core):
        raise SystemExit("⛔ 中文核心表达必须包含中文且不能仅为技术编号")
    if _PATH_CHARS.search(core):
        raise SystemExit("⛔ 中文核心表达含路径分隔符或控制字符")
    vals[2] = core
    return vals[0], vals[1], vals[2], vals[3]


def read_source_list(work: Path, fallback: str) -> list[str]:
    """对应 finalize_jianying.sh:111-123 的 SOURCE_LINES。"""
    config = json.loads((work / "config.json").read_text(encoding="utf-8"))
    sources = config.get("sources")
    if isinstance(sources, list) and sources:
        paths = [row.get("path") for row in sources]
    else:
        paths = [fallback]
    if any(not isinstance(p, str) or not p or "\n" in p for p in paths):
        raise SystemExit("⛔ config 素材路径非法")
    return paths


def check_material_type(material_type: str, *, fcp: bool) -> None:
    if material_type in _ALLOWED_MATERIAL:
        return
    if fcp:
        raise SystemExit(
            f"⛔ material_type={material_type}；当前链只允许 single_speaker / "
            "single_subject_interview。multi_speaker/unknown 必须停止。"
        )
    raise SystemExit(f"⛔ material_type={material_type}；当前链不支持。")


def require_speaker_turns(work: Path, material_type: str) -> list[Path]:
    if material_type != "single_subject_interview":
        return []
    path = work / "speaker_turns.json"
    if not path.is_file():
        raise SystemExit("⛔ 缺 speaker_turns.json")
    return [path]


def require_files(paths: list[Path]) -> None:
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"⛔ 缺少冻结后必需输入: {path}")


def find_vad_wav(root: Path) -> Path | None:
    """替代 zsh 的 `find "$WORK/sources" -type f -name '_vad16k.wav' -print -quit`。"""
    if not root.is_dir():
        return None
    for current, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            if name == "_vad16k.wav":
                return Path(current) / name
    return None


def next_free_draft_path(draft_root: Path, delivery_date: str, core: str) -> tuple[str, Path]:
    """对应 finalize_jianying.sh:103-110 的占位名递增。"""
    stem = f"{delivery_date}_{core}_剪映草稿"
    path = draft_root / stem
    version = 1
    while path.exists():
        version += 1
        stem = f"{delivery_date}_{core}_剪映草稿_第{version}版"
        path = draft_root / stem
    return stem, path


def next_free_delivery_path(deliver_dir: Path, delivery_date: str, core: str) -> tuple[str, Path]:
    """对应 finalize_fcp.sh:86-94 的占位名递增（起始版号是 2，照抄）。"""
    stem = f"{delivery_date}_{core}"
    path = deliver_dir / f"{stem}.fcpxml"
    version = 2
    while path.exists():
        stem = f"{delivery_date}_{core}_第{version}版"
        path = deliver_dir / f"{stem}.fcpxml"
        version += 1
    return stem, path


# ══════════════════════════════════════════════════════════════════════════
# 4. f1 · 内部粗剪验收冻结（等价 f1_粗剪验收冻结.sh）
# ══════════════════════════════════════════════════════════════════════════

def cmd_f1(args: argparse.Namespace) -> int:
    py = resolve_python()
    require(py, "torch", "numpy", "silero_vad")

    work = Path(args.work).resolve()
    if not work.is_dir():
        raise SystemExit(f"⛔ 工作目录不存在: {work}")
    if not (work / "keep.json").is_file():
        raise SystemExit(f"⛔ 缺少冻结前必需输入: {work / 'keep.json'}")

    freeze = [py, SCRIPTS / "decision_contract.py", "freeze", work]
    if args.force:
        freeze.append("--force")

    steps = [
        ("表达审查机制性生成与自检", [py, SCRIPTS / "p1c_生成表达审查.py", work, "--check"]),
        ("帧级声学验收", [py, SCRIPTS / "p1b_帧级验收.py", work]),
        ("决策质量闸门", [py, SCRIPTS / "p1q_决策质量闸门.py", work]),
        ("冻结决策锁", freeze),
        ("复验决策锁", [py, SCRIPTS / "decision_contract.py", "verify", work]),
    ]
    for label, command in steps:
        print(f"=== [{work.name}] {label} ===")
        rc = run(command)
        if rc != 0:
            raise StepFailed(label, rc)

    print("✅ F1 内部粗剪验收与冻结完成；可直接进入字幕和包装")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# 5. _run：带 manifest 记账的单步执行（等价 zsh 的 _run 函数）
# ══════════════════════════════════════════════════════════════════════════

def step(
    py: Path,
    manifest_path: Path,
    name: str,
    step_name: str,
    inputs: list[Path],
    outputs: list[Path],
    command: list,
    *,
    env_extra: dict[str, str] | None = None,
    note: str = "",
) -> None:
    """begin（对输入取哈希）→ 执行 → end（对输出取哈希）/ fail。"""
    manifest(py, "begin", manifest_path, step_name, *[str(p) for p in inputs])
    rc = run(command, env=child_env(**(env_extra or {})))
    if rc == 0:
        manifest(py, "end", manifest_path, step_name, *[str(p) for p in outputs])
        return
    manifest(py, "fail", manifest_path, step_name, rc, "--note", note or f"步骤命令退出 {rc}")
    raise StepFailed(step_name, rc)


# ══════════════════════════════════════════════════════════════════════════
# 6. finalize · 剪映分支（等价 finalize_jianying.sh）
# ══════════════════════════════════════════════════════════════════════════

def _finalize_jianying(work: Path) -> int:
    py = resolve_python()
    require(py, "torch", "numpy", "silero_vad")
    capcut_home, capcut_py = resolve_capcut_mate()

    manifest_path = work / "run_manifest_jianying.json"
    media_delivery = work / "media_delivery.json"
    structural_report = work / "jianying_structural_verification.json"
    audio = work / "_vad16k.wav"

    lock = work / "decision_lock.json"
    mode = "ai"
    if (work / "human_rough_lock.json").is_file():
        mode = "human"
        lock = work / "human_rough_lock.json"
        audio = find_vad_wav(work / "sources") or (work / "sources" / "_vad16k.wav")
        required = [
            work / "config.json", work / "word_track.json", work / "rough_segments.json",
            work / "cards.json", work / "captions_plan.json", work / "human_baseline.json",
            lock, audio,
        ]
    else:
        required = [
            work / "config.json", work / "word_track.json", work / "content_plan.json",
            work / "keep.json", work / "expression_review.json", work / "structure.json",
            work / "segments.json", work / "rough_segments.json", work / "cut_review.json",
            work / "cards.json", lock, audio,
        ]
    require_files([Path(p) for p in required])

    # 返工轮若已声明问题所属工序，交付前必须证明没有串改上游。
    if (work / "stage_scope_guard.json").is_file():
        rc = run([py, SCRIPTS / "stage_scope_guard.py", "verify", work])
        if rc != 0:
            raise StepFailed("stage_scope_guard.verify", rc)

    name, source, core, material_type = read_config_header(work, allow_sources_fallback=True)
    check_material_type(material_type, fcp=False)
    speaker_inputs = require_speaker_turns(work, material_type)
    if not Path(source).is_file():
        raise SystemExit(f"⛔ 真实素材不存在: {source}")

    captions = work / "captions_plan.json"
    packaging_inputs: list[Path] = []
    if (work / "jianying_packaging.json").is_file():
        packaging_inputs = [work / "jianying_packaging.json"]

    delivery_date = datetime.date.today().isoformat()
    draft_info = locate_draft_root(py)
    draft_root = Path(draft_info["selected"])
    root_meta_index = draft_info.get("root_meta_index")
    if not draft_root.is_dir():
        raise SystemExit(f"⛔ 找不到剪映草稿目录: {draft_root}")
    print(f"✅ 当前剪映草稿根: {draft_root}")
    if root_meta_index:
        print(f"   草稿箱索引: {root_meta_index}")

    draft_stem, draft_path = next_free_draft_path(draft_root, delivery_date, core)
    source_inputs = [Path(p) for p in read_source_list(work, source)]
    require_files(source_inputs)

    expected = (
        ["p4j_media_prepare", "p4j_draft_generate", "p4j_draft_verify", "p4j_draft_register"]
        if mode == "human" else
        ["decision_lock", "p3", "p1b", "p4j_media_prepare", "p4j_draft_generate",
         "p4j_draft_verify", "p4j_draft_register"]
    )
    manifest(
        py, "init", manifest_path, "--pipeline", "finalize_jianying.sh", "--work", work,
        "--name", name, "--source", source, "--audio", audio, "--lock", lock,
        "--code-dir", SCRIPTS, "--expected", *expected,
    )

    try:
        if mode == "ai":
            print(f"=== [{name}] 验证决策锁 ===")
            step(py, manifest_path, name, "decision_lock",
                 [work / "config.json", lock, work / "word_track.json", work / "keep.json",
                  work / "expression_review.json", *speaker_inputs, work / "segments.json",
                  work / "structure.json", work / "rough_segments.json", work / "cards.json"],
                 [], [py, SCRIPTS / "decision_contract.py", "verify", work])

            print(f"=== [{name}] 投影冻结字幕时间线 ===")
            step(py, manifest_path, name, "p3",
                 [lock, work / "structure.json", work / "rough_segments.json",
                  work / "word_track.json", work / "cards.json"],
                 [captions], [py, SCRIPTS / "p3_分卡.py", work])

            print(f"=== [{name}] 帧级验收 ===")
            step(py, manifest_path, name, "p1b",
                 [lock, work / "rough_segments.json", captions, work / "word_track.json",
                  work / "keep.json", audio],
                 [], [py, SCRIPTS / "p1b_帧级验收.py", work, "--captions"])
        else:
            print(f"=== [{name}] 人工粗剪锁将由剪映序列化器逐哈希验证 ===")

        print(f"=== [{name}] 准备剪映稳定交付素材 ===")
        step(py, manifest_path, name, "p4j_media_prepare",
             [work / "config.json", *source_inputs], [media_delivery],
             [py, SCRIPTS / "p4j_准备交付素材.py", work, "--draft-root", draft_root])

        print(f"=== [{name}] 生成剪映真主轨草稿 ===")
        step(py, manifest_path, name, "p4j_draft_generate",
             [lock, work / "config.json", work / "rough_segments.json", captions,
              media_delivery, *packaging_inputs, *source_inputs],
             [draft_path / "draft_info.json", draft_path / "draft_content.json"],
             [capcut_py, SCRIPTS / "p4j_出剪映草稿.py", work,
              "--draft-root", draft_root, "--project-name", draft_stem],
             env_extra={"CAPCUT_MATE_HOME": str(capcut_home)})

        print(f"=== [{name}] 剪映草稿终检 ===")
        step(py, manifest_path, name, "p4j_draft_verify",
             [draft_path / "draft_info.json", draft_path / "draft_content.json",
              draft_path / "draft_meta_info.json", work / "config.json",
              work / "rough_segments.json", media_delivery],
             [structural_report],
             [py, SCRIPTS / "p4j_验收草稿.py", draft_path, "--work", work, "--report", structural_report])

        print(f"=== [{name}] 登记剪映草稿箱 ===")
        register_cmd: list = [py, SCRIPTS / "p4j_登记草稿箱.py", draft_path]
        if root_meta_index:
            register_cmd += ["--root-meta", root_meta_index]
        step(py, manifest_path, name, "p4j_draft_register",
             [draft_path / "draft_info.json", draft_path / "draft_content.json",
              draft_path / "draft_meta_info.json"],
             [Path(root_meta_index) if root_meta_index else draft_root / "root_meta_info.json"],
             register_cmd)

        manifest(py, "finish", manifest_path,
                 "--xml", draft_path / "draft_content.json",
                 "--delivery", draft_path / "draft_content.json",
                 "--delivery-state", "complete_structural")
    except BaseException as exc:  # noqa: BLE001
        rc = getattr(exc, "rc", 1) if isinstance(exc, StepFailed) else 1
        manifest(py, "abort", manifest_path, rc,
                 "--note", "剪映 finalize 非零退出，未成功交付")
        raise

    print(f"=== [{name}] 剪映草稿结构完成，待实机验收 ===")
    print(f"    ★ 剪映原生草稿: {draft_path}")
    print(f"      清单: {manifest_path}（delivery_state=complete_structural, complete=false）")
    print("      下一步必须打开剪映，确认项目素材可见、时间线无“无访问权限”且播放器可出画面。")
    return 0


def locate_draft_root(py: Path) -> dict:
    """定位剪映草稿根，返回 `--list` 的完整 JSON。

    原版 finalize_jianying.sh 只取 `--save` 打印的那一行路径。Windows 上剪映会把
    「草稿实体」和「root_meta_info.json 索引」放在两个地方（见 p4j_定位剪映草稿根.py
    的模块注释），登记步骤必须知道索引在哪，所以这里改用 `--list` 拿结构化结果：
    `selected` = 草稿实体根（写草稿用），`root_meta_index` = 索引路径（登记用）。
    仍然带 `--save`，因此持久化行为与原来一致。
    """
    code, out, err = run_capture(
        [py, SCRIPTS / "p4j_定位剪映草稿根.py", "--save", "--list"], echo=False
    )
    if code != 0:
        echo_block("", out)
        echo_block("", err)
        raise StepFailed("p4j_定位剪映草稿根", code)
    start = out.find("{")
    if start < 0:
        raise SystemExit("⛔ 定位剪映草稿根没有返回 JSON")
    try:
        payload = json.loads(out[start:])
    except json.JSONDecodeError as exc:
        raise SystemExit(f"⛔ 定位剪映草稿根返回的 JSON 无法解析: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("selected"):
        raise SystemExit("⛔ 定位剪映草稿根没有给出 selected")
    return payload


# ══════════════════════════════════════════════════════════════════════════
# 7. finalize · Final Cut 分支（等价 finalize_fcp.sh）
# ══════════════════════════════════════════════════════════════════════════

def _finalize_fcp(work: Path) -> int:
    py = resolve_python()
    require(py, "torch", "numpy", "silero_vad")

    manifest_path = work / "run_manifest.json"
    lock = work / "decision_lock.json"
    audio = work / "_vad16k.wav"

    require_files([
        work / "config.json", work / "word_track.json", work / "content_plan.json",
        work / "keep.json", work / "expression_review.json", work / "structure.json",
        work / "segments.json", work / "rough_segments.json", work / "cut_review.json",
        work / "cards.json", lock, audio,
    ])

    if (work / "stage_scope_guard.json").is_file():
        rc = run([py, SCRIPTS / "stage_scope_guard.py", "verify", work])
        if rc != 0:
            raise StepFailed("stage_scope_guard.verify", rc)

    name, source, core, material_type = read_config_header(work, allow_sources_fallback=False)
    check_material_type(material_type, fcp=True)
    speaker_inputs = require_speaker_turns(work, material_type)
    if not Path(source).is_file():
        raise SystemExit(f"⛔ config 指向的真实素材不存在: {source}")

    xml_out = work / f"{name}_v7.fcpxml"
    captions = work / "captions_plan.json"
    packaging_inputs: list[Path] = []
    if (work / "final_cut_packaging.json").is_file():
        packaging_inputs = [work / "final_cut_packaging.json"]

    deliver_dir = Path(source).parent
    delivery_date = datetime.date.today().isoformat()
    delivery_stem, delivery_path = next_free_delivery_path(deliver_dir, delivery_date, core)

    expected = ["decision_lock", "p3", "p1b", "p4f_generate", "p4f_verify", "delivery"]
    manifest(
        py, "init", manifest_path, "--work", work, "--name", name, "--source", source,
        "--audio", audio, "--lock", lock, "--code-dir", SCRIPTS, "--expected", *expected,
    )

    try:
        print(f"=== [{name}] 验证决策锁 ===")
        step(py, manifest_path, name, "decision_lock",
             [work / "config.json", lock, work / "word_track.json", work / "keep.json",
              work / "expression_review.json", *speaker_inputs, work / "segments.json",
              work / "structure.json", work / "rough_segments.json", work / "cards.json"],
             [], [py, SCRIPTS / "decision_contract.py", "verify", work])

        print(f"=== [{name}] p3 将已冻结分卡投影为字幕时间线 ===")
        step(py, manifest_path, name, "p3",
             [lock, work / "structure.json", work / "rough_segments.json",
              work / "word_track.json", work / "cards.json"],
             [captions], [py, SCRIPTS / "p3_分卡.py", work])

        print(f"=== [{name}] p1b 帧级验收 ===")
        step(py, manifest_path, name, "p1b",
             [lock, work / "rough_segments.json", captions, work / "word_track.json",
              work / "keep.json", audio],
             [], [py, SCRIPTS / "p1b_帧级验收.py", work, "--captions"])

        print(f"=== [{name}] p4f 生成 FCPXML ===")
        step(py, manifest_path, name, "p4f_generate",
             [lock, work / "config.json", work / "rough_segments.json", captions,
              *packaging_inputs, Path(source)],
             [xml_out],
             [py, SCRIPTS / "p4f_出FCPXML.py", work, xml_out, "--project-name", delivery_stem])

        print(f"=== [{name}] p4f 终检 ===")
        step(py, manifest_path, name, "p4f_verify", [xml_out, Path(source)], [],
             [py, SCRIPTS / "p4f_验收.py", xml_out, "--work", work])

        print(f"=== [{name}] 交付副本 ===")
        step(py, manifest_path, name, "delivery", [xml_out], [delivery_path], None)

        manifest(py, "finish", manifest_path, "--xml", xml_out, "--delivery", delivery_path)
    except BaseException as exc:  # noqa: BLE001
        rc = getattr(exc, "rc", 1) if isinstance(exc, StepFailed) else 1
        if delivery_path.is_file():
            delivery_path.unlink()
            print(f"⚠ 本轮失败，已撤回本轮新建的交付副本: {delivery_path}", file=sys.stderr)
        manifest(py, "abort", manifest_path, rc, "--note", "finalize 非零退出，未成功交付")
        raise

    # 字幕校对面板属于交付后的可选复核层；缺少面板时不影响已经通过验收的 FCPXML。
    panel = Path.home() / "Developer" / "xugongzi-caption-panel" / "Scripts" / "send-to-panel.sh"
    if panel.is_file() and os.access(panel, os.X_OK):
        run([panel, delivery_path])
    else:
        print("    提示: 未安装字幕校对面板，跳过面板交接")

    print(f"=== [{name}] 完成 ===")
    print(f"    ★ 交付: {delivery_path}")
    print(f"      中间产物: {xml_out}")
    print(f"      清单: {manifest_path}（complete=true）")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# 8. finalize 分发（等价 finalize.sh）
# ══════════════════════════════════════════════════════════════════════════

def cmd_finalize(args: argparse.Namespace) -> int:
    work = Path(args.work).resolve()
    if not work.is_dir():
        raise SystemExit(f"⛔ 工作目录不存在: {work}")
    target = args.target
    if target in ("jianying", "剪映"):
        return _finalize_jianying(work)
    if target in ("final-cut", "fcp"):
        return _finalize_fcp(work)
    if target in ("both", "两份"):
        rc = _finalize_fcp(work)
        if rc != 0:
            return rc
        return _finalize_jianying(work)
    raise SystemExit("⛔ target 只能是 final-cut、jianying 或 both")


# ══════════════════════════════════════════════════════════════════════════
# 9. p0 · 建工作区 + 转写（新增入口：原版靠 SKILL.md 分步手工执行）
# ══════════════════════════════════════════════════════════════════════════

def cmd_p0(args: argparse.Namespace) -> int:
    py = resolve_python()
    source = Path(args.source).resolve()
    if not source.is_file():
        raise SystemExit(f"⛔ 素材不存在: {source}")

    command = [py, SCRIPTS / "p0_建工作区.py", source]
    if args.name:
        command.append(args.name)
    command += ["--material-type", args.material_type]
    if args.font:
        command += ["--font", args.font]
    if args.core_expression:
        command += ["--core-expression", args.core_expression]
    if args.color_reference_xml:
        command += ["--color-reference-xml", Path(args.color_reference_xml).resolve()]

    rc, out, err = run_capture(command)
    if rc != 0:
        echo_block("", err)
        raise StepFailed("p0_建工作区", rc)

    work = None
    for line in out.splitlines():
        marker = "工作区就绪:"
        if marker in line:
            work = Path(line.split(marker, 1)[1].strip())
    if work is None or not work.is_dir():
        raise SystemExit("⛔ 没能从 p0 输出里解析出工作目录（预期含「工作区就绪: <路径>」）")

    if sorted(work.glob("*_words.json")):
        print(f"✅ 转写已存在，跳过 ASR: {sorted(work.glob('*_words.json'))[0].name}")
        return 0
    if args.no_transcribe:
        print("⚠ --no-transcribe：已建工作区但未转写。请按 p0 打印的命令执行 ASR。")
        return 0

    require(py, "faster_whisper", "ctranslate2")
    transcriber = SCRIPTS_WIN / "转写_faster_whisper.py"
    print("=== 转写（faster-whisper，参数对齐 SKILL.md:113-117）===")
    command = [py, transcriber, source, "--outdir", work,
               "--model", args.model, "--language", "zh"]
    if args.hotwords:
        command += ["--hotwords", args.hotwords]
    rc = run(command)
    if rc != 0:
        raise StepFailed("转写_faster_whisper", rc)
    produced = sorted(work.glob("*_words.json"))
    if not produced:
        raise SystemExit(f"⛔ 转写完成但没有产出 *_words.json: {work}")
    print(f"✅ 转写完成: {produced[0].name}")
    print(f"   下一阶段：p1a 建词轴 → content_plan → keep → p1a2 → p1c → p1 → structure → p2 → p2b → F1")
    return 0


# ══════════════════════════════════════════════════════════════════════════
# 10. doctor · 环境自检（新增运维入口）
# ══════════════════════════════════════════════════════════════════════════

def cmd_doctor(_args: argparse.Namespace) -> int:
    ok = True
    print("=== 口播出稿 · Windows 环境自检 ===")
    print(f"SKILL_ROOT : {SKILL_ROOT}")
    try:
        py = resolve_python()
        print(f"管线解释器 : {py}")
    except SystemExit as exc:
        print(f"管线解释器 : ✗ {exc}")
        return 1

    available = probe_modules(py)
    for module, present in available.items():
        print(f"   {'✓' if present else '✗'} {module}")
        ok = ok and present

    print("\n--- 外部可执行文件 ---")
    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        print(f"   {'✓' if found else '✗'} {tool}: {found or '不在 PATH'}")
        ok = ok and bool(found)

    print("\n--- CapCutMate（仅 finalize jianying 需要）---")
    try:
        home, capcut_py = resolve_capcut_mate()
        print(f"   ✓ {capcut_py}（CAPCUT_MATE_HOME={home}）")
    except SystemExit as exc:
        print(f"   ✗ {exc}")

    print("\n--- 剪映草稿根 ---")
    try:
        info = locate_draft_root(py)
        print(f"   ✓ 草稿实体（写草稿用）: {info.get('selected')}")
        print(f"     索引（登记用）      : {info.get('root_meta_index')}")
        print(f"     剪映设置声明        : {info.get('declared_custom_root') or '（未设置，用默认位置）'}")
    except SystemExit as exc:
        print(f"   ✗ {exc}")

    print("\n--- Mac 专属调用残留扫描（应为 0）---")
    hits, retained = scan_mac_residue()
    if hits:
        print("   ✗ Windows 专属脚本里出现 Mac 命令（真缺陷）:")
        print("\n".join(hits))
        ok = False
    else:
        print("   ✓ 脚本_win/ 的可执行字符串里没有 Mac 命令")
    if retained:
        print(f"   · 共享脚本里原样保留的 macOS 路径 {len(retained)} 处（Windows 下走另一分支）:")
        print("\n".join(retained[:12]))
        if len(retained) > 12:
            print(f"     …（其余 {len(retained) - 12} 处略）")

    print("\n--- Windows 运行时断言：不 spawn Mac 命令 ---")
    verdict = check_no_mac_calls()
    print("   " + verdict)
    if verdict.startswith("✗"):
        ok = False

    print("\n结论:", "✅ 就绪" if ok else "⚠ 有缺件（见上）")
    return 0 if ok else 1


MAC_ONLY_TOKENS = ("mlx_whisper", "mlx_qwen3_asr", "pgrep", "lsof", "mdfind", "/bin/zsh")

# 判定规则（按目录，不按文件名，避免维护一份会过期的白名单）：
#   · 脚本_win/*.py  = Windows 专属实现 → 出现 Mac 命令即为**真缺陷**；
#   · 脚本/*.py      = 两平台共享实现 → macOS 原路径按硬约束必须保留 → **审计项**，不算失败。
SHARED_SCRIPT_PATHS = (
    "p0_建工作区.py",
    "p1a2_对齐词轴.py",
    "p4j_定位剪映草稿根.py",
    "p4j_登记草稿箱.py",
)


def _executable_strings(path: Path):
    """产出会被执行的字符串常量（排除 docstring 与 argparse 的说明文字）。"""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    prose: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                prose.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in {
            "help", "description", "epilog", "prog", "metavar", "usage"
        }:
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                prose.add(id(node.value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in prose):
            yield node.lineno, node.value
    # import 语句也纳入：`import mlx_whisper` 不会有字符串常量，但同样是 Mac 依赖。
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module


def scan_mac_residue() -> tuple[list[str], list[str]]:
    """扫已改脚本里**会被执行**的字符串常量是否引用 Mac 专属工具。

    返回 (真命中, 审计项)。真命中 = Windows 专属脚本里出现 Mac 命令；
    审计项 = 共享脚本里为 macOS 保留的原路径。
    """
    hits: list[str] = []
    retained: list[str] = []
    windows_only = sorted(SCRIPTS_WIN.glob("*.py"))
    shared = [SCRIPTS / name for name in SHARED_SCRIPT_PATHS]
    for path, is_windows_only in (
        [(p, True) for p in windows_only] + [(p, False) for p in shared]
    ):
        if path.name == Path(__file__).name:      # 本文件是等价物对照说明，豁免
            continue
        if not path.is_file():
            continue
        try:
            strings = list(_executable_strings(path))
        except SyntaxError as exc:
            hits.append(f"   {path.name} 无法解析: {exc}")
            continue
        for lineno, value in strings:
            for token in MAC_ONLY_TOKENS:
                if token not in value:
                    continue
                line = f"   {path.name}:{lineno} {token!r} → {value.strip()[:80]!r}"
                (hits if is_windows_only else retained).append(line)
    return hits, retained



def check_no_mac_calls() -> str:
    """运行时证明：Windows 路径不会 spawn pgrep / lsof / mdfind。

    做法：把 `subprocess.run` 换成记录器，真跑一遍定位器的候选收集
    （把 `bounded_scan` 换成空实现以避开真实目录遍历），断言没有出现 Mac 命令。
    这比静态扫描强——它证明的是「实际执行路径」而非「源码里出现过」。
    """
    import importlib.util
    import tempfile
    import unittest.mock as mock

    spec = importlib.util.spec_from_file_location(
        "_draft_root_probe", SCRIPTS / "p4j_定位剪映草稿根.py"
    )
    if spec is None or spec.loader is None:
        return "· 无法加载定位器，跳过"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module.is_windows():
        return "· 非 Windows，跳过（macOS 侧本就该调用它们）"

    seen: list[list[str]] = []
    real_run = subprocess.run

    def spy(cmd, *rest, **kwargs):  # noqa: ANN002, ANN003
        seen.append([str(c) for c in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)])
        return real_run(cmd, *rest, **kwargs)

    with tempfile.TemporaryDirectory() as temp:
        with mock.patch.object(subprocess, "run", spy), \
                mock.patch.object(module, "bounded_scan", lambda *a, **k: set()):
            try:
                module.collect_candidates(
                    config_path=Path(temp) / "jianying_draft_root", system_search=True
                )
            except SystemExit:
                pass

    banned = {"pgrep", "lsof", "mdfind"}
    offenders = [cmd for cmd in seen if cmd and Path(cmd[0]).name.lower() in banned]
    if offenders:
        return "✗ 调用了 Mac 专属命令: " + "; ".join(" ".join(c) for c in offenders)
    return f"✓ 未触发（候选收集共 spawn {len(seen)} 个子进程，均非 pgrep/lsof/mdfind）"


# ══════════════════════════════════════════════════════════════════════════
# 11. 入口
# ══════════════════════════════════════════════════════════════════════════

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipeline.py",
        description="口播出稿 · Windows 编排层（f1 / finalize / p0 / doctor）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  pipeline.py p0 \"D:\\素材\\口播.mp4\" --core-expression 试管备孕科普\n"
            "  pipeline.py f1 <工作目录>\n"
            "  pipeline.py finalize <工作目录> jianying\n"
        ),
    )
    p.add_argument("-v", "--verbose", action="store_true", help="回显每一步的命令行")
    subs = p.add_subparsers(dest="command", required=True)

    f1 = subs.add_parser("f1", help="内部粗剪验收冻结（等价 f1_粗剪验收冻结.sh）")
    f1.add_argument("work", help="工作目录")
    f1.add_argument(
        "--force", action="store_true",
        help="决策锁已存在时强制重冻（原 zsh 的第二个位置参数 --force，此处改为真正的开关）",
    )
    f1.set_defaults(func=cmd_f1)

    fin = subs.add_parser("finalize", help="冻结后交付（等价 finalize.sh）")
    fin.add_argument("work", help="工作目录")
    fin.add_argument("target", nargs="?", default="jianying",
                     help="jianying（默认）/ final-cut / both")
    fin.set_defaults(func=cmd_finalize)

    p0 = subs.add_parser("p0", help="建工作区 + 转写（新增入口）")
    p0.add_argument("source", help="视频路径")
    p0.add_argument("name", nargs="?", default=None, help="内部片名（可保留技术编号）")
    p0.add_argument("--material-type", default="single_speaker",
                    choices=list(_ALLOWED_MATERIAL))
    p0.add_argument("--core-expression", default=None, help="中文核心表达")
    p0.add_argument("--font", default=None,
                    help="剪映字幕字体名（默认新青年体）；写入 config.json，"
                         "生成与验收都读它")
    p0.add_argument("--color-reference-xml", default=None)
    p0.add_argument("--model", default="large-v3-turbo")
    p0.add_argument("--hotwords", default=None,
                    help="ASR 热词（空格或逗号分隔），透传给转写器；"
                         "IP 专名/科室术语等高频同音错字建议填这里")
    p0.add_argument("--no-transcribe", action="store_true", help="只建工作区，不跑 ASR")
    p0.set_defaults(func=cmd_p0)

    doc = subs.add_parser("doctor", help="环境自检 + Mac 专属调用残留扫描")
    doc.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    args = parser().parse_args(argv)
    VERBOSE = bool(args.verbose)
    try:
        return int(args.func(args))
    except StepFailed as exc:
        print(f"⛔ [{exc.step}] 失败（退出码 {exc.rc}），停止且不交付", file=sys.stderr)
        return exc.rc


if __name__ == "__main__":
    raise SystemExit(main())
