"""外部运行目录只验证路径和轻量 I/O，不启动剪辑管线。"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1]
WORKBENCH = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from runtime_paths import read_settings, resolve_runtime_paths  # noqa: E402


def load_tool(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RuntimePathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "中文 空格运行目录"
        for folder in ("输入", "工作区", "输出", "运行记录"):
            (self.root / folder).mkdir(parents=True)
        self.settings = self.root / "个人 设置.json"
        self.settings.write_text('{"字幕字体": "新青年体"}', encoding="utf-8")

    def env(self, **changes):
        values = {
            "KBCG_RUNTIME_ROOT": str(self.root),
            "KBCG_SETTINGS_FILE": str(self.settings),
            "PYTHONUTF8": "1",
        }
        values.update(changes)
        return patch.dict(os.environ, values)

    def test_legacy_paths(self):
        with patch.dict(os.environ, {}, clear=True):
            paths = resolve_runtime_paths(WORKBENCH)
        self.assertEqual(paths.root, WORKBENCH)
        self.assertEqual(paths.settings, WORKBENCH / "配置" / "设置.json")

    def test_both_entries_share_external_io(self):
        video = self.root / "输入" / "2026-10-08" / "中文 空格.mp4"
        video.parent.mkdir()
        video.write_bytes(b"test")
        work = self.root / "工作区" / video.stem
        work.mkdir()
        (work / "config.json").write_text(json.dumps({
            "name": video.stem, "core_expression": video.stem,
            "original_path": str(video),
        }, ensure_ascii=False), encoding="utf-8")
        (work / "jianying_structural_verification.json").write_text(
            json.dumps({"draft_path": str(self.root / "草稿")}), encoding="utf-8")
        with self.env():
            edit = load_tool("剪辑.py", "test_external_edit")
            merge = load_tool("合并草稿.py", "test_external_merge")
            self.assertEqual(edit.scan_inputs(), [video])
            self.assertEqual(edit.list_episodes(), [work])
            self.assertEqual(read_settings(edit.RUNTIME_PATHS)["字幕字体"], "新青年体")
            self.assertEqual(merge.RUNTIME_PATHS, edit.RUNTIME_PATHS)
            self.assertEqual(merge.collect_episodes(merge.RUNTIME_PATHS.root,
                                                    "2026-10-08", None)[0]["work"], work)
            with patch.dict(os.environ, {"XGZ_SKILL_ROOT": "relative"}):
                with self.assertRaises(SystemExit):
                    edit.resolve_skill()
            rc, tail = edit.call_tee([sys.executable, "-c", "print('日志已写')"],
                                     edit.LOGROOT / "中文 空格.log")
            self.assertEqual(rc, 0)
            self.assertIn("日志已写", tail)
            note = edit.write_delivery_note(work)
            self.assertTrue(note.is_relative_to(self.root / "输出"))
            self.assertTrue(note.is_file())
            merge_note = merge.write_delivery_note({
                "draft_name": "合并 中文 空格", "date": "2026-10-08",
                "total_duration_us": 1000000,
                "episodes": [{"name": video.stem, "offset_us": 0,
                              "duration_us": 1000000, "work": str(work)}],
            }, {"structural_verified": True, "checked_at": "2026-10-08"},
                self.root / "草稿")
            self.assertTrue(merge_note.is_relative_to(self.root / "输出"))
            cli_env = dict(os.environ)
            cli_env["XGZ_SKILL_ROOT"] = str(WORKBENCH.parent / ".workbuddy" / "skills" /
                                             "kbcg-xgz-win-workbuddy")
            result = subprocess.run([sys.executable, str(TOOLS / "剪辑.py"), "inputs"],
                                    env=cli_env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(str(video), result.stdout)
        self.assertTrue((self.root / "运行记录" / "中文 空格.log").is_file())
        self.assertEqual(edit.TEMPLATES, WORKBENCH / "说明书" / "待填模板")

    def test_invalid_explicit_paths_fail_closed(self):
        source = WORKBENCH.parent
        cases = [
            {"KBCG_RUNTIME_ROOT": ""},
            {"KBCG_RUNTIME_ROOT": "relative"},
            {"KBCG_RUNTIME_ROOT": str(source / "剪辑工作台")},
            {"KBCG_RUNTIME_ROOT": str(source.parent)},
            {"KBCG_RUNTIME_ROOT": str(self.root / "missing")},
            {"KBCG_SETTINGS_FILE": ""},
            {"KBCG_SETTINGS_FILE": "relative.json"},
            {"KBCG_SETTINGS_FILE": str(source / "剪辑工作台" / "配置" / "设置.json")},
            {"KBCG_SETTINGS_FILE": str(self.root / "missing.json")},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.env(**changes):
                with self.assertRaises(SystemExit):
                    resolve_runtime_paths(WORKBENCH)
        (self.root / "运行记录").rmdir()
        with self.env(), self.assertRaises(SystemExit):
            resolve_runtime_paths(WORKBENCH)

    def test_invalid_external_settings_json_fails_closed(self):
        self.settings.write_text("[]", encoding="utf-8")
        with self.env(), self.assertRaises(SystemExit):
            read_settings(resolve_runtime_paths(WORKBENCH))
        self.settings.write_text("{", encoding="utf-8")
        with self.env(), self.assertRaises(SystemExit):
            read_settings(resolve_runtime_paths(WORKBENCH))

    def test_utf8_bom_settings_are_accepted_by_both_entries(self):
        self.settings.write_text('{"字幕字体": "新青年体"}', encoding="utf-8-sig")
        with self.env():
            edit = load_tool("剪辑.py", "test_bom_edit")
            merge = load_tool("合并草稿.py", "test_bom_merge")
            self.assertEqual(edit.settings()["字幕字体"], "新青年体")
            self.assertEqual(read_settings(merge.RUNTIME_PATHS)["字幕字体"], "新青年体")

    def test_runtime_child_symlink_cannot_resolve_to_source_or_ancestor(self):
        child = self.root / "运行记录"
        for target in (WORKBENCH.parent, WORKBENCH.parent.parent):
            with self.subTest(target=target):
                child.rmdir()
                try:
                    child.symlink_to(target, target_is_directory=True)
                except (OSError, NotImplementedError):
                    child.mkdir()
                    self.skipTest("当前系统不允许创建目录符号链接")
                try:
                    with self.env(), self.assertRaises(SystemExit):
                        resolve_runtime_paths(WORKBENCH)
                finally:
                    child.unlink()
                    child.mkdir()


if __name__ == "__main__":
    unittest.main()
