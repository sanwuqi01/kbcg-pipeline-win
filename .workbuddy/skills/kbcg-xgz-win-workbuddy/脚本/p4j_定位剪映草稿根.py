#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定位剪映当前实际使用的草稿根目录，避免把草稿写进已废弃的默认路径。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Iterable


DRAFT_DIR_NAME = "com.lveditor.draft"
ROOT_META_NAME = "root_meta_info.json"
RECENT_SECONDS = 30 * 60

# ── Windows：剪映把「索引」与「草稿实体」拆成两处（2026-09-14 实测，剪映 11.3.0.14362）
#   · 索引  %LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\root_meta_info.json
#     声明全部草稿；all_draft_store 每条带 draft_root_path / draft_fold_path / draft_json_file。
#   · 实体  D:\JianyingPro Drafts\<工程名>\  —— 用户在「设置 → 草稿位置」里改到了 D 盘，
#     该目录下**没有** root_meta_info.json，目录名也不是 com.lveditor.draft。
#   · 自定义位置写在 Config\globalSetting（INI，[General] 段）的 currentCustomDraftPath。
# 结论：Windows 上「草稿根」必须从设置/索引推导，只看目录名会选中一个空壳默认目录，
# 从而把草稿写进剪映根本不会去读的位置——这正是本脚本要防的那个 bug。
JIANYING_USER_DATA = (
    Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
    / "JianyingPro" / "User Data"
)
JIANYING_DEFAULT_INDEX_ROOT = JIANYING_USER_DATA / "Projects" / DRAFT_DIR_NAME
JIANYING_GLOBAL_SETTING = JIANYING_USER_DATA / "Config" / "globalSetting"
CUSTOM_DRAFT_PATH_KEY = "currentCustomDraftPath"


def is_windows() -> bool:
    return os.name == "nt"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--draft-root", type=Path, help="显式指定草稿根，优先级最高")
    p.add_argument("--config", type=Path, help="覆盖持久化配置文件路径")
    p.add_argument("--search-root", type=Path, action="append", default=[])
    p.add_argument("--no-system-search", action="store_true", help="只用显式与测试候选")
    p.add_argument("--save", action="store_true", help="将本次选中路径原子写入配置")
    p.add_argument("--list", action="store_true", help="用 JSON 打印全部有效候选")
    return p


def default_config_path() -> Path:
    if is_windows():
        # 存的是本机绝对路径（可能指向 D 盘），所以放 LOCALAPPDATA 而不是漫游的 APPDATA。
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData/Local"))
        return base / "kbcg-xgz-video-editor" / "jianying_draft_root"
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "kbcg-xgz-video-editor" / "jianying_draft_root"


def normalize(path: Path) -> Path:
    return path.expanduser().resolve()


# ══════════════════════════════════════════════════════════════════════════
# Windows 专属：读剪映自身设置 / 索引 ↔ 实体分离
# ══════════════════════════════════════════════════════════════════════════

def windows_custom_draft_root() -> Path | None:
    """读剪映自身设置里的自定义草稿位置（Config/globalSetting 的 currentCustomDraftPath）。

    这是 AGENT.md §3 D3 第 3 条点名要读的那份设置。实测该 INI 里路径用
    双反斜杠写成 `D:\\\\JianyingPro Drafts`，`ntpath.normpath`（Path 内部即用）
    会把重复分隔符折叠，无需手工 unescape。
    """
    try:
        text = JIANYING_GLOBAL_SETTING.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key.strip() != CUSTOM_DRAFT_PATH_KEY:
            continue
        value = value.strip().strip('"').strip()
        if not value:
            return None
        try:
            return normalize(Path(value))
        except OSError:
            return None
    return None


def parse_index(path: Path) -> dict | None:
    """读一份 root_meta_info.json；不是有效索引就返回 None（不抛异常）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("all_draft_store"), list):
        return None
    return payload


def windows_index_candidates() -> list[Path]:
    """Windows 上可能存放 root_meta_info.json 的位置，索引目录优先。"""
    paths = [JIANYING_DEFAULT_INDEX_ROOT / ROOT_META_NAME]
    custom = windows_custom_draft_root()
    if custom is not None:
        paths.append(custom / ROOT_META_NAME)
    return paths


def windows_covering_index(root: Path) -> tuple[Path | None, dict | None]:
    """找一份「在 all_draft_store 里声明过 root」的索引，返回 (索引路径, payload)。"""
    for path in windows_index_candidates():
        payload = parse_index(path)
        if payload is None:
            continue
        for row in payload["all_draft_store"]:
            if not isinstance(row, dict):
                continue
            declared = row.get("draft_root_path")
            if not isinstance(declared, str) or not declared:
                continue
            try:
                if normalize(Path(declared)) == root:
                    return path, payload
            except OSError:
                continue
    return None, None


def windows_standard_search_roots() -> list[Path]:
    """Windows 的搜索根。

    与 macOS 扫 /Volumes 不同：草稿实体位置能**直接从剪映设置读到**（currentCustomDraftPath），
    不需要全盘 bounded_scan。实测教训：曾把「自定义草稿位置的父目录」放进搜索根，
    而本机那是 `D:\\`——bounded_scan 会把整块盘走一遍，直接把进程拖死。
    所以只扫剪映自己的 `User Data\\Projects`（层级浅、就是索引所在层），
    非标准安装位置由用户显式 `--search-root` 指定。
    """
    projects = JIANYING_USER_DATA / "Projects"
    return [projects] if projects.is_dir() else []


def blank_result(root: Path) -> dict:
    return {
        "path": str(root),
        "status": "not_initialized",
        "structure_valid": False,
        "current_process_writable": False,
        "detail": "未找到剪映已初始化的 root_meta_info.json",
    }


def _read_own_meta(root: Path, meta_path: Path, result: dict) -> dict | None:
    """读 `<root>/root_meta_info.json` 并校验 root_path。失败时写好 result 并返回 None。"""
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except PermissionError:
        result.update(status="current_process_read_denied", detail="当前进程无权读取草稿根索引")
        return None
    except OSError as exc:
        result.update(status="read_error", detail=f"读取草稿根索引失败: {exc}")
        return None
    except (UnicodeDecodeError, json.JSONDecodeError):
        result.update(status="invalid_meta", detail="root_meta_info.json 不是有效 JSON")
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("all_draft_store"), list):
        result.update(status="invalid_meta", detail="root_meta_info.json 缺少 all_draft_store")
        return None
    declared = payload.get("root_path")
    if isinstance(declared, str) and declared.strip():
        try:
            if normalize(Path(declared)) != root:
                result.update(status="invalid_meta", detail="root_path 与实际草稿根不一致")
                return None
        except OSError:
            result.update(status="invalid_meta", detail="root_path 无法解析")
            return None
    return payload


def _finish(root: Path, index_path: Path, payload: dict, result: dict) -> dict:
    """结构已确认后的收口：可写判定 + payload 挂载（两平台共用）。

    index_path 是「登记时真正要被写入的那份索引」——分离布局下它不在 root 里，
    所以可写判定必须看它，而不是看 `<root>/root_meta_info.json`。
    """
    result["structure_valid"] = True
    writable = os.access(root, os.W_OK | os.X_OK)
    if index_path.is_file():
        writable = writable and os.access(index_path, os.W_OK)
    else:
        parent = index_path.parent
        writable = writable and parent.is_dir() and os.access(parent, os.W_OK)
    result["current_process_writable"] = writable
    if writable:
        result.update(status="ready", detail="草稿根结构有效且当前进程可写")
    elif is_windows():
        result.update(
            status="current_process_write_denied",
            detail=(
                "草稿根已初始化且结构有效，但当前进程不可写；"
                "请给宿主进程该目录的读写权限，不需要重新建空白草稿"
            ),
        )
    else:
        result.update(
            status="current_process_write_denied",
            detail=(
                "草稿根已初始化且结构有效，但当前进程不可写；"
                "需要给 Codex 系统权限，不需要重新建空白草稿"
            ),
        )
    result["payload"] = payload
    return result


def _inspect_root_posix(root: Path, result: dict) -> dict:
    """macOS 原语义（逐字保留）：目录名必须是 com.lveditor.draft，索引必须在根里。"""
    meta_path = root / ROOT_META_NAME
    if root.name != DRAFT_DIR_NAME:
        result.update(status="invalid_root_name", detail=f"目录名必须是 {DRAFT_DIR_NAME}")
        return result
    if not root.is_dir() or not meta_path.is_file():
        return result
    payload = _read_own_meta(root, meta_path, result)
    if payload is None:
        return result
    result["index_path"] = str(meta_path)
    return _finish(root, meta_path, payload, result)


def _inspect_root_windows(root: Path, result: dict) -> dict:
    """Windows 语义：兼容「索引在根里」的默认布局，以及「索引与实体分离」的自定义布局。

    实测本机是后者：草稿实体在 D:\\JianyingPro Drafts（无 root_meta_info.json、目录名
    也不是 com.lveditor.draft），索引仍在 %LOCALAPPDATA% 的默认目录里。
    """
    declared_custom = windows_custom_draft_root()
    result["declared_custom_root"] = str(declared_custom) if declared_custom else None
    name_ok = root.name == DRAFT_DIR_NAME or (
        declared_custom is not None and declared_custom == root
    )
    meta_path = root / ROOT_META_NAME

    if meta_path.is_file():
        if not name_ok:
            result.update(status="invalid_root_name", detail=f"目录名必须是 {DRAFT_DIR_NAME}")
            return result
        payload = _read_own_meta(root, meta_path, result)
        if payload is None:
            return result
        result["index_path"] = str(meta_path)
        return _finish(root, meta_path, payload, result)

    if not root.is_dir():
        return result
    if not name_ok:
        result.update(
            status="invalid_root_name",
            detail=f"目录名不是 {DRAFT_DIR_NAME}，也不在剪映设置声明的草稿位置里",
        )
        return result
    index_path, payload = windows_covering_index(root)
    if payload is None or index_path is None:
        result.update(
            status="not_initialized",
            detail="该目录像是草稿实体，但找不到声明它的 root_meta_info.json 索引",
        )
        return result
    result["index_path"] = str(index_path)
    return _finish(root, index_path, payload, result)


def inspect_root(root: Path) -> dict:
    """只读区分草稿根不存在、未初始化、结构无效和当前进程不可写。"""
    root = normalize(root)
    result = blank_result(root)
    if is_windows():
        return _inspect_root_windows(root, result)
    return _inspect_root_posix(root, result)



def load_root_meta(root: Path, *, require_writable: bool = True) -> dict | None:
    inspection = inspect_root(root)
    if not inspection["structure_valid"]:
        return None
    if require_writable and not inspection["current_process_writable"]:
        return None
    return inspection.get("payload")


def activity_mtime(root: Path) -> float:
    # Windows 分离布局下草稿实体根里没有 root_meta_info.json，不能无条件 stat（原版会崩）。
    meta = root / ROOT_META_NAME
    mtimes = [meta.stat().st_mtime] if meta.is_file() else []
    try:
        children = list(root.iterdir())
    except OSError:
        return max(mtimes) if mtimes else 0.0
    for child in children:
        if not child.is_dir():
            continue
        for name in ("draft_meta_info.json", "draft_info.json", "draft_content.json"):
            path = child / name
            try:
                if path.is_file():
                    mtimes.append(path.stat().st_mtime)
            except OSError:
                continue
    return max(mtimes) if mtimes else 0.0



def read_saved(config_path: Path) -> Path | None:
    try:
        value = config_path.expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return normalize(Path(value)) if value else None


def roots_from_open_files() -> set[Path]:
    roots: set[Path] = set()
    try:
        pids = subprocess.run(
            ["pgrep", "-if", "JianyingPro|Jianying|CapCut|剪映"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return roots
    for pid in pids[:12]:
        try:
            output = subprocess.run(
                ["lsof", "-Fn", "-p", pid],
                check=False,
                capture_output=True,
                text=True,
                timeout=4,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in output.splitlines():
            if not line.startswith("n"):
                continue
            path = line[1:]
            marker = f"/{DRAFT_DIR_NAME}"
            pos = path.find(marker)
            if pos >= 0:
                roots.add(normalize(Path(path[: pos + len(marker)])))
    return roots


def roots_from_spotlight() -> set[Path]:
    try:
        output = subprocess.run(
            ["mdfind", f"kMDItemFSName == '{ROOT_META_NAME}'"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return set()
    roots = set()
    suffix = f"/{DRAFT_DIR_NAME}/{ROOT_META_NAME}"
    for line in output.splitlines():
        if line.endswith(suffix):
            roots.add(normalize(Path(line).parent))
    return roots


def bounded_scan(search_root: Path, *, max_depth: int = 7) -> set[Path]:
    search_root = normalize(search_root)
    if not search_root.is_dir():
        return set()
    # 拒绝扫文件系统根（`D:\` / `/`）：整盘遍历会拖死进程，且不可能是草稿根所在层。
    if search_root.parent == search_root:
        return set()
    found: set[Path] = set()
    skip_names = {
        ".git", ".Trash", "Caches", "Cache", "node_modules", "__pycache__",
        "DerivedData", "System Volume Information",
    }
    base_depth = len(search_root.parts)
    for current, dirs, _files in os.walk(search_root, followlinks=False):
        current_path = Path(current)
        depth = len(current_path.parts) - base_depth
        dirs[:] = [
            name for name in dirs
            if name not in skip_names and not name.endswith(".app")
        ]
        if current_path.name == DRAFT_DIR_NAME:
            found.add(normalize(current_path))
            dirs[:] = []
            continue
        if depth >= max_depth:
            dirs[:] = []
    return found


def standard_search_roots() -> list[Path]:
    """macOS 的搜索根（原样保留）。Windows 走 windows_standard_search_roots()。"""
    home = Path.home()
    return [
        home / "Movies",
        home / "Documents",
        home / "Desktop",
        home / "Library/Application Support",
        home / "Applications",
        Path("/Applications"),
        Path("/Volumes"),
    ]


def collect_candidates(
    *,
    config_path: Path,
    extra_search_roots: Iterable[Path] = (),
    system_search: bool = True,
) -> tuple[set[Path], set[Path], Path | None, dict[Path, dict], Path | None]:
    """收集候选。

    返回 (ready 候选, 正在被占用的, 已存配置, 诊断表, 剪映设置声明的草稿位置)。
    第 5 项是 Windows 新增：`currentCustomDraftPath` 是**剪映自己的声明**，
    权威性高于任何目录名/时间戳推断，所以交给 choose_root 单独排位。
    """
    candidates: set[Path] = set()
    running: set[Path] = set()
    settings_root: Path | None = None
    saved = read_saved(config_path)
    if saved:
        candidates.add(saved)
    if system_search:
        if is_windows():
            settings_root = windows_custom_draft_root()
            if settings_root is not None:
                candidates.add(settings_root)
            candidates.add(normalize(JIANYING_DEFAULT_INDEX_ROOT))
            search_roots = [*windows_standard_search_roots(), *extra_search_roots]
        else:
            default = Path.home() / "Movies/JianyingPro/User Data/Projects/com.lveditor.draft"
            candidates.add(normalize(default))
            # pgrep/lsof/mdfind 是 macOS 专属，Windows 下这两个收集器不调用（代码原样保留）。
            running = roots_from_open_files()
            candidates.update(running)
            candidates.update(roots_from_spotlight())
            search_roots = [*standard_search_roots(), *extra_search_roots]
    else:
        search_roots = list(extra_search_roots)

    for root in search_roots:
        candidates.update(bounded_scan(root))
    inspections = {root: inspect_root(root) for root in candidates}
    ready = {
        root for root, result in inspections.items()
        if result["status"] == "ready"
    }
    structurally_valid = {
        root for root, result in inspections.items()
        if result["structure_valid"]
    }
    return ready, {root for root in running if root in ready}, saved, {
        root: result for root, result in inspections.items()
        if result["status"] != "not_initialized" or root in structurally_valid
    }, settings_root


HOST_LABEL = "宿主进程" if is_windows() else "Codex"


def choose_root(
    candidates: set[Path],
    *,
    explicit: Path | None = None,
    running: set[Path] | None = None,
    saved: Path | None = None,
    settings_root: Path | None = None,
    inspections: dict[Path, dict] | None = None,
    now: float | None = None,
) -> Path:
    running = running or set()
    now = time.time() if now is None else now
    inspections = inspections or {}
    if explicit is not None:
        explicit = normalize(explicit)
        result = inspect_root(explicit)
        if result["status"] != "ready":
            raise SystemExit(
                f"⛔ 显式剪映草稿根不可用 [{result['status']}]: {explicit}\n"
                f"   {result['detail']}"
            )
        return explicit
    if len(running) == 1:
        return next(iter(running))
    if len(running) > 1:
        return max(running, key=activity_mtime)
    # Windows 新增：剪映设置里声明的草稿位置（currentCustomDraftPath）。
    # 这是剪映**自己**的声明，比目录名/时间戳推断可靠，因此排在推断之前。
    # 默认 None，macOS 行为不受影响。
    if settings_root is not None and settings_root in candidates:
        return settings_root
    if not candidates:
        denied = [
            result for result in inspections.values()
            if result.get("status") in {
                "current_process_write_denied", "current_process_read_denied"
            }
        ]
        if denied:
            details = "\n".join(
                f"  - {row['path']} [{row['status']}]" for row in denied
            )
            raise SystemExit(
                f"⛔ 已找到剪映草稿根，但当前 {HOST_LABEL} 没有读写权限：\n"
                f"{details}\n"
                "   请授予目录权限后重试；不要重复创建空白草稿。"
            )
        raise SystemExit(
            "⛔ 找不到剪映已初始化的草稿目录。请先启动剪映，"
            "新建并保存一个空白草稿，再重试。"
        )

    ranked = sorted(candidates, key=activity_mtime, reverse=True)
    freshest = ranked[0]
    if now - activity_mtime(freshest) <= RECENT_SECONDS:
        return freshest
    if saved is not None and saved in candidates:
        return saved
    if len(ranked) == 1:
        return freshest
    details = "\n".join(f"  - {path}" for path in ranked)
    raise SystemExit(
        "⛔ 发现多个剪映草稿目录，无法安全猜测当前使用的一个：\n"
        f"{details}\n"
        "请打开当前剪映，新建并保存一个空白草稿后重试，"
        "或设置 JIANYING_DRAFT_ROOT。"
    )


def save_config(config_path: Path, root: Path) -> None:
    config_path = config_path.expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".jianying_draft_root.", suffix=".tmp", dir=config_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{root}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, config_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main() -> int:
    args = parser().parse_args()
    config_path = args.config or default_config_path()
    env_root = os.environ.get("JIANYING_DRAFT_ROOT")
    explicit = args.draft_root or (Path(env_root) if env_root else None)
    candidates, running, saved, inspections, settings_root = collect_candidates(
        config_path=config_path,
        extra_search_roots=args.search_root,
        system_search=not args.no_system_search,
    )
    root = choose_root(
        candidates,
        explicit=explicit,
        running=running,
        saved=saved,
        settings_root=settings_root,
        inspections=inspections,
    )
    if args.save:
        save_config(config_path, root)
    if args.list:
        print(json.dumps({
            "selected": str(root),
            "root_meta_index": inspections.get(root, {}).get("index_path"),
            "declared_custom_root": (
                str(settings_root) if settings_root else None
            ),
            "saved": str(saved) if saved else None,
            "running": sorted(map(str, running)),
            "candidates": [
                {
                    "path": str(path),
                    "activity_mtime": activity_mtime(path),
                    "status": "ready",
                }
                for path in sorted(candidates, key=str)
            ],
            "diagnostics": [
                {key: value for key, value in result.items() if key != "payload"}
                for _, result in sorted(inspections.items(), key=lambda item: str(item[0]))
            ],
        }, ensure_ascii=False, indent=2))
    else:
        print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
