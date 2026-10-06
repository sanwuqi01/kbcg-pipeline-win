#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为剪映交付准备稳定素材路径，避免 Downloads 等受保护目录导致无访问权限。"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


SCHEMA = "kbcg-xgz/jianying_media_delivery@1"
MODE_ENV = "XU_GONGZI_MEDIA_MODE"
VALID_MODES = {"auto", "managed_copy", "reference_original"}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("--draft-root", type=Path, required=True)
    p.add_argument("--mode", choices=sorted(VALID_MODES))
    return p


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def source_rows(config: dict[str, Any]) -> list[dict[str, str]]:
    declared = config.get("sources")
    if isinstance(declared, list) and declared:
        rows = []
        for pos, row in enumerate(declared):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise SystemExit(f"⛔ config.sources[{pos}] 缺少 id")
            path = Path(str(row.get("path") or "")).expanduser().resolve()
            rows.append({"id": row["id"], "path": str(path)})
        return rows
    path = Path(config.get("original_path") or config.get("proxy") or "").expanduser().resolve()
    return [{"id": "source", "path": str(path)}]


def protected_reason(path: Path) -> str | None:
    path = path.expanduser().resolve()
    home = Path.home().resolve()
    protected = {
        home / "Downloads": "downloads",
        home / "Desktop": "desktop",
        home / "Documents": "documents",
        home / "Library/Mobile Documents": "icloud_drive",
        home / "Library/Containers": "app_container",
        home / "Library/Group Containers": "app_group_container",
    }
    for root, reason in protected.items():
        try:
            path.relative_to(root)
            return reason
        except ValueError:
            pass
    if path.parts[:2] == ("/", "Volumes") or str(path).startswith("/Volumes/"):
        return "external_or_network_volume"
    if str(path).startswith("/private/var/folders/") or str(path).startswith("/var/folders/"):
        return "temporary_attachment"
    return None


def managed_root(draft_root: Path) -> Path:
    """放在剪映 Projects 数据位置内，但不混入 com.lveditor.draft 草稿列表。"""
    draft_root = draft_root.expanduser().resolve()
    return draft_root.parent / "kbcg-xgz-managed-media"


def clone_or_copy(source: Path, target: Path) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".partial", dir=target.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        temp.unlink()
        method = "copy"
        if Path("/bin/cp").is_file():
            result = subprocess.run(
                ["/bin/cp", "-c", str(source), str(temp)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                method = "apfs_clone"
            else:
                shutil.copy2(source, temp)
        else:
            shutil.copy2(source, temp)
        os.replace(temp, target)
        return method
    finally:
        if temp.exists():
            temp.unlink()


def prepare_source(row: dict[str, str], draft_root: Path, requested_mode: str) -> dict[str, Any]:
    source = Path(row["path"]).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"⛔ 原素材不存在: {source}")
    source_hash = sha256(source)
    stat = source.stat()
    risk = protected_reason(source)
    effective_mode = requested_mode
    if requested_mode == "auto":
        effective_mode = "managed_copy" if risk else "reference_original"

    copied = False
    reused = False
    copy_method = None
    if effective_mode == "managed_copy":
        target = managed_root(draft_root) / source_hash[:16] / source.name
        if target.is_file():
            if sha256(target) != source_hash:
                raise SystemExit(f"⛔ 受管素材路径已存在但哈希不一致: {target}")
            reused = True
        else:
            try:
                copy_method = clone_or_copy(source, target)
            except PermissionError as exc:
                raise SystemExit(
                    f"⛔ 无权写入剪映受管素材目录: {target.parent}\n"
                    "   请授予 Codex 文件权限后重试。"
                ) from exc
            if sha256(target) != source_hash:
                raise SystemExit(f"⛔ 受管素材复制后 SHA-256 校验失败: {target}")
            copied = True
        delivery = target.resolve()
    else:
        delivery = source

    return {
        "id": row["id"],
        "original_path": str(source),
        "delivery_path": str(delivery),
        "mode": effective_mode,
        "risk": risk,
        "sha256": source_hash,
        "bytes": stat.st_size,
        "copied": copied,
        "reused": reused,
        "copy_method": copy_method,
        "runtime_access_risk": bool(risk and effective_mode == "reference_original"),
    }


def main() -> int:
    args = parser().parse_args()
    work = args.work.expanduser().resolve()
    draft_root = args.draft_root.expanduser().resolve()
    requested_mode = args.mode or os.environ.get(MODE_ENV, "auto")
    if requested_mode not in VALID_MODES:
        raise SystemExit(f"⛔ {MODE_ENV} 必须是: {', '.join(sorted(VALID_MODES))}")
    config = json.loads((work / "config.json").read_text(encoding="utf-8"))
    sources = [prepare_source(row, draft_root, requested_mode) for row in source_rows(config)]
    payload = {
        "schema": SCHEMA,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "requested_mode": requested_mode,
        "managed_root": str(managed_root(draft_root)),
        "sources": sources,
        "runtime_access_risk": any(row["runtime_access_risk"] for row in sources),
    }
    output = work / "media_delivery.json"
    atomic_json(output, payload)
    managed = sum(row["mode"] == "managed_copy" for row in sources)
    print(f"✅ 剪映交付素材已准备: {len(sources)} 份，受管副本 {managed} 份")
    if payload["runtime_access_risk"]:
        print("⚠️ 保留了受保护目录原路径；剪映实机验收前只能标记 relink_required")
    print(f"   记录: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
