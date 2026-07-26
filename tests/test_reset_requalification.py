import unittest

from tools.build_lwar4_reset_requalification_suite import (
    PHASE_COUNTS,
    build_suite,
    unique_constraints,
)
from tools.run_lwar4_reset_requalification import grade


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
