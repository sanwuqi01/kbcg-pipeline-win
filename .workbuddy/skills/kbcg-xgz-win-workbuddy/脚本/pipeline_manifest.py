#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""finalize_fcp.sh 的可证明运行清单记录器。"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


SCHEMA = "kbcg-xgz/run_manifest@2"

# 只记录真正参与正式工作流的代码。开发审计工具的增删不应改变生产指纹。
PRODUCTION_CODE = (
    "media_contract.py",
    "jianying_contract.py",
    "p0_建工作区.py",
    "p1a_建词轴.py",
    "p1a2_对齐词轴.py",
    "expression_review_contract.py",
    "p1c_生成表达审查.py",
    "p1_建段.py",
    "decision_contract.py",
    "p2_结构编排.py",
    "p2b_生成切口清单.py",
    "p2c_出粗剪FCPXML.py",
    "p2d_回灌人工粗剪.py",
    "p1q_决策质量闸门.py",
    "f1_粗剪验收冻结.sh",
    "l1_学习用户精调XML.py",
    "p3_分卡.py",
    "p1b_帧级验收.py",
    "p4f_出FCPXML.py",
    "p4f_验收.py",
    "p4h_包装人工粗剪.py",
    "p4j_出剪映XML.py",
    "p4j_验收.py",
    "p4j_出剪映草稿.py",
    "p4j_准备交付素材.py",
    "p4j_验收草稿.py",
    "p4j_记录剪映实机验收.py",
    "p4j_定位剪映草稿根.py",
    "p4j_登记草稿箱.py",
    "stage_scope_guard.py",
    "pipeline_manifest.py",
    "finalize_fcp.sh",
    "finalize_jianying.sh",
    "finalize.sh",
    "查词.py",
    "核专名.py",
)


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def entry(path: str | Path, *, required: bool = True) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    exists = target.is_file()
    if required and not exists:
        raise SystemExit(f"⛔ 清单要求的文件不存在: {target}")
    data: dict[str, Any] = {"path": str(target), "exists": exists}
    if exists:
        stat = target.stat()
        data.update(
            {
                "sha256": sha256(target),
                "bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    else:
        data.update({"sha256": None, "bytes": None, "mtime_ns": None})
    return data


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"⛔ run_manifest 无法读取: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise SystemExit("⛔ run_manifest schema 不匹配")
    return data


def command_init(args: argparse.Namespace) -> None:
    scripts = {
        name: entry(args.code_dir / name)
        for name in PRODUCTION_CODE
    }
    try:
        decision_lock = json.loads(Path(args.lock).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"⛔ decision_lock 无法读取: {exc}") from exc
    if decision_lock.get("schema") == "human-rough-lock@1":
        review = {
            "level": "human_approved",
            "source": "human_rough_lock",
            "human_xml_path": decision_lock.get("human_xml_path"),
        }
    else:
        review = decision_lock.get("review")
        if not isinstance(review, dict) or review.get("level") not in {
            "agent_reviewed", "human_reviewed", "human_approved"
        }:
            raise SystemExit("⛔ decision_lock 缺少可追溯 review.level")
    manifest = {
        "schema": SCHEMA,
        "pipeline": args.pipeline,
        "run_id": f"{dt.datetime.now():%Y%m%dT%H%M%S}-{os.getpid()}",
        "work": str(args.work.resolve()),
        "name": args.name,
        "started_at": now(),
        "finished_at": None,
        "status": "running",
        "exit_code": None,
        "complete": False,
        "delivery_state": "running",
        "steps_expected": args.expected,
        "steps": [],
        "run_inputs": {
            "source_media": entry(args.source),
            "audio_wav": entry(args.audio),
            "decision_lock": entry(args.lock),
        },
        "decision_review": review,
        "code_version": scripts,
        "delivery": None,
    }
    atomic_write(args.manifest, manifest)


def _step(manifest: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((row for row in manifest["steps"] if row.get("step") == name), None)


def command_begin(args: argparse.Namespace) -> None:
    data = load(args.manifest)
    if data.get("status") != "running":
        raise SystemExit("⛔ 只能在 running 清单上开始步骤")
    if args.step not in data.get("steps_expected", []):
        raise SystemExit(f"⛔ 非预期步骤: {args.step}")
    if _step(data, args.step):
        raise SystemExit(f"⛔ 步骤重复开始: {args.step}")
    data["steps"].append(
        {
            "step": args.step,
            "status": "running",
            "started_at": now(),
            # 关键语义：输入在命令执行前取哈希。
            "inputs_before": [entry(path) for path in args.inputs],
            "outputs_after": [],
        }
    )
    atomic_write(args.manifest, data)


def command_end(args: argparse.Namespace) -> None:
    data = load(args.manifest)
    row = _step(data, args.step)
    if not row or row.get("status") != "running":
        raise SystemExit(f"⛔ 步骤未开始或已结束: {args.step}")
    row["status"] = "ok"
    row["finished_at"] = now()
    row["outputs_after"] = [entry(path) for path in args.outputs]
    atomic_write(args.manifest, data)


def command_fail(args: argparse.Namespace) -> None:
    data = load(args.manifest)
    row = _step(data, args.step)
    if row and row.get("status") == "running":
        row["status"] = "failed"
        row["finished_at"] = now()
        row["exit_code"] = args.exit_code
        if args.note:
            row["note"] = args.note
    data["status"] = "failed"
    data["exit_code"] = args.exit_code
    data["complete"] = False
    data["finished_at"] = now()
    atomic_write(args.manifest, data)


def command_abort(args: argparse.Namespace) -> None:
    data = load(args.manifest)
    if data.get("complete"):
        return
    running = next((row for row in data["steps"] if row.get("status") == "running"), None)
    if running:
        running["status"] = "failed"
        running["finished_at"] = now()
        running["exit_code"] = args.exit_code
        running["note"] = args.note
    data["status"] = "failed"
    data["exit_code"] = args.exit_code
    data["complete"] = False
    data["finished_at"] = now()
    data["failure_note"] = args.note
    atomic_write(args.manifest, data)


def command_finish(args: argparse.Namespace) -> None:
    data = load(args.manifest)
    states = {row["step"]: row.get("status") for row in data["steps"]}
    missing = [step for step in data["steps_expected"] if states.get(step) != "ok"]
    if missing:
        raise SystemExit("⛔ 不能完成清单，未成功步骤: " + ", ".join(missing))
    xml = entry(args.xml)
    delivery = entry(args.delivery)
    if xml["sha256"] != delivery["sha256"]:
        raise SystemExit("⛔ 交付副本与本轮 FCPXML 哈希不同")
    data["delivery"] = delivery
    data["final_output"] = xml
    data["delivery_state"] = args.delivery_state
    runtime_complete = args.delivery_state in {"complete", "complete_runtime_verified"}
    data["status"] = "complete" if runtime_complete else "action_required"
    data["exit_code"] = 0
    data["complete"] = runtime_complete
    data["finished_at"] = now()
    atomic_write(args.manifest, data)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subs = root.add_subparsers(dest="command", required=True)
    init = subs.add_parser("init")
    init.add_argument("manifest", type=Path)
    init.add_argument("--work", type=Path, required=True)
    init.add_argument("--name", required=True)
    init.add_argument("--source", required=True)
    init.add_argument("--audio", required=True)
    init.add_argument("--lock", required=True)
    init.add_argument("--code-dir", type=Path, required=True)
    init.add_argument("--pipeline", default="finalize_fcp.sh")
    init.add_argument("--expected", nargs="+", required=True)
    init.set_defaults(func=command_init)

    begin = subs.add_parser("begin")
    begin.add_argument("manifest", type=Path)
    begin.add_argument("step")
    begin.add_argument("inputs", nargs="*")
    begin.set_defaults(func=command_begin)

    end = subs.add_parser("end")
    end.add_argument("manifest", type=Path)
    end.add_argument("step")
    end.add_argument("outputs", nargs="*")
    end.set_defaults(func=command_end)

    fail = subs.add_parser("fail")
    fail.add_argument("manifest", type=Path)
    fail.add_argument("step")
    fail.add_argument("exit_code", type=int)
    fail.add_argument("--note", default="")
    fail.set_defaults(func=command_fail)

    abort = subs.add_parser("abort")
    abort.add_argument("manifest", type=Path)
    abort.add_argument("exit_code", type=int)
    abort.add_argument("--note", default="finalize 非零退出")
    abort.set_defaults(func=command_abort)

    finish = subs.add_parser("finish")
    finish.add_argument("manifest", type=Path)
    finish.add_argument("--xml", required=True)
    finish.add_argument("--delivery", required=True)
    finish.add_argument(
        "--delivery-state",
        choices=(
            "complete", "complete_structural", "relink_required",
            "material_bin_failed", "complete_runtime_verified",
        ),
        default="complete",
    )
    finish.set_defaults(func=command_finish)
    return root


def main() -> int:
    args = parser().parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
