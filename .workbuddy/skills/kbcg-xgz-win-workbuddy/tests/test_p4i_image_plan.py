#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""p4i_配图对齐 的契约回归（纯合成数据，不依赖任何真实素材）。

复刻用户场景：配图/ 文件夹里放「文件名=触发词」的图片，
管线要把它锚到播放顺序里对应的那句话（句首），并支持
#序号 消歧、_Ns 时长覆盖、config.image_duration_s 默认秒数。
触发词没出现 / 序号超界 / 触发词重复必须响亮失败。

跑法（在 Skill 根目录下）：
    python -m unittest tests.test_p4i_image_plan
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "脚本" / "p4i_配图对齐.py"
sys.path.insert(0, str(ROOT / "脚本"))

FPS = 60


class ImagePlanCase(unittest.TestCase):
    """公共脚手架：三句话的最小工作区。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="p4i_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = self.tmp
        # 三句话：检查 出现两次；多囊 只出现一次。
        rough = [
            self._seg(0, "医生我老公说生不了是我的问题不是他的", 3.8, 6.8),
            self._seg(1, "先检查男方因素不要走弯路", 7.0, 10.0),
            self._seg(2, "多囊备孕要先检查激素六项", 10.2, 15.0),
        ]
        (self.work / "rough_segments.json").write_text(
            json.dumps(rough, ensure_ascii=False), encoding="utf-8")
        captions = {
            "fps": FPS,
            "segments_tl": [
                {"in_f": int(round(r["in"] * FPS)), "out_f": int(round(r["out"] * FPS)),
                 "tl_in_f": int(round((r["in"] - 3.8) * FPS)),
                 "tl_out_f": int(round((r["out"] - 3.8) * FPS))}
                for r in rough
            ],
        }
        (self.work / "captions_plan.json").write_text(
            json.dumps(captions, ensure_ascii=False), encoding="utf-8")
        (self.work / "config.json").write_text(
            json.dumps({"image_duration_s": 4.0}, ensure_ascii=False),
            encoding="utf-8")
        (self.work / "配图").mkdir()

    @staticmethod
    def _seg(index: int, text: str, start: float, end: float) -> dict:
        words = []
        t = start
        for ch in text:
            words.append({
                "w": ch, "s": round(t, 3), "e": round(t + 0.12, 3),
                "char_times": [{"c": ch, "s": round(t, 3), "e": round(t + 0.12, 3)}],
            })
            t += 0.12
        return {"in": start, "out": end, "text": text, "words": words}

    def image(self, name: str) -> Path:
        path = self.work / "配图" / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())  # 假图，p4i 不读内容
        return path

    def run_p4i(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.work)],
            capture_output=True, text=True, encoding="utf-8", cwd=str(ROOT),
        )

    def plan(self) -> dict:
        return json.loads(
            (self.work / "配图计划.json").read_text(encoding="utf-8"))

    # ---- 用例 ----

    def test_触发词命中句首与默认时长(self) -> None:
        self.image("多囊.png")
        proc = self.run_p4i()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        entry = self.plan()["images"][0]
        self.assertEqual(entry["si"], 2)               # 「多囊」在第 3 句
        self.assertEqual(entry["trigger"], "多囊")
        self.assertEqual(entry["occurrence"], 1)
        self.assertEqual(entry["duration_s"], 4.0)     # config 默认
        self.assertEqual(entry["tl_in_f"], int(round((10.2 - 3.8) * FPS)))  # 句首
        self.assertIn("多囊备孕要先检查激素六项", entry["sentence"])

    def test_序号消歧与时长覆盖(self) -> None:
        self.image("检查#2_2.5s.png")
        proc = self.run_p4i()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        entry = self.plan()["images"][0]
        self.assertEqual(entry["si"], 2)               # 第 2 次「检查」在第 3 句
        self.assertEqual(entry["occurrence"], 2)
        self.assertEqual(entry["duration_s"], 2.5)     # 文件名覆盖 config

    def test_空配图目录不产出计划(self) -> None:
        proc = self.run_p4i()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.work / "配图计划.json").exists())

    def test_触发词没出现要响亮失败(self) -> None:
        self.image("子宫肌瘤.png")
        proc = self.run_p4i()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("一次都没出现", proc.stderr + proc.stdout)

    def test_序号超界要响亮失败(self) -> None:
        self.image("检查#3.png")
        proc = self.run_p4i()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("只出现 2 次", proc.stderr + proc.stdout)

    def test_触发词重复要响亮失败(self) -> None:
        self.image("检查.png")
        self.image("检查_2s.png")
        proc = self.run_p4i()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("触发词重复", proc.stderr + proc.stdout)

    def test_时长越片尾收窄(self) -> None:
        # 第 3 句句首到片尾只剩 4.8s，要 6s 应收窄到 4.8s。
        self.image("多囊_6s.png")
        proc = self.run_p4i()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        entry = self.plan()["images"][0]
        self.assertTrue(entry["clamped_to_end"])
        self.assertAlmostEqual(entry["duration_s"], 4.8, places=2)

    # ---- 防重叠内建（2026-09-16 复盘固化） ----

    def test_同句多图只留触发词最长的一张(self) -> None:
        # 「检查」和「男方因素」都命中第 2 句 → 只留 4 字触发词，其余进 auto_dropped。
        self.image("检查.png")
        self.image("男方因素.png")
        proc = self.run_p4i()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        plan = self.plan()
        images = plan["images"]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["file"], "男方因素.png")
        dropped = plan.get("auto_dropped") or []
        self.assertEqual([d["file"] for d in dropped], ["检查.png"])
        self.assertIn("同句多图", proc.stdout)

    def test_相邻句重叠自动压时长(self) -> None:
        # 第 1 句默认 4s，但第 2 句句首在 3.2s → 前图压到 3.2s，正好不重叠。
        self.image("医生.png")
        self.image("检查.png")
        proc = self.run_p4i()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        images = sorted(self.plan()["images"], key=lambda e: e["tl_in_f"])
        self.assertEqual(len(images), 2)               # 不同句，两张都留
        self.assertTrue(images[0]["auto_shortened"])
        self.assertAlmostEqual(images[0]["duration_s"], 3.2, places=2)
        self.assertAlmostEqual(images[1]["duration_s"], 4.0, places=2)
        # p4j 同款重叠校验：prev_end_f 不得越过 next tl_in_f。
        fps = self.plan()["fps"]
        prev_end = images[0]["tl_in_f"] + round(images[0]["duration_s"] * fps)
        self.assertLessEqual(prev_end, images[1]["tl_in_f"])
        self.assertIn("自动压时长", proc.stdout)


if __name__ == "__main__":
    unittest.main()
