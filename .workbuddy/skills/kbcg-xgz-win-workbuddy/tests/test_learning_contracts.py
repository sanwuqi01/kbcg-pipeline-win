#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from fractions import Fraction
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "脚本"
sys.path.insert(0, str(SCRIPTS))

from expression_review_contract import (  # noqa: E402
    ExpressionReviewError,
    build_review,
    discover_pause_candidates,
    validate_review,
)
import decision_contract  # noqa: E402
from caption_boundary_contract import (  # noqa: E402
    CaptionBoundaryError,
    validate_atomic_boundaries,
)
from jianying_contract import (  # noqa: E402
    caption_profile as jianying_caption_profile,
    project_card_timerange,
)
from fcpxml_packaging_contract import load_final_cut_packaging  # noqa: E402
from media_contract import (  # noqa: E402
    build_final_cut_color_contract,
    caption_profile,
    fcpxml_color_space,
    fcpxml_shadow_attributes,
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LEARNER = load_module("fcpxml_feedback", SCRIPTS / "l1_学习用户精调XML.py")
SCORER = load_module("training_regression", SCRIPTS / "l2_训练回归评分.py")
CUT_REVIEW = load_module("cut_review", SCRIPTS / "p2b_生成切口清单.py")
DRAFT_ROOT_LOCATOR = load_module(
    "jianying_draft_root", SCRIPTS / "p4j_定位剪映草稿根.py"
)
MEDIA_DELIVERY = load_module(
    "jianying_media_delivery", SCRIPTS / "p4j_准备交付素材.py"
)


class JianyingDraftRootTests(unittest.TestCase):
    def make_root(self, base: Path, label: str, mtime: float) -> Path:
        root = base / label / "JianyingPro/User Data/Projects/com.lveditor.draft"
        root.mkdir(parents=True)
        meta = root / "root_meta_info.json"
        meta.write_text(json.dumps({
            "root_path": str(root.resolve()),
            "all_draft_store": [],
            "draft_ids": 0,
        }), encoding="utf-8")
        os.utime(meta, (mtime, mtime))
        return root.resolve()

    def test_explicit_root_wins(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old = self.make_root(base, "old", 100)
            new = self.make_root(base, "new", 200)
            selected = DRAFT_ROOT_LOCATOR.choose_root(
                {old, new}, explicit=old, now=300
            )
            self.assertEqual(selected, old)

    def test_running_root_wins_over_saved_root(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            saved = self.make_root(base, "saved", 200)
            running = self.make_root(base, "running", 100)
            selected = DRAFT_ROOT_LOCATOR.choose_root(
                {saved, running}, running={running}, saved=saved, now=300
            )
            self.assertEqual(selected, running)

    def test_recent_custom_root_wins_over_stale_saved_root(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            saved = self.make_root(base, "saved", 100)
            custom = self.make_root(base, "custom", 995)
            selected = DRAFT_ROOT_LOCATOR.choose_root(
                {saved, custom}, saved=saved, now=1000
            )
            self.assertEqual(selected, custom)

    def test_saved_root_is_fallback_when_no_root_is_recent(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            saved = self.make_root(base, "saved", 100)
            other = self.make_root(base, "other", 200)
            selected = DRAFT_ROOT_LOCATOR.choose_root(
                {saved, other}, saved=saved, now=10000
            )
            self.assertEqual(selected, saved)

    def test_bounded_scan_finds_nonstandard_install_location(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            custom = self.make_root(base, "external/custom/location", 100)
            self.assertIn(custom, DRAFT_ROOT_LOCATOR.bounded_scan(base))

    def test_initialized_but_unwritable_is_not_reported_as_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.make_root(Path(temp), "denied", 100)
            with mock.patch.object(DRAFT_ROOT_LOCATOR.os, "access", return_value=False):
                result = DRAFT_ROOT_LOCATOR.inspect_root(root)
            self.assertTrue(result["structure_valid"])
            self.assertEqual(result["status"], "current_process_write_denied")

    def test_choose_root_reports_permission_instead_of_reinitialize(self):
        denied = Path("/tmp/denied/com.lveditor.draft")
        inspections = {
            denied: {
                "path": str(denied),
                "status": "current_process_write_denied",
                "structure_valid": True,
            }
        }
        with self.assertRaises(SystemExit) as caught:
            DRAFT_ROOT_LOCATOR.choose_root(set(), inspections=inspections)
        self.assertIn("不要重复创建空白草稿", str(caught.exception))


class JianyingMediaDeliveryTests(unittest.TestCase):
    def test_downloads_is_a_managed_copy_risk(self):
        source = Path.home() / "Downloads" / "sample.mov"
        self.assertEqual(MEDIA_DELIVERY.protected_reason(source), "downloads")

    def test_safe_source_keeps_original_in_auto_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.mov"
            source.write_bytes(b"same-media")
            draft_root = base / "Projects/com.lveditor.draft"
            draft_root.mkdir(parents=True)
            with mock.patch.object(MEDIA_DELIVERY, "protected_reason", return_value=None):
                result = MEDIA_DELIVERY.prepare_source(
                    {"id": "source", "path": str(source)}, draft_root, "auto"
                )
            self.assertEqual(result["mode"], "reference_original")
            self.assertEqual(result["delivery_path"], str(source.resolve()))
            self.assertFalse(result["runtime_access_risk"])

    def test_managed_copy_preserves_hash_and_original(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.mov"
            source.write_bytes(b"managed-media")
            draft_root = base / "Projects/com.lveditor.draft"
            draft_root.mkdir(parents=True)
            result = MEDIA_DELIVERY.prepare_source(
                {"id": "source", "path": str(source)}, draft_root, "managed_copy"
            )
            target = Path(result["delivery_path"])
            self.assertTrue(source.is_file())
            self.assertTrue(target.is_file())
            self.assertEqual(MEDIA_DELIVERY.sha256(source), MEDIA_DELIVERY.sha256(target))
            self.assertNotEqual(source.resolve(), target.resolve())


class JianyingRuntimeStateTests(unittest.TestCase):
    def test_runtime_report_is_the_only_way_to_mark_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work / "jianying_structural_verification.json").write_text(
                json.dumps({
                    "schema": "kbcg-xgz/jianying_structural_verification@1",
                    "structural_verified": True,
                    "draft_path": str(work / "draft"),
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            (work / "run_manifest_jianying.json").write_text(
                json.dumps({
                    "schema": "kbcg-xgz/run_manifest@2",
                    "status": "action_required",
                    "complete": False,
                    "delivery_state": "complete_structural",
                    "steps": [],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPTS / "p4j_记录剪映实机验收.py"),
                    str(work),
                    "--material-bin-visible", "yes",
                    "--timeline-media-accessible", "yes",
                    "--preview-frame-visible", "yes",
                    "--evidence-note", "测试中已打开剪映草稿并逐项检查",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((work / "run_manifest_jianying.json").read_text())
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["delivery_state"], "complete_runtime_verified")

    def test_runtime_failure_stays_in_action_required(self):
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work / "jianying_structural_verification.json").write_text(
                json.dumps({"structural_verified": True, "draft_path": str(work / "draft")}),
                encoding="utf-8",
            )
            (work / "run_manifest_jianying.json").write_text(
                json.dumps({
                    "schema": "kbcg-xgz/run_manifest@2",
                    "status": "action_required",
                    "complete": False,
                    "steps": [],
                }),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPTS / "p4j_记录剪映实机验收.py"),
                    str(work),
                    "--material-bin-visible", "no",
                    "--timeline-media-accessible", "no",
                    "--preview-frame-visible", "no",
                    "--evidence-note", "测试中确认剪映无权读取素材",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((work / "run_manifest_jianying.json").read_text())
            self.assertFalse(manifest["complete"])
            self.assertEqual(manifest["status"], "action_required")
            self.assertEqual(manifest["delivery_state"], "relink_required")


class CaptionProfileTests(unittest.TestCase):
    def test_portrait_4k_profile_matches_user_calibration(self):
        profile = caption_profile(2160, 3840)
        self.assertEqual(profile["font_size"], 52)
        self.assertEqual(profile["xml_y"], -190)
        self.assertEqual(
            fcpxml_shadow_attributes(profile),
            {
                "shadowColor": "0 0 0 0.75",
                "shadowOffset": "2 315",
                "shadowBlurRadius": "0.63",
            },
        )

    def test_other_profiles_do_not_inherit_portrait_4k_shadow(self):
        self.assertEqual(fcpxml_shadow_attributes(caption_profile(1080, 1920)), {})

    def test_jianying_portrait_4k_is_user_confirmed(self):
        profile = jianying_caption_profile(2160, 3840)
        self.assertEqual(profile["font_size"], 14.0)
        self.assertEqual(profile["transform_y"], -0.3791667)
        self.assertEqual(profile["evidence_status"], "user_confirmed")

    def test_confirmed_jianying_profiles_are_explicit(self):
        self.assertEqual(
            jianying_caption_profile(1080, 1920)["evidence_status"],
            "user_confirmed",
        )
        self.assertEqual(
            jianying_caption_profile(3840, 2160)["evidence_status"],
            "user_confirmed",
        )


class PackagingAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.cards = [
            {
                "si": index,
                "card_index": 0,
                "text": f"第{index}张字幕包含重点词" if index == 0 else f"第{index}张普通字幕",
                # P2 候选必须被忽略，除非 P5 显式登记。
                "keyword": "重点词",
            }
            for index in range(20)
        ]

    def test_final_cut_has_no_implicit_packaging(self):
        plan = load_final_cut_packaging(self.work, self.cards)
        self.assertEqual(plan["selected"], {})
        self.assertIsNone(plan["top_title"])

    def test_final_cut_uses_only_explicit_sparse_packaging(self):
        payload = {
            "schema": "final-cut-packaging@1",
            "delivery_branch": "final-cut",
            "highlight_cards": [
                {
                    "si": 0,
                    "card_index": 0,
                    "text": "第0张字幕包含重点词",
                    "keyword": "重点词",
                    "reason": "核心命题",
                }
            ],
            "top_title": {"text": "明确标题", "reason": "用户授权"},
        }
        (self.work / "final_cut_packaging.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        plan = load_final_cut_packaging(self.work, self.cards)
        self.assertEqual(plan["selected"][(0, 0)]["keyword"], "重点词")
        self.assertEqual(plan["top_title"]["text"], "明确标题")
        self.assertEqual(plan["ratio"], 0.05)

    def test_final_cut_rejects_dense_highlights(self):
        payload = {
            "schema": "final-cut-packaging@1",
            "delivery_branch": "final-cut",
            "highlight_cards": [
                {
                    "si": index,
                    "card_index": 0,
                    "text": self.cards[index]["text"],
                    "keyword": "重点词" if index == 0 else "普通字幕",
                    "reason": "错误地铺满重点",
                }
                for index in range(2)
            ],
        }
        (self.work / "final_cut_packaging.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            load_final_cut_packaging(self.work, self.cards)

    def test_final_cut_generator_does_not_promote_p2_candidates(self):
        source = (SCRIPTS / "p4f_出FCPXML.py").read_text(encoding="utf-8")
        self.assertIn("load_final_cut_packaging", source)
        self.assertNotIn("TITLE_TOP = plan.get('title')", source)
        self.assertNotIn("kw = c.get('keyword')", source)


class CapabilityBoundaryTests(unittest.TestCase):
    def test_multi_source_raw_ai_boundary_is_explicit(self):
        routing = (ROOT / "references" / "material-routing.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("single_speaker_multi_source", routing)
        self.assertIn("原始多文件 AI 粗剪", routing)
        self.assertIn("新建 Final Cut", routing)

    def test_portrait_4k_jianying_needs_no_second_confirmation(self):
        stages = (ROOT / "references" / "stage-boundaries.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("2160×3840", stages)
        self.assertIn("不再设置二次用户确认闸门", stages)

    def test_default_flow_does_not_pause_for_rough_cut_review(self):
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("不导出粗剪审核版", skill)
        self.assertIn("直接生成并登记剪映原生草稿箱最终版", skill)

    def test_jianying_finalizer_uses_dynamic_draft_root(self):
        finalizer = (SCRIPTS / "finalize_jianying.sh").read_text(encoding="utf-8")
        self.assertIn("p4j_定位剪映草稿根.py", finalizer)
        self.assertNotIn(
            'DRAFT_ROOT="$HOME/Movies/JianyingPro/User Data/Projects/com.lveditor.draft"',
            finalizer,
        )

    def test_jianying_finalizer_stops_at_structural_completion(self):
        finalizer = (SCRIPTS / "finalize_jianying.sh").read_text(encoding="utf-8")
        self.assertIn("p4j_准备交付素材.py", finalizer)
        self.assertIn("--delivery-state complete_structural", finalizer)
        self.assertIn("complete=false", finalizer)

    def test_jianying_runtime_completion_requires_three_checks(self):
        runtime = (SCRIPTS / "p4j_记录剪映实机验收.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("material_bin_visible", runtime)
        self.assertIn("timeline_media_accessible", runtime)
        self.assertIn("preview_frame_visible", runtime)
        self.assertIn("complete_runtime_verified", runtime)


class CaptionBoundaryTests(unittest.TestCase):
    def test_asr_word_and_english_token_cannot_be_split(self):
        with self.assertRaises(CaptionBoundaryError):
            validate_atomic_boundaries(
                "你也可以让", ["你", "也可以", "让"], ["你也", "可以让"]
            )
        with self.assertRaises(CaptionBoundaryError):
            validate_atomic_boundaries(
                "教你做IP的账号", ["教", "你", "做", "IP", "的", "账", "号"],
                ["教你做I", "P的账号"],
            )

    def test_semantic_cut_at_atomic_edge_passes(self):
        validate_atomic_boundaries(
            "推荐一些教你做IP的账号",
            ["推", "荐", "一些", "教", "你", "做", "IP", "的", "账", "号"],
            ["推荐一些", "教你做IP的账号"],
        )

    def test_p3_and_final_cut_verifier_require_zero_first_card_gap(self):
        p3 = (SCRIPTS / "p3_分卡.py").read_text(encoding="utf-8")
        verifier = (SCRIPTS / "p4f_验收.py").read_text(encoding="utf-8")
        self.assertIn("first_f = tl['tl_in_f']", p3)
        self.assertIn("max_first_gap = 0", verifier)


class ExpressionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.track_path = self.work / "word_track.json"
        self.track = {
            "words": [
                {"t": "呢", "ws": 0.20, "we": 0.35},
                {"t": "方法", "ws": 0.36, "we": 0.80},
            ],
            "residual_candidates": [
                {
                    "residual_id": "R0000",
                    "block_index": 0,
                    "start": 0.0,
                    "end": 0.2,
                    "duration": 0.2,
                    "next_word_index": 0,
                    "next_word_text": "呢",
                }
            ],
        }
        self.track_path.write_text(
            json.dumps(self.track, ensure_ascii=False), encoding="utf-8"
        )
        self.keep = {"drop": [], "fix": [], "retain": [[0, 0, "设问", "保留语气"]]}
        (self.work / "keep.json").write_text(
            json.dumps(self.keep, ensure_ascii=False), encoding="utf-8"
        )

    def reviewed(self):
        data = build_review(
            self.track_path, self.track, self.work / "keep.json", self.keep
        )
        micro = data["micro_candidates"][0]
        micro.update(
            {
                "decision": "retain",
                "function": "question_rhythm",
                "caption_action": "show",
                "reason": "承担设问语气，删除会改变句子功能",
                "acoustic_review": "heard",
            }
        )
        residual = data["residual_candidates"][0]
        residual.update(
            {
                "decision": "breath_noise",
                "recognized_text": "",
                "reason": "听审为非语言呼吸",
                "acoustic_review": "heard",
            }
        )
        return data

    def test_completed_review_passes(self):
        summary = validate_review(
            self.reviewed(),
            track_path=self.track_path,
            track=self.track,
            keep=self.keep,
        )
        self.assertEqual(
            summary,
            {"micro": 1, "residual": 1, "pause": 0, "pause_compress": 0},
        )

    def test_pending_micro_is_blocked(self):
        data = self.reviewed()
        data["micro_candidates"][0]["decision"] = "pending"
        with self.assertRaises(ExpressionReviewError):
            validate_review(
                data, track_path=self.track_path, track=self.track, keep=self.keep
            )

    def test_meaningful_residual_must_return_to_transcript(self):
        data = self.reviewed()
        data["residual_candidates"][0].update(
            {"decision": "restore_text", "recognized_text": "所以"}
        )
        with self.assertRaises(ExpressionReviewError):
            validate_review(
                data, track_path=self.track_path, track=self.track, keep=self.keep
            )

    def test_retained_voice_cannot_hide_caption(self):
        data = self.reviewed()
        data["micro_candidates"][0]["caption_action"] = "hide"
        with self.assertRaises(ExpressionReviewError):
            validate_review(
                data, track_path=self.track_path, track=self.track, keep=self.keep
            )

    def test_pause_candidate_requires_explicit_functional_decision(self):
        self.track["words"][1]["ws"] = 1.00
        self.track_path.write_text(
            json.dumps(self.track, ensure_ascii=False), encoding="utf-8"
        )
        rows = discover_pause_candidates(self.track["words"], self.keep)
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0]["source_gap_s"], 0.6)
        data = build_review(
            self.track_path, self.track, self.work / "keep.json", self.keep
        )
        data["micro_candidates"][0].update(
            {
                "decision": "retain",
                "function": "question_rhythm",
                "caption_action": "show",
                "reason": "承担设问语气",
                "acoustic_review": "heard",
            }
        )
        data["residual_candidates"][0].update(
            {
                "decision": "breath_noise",
                "recognized_text": "",
                "reason": "听审为呼吸",
                "acoustic_review": "heard",
            }
        )
        pause = data["pause_candidates"][0]
        pause.update(
            {
                "decision": "compress",
                "function": "thinking",
                "reason": "这是思考停顿",
                "acoustic_review": "heard",
            }
        )
        with self.assertRaises(ExpressionReviewError):
            validate_review(
                data, track_path=self.track_path, track=self.track, keep=self.keep
            )

    def test_keep_hash_change_invalidates_review(self):
        data = self.reviewed()
        changed = {**self.keep, "split_after": [0]}
        (self.work / "keep.json").write_text(
            json.dumps(changed, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(ExpressionReviewError):
            validate_review(
                data, track_path=self.track_path, track=self.track, keep=changed
            )


class SampleAndLearningTests(unittest.TestCase):
    def test_all_runtime_samples_have_scopes(self):
        registry = decision_contract._sample_pair_registry()
        self.assertEqual(len(registry), 12)
        for sample in registry.values():
            self.assertTrue(sample["evidence_scopes"])
            self.assertIsInstance(sample["known_defects"], list)

    def test_real_xml_feedback_diffs_match_known_counts(self):
        cases = [
            (
                ROOT / "样本库/口播/C2897_尾音与切口密度/AI第2版.fcpxml",
                ROOT / "样本库/口播/C2897_尾音与切口密度/用户精调第2版.fcpxml",
                (65, 66),
            ),
            (
                ROOT / "样本库/口播/DSCF2925_普通人用好AI的三个思维/AI第4版_粗剪字幕.fcpxml",
                ROOT / "样本库/口播/DSCF2925_普通人用好AI的三个思维/用户精调版.fcpxml",
                (28, 40),
            ),
        ]
        for ai_path, user_path, counts in cases:
            report = LEARNER.build_report(ai_path, user_path)
            self.assertEqual(
                (
                    report["P1_rough_cut"]["ai_clip_count"],
                    report["P1_rough_cut"]["user_clip_count"],
                ),
                counts,
            )
            self.assertTrue(report["P1_rough_cut"]["changed"])
        c2897 = LEARNER.build_report(cases[0][0], cases[0][1])
        self.assertFalse(c2897["P0_media_project"]["changed"])

    def test_f1_orders_hard_gates_before_freeze(self):
        text = (SCRIPTS / "f1_粗剪验收冻结.sh").read_text(encoding="utf-8")
        order = [
            text.index("p1c_生成表达审查.py"),
            text.index("p1b_帧级验收.py"),
            text.index("p1q_决策质量闸门.py"),
            text.index('decision_contract.py\" freeze'),
            text.index('decision_contract.py\" verify'),
        ]
        self.assertEqual(order, sorted(order))


class DeliveryAndAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_unknown_color_requires_and_revalidates_final_cut_evidence(self):
        source = self.root / "unknown.mov"
        source.write_bytes(b"source-fingerprint")
        stat = source.stat()
        actual = {
            "path": str(source.resolve()),
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "duration": Fraction(10, 1),
            "width": 1920,
            "height": 1080,
            "rotation": 0,
            "frame_rate_raw": "60000/1001",
            "audio_sample_rate": 48000,
            "audio_channels": 2,
            "color_primaries": None,
            "color_transfer": None,
            "color_space": None,
        }
        with self.assertRaises(ValueError):
            fcpxml_color_space(actual)
        reference = self.root / "observed.fcpxml"
        reference.write_text(
            "<fcpxml><resources>"
            '<format id="r1" frameDuration="1001/60000s" width="1920" '
            'height="1080" colorSpace="1-1-1 (Rec. 709)"/>'
            '<asset id="r2" format="r1" duration="10s" audioRate="48000" '
            f'audioChannels="2"><media-rep src="{source.resolve().as_uri()}"/>'
            "</asset></resources></fcpxml>",
            encoding="utf-8",
        )
        contract = build_final_cut_color_contract(reference, actual)
        self.assertEqual(fcpxml_color_space(actual, contract), "1-1-1 (Rec. 709)")
        changed = {**actual, "mtime_ns": actual["mtime_ns"] + 1}
        with self.assertRaises(ValueError):
            fcpxml_color_space(changed, contract)
        reference.write_text(reference.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            fcpxml_color_space(actual, contract)

    def test_caption_projection_uses_segment_local_and_source_anchors(self):
        card = {
            "si": 0,
            "card_index": 0,
            "text": "测试",
            "sf": 0,
            "ef": 30,
            "local_sf": 0,
            "local_ef": 30,
            "source_sf": 600,
            "source_ef": 630,
        }
        result = project_card_timerange(
            card,
            {"tl_in_f": 0, "tl_out_f": 60},
            {"in_f": 600, "out_f": 660},
            2_000_000,
            1_000_000,
            "30/1",
        )
        self.assertEqual(result, {"start": 2_000_000, "duration": 500_000})
        with self.assertRaises(ValueError):
            project_card_timerange(
                {**card, "source_sf": 601},
                {"tl_in_f": 0, "tl_out_f": 60},
                {"in_f": 600, "out_f": 660},
                2_000_000,
                1_000_000,
                "30/1",
            )

    def test_p3_source_timing_is_not_penalized_by_p1_timeline_delta(self):
        base = {
            "norm": "同一字幕",
            "source": "same.mov",
            "source_anchors": [100.0],
            "styles": [],
            "effect": "Basic Title",
            "position": "0 -470",
        }
        generated = {"titles": [{**base, "timeline_start": 30.0, "timeline_end": 31.0}]}
        reference = {
            "titles": [
                {**base, "source_anchors": [103.0, 100.0], "timeline_start": 5.0, "timeline_end": 6.0}
            ]
        }
        _, _, timing, detail = SCORER.sequence_metrics(generated, reference)
        self.assertEqual(timing, 1.0)
        self.assertEqual(detail["timing_matches"], 1)

    def test_pause_cut_must_have_explicit_review_decision(self):
        left = {"words": [{"i": 3}]}
        right = {"words": [{"i": 4}]}
        with self.assertRaises(ValueError):
            CUT_REVIEW.edit_purpose(left, right, set(), set())
        self.assertEqual(
            CUT_REVIEW.edit_purpose(left, right, set(), {3}),
            "pause_compression",
        )


class ReviewStatusVocabularyTests(unittest.TestCase):
    """闸口 `review.status` 的词表必须与契约一致。

    2026-10-09 Windows 全链验收实测：`decision_contract._validate_basic_review`
    只接受 `reviewed` / `approved`，而 `agent_reviewed / human_reviewed /
    human_approved` 是它**算出来**写进 `decision_lock.json` 的 `review.level`。
    说明书和 `cut_review.样例.json` 当时把 level 词表写进了 `status` 字段，
    照抄模板会让 F1 冻结失败（报「尚未裁决」，与真实原因无关）。
    本测试锁住这个边界：文档/模板不得再混用两套词表。
    """

    REPO = ROOT.parents[2]
    TEMPLATES = REPO / "剪辑工作台/说明书/待填模板"
    MANUAL = REPO / "剪辑工作台/说明书/工序与闸口.md"
    LEVEL_WORDS = {"agent_reviewed", "human_reviewed", "human_approved"}
    PROBE_TIME = "2026-01-01T00:00:00+00:00"

    def _accepted(self, reviewer_type):
        """直接问契约哪些 status 能通过，避免测试与契约各写一份词表。"""
        accepted = set()
        for candidate in sorted(self.LEVEL_WORDS | {"reviewed", "approved", "pending"}):
            review = {
                "reviewer_type": reviewer_type,
                "status": candidate,
                "reviewed_by": "vocabulary-probe",
                "reviewed_at": self.PROBE_TIME,
            }
            try:
                decision_contract._validate_basic_review(review, "probe")
            except Exception:
                continue
            accepted.add(candidate)
        return accepted

    def test_contract_accepts_reviewed_and_rejects_level_words(self):
        self.assertEqual(self._accepted("agent"), {"reviewed"})
        self.assertEqual(self._accepted("human"), {"reviewed", "approved"})
        for word in self.LEVEL_WORDS:
            self.assertNotIn(word, self._accepted("agent"))

    def test_gate_templates_use_contract_status_vocabulary(self):
        accepted = self._accepted("agent") | self._accepted("human")
        seen = 0
        for path in sorted(self.TEMPLATES.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            review = data.get("review")
            if not isinstance(review, dict):
                continue
            seen += 1
            status = review.get("status")
            self.assertIn(status, accepted, f"{path.name} review.status 不在契约词表内")
            self.assertNotIn(
                status, self.LEVEL_WORDS,
                f"{path.name} 把 level 词表写进了 status 字段",
            )
        self.assertGreater(seen, 0, "没有扫描到任何带 review 的模板")

    def test_manual_does_not_teach_level_words_as_status(self):
        text = self.MANUAL.read_text(encoding="utf-8")
        for word in self.LEVEL_WORDS:
            self.assertNotIn(
                f'"status": "{word}"', text,
                f"说明书仍在教把 {word} 写进 review.status",
            )
        self.assertIn('"status": "reviewed"', text)


if __name__ == "__main__":
    unittest.main()


    unittest.main()
