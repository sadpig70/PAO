import json
import tempfile
import unittest
from pathlib import Path

from pao_helpers import PaoTestCase
from pao_runtime.canary_routing import (
    empty_circuit_state,
    load_circuit_state,
    load_routing_observations,
    make_routing_observation,
    refresh_circuits,
    reset_circuit,
    select_confidence_canary,
    wilson_interval,
    write_circuit_state,
    write_routing_observation,
)
from pao_runtime.contracts import ContractError
from pao_runtime.predictive_routing import canonical_sha256, compile_routing_profile
from pao_runtime.transport import FileTransport
from tools.run_canary_router_evidence import analyze, heldout_observations


def calibration_observations():
    rows = []
    matrix = {
        "task-cal-logic-1": (
            "logic",
            {"LWAR1": (True, 20), "LWAR2": (True, 10)},
        ),
        "task-cal-logic-2": (
            "logic",
            {"LWAR1": (True, 22), "LWAR2": (True, 12)},
        ),
        "task-cal-code-1": (
            "code_review",
            {"LWAR1": (True, 20), "LWAR2": (False, 10)},
        ),
        "task-cal-code-2": (
            "code_review",
            {"LWAR1": (True, 22), "LWAR2": (False, 12)},
        ),
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


def canary_policy(**overrides):
    policy = {
        "schema_version": "pao.canary-policy.v1",
        "policy_id": "canary-policy-test",
        "created_at": "2026-07-25T00:00:00Z",
        "min_accepted_observations": 10,
        "confidence_z": 1.96,
        "max_quality_drop": 0.0,
        "drift_window": 10,
        "max_drift_drop": 0.0,
        "trip_on_rejection": True,
    }
    policy.update(overrides)
    return policy


def eligible_identities():
    return {
        alias: {
            "instance_id": f"lwar-instance-{index:032x}",
            "generation": 1,
            "registry_version": 1,
        }
        for index, alias in enumerate(("LWAR1", "LWAR2"), start=1)
    }


def online_observation(
    index,
    lwar_id,
    accepted=True,
    *,
    task_class="logic",
    route_mode="shadow",
    generation=1,
):
    identity = eligible_identities()[lwar_id]
    binding = {
        "task_id": f"task-online-{index}",
        "task_class": task_class,
        "lwar_id": lwar_id,
        "instance_id": identity["instance_id"],
        "generation": generation,
        "registry_version": identity["registry_version"],
        "receipt_id": f"canary-routing-{index:032x}",
        "route_mode": route_mode,
        "accepted": accepted,
        "reported_tokens": 10 + index,
        "validation_sha256": f"{index:064x}",
    }
    return {
        "schema_version": "pao.routing-observation.v1",
        "observation_id": (
            f"routing-observation-{canonical_sha256(binding)[:32]}"
        ),
        "observed_at": f"2026-07-25T00:{index // 60:02d}:{index % 60:02d}Z",
        **binding,
    }


class CanaryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.profile = compile_routing_profile(
            calibration_observations(),
            profile_id="routing-profile-canary-test",
            source_experiment_ids=["test"],
            min_observations=2,
            created_at="2026-07-25T00:00:00Z",
        )

    def test_wilson_interval_is_bounded_and_deterministic(self):
        self.assertEqual(wilson_interval(0, 0), (0.0, 1.0))
        lower, upper = wilson_interval(10, 10)
        self.assertAlmostEqual(lower, 0.7224598312333834)
        self.assertEqual(upper, 1.0)

    def test_policy_floor_below_ten_fails_schema(self):
        with self.assertRaises(ContractError):
            select_confidence_canary(
                self.profile,
                canary_policy(min_accepted_observations=9),
                [],
                empty_circuit_state(),
                "logic",
                ["LWAR1", "LWAR2"],
                eligible_identities(),
            )

    def test_production_stays_on_incumbent_before_ten_accepted(self):
        decision = select_confidence_canary(
            self.profile,
            canary_policy(),
            [],
            empty_circuit_state(),
            "logic",
            ["LWAR1", "LWAR2"],
            eligible_identities(),
        )
        self.assertEqual(decision["incumbent_lwar_id"], "LWAR1")
        self.assertEqual(decision["candidate_lwar_id"], "LWAR2")
        self.assertEqual(decision["selected_lwar_id"], "LWAR1")
        self.assertEqual(decision["route_mode"], "leader")
        self.assertEqual(decision["reason"], "shadow_insufficient_accepted")

    def test_explicit_shadow_executes_candidate_before_promotion(self):
        decision = select_confidence_canary(
            self.profile,
            canary_policy(),
            [],
            empty_circuit_state(),
            "logic",
            ["LWAR1", "LWAR2"],
            eligible_identities(),
            shadow_execution=True,
        )
        self.assertEqual(decision["selected_lwar_id"], "LWAR2")
        self.assertEqual(decision["route_mode"], "shadow")

    def test_explicit_shadow_target_collects_non_candidate_alias(self):
        decision = select_confidence_canary(
            self.profile,
            canary_policy(),
            [],
            empty_circuit_state(),
            "code_review",
            ["LWAR1", "LWAR2"],
            eligible_identities(),
            shadow_lwar_id="LWAR2",
        )
        self.assertIsNone(decision["candidate_lwar_id"])
        self.assertEqual(decision["selected_lwar_id"], "LWAR2")
        self.assertEqual(decision["route_mode"], "shadow")
        self.assertEqual(decision["reason"], "explicit_shadow_target")

    def test_balanced_ten_accepted_and_confidence_promote_candidate(self):
        rows = []
        for index in range(1, 11):
            rows.append(online_observation(index, "LWAR1", route_mode="leader"))
            rows.append(online_observation(index + 20, "LWAR2"))
        decision = select_confidence_canary(
            self.profile,
            canary_policy(),
            rows,
            empty_circuit_state(),
            "logic",
            ["LWAR1", "LWAR2"],
            eligible_identities(),
        )
        self.assertTrue(decision["confidence"]["balanced_accepted_ready"])
        self.assertEqual(decision["selected_lwar_id"], "LWAR2")
        self.assertEqual(decision["route_mode"], "live")

    def test_accepted_floor_does_not_override_confidence_noninferiority(self):
        rows = []
        for index in range(1, 11):
            rows.append(online_observation(index, "LWAR1", route_mode="leader"))
            rows.append(online_observation(index + 20, "LWAR2"))
        rows.append(online_observation(50, "LWAR2", accepted=False))
        decision = select_confidence_canary(
            self.profile,
            canary_policy(),
            rows,
            empty_circuit_state(),
            "logic",
            ["LWAR1", "LWAR2"],
            eligible_identities(),
        )
        self.assertTrue(decision["confidence"]["balanced_accepted_ready"])
        self.assertEqual(decision["selected_lwar_id"], "LWAR1")
        self.assertEqual(decision["reason"], "shadow_confidence_not_qualified")

    def test_previous_generation_observations_do_not_count_toward_promotion(self):
        rows = []
        for index in range(1, 11):
            rows.append(online_observation(index, "LWAR1", route_mode="leader"))
            stale = online_observation(index + 20, "LWAR2", generation=2)
            rows.append(stale)
        decision = select_confidence_canary(
            self.profile,
            canary_policy(),
            rows,
            empty_circuit_state(),
            "logic",
            ["LWAR1", "LWAR2"],
            eligible_identities(),
        )
        self.assertFalse(decision["confidence"]["balanced_accepted_ready"])
        self.assertEqual(decision["selected_lwar_id"], "LWAR1")

    def test_open_circuit_forces_incumbent_even_for_explicit_shadow(self):
        state = empty_circuit_state()
        state["updated_at"] = "2026-07-25T00:10:00Z"
        state["circuits"]["logic::LWAR2"] = {
            "task_class": "logic",
            "lwar_id": "LWAR2",
            "status": "open",
            "opened_at": "2026-07-25T00:10:00Z",
            "reason": "candidate_rejected",
            "trigger_observation_id": "routing-observation-" + "1" * 32,
            "policy_sha256": "a" * 64,
        }
        decision = select_confidence_canary(
            self.profile,
            canary_policy(),
            [],
            state,
            "logic",
            ["LWAR1", "LWAR2"],
            eligible_identities(),
            shadow_execution=True,
        )
        self.assertEqual(decision["selected_lwar_id"], "LWAR1")
        self.assertEqual(decision["route_mode"], "circuit_open")


class CanaryCircuitTests(unittest.TestCase):
    def test_rejected_live_observation_opens_sticky_circuit(self):
        row = online_observation(1, "LWAR2", accepted=False, route_mode="live")
        state, events = refresh_circuits(
            canary_policy(),
            [row],
            empty_circuit_state(),
            opened_at="2026-07-25T00:10:00Z",
        )
        self.assertEqual(events[0]["reason"], "candidate_rejected")
        replay, replay_events = refresh_circuits(
            canary_policy(), [row], state, opened_at="2026-07-25T00:20:00Z"
        )
        self.assertEqual(replay, state)
        self.assertEqual(replay_events, [])

    def test_shadow_window_confidence_drift_opens_circuit(self):
        rows = [
            online_observation(index, "LWAR2", accepted=index <= 10)
            for index in range(1, 21)
        ]
        state, events = refresh_circuits(
            canary_policy(),
            rows,
            empty_circuit_state(),
            opened_at="2026-07-25T00:30:00Z",
        )
        self.assertEqual(events[0]["reason"], "confidence_drift")
        self.assertIn("logic::LWAR2", state["circuits"])

    def test_explicit_reset_creates_watermark_for_historical_rejection(self):
        row = online_observation(1, "LWAR2", accepted=False, route_mode="live")
        state, _ = refresh_circuits(
            canary_policy(),
            [row],
            empty_circuit_state(),
            opened_at="2026-07-25T00:10:00Z",
        )
        reset = reset_circuit(
            state,
            task_class="logic",
            lwar_id="LWAR2",
            reason="operator reviewed failure",
            decided_by="oa-test",
            reset_at="2026-07-25T00:20:00Z",
        )
        refreshed, events = refresh_circuits(canary_policy(), [row], reset)
        self.assertEqual(events, [])
        self.assertNotIn("logic::LWAR2", refreshed["circuits"])


class CanaryObservationTests(unittest.TestCase):
    def test_observation_binds_receipt_and_recorded_validation(self):
        receipt = {
            "schema_version": "pao.canary-routing-receipt.v1",
            "receipt_id": "canary-routing-" + "0" * 32,
            "decided_at": "2026-07-25T00:00:00Z",
            "task_id": "task-online-observation",
            "task_class": "logic",
            "profile_sha256": "1" * 64,
            "policy_sha256": canonical_sha256(canary_policy()),
            "observations_sha256": "3" * 64,
            "circuit_state_sha256": "4" * 64,
            "selected_lwar_id": "LWAR2",
            "selected_instance_id": "lwar-instance-" + "2".zfill(32),
            "selected_generation": 1,
            "selected_registry_version": 1,
            "incumbent_lwar_id": "LWAR1",
            "candidate_lwar_id": "LWAR2",
            "eligible_lwar_ids": ["LWAR1", "LWAR2"],
            "route_mode": "shadow",
            "reason": "shadow_insufficient_accepted",
            "decision": {
                "policy": canary_policy(),
                "class_stats": {},
                "global_stats": {},
                "confidence": {},
            },
        }
        receipt_binding = {
            key: receipt[key]
            for key in (
                "task_id",
                "task_class",
                "profile_sha256",
                "policy_sha256",
                "observations_sha256",
                "circuit_state_sha256",
                "selected_lwar_id",
                "selected_instance_id",
                "selected_generation",
                "selected_registry_version",
                "incumbent_lwar_id",
                "candidate_lwar_id",
                "eligible_lwar_ids",
                "route_mode",
                "reason",
            )
        }
        receipt["receipt_id"] = (
            f"canary-routing-{canonical_sha256(receipt_binding)[:32]}"
        )
        validation = {
            "schema_version": "pao.validation-decision.v1",
            "verdict": "ready_for_oa_review",
            "semantic_verdict": "accepted",
            "reason": "objective grader passed",
            "checks": {},
            "criteria": [],
            "artifact_verification": {
                "verified": True,
                "checked": 0,
                "failures": [],
            },
            "decided_by": "oa-test",
            "decided_at": "2026-07-25T00:01:00Z",
        }
        observation = make_routing_observation(receipt, validation, 123)
        self.assertTrue(observation["accepted"])
        self.assertEqual(observation["lwar_id"], "LWAR2")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, first = write_routing_observation(root, observation)
            _, replay = write_routing_observation(root, observation)
            self.assertEqual(first, replay)
            conflict = {**observation, "reported_tokens": 124}
            with self.assertRaisesRegex(
                ContractError, "observation id binding mismatch"
            ):
                write_routing_observation(root, conflict)

    def test_evidence_analyzer_keeps_small_panel_in_shadow(self):
        profile = compile_routing_profile(
            calibration_observations(),
            profile_id="routing-profile-evidence-test",
            source_experiment_ids=["test"],
            min_observations=2,
            created_at="2026-07-25T00:00:00Z",
        )
        experiment = {
            "records": [
                {
                    "alias": "LWAR2",
                    "task": "H1",
                    "task_class": "logic",
                    "grade": {"score": 1},
                    "provider": {
                        "reported_tokens": 9,
                        "started_at": "2026-07-25T00:01:00Z",
                    },
                },
                {
                    "alias": "LWAR1",
                    "task": "H1",
                    "task_class": "logic",
                    "grade": {"score": 1},
                    "provider": {
                        "reported_tokens": None,
                        "started_at": "2026-07-25T00:01:01Z",
                    },
                },
            ]
        }
        rows, skipped = heldout_observations(experiment, source="heldout")
        report = analyze(profile, canary_policy(), rows)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(report["verdict"], "current_evidence_remains_shadow_only")


class CanaryOAIntegrationTests(PaoTestCase):
    def setUp(self):
        self.profile = compile_routing_profile(
            calibration_observations(),
            profile_id="routing-profile-canary-integration",
            source_experiment_ids=["test"],
            min_observations=2,
            created_at="2026-07-25T00:00:00Z",
        )

    def prepare(self, root):
        _, first = self.register_lwar(root)
        _, second = self.register_lwar(root)
        transport = FileTransport(root)
        transport.write_heartbeat(first, "idle", None)
        transport.write_heartbeat(second, "idle", None)
        profile_path = root / "profile.json"
        policy_path = root / "policy.json"
        profile_path.write_text(json.dumps(self.profile), encoding="utf-8")
        policy_path.write_text(json.dumps(canary_policy()), encoding="utf-8")
        return first, second, profile_path, policy_path

    def send(self, root, task_id, profile_path, policy_path, *extra, expected=0):
        task_path = root / f"{task_id}.json"
        task_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "goal": "Evaluate a side-effect-free task",
                    "completion_criteria": ["Return evidence"],
                    "permissions": {
                        "read": [str(root)],
                        "write": [],
                        "network": False,
                    },
                }
            ),
            encoding="utf-8",
        )
        return self.run_module(
            "pao_runtime.oa_cli",
            "send",
            "--auto",
            "--routing-profile",
            str(profile_path),
            "--routing-class",
            "logic",
            "--canary-policy",
            str(policy_path),
            *extra,
            "--task-file",
            str(task_path),
            "--root",
            str(root),
            expected=expected,
        )

    def test_production_and_explicit_shadow_routes_are_evidence_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, profile_path, policy_path = self.prepare(root)
            _, production = self.send(
                root, "task-canary-production", profile_path, policy_path
            )
            self.assertEqual(production["lwar_id"], "LWAR1")
            self.assertEqual(production["routing_mode"], "leader")
            self.assertEqual(production["routing_candidate_lwar_id"], "LWAR2")
            _, shadow = self.send(
                root,
                "task-canary-shadow",
                profile_path,
                policy_path,
                "--routing-shadow",
            )
            self.assertEqual(shadow["lwar_id"], "LWAR2")
            self.assertEqual(shadow["routing_mode"], "shadow")

    def test_unsafe_shadow_permissions_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, _, profile_path, policy_path = self.prepare(root)
            task_path = root / "unsafe.json"
            task_path.write_text(
                json.dumps(
                    {
                        "task_id": "task-canary-unsafe",
                        "goal": "Unsafe shadow",
                        "permissions": {
                            "read": [str(root)],
                            "write": [str(root)],
                            "network": False,
                        },
                    }
                ),
                encoding="utf-8",
            )
            completed, _ = self.run_module(
                "pao_runtime.oa_cli",
                "send",
                "--auto",
                "--routing-profile",
                str(profile_path),
                "--routing-class",
                "logic",
                "--canary-policy",
                str(policy_path),
                "--routing-shadow",
                "--task-file",
                str(task_path),
                "--root",
                str(root),
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("permissions.write=[]", completed.stderr)

    def test_validate_records_shadow_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, second, profile_path, policy_path = self.prepare(root)
            _, published = self.send(
                root,
                "task-canary-observed",
                profile_path,
                policy_path,
                "--routing-shadow",
            )
            self.watch_once(root, second, expected=0)
            self.complete_task(root, second, published["task_id"])
            self.run_module(
                "pao_runtime.oa_cli",
                "collect",
                "--lwar-id",
                "LWAR2",
                "--root",
                str(root),
                expected=0,
            )
            _, report = self.run_module(
                "pao_runtime.oa_cli",
                "validate",
                "--task-id",
                published["task_id"],
                "--record",
                "--decision",
                "accepted",
                "--reason",
                "objective grader passed",
                "--routing-reported-tokens",
                "123",
                "--root",
                str(root),
                expected=0,
            )
            self.assertIsNotNone(report["routing_observation"])
            observations = load_routing_observations(root)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0]["route_mode"], "shadow")
            self.assertEqual(load_circuit_state(root), empty_circuit_state())
            receipt_path = Path(published["routing_receipt"])
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["decision"]["policy"]["min_accepted_observations"] = 11
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(
                ContractError, "receipt policy binding mismatch"
            ):
                load_routing_observations(root)
            receipt["decision"]["policy"]["min_accepted_observations"] = 10
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            observation_path = Path(report["routing_observation"])
            observation = json.loads(
                observation_path.read_text(encoding="utf-8")
            )
            observation["accepted"] = False
            observation_path.write_text(json.dumps(observation), encoding="utf-8")
            with self.assertRaisesRegex(
                ContractError, "observation id binding mismatch"
            ):
                load_routing_observations(root)
            observation["accepted"] = True
            observation_path.write_text(json.dumps(observation), encoding="utf-8")
            ledger_path = next((root / "var" / "tasks").glob("*/*.json"))
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            ledger["validation"]["reason"] = "tampered"
            ledger_path.write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaisesRegex(
                ContractError, "validation binding mismatch"
            ):
                load_routing_observations(root)

    def test_routing_circuit_reset_command_is_reason_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = empty_circuit_state()
            state["updated_at"] = "2026-07-25T00:10:00Z"
            state["circuits"]["logic::LWAR2"] = {
                "task_class": "logic",
                "lwar_id": "LWAR2",
                "status": "open",
                "opened_at": "2026-07-25T00:10:00Z",
                "reason": "candidate_rejected",
                "trigger_observation_id": "routing-observation-" + "1" * 32,
                "policy_sha256": "a" * 64,
            }
            write_circuit_state(root, state)
            _, report = self.run_module(
                "pao_runtime.oa_cli",
                "routing-circuit-reset",
                "--lwar-id",
                "LWAR2",
                "--routing-class",
                "logic",
                "--reason",
                "root cause fixed",
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(report["event"], "routing_circuit_reset")
            reset = load_circuit_state(root)
            self.assertNotIn("logic::LWAR2", reset["circuits"])
            self.assertEqual(
                reset["resets"]["logic::LWAR2"]["reason"], "root cause fixed"
            )


if __name__ == "__main__":
    unittest.main()
