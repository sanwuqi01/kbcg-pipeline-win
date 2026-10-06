#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""可续传下载（pip 的临时下载无法跨进程续传，本脚本补齐这一点）。

背景：`pip install torch --index-url https://download.pytorch.org/whl/cu126`
的轮子是 2.42GB，国内直连约 2MB/s ≈ 21 分钟；一旦中断，pip 会从头再来。
本脚本用 HTTP Range 断点续传，并把进度写到日志文件，便于外部观察。

用法：
  fetch_resumable.py <url> <目标文件> [--expect-size N] [--log <日志路径>]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request
from pathlib import Path


def log(fh, msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    fh.write(line + "\n")
    fh.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url")
    ap.add_argument("dest", type=Path)
    ap.add_argument("--expect-size", type=int, default=0,
                    help="期望总字节数；仅在服务器不返回 Content-Range 时用于校验")
    ap.add_argument("--log", type=Path, default=None)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--retries", type=int, default=8)
    args = ap.parse_args()

    args.dest.parent.mkdir(parents=True, exist_ok=True)
    log_path = args.log or args.dest.with_suffix(args.dest.suffix + ".log")
    total = args.expect_size

    with open(log_path, "a", encoding="utf-8") as fh:
        for attempt in range(1, args.retries + 1):
            have = args.dest.stat().st_size if args.dest.exists() else 0
            if total and have >= total:
                log(fh, f"已完成 {have:,}/{total:,} 字节")
                break
            headers = {"User-Agent": "pip/24"}
            mode = "wb"
            if have:
                headers["Range"] = f"bytes={have}-"
                mode = "ab"
            log(fh, f"第 {attempt} 次：从 {have:,} 字节开始（{mode}）")
            try:
                req = urllib.request.Request(args.url, headers=headers)
                with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                    if have and resp.status != 206:
                        log(fh, f"服务器未接受 Range（HTTP {resp.status}），从头写")
                        have, mode = 0, "wb"
                    cr = resp.headers.get("Content-Range")
                    if cr and "/" in cr:
                        total = int(cr.rsplit("/", 1)[1])
                    elif not total:
                        cl = resp.headers.get("Content-Length")
                        total = (int(cl) + have) if cl else 0
                    log(fh, f"  目标总大小 {total:,} 字节（Content-Range: {cr}）")
                    t0 = time.time()
                    last = t0
                    with open(args.dest, mode) as out:
                        while True:
                            block = resp.read(1 << 20)
                            if not block:
                                break
                            out.write(block)
                            now = time.time()
                            if now - last >= 20:
                                done = args.dest.stat().st_size
                                spd = (done - have) / max(now - t0, 1e-6)
                                pct = f"{done/total*100:.1f}%" if total else "?"
                                eta = (total - done) / max(spd, 1) if total else 0
                                log(fh, f"  {done:,} / {total:,} ({pct}) "
                                        f"{spd/1048576:.2f} MB/s  ETA {eta/60:.1f} min")
                                last = now
            except Exception as exc:  # noqa: BLE001
                log(fh, f"  中断：{type(exc).__name__}: {exc}")
                time.sleep(2)
                continue
            done = args.dest.stat().st_size
            if not total or done >= total:
                log(fh, f"下载完成 {done:,} 字节 -> {args.dest}")
                break
        else:
            log(fh, "⚠ 重试次数用尽，仍未完成")
            return 1

    size = args.dest.stat().st_size
    if total and size != total:
        print(f"⛔ 尺寸不符：{size:,} != {total:,}")
        return 1
    print(f"✅ {args.dest}  {size:,} 字节 = {size/1048576:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
