#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""p1a3_字缝吸附 的契约回归（合成音频，不依赖任何真实素材）。

复刻的就是 2026-09-14 用户人耳抽听暴露的那个几何形状：两个音节之间有一段静音，
对齐器却把字缝标在**前一个字内部**（偏晚 60ms）—— 于是从前字终点切到后字终点，
听到的是两个字（实测是「第三步」的「三」被切在「第」字内部）。

跑法（在 Skill 根目录下）：
    python -m unittest tests.test_p1a3_char_snap
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

try:
    import numpy as np
except ImportError:  # 无 numpy 的解释器（如托管 3.13.12）整类跳过；打包回归用 asr-win
    np = None

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "脚本" / "p1a3_字缝吸附.py"
SR = 16000
FPS = 30


def _tone(a: float, b: float, freq: float, amp: float = 0.12) -> np.ndarray:
    n = int(round((b - a) * SR))
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _write_wav(path: Path, pcm: np.ndarray) -> None:
    data = np.clip(pcm, -1.0, 1.0)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((data * 32767).astype("<i2").tobytes())


def _char(text: str, s: float, e: float) -> dict:
    return {"c": text, "s": s, "e": e}


@unittest.skipIf(np is None, "本测试需要 numpy（合成音频），当前解释器没有")
class SnapCase(unittest.TestCase):
    """公共脚手架：造一个只有 wav + 词轴的最小工作区。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="p1a3_test_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    # ---- 组装 ----
    def build(self, pcm: np.ndarray, words: list[dict],
              voice_blocks: list[list[float]]) -> Path:
        wav = self.tmp / "_vad16k.wav"
        _write_wav(wav, pcm)
        import hashlib
        h = hashlib.sha256(wav.read_bytes()).hexdigest()
        (self.tmp / "_vad16k.meta.json").write_text(
            json.dumps({"wav_sha256": h}), encoding="utf-8")
        track = {
            "fps": FPS,
            "aligner": "Qwen/Qwen3-ForcedAligner-0.6B",
            "char_timing": "forced-aligner-char@1",
            "voice_blocks": voice_blocks,
            "audio_cache": {"wav_sha256": h},
            "words": words,
        }
        (self.tmp / "word_track.json").write_text(
            json.dumps(track, ensure_ascii=False), encoding="utf-8")
        return self.tmp

    def run_snap(self, *extra: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.tmp), *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env,
        )

    def track(self) -> dict:
        return json.loads((self.tmp / "word_track.json").read_text(encoding="utf-8"))

    # ---- 造两个字的词轴 ----
    @staticmethod
    def two_char_words(a_s: float, a_e: float, b_s: float, b_e: float) -> list[dict]:
        return [
            {"i": 0, "t": "第", "ws": a_s, "we": a_e, "vs": a_s, "ve": a_e, "blk": 0,
             "align_text": "第", "char_times": [_char("第", a_s, a_e)]},
            {"i": 1, "t": "三", "ws": b_s, "we": b_e, "vs": b_s, "ve": b_e, "blk": 0,
             "align_text": "三", "char_times": [_char("三", b_s, b_e)]},
        ]

    # ---- ① 用户听到「第三」的那个几何形状 ----
    def test_shared_seam_snaps_to_silence(self):
        """字缝被标在前字内部 → 必须挪到静音谷中心，且前字时长被恢复。"""
        pcm = np.concatenate([
            np.zeros(int(0.05 * SR), dtype=np.float32),   # 0.00-0.05 静音
            _tone(0.05, 0.50, 220.0),                     # 0.05-0.50 「第」的语音
            np.zeros(int(0.08 * SR), dtype=np.float32),   # 0.50-0.58 真字缝（80ms）
            _tone(0.58, 1.10, 330.0),                     # 0.58-1.10 「三」的语音
            np.zeros(int(0.10 * SR), dtype=np.float32),   # 1.10-1.20 静音
        ])
        # 对齐器把字缝标在 0.600 —— 落在「第」的语音区里（真字缝 0.50-0.58）
        words = self.two_char_words(0.05, 0.600, 0.600, 1.10)
        self.build(pcm, words, voice_blocks=[[0.05, 1.10]])

        proc = self.run_snap()
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

        t = self.track()
        seam = t["words"][0]["we"]
        self.assertAlmostEqual(
            t["words"][1]["ws"], seam, places=3,
            msg="共享字缝必须两边一起落（否则会切出半个字）")
        self.assertLess(seam, 0.600, "字缝应当往前挪进静音区")
        self.assertGreater(seam, 0.495, "不能挪出静音区（不能切进前一个字）")
        self.assertLess(seam, 0.585, "不能挪出静音区（不能切进后一个字）")
        # 前字时长从 550ms 恢复成 ~490ms 而不是被压成 100ms
        self.assertGreater(t["words"][0]["we"] - t["words"][0]["ws"], 0.40)

    # ---- ② 连读处没有谷就不该动 ----
    def test_no_valley_no_move(self):
        """整段连续发声（连读、没有谷）→ 边界保持原样，不许硬挪。"""
        pcm = np.concatenate([
            np.zeros(int(0.05 * SR), dtype=np.float32),
            _tone(0.05, 1.10, 220.0),                     # 一整段连续发声
            np.zeros(int(0.10 * SR), dtype=np.float32),
        ])
        words = self.two_char_words(0.05, 0.600, 0.600, 1.10)
        self.build(pcm, words, voice_blocks=[[0.05, 1.10]])

        proc = self.run_snap()
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)

        t = self.track()
        self.assertAlmostEqual(t["words"][0]["we"], 0.600, places=3)
        self.assertAlmostEqual(t["words"][1]["ws"], 0.600, places=3)

    # ---- ③ 文本 / 下标 / 顺序一个字都不许动 ----
    def test_text_and_index_untouched(self):
        pcm = np.concatenate([
            np.zeros(int(0.05 * SR), dtype=np.float32),
            _tone(0.05, 0.50, 220.0),
            np.zeros(int(0.08 * SR), dtype=np.float32),
            _tone(0.58, 1.10, 330.0),
        ])
        self.build(pcm, self.two_char_words(0.05, 0.600, 0.600, 1.10),
                   voice_blocks=[[0.05, 1.10]])
        self.assertEqual(self.run_snap().returncode, 0)

        t = self.track()
        self.assertEqual([w["t"] for w in t["words"]], ["第", "三"])
        self.assertEqual([w["i"] for w in t["words"]], [0, 1])
        self.assertEqual([c["c"] for w in t["words"] for c in w["char_times"]],
                         ["第", "三"])
        # 词时间必须含住自己的字
        for w in t["words"]:
            rows = w["char_times"]
            self.assertLessEqual(w["ws"], rows[0]["s"] + 1e-9)
            self.assertLessEqual(rows[-1]["e"], w["we"] + 1e-9)
        # vs/ve 必须跟着 ws/we（下游 p1/p3/p1b 读的是 vs/ve）
        for w in t["words"]:
            self.assertEqual((w["vs"], w["ve"]), (w["ws"], w["we"]))

    # ---- ④ 幂等 ----
    def test_idempotent(self):
        pcm = np.concatenate([
            np.zeros(int(0.05 * SR), dtype=np.float32),
            _tone(0.05, 0.50, 220.0),
            np.zeros(int(0.08 * SR), dtype=np.float32),
            _tone(0.58, 1.10, 330.0),
        ])
        self.build(pcm, self.two_char_words(0.05, 0.600, 0.600, 1.10),
                   voice_blocks=[[0.05, 1.10]])
        self.assertEqual(self.run_snap().returncode, 0)
        first = (self.tmp / "word_track.json").read_text(encoding="utf-8")

        again = self.run_snap()
        self.assertEqual(again.returncode, 0)
        self.assertIn("跳过", again.stdout or "")
        self.assertEqual(first, (self.tmp / "word_track.json").read_text(encoding="utf-8"))

    # ---- ⑤ --dry 不写任何文件 ----
    def test_dry_run_writes_nothing(self):
        pcm = np.concatenate([
            np.zeros(int(0.05 * SR), dtype=np.float32),
            _tone(0.05, 0.50, 220.0),
            np.zeros(int(0.08 * SR), dtype=np.float32),
            _tone(0.58, 1.10, 330.0),
        ])
        self.build(pcm, self.two_char_words(0.05, 0.600, 0.600, 1.10),
                   voice_blocks=[[0.05, 1.10]])
        before = (self.tmp / "word_track.json").read_text(encoding="utf-8")

        proc = self.run_snap("--dry")
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        self.assertFalse((self.tmp / "char_snap_report.json").exists())
        self.assertFalse((self.tmp / "word_track.吸附前备份.json").exists())
        self.assertEqual(before, (self.tmp / "word_track.json").read_text(encoding="utf-8"))

    # ---- ⑥ 审计报告要能复核每一处位移 ----
    def test_report_records_every_move(self):
        pcm = np.concatenate([
            np.zeros(int(0.05 * SR), dtype=np.float32),
            _tone(0.05, 0.50, 220.0),
            np.zeros(int(0.08 * SR), dtype=np.float32),
            _tone(0.58, 1.10, 330.0),
        ])
        self.build(pcm, self.two_char_words(0.05, 0.600, 0.600, 1.10),
                   voice_blocks=[[0.05, 1.10]])
        self.assertEqual(self.run_snap().returncode, 0)

        rep = json.loads((self.tmp / "char_snap_report.json").read_text(encoding="utf-8"))
        self.assertEqual(rep["schema"], "char-snap@1")
        fields = {"i", "word", "c", "field", "from", "to", "delta_ms", "drop_db"}
        self.assertTrue(rep["moves"], "应当记录到位移")
        for m in rep["moves"]:
            self.assertTrue(fields <= set(m), m)
            self.assertLessEqual(abs(m["delta_ms"]), 200)
        # 吸附前存档必须在，且是吸附前的样子
        bak = json.loads((self.tmp / "word_track.吸附前备份.json")
                         .read_text(encoding="utf-8"))
        self.assertAlmostEqual(bak["words"][0]["we"], 0.600, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
