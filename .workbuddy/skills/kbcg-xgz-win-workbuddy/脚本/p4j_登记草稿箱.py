#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""将已验收的剪映草稿原子登记到 root_meta_info.json。

Windows 说明（2026-09-14 实测，剪映 11.3.0.14362）：
剪映可以把「草稿实体」放到用户自定义位置（如 `D:\\JianyingPro Drafts`），而
`root_meta_info.json` 索引仍留在默认目录 → 索引**不一定**在 `draft.parent` 里。
用 `--root-meta` 显式指定索引路径（由 `p4j_定位剪映草稿根.py --list` 的
`root_meta_index` 字段给出）；不给则沿用原行为 `draft.parent/root_meta_info.json`。
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def acquire_exclusive(handle) -> None:
        """Windows 没有 flock，用 msvcrt 区域锁；LK_LOCK 自带重试但有上限，故外层再循环。"""
        while True:
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.1)

    def release_exclusive(handle) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def acquire_exclusive(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def release_exclusive(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("draft", type=Path)
    p.add_argument(
        "--root-meta",
        type=Path,
        default=None,
        help="草稿箱索引 root_meta_info.json 的路径（默认 draft.parent/root_meta_info.json）",
    )
    return p


def root_entry(draft: Path, meta: dict, content_size: int) -> dict:
    """拼草稿箱索引 root_meta_info.json 里的一行。

    分隔符照抄剪映自己的写法 —— 2026-09-14 本机实测（索引 339 条 / 草稿实体 343 个）：

      · `draft_fold_path` 一律**正斜杠**（索引 338/338，草稿实体 draft_meta_info.json 256/256 零例外）
      · `draft_root_path` 索引里也是**正斜杠**（337/338）
      · `draft_json_file` / `draft_cover` 剪映用的是**混合**写法：
        目录部分正斜杠 + `\\` + 文件名，形如
        `D:/JianyingPro Drafts/<草稿名>\\draft_content.json`

    这里刻意不"统一成一种分隔符"：一旦写成另一种串，任何按字符串硬比的下游
    （剪映自己、或 p4j_* 之间的交接）就会假失败 —— 这个坑踩过一次了。
    """
    root = draft.parent
    fold = draft.as_posix()
    return {
        "cloud_draft_cover": False,
        "cloud_draft_sync": False,
        "draft_cloud_last_action_download": False,
        "draft_cloud_purchase_info": "",
        "draft_cloud_template_id": "",
        "draft_cloud_tutorial_info": "",
        "draft_cloud_videocut_purchase_info": "",
        "draft_cover": fold + "\\draft_cover.jpg",
        "draft_fold_path": fold,
        "draft_id": meta["draft_id"],
        "draft_is_ai_shorts": False,
        "draft_is_cloud_temp_draft": False,
        "draft_is_invisible": False,
        "draft_is_pippit_draft": False,
        "draft_is_web_article_video": False,
        "draft_json_file": fold + "\\draft_info.json",
        "draft_name": meta["draft_name"],
        "draft_new_version": meta.get("draft_new_version", ""),
        "draft_root_path": root.as_posix(),
        "draft_timeline_materials_size": int(content_size),
        "draft_type": meta.get("draft_type", ""),
        "draft_web_article_video_enter_from": "",
        "pippit_avatar_url": "",
        "pippit_extra_info": "",
        "pippit_id": "",
        "pippit_user_name": "",
        "streaming_edit_draft_ready": True,
        "tm_draft_cloud_completed": "",
        "tm_draft_cloud_entry_id": -1,
        "tm_draft_cloud_modified": 0,
        "tm_draft_cloud_parent_entry_id": -1,
        "tm_draft_cloud_space_id": -1,
        "tm_draft_cloud_user_id": -1,
        "tm_draft_create": int(meta["tm_draft_create"]),
        "tm_draft_modified": int(meta["tm_draft_modified"]),
        "tm_draft_removed": 0,
        "tm_duration": int(meta["tm_duration"]),
    }


def main() -> int:
    args = parser().parse_args()
    draft = args.draft.expanduser().resolve()
    info = draft / "draft_info.json"
    content = draft / "draft_content.json"
    meta_path = draft / "draft_meta_info.json"
    if not all(path.is_file() for path in (info, content, meta_path)):
        raise SystemExit("⛔ 草稿未通过基础文件完整性检查")
    if info.read_bytes() != content.read_bytes():
        raise SystemExit("⛔ draft_info.json 与 draft_content.json 不一致")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    # 分隔符归一化后再比：剪映自己写 draft_fold_path 用正斜杠（本机 256 份可读真实草稿
    # 0 例外），而 str(Path) 在 Windows 上是反斜杠 —— 直接字符串相等在 Windows 上必然假失败。
    fold = meta.get("draft_fold_path")
    fold_norm = os.path.normpath(fold) if isinstance(fold, str) and fold else None
    if fold_norm != os.path.normpath(str(draft)) or meta.get("draft_name") != draft.name:
        raise SystemExit("⛔ 草稿目录与 draft_meta_info.json 不一致")

    root_meta = (args.root_meta or (draft.parent / "root_meta_info.json")).expanduser().resolve()
    base_dir = root_meta.parent
    if not base_dir.is_dir():
        raise SystemExit(f"⛔ 草稿箱索引所在目录不存在: {base_dir}")
    if not root_meta.is_file():
        raise SystemExit(f"⛔ 草稿箱索引不存在: {root_meta}")
    lock_path = base_dir / ".codex_root_meta.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        acquire_exclusive(lock)
        try:
            payload = json.loads(root_meta.read_text(encoding="utf-8"))
            rows = payload.setdefault("all_draft_store", [])
            same_name = [row for row in rows if row.get("draft_name") == draft.name]
            if same_name:
                if len(same_name) != 1 or same_name[0].get("draft_id") != meta["draft_id"]:
                    raise SystemExit("⛔ 草稿箱已有同名不同 ID 项，拒绝覆盖")
                print(f"✅ 剪映草稿箱已登记: {draft.name}")
                return 0
            if any(row.get("draft_id") == meta["draft_id"] for row in rows):
                raise SystemExit("⛔ 草稿 ID 已被其他项目占用")
            rows.append(root_entry(draft, meta, info.stat().st_size))
            if isinstance(payload.get("draft_ids"), int):
                payload["draft_ids"] += 1
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            fd, temp_name = tempfile.mkstemp(prefix=".root_meta.", suffix=".tmp", dir=base_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, root_meta)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        finally:
            release_exclusive(lock)
    print(f"✅ 剪映草稿箱登记完成: {draft.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
