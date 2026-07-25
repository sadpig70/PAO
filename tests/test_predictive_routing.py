import json
import tempfile
import unittest
from pathlib import Path

from pao_helpers import PaoTestCase
from pao_runtime.predictive_routing import (
    canonical_sha256,
    compile_routing_profile,
    make_routing_receipt,
    select_predictive_lwar,
    write_routing_receipt,
)
from pao_runtime.transport import FileTransport
from tools.run_predictive_lwar_router import experiment_observations


def observations():
    rows = []
    matrix = {
        "task-cal-logic-1": ("logic", {"LWAR1": (True, 20), "LWAR2": (True, 10)}),
        "task-cal-logic-2": ("logic", {"LWAR1": (True, 22), "LWAR2": (True, 12)}),
        "task-cal-code-1": ("code_review", {"LWAR1": (True, 20), "LWAR2": (False, 10)}),
        "task-cal-code-2": ("code_review", {"LWAR1": (True, 22), "LWAR2": (False, 12)}),
    }
    for task_id, (task_class, aliases) in matrix.items():
        for lwar_id, (accepted, tokens) in aliases.items():
            rows.append(
                {
                    "task_id": task_id,
                    "task_class": task_class,
                    "lwar_id": lwar_id,
                    "accepted": accepted,
                    "reported_tokens": tokens,
                }
            )
    return rows


class PredictivePolicyTests(unittest.TestCase):
    def setUp(self):
        self.profile = compile_routing_profile(
            observations(),
            profile_id="routing-profile-test",
            source_experiment_ids=["test"],
            min_observations=2,
            created_at="2026-07-25T00:00:00Z",
        )

    def test_selects_lowest_tokens_only_among_top_quality_candidates(self):
        logic = select_predictive_lwar(self.profile, "logic", ["LWAR1", "LWAR2"])
        code = select_predictive_lwar(self.profile, "code_review", ["LWAR1", "LWAR2"])
        self.assertEqual(logic["selected_lwar_id"], "LWAR2")
        self.assertEqual(code["selected_lwar_id"], "LWAR1")
        self.assertEqual(logic["reason"], "class_quality_qualified_lowest_tokens")

    def test_unknown_class_falls_back_to_global_quality_leader(self):
        decision = select_predictive_lwar(self.profile, "unseen", ["LWAR1", "LWAR2"])
        self.assertEqual(decision["selected_lwar_id"], "LWAR1")
        self.assertEqual(decision["reason"], "fallback_unknown_class")

    def test_low_support_falls_back_instead_of_optimizing_tokens(self):
        profile = compile_routing_profile(
            observations()[:2],
            profile_id="routing-profile-low-support",
            source_experiment_ids=["test"],
            min_observations=2,
            created_at="2026-07-25T00:00:00Z",
        )
        decision = select_predictive_lwar(profile, "logic", ["LWAR1", "LWAR2"])
        self.assertEqual(decision["reason"], "fallback_insufficient_support")

    def test_one_under_supported_profiled_candidate_blocks_class_optimization(self):
        rows = [
            row
            for row in observations()
            if not (row["task_id"] == "task-cal-logic-2" and row["lwar_id"] == "LWAR2")
        ]
        profile = compile_routing_profile(
            rows,
            profile_id="routing-profile-unbalanced-support",
            source_experiment_ids=["test"],
            min_observations=2,
            created_at="2026-07-25T00:00:00Z",
        )
        decision = select_predictive_lwar(profile, "logic", ["LWAR1", "LWAR2"])
        self.assertEqual(decision["selected_lwar_id"], "LWAR1")
        self.assertEqual(decision["reason"], "fallback_insufficient_support")

    def test_profile_compiler_default_requires_five_class_observations(self):
        profile = compile_routing_profile(
            observations(),
            profile_id="routing-profile-safe-default",
            source_experiment_ids=["test"],
            created_at="2026-07-25T00:00:00Z",
        )
        self.assertEqual(profile["policy"]["min_observations"], 5)
        decision = select_predictive_lwar(profile, "logic", ["LWAR1", "LWAR2"])
        self.assertEqual(decision["selected_lwar_id"], "LWAR1")
        self.assertEqual(decision["reason"], "fallback_insufficient_support")

    def test_quality_drop_above_one_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "between 0 and 1"):
            compile_routing_profile(
                observations(),
                profile_id="routing-profile-invalid-drop",
                source_experiment_ids=["test"],
                max_quality_drop=1.1,
            )

    def test_profile_hash_and_decision_are_deterministic(self):
        other = compile_routing_profile(
            list(reversed(observations())),
            profile_id="routing-profile-test",
            source_experiment_ids=["test"],
            min_observations=2,
            created_at="2026-07-25T00:00:00Z",
        )
        self.assertEqual(self.profile, other)
        self.assertEqual(canonical_sha256(self.profile), canonical_sha256(other))

    def test_calibration_task_cannot_be_receipt_target(self):
        decision = select_predictive_lwar(self.profile, "logic", ["LWAR1", "LWAR2"])
        with self.assertRaisesRegex(ValueError, "present in calibration"):
            make_routing_receipt(
                task_id="task-cal-logic-1",
                task_class="logic",
                profile=self.profile,
                decision=decision,
            )

    def test_receipt_replay_is_idempotent_and_conflict_fails(self):
        decision = select_predictive_lwar(self.profile, "logic", ["LWAR1", "LWAR2"])
        receipt = make_routing_receipt(
            task_id="task-heldout-1",
            task_class="logic",
            profile=self.profile,
            decision=decision,
            decided_at="2026-07-25T00:01:00Z",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, first = write_routing_receipt(root, receipt)
            _, replay = write_routing_receipt(root, {**receipt, "decided_at": "2026-07-25T00:02:00Z"})
            self.assertEqual(first, replay)
            conflict = json.loads(json.dumps(receipt))
            conflict["reason"] = "different"
            with self.assertRaisesRegex(RuntimeError, "conflicting routing receipt"):
                write_routing_receipt(root, conflict)
            self.assertTrue(path.is_file())

    def test_experiment_records_become_provider_neutral_calibration_observations(self):
        experiment = {
            "records": [
                {
                    "alias": "LWAR1",
                    "task": "C1",
                    "task_class": "logic",
                    "grade": {"score": 1},
                    "provider": {"reported_tokens": 123},
                }
            ]
        }
        self.assertEqual(
            experiment_observations(experiment, source_prefix="new"),
            [
                {
                    "task_id": "task-new-c1",
                    "task_class": "logic",
                    "lwar_id": "LWAR1",
                    "accepted": True,
                    "reported_tokens": 123,
                }
            ],
        )


class PredictiveOAIntegrationTests(PaoTestCase):
    def test_predictive_send_persists_and_audits_route_before_publish(self):
        profile = compile_routing_profile(
            observations(),
            profile_id="routing-profile-integration",
            source_experiment_ids=["test"],
            min_observations=2,
            created_at="2026-07-25T00:00:00Z",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, first = self.register_lwar(root)
            _, second = self.register_lwar(root)
            transport = FileTransport(root)
            transport.write_heartbeat(first, "idle", None)
            transport.write_heartbeat(second, "idle", None)
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            task_path = root / "task.json"
            task_path.write_text(
                json.dumps({"task_id": "task-heldout-route", "goal": "Route me"}),
                encoding="utf-8",
            )
            _, published = self.run_module(
                "pao_runtime.oa_cli",
                "send",
                "--auto",
                "--routing-profile",
                str(profile_path),
                "--routing-class",
                "logic",
                "--task-file",
                str(task_path),
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(published["lwar_id"], "LWAR2")
            receipt_path = Path(published["routing_receipt"])
            self.assertTrue(receipt_path.is_file())
            events = [
                json.loads(line)
                for line in (root / "var" / "audit" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            names = [event["event"] for event in events]
            self.assertLess(names.index("routing_decided"), names.index("task_published"))

    def test_predictive_arguments_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            task_path = root / "task.json"
            task_path.write_text(json.dumps({"goal": "bad flags"}), encoding="utf-8")
            completed, _ = self.run_module(
                "pao_runtime.oa_cli",
                "send",
                "--auto",
                "--routing-class",
                "logic",
                "--task-file",
                str(task_path),
                "--root",
                str(root),
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("must be used together", completed.stderr)


if __name__ == "__main__":
    unittest.main()
