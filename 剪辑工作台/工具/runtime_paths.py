"""剪辑工作台的业务 I/O 路径；源码、Skill、说明书仍由入口原位定位。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


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
    """显式外部路径严格校验；未指定时沿用工作台自身的旧目录。"""
    workbench = workbench.resolve()
    source_root = workbench.parent
    root = _external_path("KBCG_RUNTIME_ROOT", source_root)
    if root is None:
        root = workbench
    elif not root.is_dir():
        raise SystemExit(f"⛔ KBCG_RUNTIME_ROOT 目录不存在: {root}")

    inbox = root / "输入"
    workroot = root / "工作区"
    outbox = root / "输出"
    logroot = root / "运行记录"
    if "KBCG_RUNTIME_ROOT" in os.environ:
        missing = [str(p) for p in (inbox, workroot, outbox, logroot) if not p.is_dir()]
        if missing:
            raise SystemExit("⛔ 外部运行目录缺少子目录: " + "、".join(missing))
        if any(p.resolve().is_relative_to(source_root) or
               source_root.is_relative_to(p.resolve()) for p in
               (inbox, workroot, outbox, logroot)):
            raise SystemExit("⛔ 外部运行目录的子目录不能指向源码目录或其上级")

    settings = _external_path("KBCG_SETTINGS_FILE", source_root)
    external_settings = settings is not None
    if settings is None:
        settings = workbench / "配置" / "设置.json"
    elif not settings.is_file():
        raise SystemExit(f"⛔ KBCG_SETTINGS_FILE 文件不存在: {settings}")

    paths = RuntimePaths(root, inbox, workroot, outbox, logroot, settings,
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
