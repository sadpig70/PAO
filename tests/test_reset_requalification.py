import json
import unittest
from pathlib import Path

from tools.build_lwar4_reset_requalification_suite import (
    PHASE_COUNTS,
    build_suite,
    unique_constraints,
)
from tools.run_heterogeneous_lwar_ab import verify_finite_json_answer
from tools.run_lwar4_reset_requalification import grade

REPO = Path(__file__).resolve().parents[1]


class ResetRequalificationSuiteTests(unittest.TestCase):
    def test_suite_is_unique_and_has_the_preregistered_phase_counts(self):
        suite = build_suite()
        prompts = [task["prompt"] for task in suite["tasks"].values()]
        self.assertEqual(len(prompts), sum(PHASE_COUNTS.values()))
        self.assertEqual(len(prompts), len(set(prompts)))
        for phase, count in PHASE_COUNTS.items():
            self.assertEqual(
                sum(task["phase"] == phase for task in suite["tasks"].values()),
                count,
            )

    def test_v2_suite_is_nonoverlapping_with_v1(self):
        v1 = build_suite()
        v2 = build_suite(campaign_version=2, seed_offset=1000)
        v1_prompts = {task["prompt"] for task in v1["tasks"].values()}
        v2_prompts = {task["prompt"] for task in v2["tasks"].values()}
        self.assertFalse(v1_prompts & v2_prompts)
        self.assertEqual(
            v2["suite_id"], "lwar4-reset-requalification-suite-v2"
        )
        for task in v2["tasks"].values():
            self.assertEqual(
                verify_finite_json_answer(
                    task["prompt"], json.dumps(task["expected"])
                ),
                [],
            )

    def test_v1_closed_negative_evidence_is_preserved(self):
        evidence = json.loads(
            (
                REPO
                / "benchmarks"
                / "lwar4-reset-requalification-evidence-v1.json"
            ).read_text(encoding="utf-8")
        )
        gate = evidence["recovery_gate"]
        self.assertEqual(
            evidence["verdict"],
            "recovery_gate_failed_circuit_preserved_open",
        )
        self.assertEqual(gate["lwar1_accepted"], 12)
        self.assertEqual(gate["lwar4_accepted"], 8)
        self.assertTrue(gate["telemetry_complete"])
        self.assertTrue(gate["circuit_unchanged"])
        self.assertTrue(gate["audit_healthy"])
        self.assertFalse(gate["passed"])
        self.assertTrue(all(value == 0 for value in gate["active_work"].values()))

    def test_generated_constraints_have_exactly_one_solution(self):
        ordering = "FADBEC"
        descriptions = unique_constraints(ordering, 123)
        suite = build_suite()
        task = next(iter(suite["tasks"].values()))
        self.assertTrue(descriptions)
        self.assertIn("Arrange A-", task["prompt"])
        self.assertEqual(
            grade(task, f'{{"answer":"{task["expected"]["answer"]}"}}')[
                "score"
            ],
            1,
        )

    def test_objective_grader_rejects_whitespace_and_wrong_order(self):
        task = next(iter(build_suite()["tasks"].values()))
        expected = task["expected"]["answer"]
        with_space = expected[:2] + " " + expected[2:]
        self.assertEqual(
            grade(task, f'{{"answer":"{with_space}"}}')["reason"],
            "objective_mismatch",
        )
        self.assertEqual(
            grade(task, f'{{"answer":"{expected[::-1]}"}}')["score"],
            0,
        )

    def test_each_generated_expected_order_satisfies_its_alphabet(self):
        for task in build_suite()["tasks"].values():
            expected = task["expected"]["answer"]
            alphabet = "".join(
                chr(ord("A") + index) for index in range(len(expected))
            )
            self.assertEqual("".join(sorted(expected)), alphabet)
            self.assertEqual(len(set(expected)), len(expected))


if __name__ == "__main__":
    unittest.main()
