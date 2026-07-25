import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools.build_canary_online_suite import build_suite
from tools.build_lwar4_remediation_suite import (
    build_registration as build_remediation_registration,
    build_suite as build_remediation_suite,
    canonical_sha256,
)
from tools.run_heterogeneous_lwar_ab import (
    TASKS,
    build_opencode_command,
    build_assignment,
    build_report,
    combine_provider_results,
    grade,
    load_task_suite,
    read_kimi_usage,
    reported_tokens,
    routing_upper_bound,
    run_provider_command,
    run_provider_with_retry,
    verify_finite_json_answer,
)


class HeterogeneousABHarnessTests(unittest.TestCase):
    def test_finite_answer_verifier_uses_prompt_not_answer_key(self):
        prompt = build_suite()["tasks"]["BO04"]["prompt"]
        self.assertEqual(
            verify_finite_json_answer(
                prompt,
                '{"selection":["B","C","E"],"value":29,"cost":8,"risk":6}',
            ),
            [],
        )
        self.assertEqual(
            verify_finite_json_answer(
                prompt,
                '{"selection":["B","C","E"],"value":21,"cost":8,"risk":3}',
            ),
            ["aggregate_mismatch", "selection_not_global_optimum"],
        )
        ordering = build_suite()["tasks"]["CO08"]["prompt"]
        self.assertEqual(
            verify_finite_json_answer(ordering, '{"answer":"CDEAB"}'),
            ["ordering_constraint_violation"],
        )

    def test_internal_verification_attempts_preserve_total_tokens(self):
        first = {
            "adapter": "opencode",
            "ok": True,
            "duration_s": 2.0,
            "error": None,
            "answer": "{}",
            "metrics": {"usage": {"total": 10}, "telemetry_complete": True},
        }
        second = {
            **first,
            "duration_s": 3.0,
            "metrics": {"usage": {"total": 20}, "telemetry_complete": True},
        }
        combined = combine_provider_results(first, second)
        self.assertEqual(combined["duration_s"], 5.0)
        self.assertEqual(reported_tokens(combined["metrics"]), 30)
        self.assertTrue(combined["metrics"]["telemetry_complete"])

    def test_remediation_suite_is_unique_nonoverlapping_and_preregistered(self):
        prior = build_suite()
        suite = build_remediation_suite()
        registration = build_remediation_registration(suite)
        prompts = [task["prompt"] for task in suite["tasks"].values()]
        prior_prompts = {
            task["prompt"] for task in prior["tasks"].values()
        }
        counts = {}
        for task in suite["tasks"].values():
            counts[task["task_class"]] = (
                counts.get(task["task_class"], 0) + 1
            )
        self.assertEqual(len(prompts), len(set(prompts)))
        self.assertFalse(prior_prompts & set(prompts))
        self.assertEqual(
            counts,
            {"bounded_optimization": 12, "constraint_ordering": 12},
        )
        self.assertEqual(registration["suite_sha256"], canonical_sha256(suite))
        self.assertTrue(registration["sealed_before_provider_execution"])

    def test_remediation_evidence_binds_preregistered_contract(self):
        repo = Path(__file__).parents[1]
        registration = json.loads(
            (
                repo
                / "benchmarks"
                / "lwar4-remediation-preregistration-v1.json"
            ).read_text(encoding="utf-8")
        )
        evidence = json.loads(
            (
                repo / "benchmarks" / "lwar4-remediation-evidence-v1.json"
            ).read_text(encoding="utf-8")
        )
        for field in (
            "suite_sha256",
            "answer_key_sha256",
            "adapter_contract_sha256",
        ):
            self.assertEqual(evidence[field], registration[field])
        self.assertEqual(evidence["blind_run"]["provider_calls"]["total"], 96)
        self.assertEqual(evidence["blind_run"]["online_observations"], 94)
        self.assertEqual(
            evidence["promoted_classes"], ["constraint_ordering"]
        )
        self.assertEqual(
            evidence["verdict"],
            "constraint_ordering_promoted_bounded_optimization_blocked",
        )

    def test_production_canary_records_rejection_and_sticky_fallback(self):
        repo = Path(__file__).parents[1]
        registration = json.loads(
            (
                repo
                / "benchmarks"
                / "lwar4-production-canary-preregistration-v1.json"
            ).read_text(encoding="utf-8")
        )
        evidence = json.loads(
            (
                repo
                / "benchmarks"
                / "lwar4-production-canary-evidence-v1.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            evidence["contract"]["prompt_sha256"],
            registration["task"]["prompt_sha256"],
        )
        self.assertEqual(
            evidence["contract"]["expected_answer_sha256"],
            registration["task"]["expected_answer_sha256"],
        )
        self.assertEqual(
            evidence["contract"]["profile_sha256"],
            registration["routing_contract"]["profile_sha256"],
        )
        self.assertEqual(
            evidence["contract"]["policy_sha256"],
            registration["routing_contract"]["policy_sha256"],
        )
        live = evidence["production_canary"]
        self.assertEqual(live["selected_lwar_id"], "LWAR4")
        self.assertEqual(live["route_mode"], "live")
        self.assertEqual(live["semantic_verdict"], "rejected")
        self.assertEqual(evidence["circuit"]["status"], "open")
        self.assertEqual(evidence["circuit"]["reset_count"], 0)
        fallback = evidence["fallback_probe"]
        self.assertEqual(fallback["selected_lwar_id"], "LWAR1")
        self.assertEqual(fallback["route_mode"], "circuit_open")
        self.assertEqual(fallback["semantic_verdict"], "accepted")
        self.assertEqual(
            evidence["final_routing_state"]["promoted_classes"], []
        )
        self.assertEqual(
            evidence["verdict"],
            "production_canary_rejected_sticky_fallback_verified",
        )

    def test_opencode_adapter_adds_generic_private_verification(self):
        command = build_opencode_command(
            Path("opencode.exe"),
            Path("work"),
            'Return {"answer":"<value>"}.',
        )
        self.assertEqual(command[command.index("--variant") + 1], "high")
        self.assertIn("verify every stated constraint", command[-1])
        self.assertTrue(command[-1].endswith('Return {"answer":"<value>"}.'))
        self.assertNotIn("DBAEC", command[-1])

    def test_provider_timeout_becomes_retryable_result(self):
        with patch(
            "tools.run_heterogeneous_lwar_ab.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["provider"], 180),
        ):
            result = run_provider_command(
                "test",
                ["provider"],
                lambda stdout, stderr: (stdout, {}),
                Path("."),
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout_180s")
        self.assertFalse(result["metrics"]["telemetry_complete"])

    def test_online_canary_suite_is_balanced_and_objective(self):
        suite = build_suite()
        tasks = suite["tasks"]
        counts = {}
        for task in tasks.values():
            counts[task["task_class"]] = counts.get(task["task_class"], 0) + 1
            self.assertTrue(task["expected"])
            self.assertIn("Return one JSON object", task["prompt"])
        self.assertEqual(
            counts,
            {
                "bounded_optimization": 10,
                "code_review": 10,
                "constraint_ordering": 10,
            },
        )

    def test_online_canary_evidence_binds_the_published_suite(self):
        repo = Path(__file__).parents[1]
        suite_path = repo / "benchmarks" / "canary-online-suite-v1.json"
        evidence = json.loads(
            (
                repo / "benchmarks" / "canary-online-evidence-v1.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            evidence["suite_sha256"],
            hashlib.sha256(
                json.dumps(
                    json.loads(suite_path.read_text(encoding="utf-8")),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )
        self.assertEqual(evidence["provider_calls"]["total"], 120)
        self.assertEqual(evidence["online_observations"], 117)
        self.assertEqual(
            evidence["verdict"], "current_evidence_remains_shadow_only"
        )

    def test_objective_graders_accept_only_exact_answers(self):
        answers = {
            "T1": '{"answer":"DBAEC","reason":"unique"}',
            "T2": '{"defects":["B1","B2","B3"],"fix":"validate and avoid shared default"}',
            "T3": '{"selection":["A","B"],"value":17,"cost":10,"risk":6}',
        }
        for task_name, answer in answers.items():
            with self.subTest(task=task_name):
                self.assertEqual(grade(task_name, answer)["score"], 1)
                self.assertTrue(
                    TASKS[task_name]["expected"].keys()
                    <= grade(task_name, answer)["parsed"].keys()
                )

    def test_invalid_or_objectively_wrong_answer_fails(self):
        self.assertEqual(grade("T1", "not json")["score"], 0)
        self.assertEqual(
            grade("T3", '{"selection":["C","D","E"],"value":16,"cost":10,"risk":6}')[
                "score"
            ],
            0,
        )

    def test_report_does_not_claim_complete_tokens_when_one_adapter_lacks_them(self):
        records = []
        for alias in ("LWAR1", "LWAR2", "LWAR3", "LWAR4"):
            records.append(
                {
                    "alias": alias,
                    "task": "T1",
                    "grade": {"score": 1},
                    "provider": {
                        "ok": True,
                        "duration_s": 1.0,
                        "reported_tokens": 10,
                        "metrics": {
                            "telemetry_complete": alias != "LWAR3",
                        },
                    },
                }
            )
        report = build_report(
            Path("experiment"),
            records,
            {"status": "healthy"},
        )
        self.assertIn("token_efficiency_partial", report)
        self.assertNotIn("token_efficiency_measured`", report)

    def test_kimi_usage_reads_only_nonempty_wire_status(self):
        events = [
            {"message": {"type": "StatusUpdate", "payload": {"token_usage": None}}},
            {
                "message": {
                    "type": "StatusUpdate",
                    "payload": {
                        "token_usage": {
                            "input_other": 100,
                            "input_cache_read": 200,
                            "output": 30,
                        }
                    },
                }
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "session.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(
                    "wire.jsonl",
                    "".join(json.dumps(event) + "\n" for event in events),
                )
                bundle.writestr("logs/kimi.log", "must not be read")
            self.assertEqual(
                read_kimi_usage(archive),
                {
                    "input_other": 100,
                    "input_cache_read": 200,
                    "output": 30,
                },
            )

    def test_bounded_retry_keeps_failed_attempt_telemetry(self):
        outcomes = [
            {
                "ok": False,
                "duration_s": 1.0,
                "error": "empty_answer",
                "answer": "",
                "metrics": {"usage": {"total": 10}, "telemetry_complete": True},
            },
            {
                "ok": True,
                "duration_s": 2.0,
                "error": None,
                "answer": "{}",
                "metrics": {"usage": {"total": 20}, "telemetry_complete": True},
            },
        ]

        def runner(prompt, work_dir):
            _ = prompt, work_dir
            return outcomes.pop(0)

        result = run_provider_with_retry(runner, "prompt", Path("."), max_attempts=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["duration_s"], 3.0)
        self.assertEqual(result["metrics"]["attempt_count"], 2)
        self.assertEqual(len(result["metrics"]["attempts"]), 2)
        self.assertTrue(result["metrics"]["telemetry_complete"])
        self.assertEqual(reported_tokens(result["metrics"]), 30)

    def test_reported_tokens_normalizes_adapter_usage_shapes(self):
        self.assertEqual(
            reported_tokens(
                {
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 3,
                        "reasoning_output_tokens": 2,
                    }
                }
            ),
            15,
        )
        self.assertEqual(
            reported_tokens(
                {
                    "usage": {
                        "input_other": 10,
                        "input_cache_read": 20,
                        "input_cache_creation": 5,
                        "output": 3,
                    }
                }
            ),
            38,
        )
        self.assertEqual(reported_tokens({"usage": {"total": 99}}), 99)

    def test_routing_upper_bound_is_labeled_posthoc(self):
        records = []
        matrix = {
            "LWAR1": {"T1": (1, 20), "T2": (1, 20)},
            "LWAR2": {"T1": (1, 10), "T2": (0, 10)},
        }
        for alias, tasks in matrix.items():
            for task_name, (score, tokens) in tasks.items():
                records.append(
                    {
                        "alias": alias,
                        "task": task_name,
                        "grade": {"score": score},
                        "provider": {"reported_tokens": tokens},
                    }
                )
        analysis = routing_upper_bound(records)
        self.assertEqual(analysis["best_full_quality_single"]["reported_tokens"], 40)
        self.assertEqual(analysis["posthoc_oracle"]["reported_tokens"], 30)
        self.assertEqual(analysis["reported_token_savings_percent"], 25.0)
        self.assertEqual(
            analysis["claim_scope"],
            "posthoc_upper_bound_not_heldout_routing_proof",
        )

    def test_custom_task_suite_is_validated_and_assigned_deterministically(self):
        suite = {
            "schema_version": "pao.benchmark-suite.v1",
            "tasks": {
                "C1": {
                    "task_class": "logic",
                    "prompt": "Return JSON.",
                    "expected": {"answer": "A"},
                },
                "C2": {
                    "task_class": "code_review",
                    "prompt": "Return JSON.",
                    "expected": {"answer": "B"},
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "suite.json"
            path.write_text(json.dumps(suite), encoding="utf-8")
            loaded = load_task_suite(path)
        assignment = build_assignment(list(loaded))
        self.assertEqual(assignment["LWAR1"], ["C1", "C2"])
        self.assertEqual(assignment["LWAR2"], ["C2", "C1"])
        self.assertEqual(set(assignment["LWAR4"]), {"C1", "C2"})


if __name__ == "__main__":
    unittest.main()
