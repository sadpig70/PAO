import json
import tempfile
import unittest
import uuid
from pathlib import Path

from pao_helpers import RUNTIME_HOME, PaoTestCase


def ledger_entry(root, task_id):
    for path in sorted((root / "var" / "tasks").glob(f"*/{task_id}.json")):
        return json.loads(path.read_text(encoding="utf-8"))
    return None


class ExtendedTerminalStatusTests(PaoTestCase):
    def test_timed_out_result_is_collected_but_not_acceptance_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            _, sent = self.send_task(root, "LWAR1", {"goal": "long job", "cwd": str(root)})
            _, event = self.watch_once(root, adopted, expected=0)
            task_id = event["task_id"]
            self.complete_task(
                root, adopted, task_id,
                result={
                    "status": "timed_out",
                    "summary": "budget exceeded",
                    "evidence": {"elapsed_s": 999},
                    "artifacts": [],
                    "exit_code": 1,
                },
            )
            _, collected = self.run_module("pao_runtime.oa_cli", "collect", "--root", str(root), expected=0)
            self.assertEqual(collected["count"], 1)
            result = collected["results"][0]["result"]
            self.assertEqual(result["status"], "timed_out")
            self.assertEqual(result["attempt"], 1)
            self.assertRegex(result["claim_token"], r"^claim-[a-f0-9]{32}$")
            _, report = self.run_module(
                "pao_runtime.oa_cli", "validate", "--task-id", task_id, "--root", str(root), expected=0
            )
            self.assertEqual(report["verdict"], "attention_required")


class AttemptFenceTests(PaoTestCase):
    def _claim_and_expire(self, root, adopted):
        _, event = self.watch_once(root, adopted, expected=0)
        task_id = event["task_id"]
        self.expire_lease(root, "LWAR1", task_id)
        return task_id

    def test_superseded_attempt_result_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            self.send_task(root, "LWAR1", {"goal": "raced job", "cwd": str(root)})
            task_id = self._claim_and_expire(root, adopted)
            self.run_module("pao_runtime.oa_cli", "recover", "--root", str(root), expected=0)
            entry = ledger_entry(root, task_id)
            self.assertEqual(entry["attempt"], 2)
            # Simulate the race outcome: a result from the superseded attempt=1
            # claim landing in outgoing after recover already re-queued.
            stale = {
                "schema_version": "pao.result.v1",
                "task_id": task_id,
                "workflow_id": entry["workflow_id"],
                "lwar_id": "LWAR1",
                "instance_id": adopted["instance_id"],
                "generation": adopted["generation"],
                "status": "succeeded",
                "summary": "from a superseded claim",
                "evidence": {"ok": True},
                "artifacts": [],
                "exit_code": 0,
                "attempt": 1,
                "submitted_at": "2026-01-01T00:00:00Z",
            }
            outgoing = root / "mailbox" / "LWAR1" / "outgoing" / f"{task_id}.result.json"
            outgoing.write_text(json.dumps(stale), encoding="utf-8")
            _, collected = self.run_module("pao_runtime.oa_cli", "collect", "--root", str(root), expected=0)
            self.assertEqual(collected["count"], 0)
            self.assertEqual(collected["quarantined"][0]["reason"], "stale_attempt_result")

    def test_recover_records_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            self.send_task(root, "LWAR1", {"goal": "vanishing job", "cwd": str(root)})
            task_id = self._claim_and_expire(root, adopted)
            self.run_module("pao_runtime.oa_cli", "recover", "--root", str(root), expected=0)
            entry = ledger_entry(root, task_id)
            self.assertEqual(entry["status"], "requeued")
            self.assertEqual(entry["interruption"]["status"], "interrupted")
            self.assertEqual(entry["interruption"]["recorded_by"], "oa_reconciler")

    def test_dead_requeue_keeps_attempt_monotonic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            self.send_task(root, "LWAR1", {"goal": "doomed job", "cwd": str(root), "max_retries": 1})
            task_id = self._claim_and_expire(root, adopted)
            self.run_module("pao_runtime.oa_cli", "recover", "--root", str(root), expected=0)
            self.assertEqual(ledger_entry(root, task_id)["status"], "dead")
            self.run_module(
                "pao_runtime.oa_cli",
                "dead", "--lwar-id", "LWAR1", "--requeue", task_id,
                "--root", str(root),
                expected=0,
            )
            entry = ledger_entry(root, task_id)
            self.assertEqual(entry["status"], "requeued")
            self.assertEqual(entry["attempt"], 3)  # 1 (claim) → 2 (recover) → 3 (manual requeue)


class SingleWriterOATests(PaoTestCase):
    def test_second_oa_id_is_rejected_until_lease_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_module(
                "pao_runtime.oa_cli", "reconcile", "--root", str(root),
                env={"PAO_OA_ID": "oa-alpha"}, expected=0,
            )
            completed, _ = self.run_module(
                "pao_runtime.oa_cli", "reconcile", "--root", str(root),
                env={"PAO_OA_ID": "oa-beta"},
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("writer lease", completed.stderr)
            self.run_module(
                "pao_runtime.oa_cli", "reconcile", "--root", str(root),
                env={"PAO_OA_ID": "oa-alpha"}, expected=0,
            )

    def test_read_only_commands_ignore_the_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.run_module(
                "pao_runtime.oa_cli", "reconcile", "--root", str(root),
                env={"PAO_OA_ID": "oa-alpha"}, expected=0,
            )
            self.run_module(
                "pao_runtime.oa_cli", "status", "--root", str(root),
                env={"PAO_OA_ID": "oa-beta"}, expected=0,
            )
            self.run_module(
                "pao_runtime.oa_cli", "dead", "--root", str(root),
                env={"PAO_OA_ID": "oa-beta"}, expected=0,
            )


class VersionHandshakeTests(PaoTestCase):
    def _write_request(self, root, runtime_version):
        request_id = f"lwar-reg-{uuid.uuid4().hex}"
        request = {
            "schema_version": "pao.lwar-registration-request.v1",
            "request_id": request_id,
            "instance_id": f"lwar-instance-{uuid.uuid4().hex}",
            "requested_lwar_id": None,
            "allocation_mode": "auto",
            "requested_state": "on",
            "profile": {
                "runtime_name": "Legacy",
                "model": "Legacy Model",
                "adapter_id": "legacy",
                "vendor_family": "legacy",
                "interface": "cli",
                "capabilities": [],
            },
            "behavior_contract": "lwar-runtime.v2-adp",
            "created_at": "2026-01-01T00:00:00Z",
        }
        if runtime_version is not None:
            request["runtime_version"] = runtime_version
        path = root / "control" / "registration" / "requests" / f"{request_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(request), encoding="utf-8")
        return request_id

    def _response(self, root, request_id):
        path = root / "control" / "registration" / "responses" / f"{request_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_mismatched_runtime_version_is_rejected_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_id = self._write_request(root, "0.0.1")
            self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
            response = self._response(root, request_id)
            self.assertFalse(response["accepted"])
            self.assertEqual(response["reason"], "runtime_version_mismatch")

    def test_absent_runtime_version_is_accepted_as_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request_id = self._write_request(root, None)
            self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
            response = self._response(root, request_id)
            self.assertTrue(response["accepted"])


class DoctorTests(PaoTestCase):
    def test_doctor_healthy_on_fresh_bus(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.run_module(
                "pao_runtime.pao_cli", "doctor", "--role", "lwar", "--root", directory, expected=0
            )
            self.assertTrue(report["healthy"])
            names = {check["check"] for check in report["checks"]}
            self.assertIn("bus_writable_atomic", names)
            self.assertIn("role_references", names)

    def test_doctor_fails_when_root_is_inside_the_skill_dir(self):
        completed, report = self.run_module(
            "pao_runtime.pao_cli", "doctor", "--root", str(RUNTIME_HOME)
        )
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report["healthy"])
        by_name = {check["check"]: check for check in report["checks"]}
        self.assertFalse(by_name["root_outside_skill_dir"]["ok"])
        # The probe must never write into the bundle itself.
        self.assertFalse((RUNTIME_HOME / "var").exists())


class WatcherBackoffTests(PaoTestCase):
    def test_state_wait_with_backoff_flag_keeps_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            self.run_module(
                "pao_runtime.lwar_cli",
                "state", "draining", "--identity-file", adopted["identity_file"],
                "--root", str(root),
                expected=0,
            )
            self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
            completed, event = self.run_module(
                "pao_runtime.adp_watch",
                "--identity-file", adopted["identity_file"],
                "--interval", "0.01", "--timeout", "0.3",
                "--state-wait-backoff-max", "0.05",
                "--root", str(root),
                expected=10,
            )
            self.assertEqual(event["event"], "state_wait")

    def test_backoff_below_interval_is_rejected(self):
        completed, _ = self.run_module(
            "pao_runtime.adp_watch",
            "--identity-file", "unused.json",
            "--interval", "5", "--state-wait-backoff-max", "1",
        )
        self.assertNotEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
