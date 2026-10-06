#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""记录剪映实机结果；只能在真实打开草稿并检查素材/播放器后运行。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import tempfile
from pathlib import Path

import pipeline_manifest


SCHEMA = "kbcg-xgz/jianying_runtime_verification@1"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work", type=Path)
    p.add_argument("--material-bin-visible", choices=("yes", "no"), required=True)
    p.add_argument("--timeline-media-accessible", choices=("yes", "no"), required=True)
    p.add_argument("--preview-frame-visible", choices=("yes", "no"), required=True)
    p.add_argument("--evidence-note", required=True)
    p.add_argument("--evidence-file", type=Path)
    return p


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def yes(value: str) -> bool:
    return value == "yes"


def main() -> int:
    args = parser().parse_args()
    work = args.work.expanduser().resolve()
    structural_path = work / "jianying_structural_verification.json"
    manifest_path = work / "run_manifest_jianying.json"
    if not structural_path.is_file():
        raise SystemExit("⛔ 缺少 jianying_structural_verification.json，不能跳过结构验收")
    structural = json.loads(structural_path.read_text(encoding="utf-8"))
    if structural.get("structural_verified") is not True:
        raise SystemExit("⛔ 剪映草稿结构未通过，禁止标记实机完成")
    evidence_file = None
    if args.evidence_file:
        evidence_file = args.evidence_file.expanduser().resolve()
        if not evidence_file.is_file():
            raise SystemExit(f"⛔ 实机验收证据文件不存在: {evidence_file}")
    checks = {
        "material_bin_visible": yes(args.material_bin_visible),
        "timeline_media_accessible": yes(args.timeline_media_accessible),
        "preview_frame_visible": yes(args.preview_frame_visible),
    }
    if all(checks.values()):
        state = "complete_runtime_verified"
    elif not checks["timeline_media_accessible"] or not checks["preview_frame_visible"]:
        state = "relink_required"
    else:
        state = "material_bin_failed"
    report = {
        "schema": SCHEMA,
        "checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "draft_path": structural.get("draft_path"),
        **checks,
        "delivery_state": state,
        "evidence_note": args.evidence_note.strip(),
        "evidence_file": str(evidence_file) if evidence_file else None,
    }
    if not report["evidence_note"]:
        raise SystemExit("⛔ evidence-note 不能为空，必须说明如何实机检查")
    output = work / "jianying_runtime_verification.json"
    atomic_json(output, report)

    manifest = pipeline_manifest.load(manifest_path)
    manifest["runtime_verification"] = pipeline_manifest.entry(output)
    manifest["delivery_state"] = state
    manifest["complete"] = state == "complete_runtime_verified"
    manifest["status"] = "complete" if manifest["complete"] else "action_required"
    manifest["finished_at"] = pipeline_manifest.now()
    pipeline_manifest.atomic_write(manifest_path, manifest)

    print(f"✅ 剪映实机验收已记录: {state}")
    if state != "complete_runtime_verified":
        print("⚠️ 当前不能宣称剪映实机交付完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
