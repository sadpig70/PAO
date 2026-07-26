import json
import unittest
from pathlib import Path

from tools.build_lwar4_generation2_calibration_suite import (
    TARGET,
    build_generation2_suite,
)
from tools.build_lwar4_reset_requalification_suite import (
    canonical_sha256,
    prompt_sha256,
)
from tools.run_heterogeneous_lwar_ab import verify_finite_json_answer


REPO = Path(__file__).resolve().parents[1]


class GenerationCalibrationSuiteTests(unittest.TestCase):
    def test_tracked_suite_matches_the_deterministic_builder(self):
        tracked = json.loads(TARGET.read_text(encoding="utf-8"))
        self.assertEqual(tracked, build_generation2_suite())

    def test_suite_targets_only_lwar4_generation2_with_identity_pending(self):
        suite = build_generation2_suite()
        self.assertEqual(
            suite["suite_id"], "lwar4-generation2-calibration-suite-v1"
        )
        self.assertEqual(suite["target"]["lwar_id"], "LWAR4")
        self.assertEqual(suite["target"]["required_generation"], 2)
        self.assertEqual(
            suite["target"]["identity_and_adapter_binding"],
            "pending_before_provider_execution",
        )
        self.assertEqual(suite["max_campaign_executions"], 1)
        self.assertFalse(suite["provider_receives_expected_answer"])

    def test_prompts_are_unique_nonoverlapping_and_objectively_verifiable(self):
        suite = build_generation2_suite()
        prompts = [task["prompt"] for task in suite["tasks"].values()]
        self.assertEqual(len(prompts), 27)
        self.assertEqual(len(prompts), len(set(prompts)))

        prior_prompts = set()
        for path in sorted((REPO / "benchmarks").glob("*suite*.json")):
            if path == TARGET:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            for task in value.get("tasks", {}).values():
                prompt = task.get("prompt")
                if isinstance(prompt, str):
                    prior_prompts.add(prompt_sha256(prompt))
        self.assertFalse(
            {prompt_sha256(prompt) for prompt in prompts} & prior_prompts
        )
        for task in suite["tasks"].values():
            self.assertEqual(
                verify_finite_json_answer(
                    task["prompt"], json.dumps(task["expected"])
                ),
                [],
            )

    def test_predecessor_is_bound_without_reuse(self):
        suite = build_generation2_suite()
        predecessor = json.loads(
            (
                REPO
                / "benchmarks"
                / "lwar4-reset-requalification-evidence-v2.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            suite["predecessor_evidence"]["sha256"],
            canonical_sha256(predecessor),
        )
        self.assertEqual(
            suite["predecessor_evidence"]["verdict"],
            "post_reset_requalification_failed_production_not_run",
        )
        self.assertFalse(suite["predecessor_evidence"]["reuse_allowed"])


if __name__ == "__main__":
    unittest.main()
