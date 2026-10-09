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
from runtime_paths import KBCG_PATHS_API_VERSION, read_settings, resolve_runtime_paths  # noqa: E402


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
        values = {key: value for key, value in os.environ.items()
                  if not key.startswith("KBCG_")}
        values.update({
            "KBCG_RUNTIME_ROOT": str(self.root),
            "KBCG_SETTINGS_FILE": str(self.settings),
            "PYTHONUTF8": "1",
        })
        values.update(changes)
        return patch.dict(os.environ, values, clear=True)

    def test_legacy_paths(self):
        with patch.dict(os.environ, {}, clear=True):
            paths = resolve_runtime_paths(WORKBENCH)
        self.assertEqual(KBCG_PATHS_API_VERSION, 2)
        self.assertEqual(paths.root, WORKBENCH)
        self.assertEqual(paths.inbox, WORKBENCH / "输入")
        self.assertEqual(paths.workroot, WORKBENCH / "工作区")
        self.assertEqual(paths.outbox, WORKBENCH / "输出")
        self.assertEqual(paths.logroot, WORKBENCH / "运行记录")
        self.assertEqual(paths.settings, WORKBENCH / "配置" / "设置.json")

    def independent_env(self, task: str) -> dict[str, str]:
        task_root = Path(self.tmp.name).resolve() / f"{task} 中文 空格"
        roots = {}
        for name, folder in (("KBCG_INPUT_ROOT", "素材 输入"),
                             ("KBCG_WORK_ROOT", "处理中 工作区"),
                             ("KBCG_OUTPUT_ROOT", "交付 输出"),
                             ("KBCG_LOG_ROOT", "诊断 日志")):
            path = task_root / folder
            path.mkdir(parents=True)
            roots[name] = str(path)
        return roots

    def test_independent_roots_isolate_same_date_tasks_and_both_entries(self):
        date = "2026-10-08"
        roots_a = self.independent_env("任务甲")
        roots_b = self.independent_env("任务乙")
        for roots, name in ((roots_a, "甲 中文 空格"), (roots_b, "乙 中文 空格")):
            video = Path(roots["KBCG_INPUT_ROOT"]) / date / f"{name}.mp4"
            video.parent.mkdir()
            video.write_bytes(b"test")
            work = Path(roots["KBCG_WORK_ROOT"]) / name
            work.mkdir()
            (work / "config.json").write_text(json.dumps({
                "name": name, "core_expression": name,
                "original_path": str(video),
            }, ensure_ascii=False), encoding="utf-8")
            (work / "jianying_structural_verification.json").write_text(
                json.dumps({"draft_path": str(Path(self.tmp.name) / "剪映草稿")}),
                encoding="utf-8")

        for roots, own, foreign in ((roots_a, "甲 中文 空格", "乙 中文 空格"),
                                    (roots_b, "乙 中文 空格", "甲 中文 空格")):
            with patch.dict(os.environ, {**roots, "PYTHONUTF8": "1"}, clear=True):
                edit = load_tool("剪辑.py", f"test_independent_edit_{own}")
                merge = load_tool("合并草稿.py", f"test_independent_merge_{own}")
                paths = edit.RUNTIME_PATHS
                self.assertEqual(paths, merge.RUNTIME_PATHS)
                self.assertEqual(paths.inbox, Path(roots["KBCG_INPUT_ROOT"]))
                self.assertEqual(paths.workroot, Path(roots["KBCG_WORK_ROOT"]))
                self.assertEqual(paths.outbox, Path(roots["KBCG_OUTPUT_ROOT"]))
                self.assertEqual(paths.logroot, Path(roots["KBCG_LOG_ROOT"]))
                self.assertEqual([p.stem for p in edit.scan_inputs()], [own])
                self.assertEqual([p.name for p in edit.list_episodes()], [own])
                episodes = merge.collect_episodes(paths, date, None)
                self.assertEqual([row["name"] for row in episodes], [own])
                self.assertNotIn(foreign, [row["name"] for row in episodes])
                with patch.object(merge, "load_context", return_value={"剪辑": object()}):
                    with self.assertRaisesRegex(SystemExit, "只凑出 1 期"):
                        merge.main([date])
                rc, _ = edit.call_tee([sys.executable, "-c", "print('ok')"],
                                      edit.LOGROOT / "入口 日志.log")
                self.assertEqual(rc, 0)
                self.assertTrue((paths.logroot / "入口 日志.log").is_file())
                note = edit.write_delivery_note(paths.workroot / own)
                self.assertTrue(note.is_file())
                self.assertTrue(note.is_relative_to(paths.outbox))
                merge_note = merge.write_delivery_note({
                    "draft_name": "合并 中文 空格", "date": date,
                    "total_duration_us": 1000000,
                    "episodes": [{"name": own, "offset_us": 0,
                                  "duration_us": 1000000,
                                  "work": str(paths.workroot / own)}],
                }, {"structural_verified": True, "checked_at": date},
                    Path(self.tmp.name) / "剪映草稿")
                self.assertTrue(merge_note.is_file())
                self.assertTrue(merge_note.is_relative_to(paths.outbox))

    def test_invalid_independent_roots_fail_closed(self):
        roots = self.independent_env("校验任务")
        source = WORKBENCH.parent
        cases = [
            ({"KBCG_LOG_ROOT": None}, "KBCG_LOG_ROOT"),
            ({"KBCG_INPUT_ROOT": "relative"}, "KBCG_INPUT_ROOT"),
            ({"KBCG_INPUT_ROOT": str(Path(self.tmp.name) / "missing")},
             "KBCG_INPUT_ROOT"),
            ({"KBCG_INPUT_ROOT": str(source / "剪辑工作台")}, "KBCG_INPUT_ROOT"),
            ({"KBCG_INPUT_ROOT": str(source.parent)}, "KBCG_INPUT_ROOT"),
            ({"KBCG_OUTPUT_ROOT": roots["KBCG_INPUT_ROOT"]}, "KBCG_OUTPUT_ROOT"),
        ]
        nested = Path(roots["KBCG_INPUT_ROOT"]) / "子目录"
        nested.mkdir()
        cases.append(({"KBCG_OUTPUT_ROOT": str(nested)}, "KBCG_OUTPUT_ROOT"))
        source_link = Path(self.tmp.name) / "源码 链接"
        try:
            source_link.symlink_to(source, target_is_directory=True)
        except (OSError, NotImplementedError):
            pass
        else:
            cases.append(({"KBCG_INPUT_ROOT": str(source_link)}, "KBCG_INPUT_ROOT"))
        for changes, expected in cases:
            env = dict(roots)
            env.update(changes)
            env = {k: v for k, v in env.items() if v is not None}
            with self.subTest(changes=changes), patch.dict(os.environ, env, clear=True):
                with self.assertRaisesRegex(SystemExit, expected):
                    resolve_runtime_paths(WORKBENCH)

    def test_runtime_root_defaults_can_be_overridden_individually(self):
        roots = self.independent_env("覆盖任务")
        with self.env(KBCG_INPUT_ROOT=roots["KBCG_INPUT_ROOT"]):
            paths = resolve_runtime_paths(WORKBENCH)
            self.assertEqual(paths.inbox, Path(roots["KBCG_INPUT_ROOT"]))
            self.assertEqual(paths.workroot, self.root / "工作区")
            self.assertEqual(paths.outbox, self.root / "输出")
            self.assertEqual(paths.logroot, self.root / "运行记录")
        with self.env(KBCG_INPUT_ROOT=str(self.root / "工作区")):
            with self.assertRaisesRegex(SystemExit, "KBCG_INPUT_ROOT.*KBCG_WORK_ROOT"):
                resolve_runtime_paths(WORKBENCH)

    def test_runtime_anchor_needs_no_default_children_when_all_overridden(self):
        roots = self.independent_env("兼容锚点")
        anchor = Path(self.tmp.name).resolve() / "只作锚点"
        anchor.mkdir()
        with self.env(KBCG_RUNTIME_ROOT=str(anchor), **roots):
            paths = resolve_runtime_paths(WORKBENCH)
        self.assertEqual(paths.root, anchor)
        self.assertEqual(paths.inbox, Path(roots["KBCG_INPUT_ROOT"]))
        self.assertEqual(paths.workroot, Path(roots["KBCG_WORK_ROOT"]))
        self.assertEqual(paths.outbox, Path(roots["KBCG_OUTPUT_ROOT"]))
        self.assertEqual(paths.logroot, Path(roots["KBCG_LOG_ROOT"]))

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
                                    env=cli_env, capture_output=True, text=True, encoding="utf-8")
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
