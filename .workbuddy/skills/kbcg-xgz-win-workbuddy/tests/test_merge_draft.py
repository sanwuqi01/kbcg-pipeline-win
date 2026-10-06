#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并草稿（剪辑工作台/工具/合并草稿.py）单元回归 —— 纯标准库。

覆盖：接龙偏移量、时间压叠检查、草稿名递增、期收集与 --only 规则。
建稿/验收需要 capcut-mate 与真实素材，由全链实跑验证，此处不重复。

被测模块按候选路径自动定位（与 test_icon_factory 同一套路）：
  · 打包树：  <包根>/剪辑工作台/工具/合并草稿.py
  · 源码树：  win-port → ../../剪辑工作台/工具/合并草稿.py
找不到则整组 skip（不阻塞其他回归）。
"""
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


def _locate_tool() -> Path | None:
    here = Path(__file__).resolve()
    cands = [here.parent.parent / "剪辑工作台" / "工具" / "合并草稿.py"]
    cands += [p / "剪辑工作台" / "工具" / "合并草稿.py" for p in here.parents]
    return next((c for c in cands if c.is_file()), None)


TOOL = _locate_tool()


def _load():
    if TOOL is None:
        return None
    spec = importlib.util.spec_from_file_location("xgz_hebing", TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules["xgz_hebing"] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipIf(TOOL is None, "找不到 剪辑工作台/工具/合并草稿.py")
class TestMerge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load()

    def test_accumulate_offsets(self):
        self.assertEqual(self.m.accumulate_offsets([]), [])
        self.assertEqual(self.m.accumulate_offsets([5]), [0])
        self.assertEqual(self.m.accumulate_offsets([3, 4, 5]), [0, 3, 7])
        self.assertEqual(self.m.accumulate_offsets([0, 0]), [0, 0])

    def test_check_no_overlap(self):
        ok = [(0, 100), (100, 250), (250, 260)]
        self.assertIsNone(self.m.check_no_overlap(ok))
        bad = [(0, 100), (50, 200)]
        self.assertIn("重叠", self.m.check_no_overlap(bad))
        # 乱序输入也要能查出压叠
        self.assertIn("重叠", self.m.check_no_overlap([(100, 200), (0, 150)]))

    def test_next_free_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(self.m.next_free_name(root, "草稿"), "草稿")
            (root / "草稿").mkdir()
            self.assertEqual(self.m.next_free_name(root, "草稿"), "草稿_第2版")
            (root / "草稿_第2版").mkdir()
            self.assertEqual(self.m.next_free_name(root, "草稿"), "草稿_第3版")

    def _fake_base(self, tmp: Path) -> Path:
        """工作台骨架：输入/2026-09-15/{a,b}.mp4 + 输入/2026-09-16/c.mp4，
        工作区里 甲乙 期的素材在 09-15、丙 在 09-16、丁 在输入根。"""
        base = Path(tmp)
        d1 = base / "输入" / "2026-09-15"
        d2 = base / "输入" / "2026-09-16"
        for d, names in ((d1, ["b.mp4", "a.mp4"]), (d2, ["c.mp4"])):
            d.mkdir(parents=True)
            for n in names:
                (d / n).write_bytes(b"x")
        (base / "输入" / "loose.mp4").write_bytes(b"x")

        def make_ep(name: str, source: Path):
            work = base / "工作区" / name
            work.mkdir(parents=True)
            (work / "config.json").write_text(json.dumps(
                {"name": name, "original_path": str(source)}, ensure_ascii=False),
                encoding="utf-8")

        make_ep("甲", d1 / "a.mp4")
        make_ep("乙", d1 / "b.mp4")
        make_ep("丙", d2 / "c.mp4")
        make_ep("丁", base / "输入" / "loose.mp4")
        return base

    def test_collect_episodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = self._fake_base(Path(tmp))
            rows = self.m.collect_episodes(base, "2026-09-15", None)
            self.assertEqual([r["name"] for r in rows], ["甲", "乙"])  # 按素材文件名排序
            rows = self.m.collect_episodes(base, "2026-09-16", None)
            self.assertEqual([r["name"] for r in rows], ["丙"])
            # --only 挑选 + 指定顺序
            rows = self.m.collect_episodes(base, "2026-09-15", ["乙", "甲"])
            self.assertEqual([r["name"] for r in rows], ["乙", "甲"])
            # 名字没命中 → 响亮失败
            with self.assertRaises(SystemExit):
                self.m.collect_episodes(base, "2026-09-15", ["乙", "丙"])

    def test_collect_episodes_missing_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.m.collect_episodes(Path(tmp), "1999-01-01", None)


if __name__ == "__main__":
    unittest.main()
