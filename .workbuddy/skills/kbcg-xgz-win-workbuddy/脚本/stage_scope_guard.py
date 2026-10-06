#!/usr/bin/env python3
"""按问题所属工序锁住所有上游不变量，防止修一层时串改其他层。

用法：
  stage_scope_guard.py begin  <工作目录> <scope> [--force]
  stage_scope_guard.py verify <工作目录>

scope：rough / subtitle-text / subtitle-alignment / subtitle-style / packaging / delivery
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from media_contract import caption_profile


SCHEMA = "kbcg-xgz-stage-scope/v1"
LOCK_NAME = "stage_scope_guard.json"
SCOPES = {
    "rough": ("media",),
    "subtitle-text": ("media", "rough_geometry"),
    "subtitle-alignment": ("media", "rough_geometry", "cards"),
    "subtitle-style": ("media", "rough_geometry", "cards", "captions_plan"),
    "packaging": ("media", "rough_geometry", "cards", "captions_plan", "caption_profile"),
    "delivery": ("media", "rough_geometry", "cards", "captions_plan", "caption_profile"),
}


class ScopeError(RuntimeError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScopeError(f"缺少工序文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ScopeError(f"JSON 无法解析：{path}: {exc}") from exc


def digest(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def media_invariant(work: Path) -> dict[str, Any]:
    cfg = read_json(work / "config.json")
    sources = cfg.get("sources")
    if isinstance(sources, list) and sources:
        source_identity = [
            {
                "id": row.get("id"),
                "path": row.get("path"),
                "sha256": row.get("sha256"),
            }
            for row in sources
        ]
    else:
        source_identity = {
            "path": cfg.get("original_path") or cfg.get("proxy"),
            "sha256": cfg.get("source_sha256"),
        }
    return {
        "material_type": cfg.get("material_type"),
        "media": cfg.get("media"),
        "sources": source_identity,
    }


def rough_geometry(work: Path) -> list[dict[str, Any]]:
    rows = read_json(work / "rough_segments.json")
    if not isinstance(rows, list) or not rows:
        raise ScopeError("rough_segments.json 必须是非空数组")
    result = []
    for pos, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ScopeError(f"rough_segments[{pos}] 不是对象")
        words = row.get("words") or []
        word_ids = []
        for word in words:
            if not isinstance(word, dict):
                raise ScopeError(f"rough_segments[{pos}].words 含非对象")
            word_ids.append([
                word.get("source_id", row.get("source_id")),
                word.get("source_word_i", word.get("i")),
            ])
        meta = row.get("_rough_structure") or {}
        result.append({
            "source_id": row.get("source_id"),
            "in_f": row.get("in_f"),
            "out_f": row.get("out_f"),
            "line": row.get("line"),
            "part": row.get("part"),
            "human_clip_index": row.get("human_clip_index"),
            "playback_key": meta.get("playback_key"),
            "word_ids": word_ids,
        })
    return result


def expected_caption_profile(work: Path) -> dict[str, Any]:
    cfg = read_json(work / "config.json")
    media = cfg.get("media") or {}
    video = media.get("video") or {}
    width, height = video.get("width"), video.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        raise ScopeError("config.media.video 缺少整数 width/height")
    try:
        expected = caption_profile(width, height)
    except ValueError:
        expected = cfg.get("caption_profile")
        if not isinstance(expected, dict):
            raise ScopeError("非标准画幅缺少显式 caption_profile")
    declared = cfg.get("caption_profile")
    if declared not in (None, expected.get("name"), expected):
        raise ScopeError("config.caption_profile 与当前画幅标准不一致")
    return expected


def invariant_value(work: Path, name: str) -> Any:
    if name == "media":
        return media_invariant(work)
    if name == "rough_geometry":
        return rough_geometry(work)
    if name == "cards":
        return read_json(work / "cards.json")
    if name == "captions_plan":
        return read_json(work / "captions_plan.json")
    if name == "caption_profile":
        return expected_caption_profile(work)
    raise ScopeError(f"未知不变量：{name}")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def begin(work: Path, scope: str, force: bool) -> dict[str, Any]:
    work = work.resolve()
    lock_path = work / LOCK_NAME
    if lock_path.exists() and not force:
        raise ScopeError(f"{lock_path} 已存在；新一轮修正请显式加 --force")
    names = SCOPES[scope]
    invariants = {}
    for name in names:
        value = invariant_value(work, name)
        invariants[name] = {"sha256": digest(value), "value": value}
    payload = {
        "schema": SCHEMA,
        "scope": scope,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "work": str(work),
        "protected": list(names),
        "invariants": invariants,
        "rule": "只修当前 scope；上游不变量任一漂移即失败。下游产物可按固定流程重新生成。",
    }
    atomic_json(lock_path, payload)
    return payload


def verify(work: Path) -> dict[str, Any]:
    work = work.resolve()
    payload = read_json(work / LOCK_NAME)
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ScopeError(f"{LOCK_NAME}.schema 必须是 {SCHEMA}")
    scope = payload.get("scope")
    if scope not in SCOPES:
        raise ScopeError(f"{LOCK_NAME}.scope 非法：{scope!r}")
    if payload.get("work") != str(work):
        raise ScopeError("工序锁属于其他工作目录")
    if tuple(payload.get("protected") or []) != SCOPES[scope]:
        raise ScopeError("工序锁 protected 与 scope 不一致")
    locked = payload.get("invariants")
    if not isinstance(locked, dict):
        raise ScopeError("工序锁缺 invariants")
    drift = []
    for name in SCOPES[scope]:
        old = locked.get(name)
        if not isinstance(old, dict) or not isinstance(old.get("sha256"), str):
            raise ScopeError(f"工序锁缺 {name} 摘要")
        current = invariant_value(work, name)
        current_hash = digest(current)
        if current_hash != old["sha256"]:
            drift.append(f"{name} {old['sha256'][:12]} -> {current_hash[:12]}")
    if drift:
        raise ScopeError(
            f"scope={scope} 越权修改了上游：" + "；".join(drift)
        )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_begin = sub.add_parser("begin")
    p_begin.add_argument("work", type=Path)
    p_begin.add_argument("scope", choices=sorted(SCOPES))
    p_begin.add_argument("--force", action="store_true")
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("work", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "begin":
            payload = begin(args.work, args.scope, args.force)
            print(
                f"✅ 已锁定修正范围 {payload['scope']}：保护 "
                + " / ".join(payload["protected"])
            )
        else:
            payload = verify(args.work)
            print(f"✅ 工序隔离通过：scope={payload['scope']}，上游未漂移")
        return 0
    except ScopeError as exc:
        print(f"⛔ 工序隔离失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
