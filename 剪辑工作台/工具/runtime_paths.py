"""剪辑工作台的业务 I/O 路径；源码、Skill、说明书仍由入口原位定位。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


KBCG_PATHS_API_VERSION = 2

_DIRECTORY_ROOTS = (
    ("KBCG_INPUT_ROOT", "输入", "inbox"),
    ("KBCG_WORK_ROOT", "工作区", "workroot"),
    ("KBCG_OUTPUT_ROOT", "输出", "outbox"),
    ("KBCG_LOG_ROOT", "运行记录", "logroot"),
)


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    inbox: Path
    workroot: Path
    outbox: Path
    logroot: Path
    settings: Path
    external_settings: bool


def _external_path(name: str, source_root: Path) -> Path | None:
    if name not in os.environ:
        return None
    raw = os.environ[name]
    path = Path(raw)
    if not raw or not path.is_absolute():
        raise SystemExit(f"⛔ {name} 必须是已有的绝对外部路径: {raw!r}")
    path = path.resolve()
    source_root = source_root.resolve()
    if path.is_relative_to(source_root) or source_root.is_relative_to(path):
        raise SystemExit(f"⛔ {name} 不能位于源码目录内，也不能包含源码目录: {path}")
    return path


def resolve_runtime_paths(workbench: Path) -> RuntimePaths:
    """解析独立业务目录；未指定环境变量时沿用工作台旧布局。"""
    workbench = workbench.resolve()
    source_root = workbench.parent
    root = _external_path("KBCG_RUNTIME_ROOT", source_root)
    explicit_root = root is not None
    overridden = [name for name, _, _ in _DIRECTORY_ROOTS if name in os.environ]
    if not explicit_root and overridden and len(overridden) != len(_DIRECTORY_ROOTS):
        missing = [name for name, _, _ in _DIRECTORY_ROOTS if name not in os.environ]
        raise SystemExit("⛔ 未设置 KBCG_RUNTIME_ROOT 时，四类独立目录必须全部提供；缺少: "
                         + "、".join(missing))
    if root is None:
        root = workbench
    elif not root.is_dir():
        raise SystemExit(f"⛔ KBCG_RUNTIME_ROOT 目录不存在: {root}")

    directories = {}
    for name, folder, field in _DIRECTORY_ROOTS:
        override = _external_path(name, source_root)
        path = override if override is not None else root / folder
        if explicit_root or override is not None:
            path = path.resolve()  # 同时检查 Windows junction 与目录符号链接
            if not path.is_dir():
                raise SystemExit(f"⛔ {name} 目录不存在: {path}")
            if path.is_relative_to(source_root) or source_root.is_relative_to(path):
                raise SystemExit(f"⛔ {name} 不能位于源码目录内，也不能包含源码目录: {path}")
        directories[field] = path

    if explicit_root or overridden:
        for i, (name, _, field) in enumerate(_DIRECTORY_ROOTS):
            for other_name, _, other_field in _DIRECTORY_ROOTS[i + 1:]:
                path, other = directories[field], directories[other_field]
                if path.is_relative_to(other) or other.is_relative_to(path):
                    raise SystemExit(f"⛔ {name} 与 {other_name} 不能相同或互相包含: "
                                     f"{path}、{other}")

    settings = _external_path("KBCG_SETTINGS_FILE", source_root)
    external_settings = settings is not None
    if settings is None:
        settings = workbench / "配置" / "设置.json"
    elif not settings.is_file():
        raise SystemExit(f"⛔ KBCG_SETTINGS_FILE 文件不存在: {settings}")

    paths = RuntimePaths(root, directories["inbox"], directories["workroot"],
                         directories["outbox"], directories["logroot"], settings,
                         external_settings)
    if external_settings:
        read_settings(paths)
    return paths


def read_settings(paths: RuntimePaths) -> dict:
    """外部显式配置失败时停止；本地旧配置维持原有空配置兼容行为。"""
    try:
        data = json.loads(paths.settings.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("JSON 顶层必须是对象")
        return data
    except (OSError, ValueError) as exc:
        if paths.external_settings:
            raise SystemExit(f"⛔ KBCG_SETTINGS_FILE 无效: {paths.settings} ({exc})") from exc
        return {}
