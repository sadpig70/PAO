import json
import tempfile
import unittest
from pathlib import Path

from pao_helpers import PaoTestCase


class RetryBudgetTests(PaoTestCase):
    def test_recover_increments_attempt_on_requeue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, published = self.send_task(root, "LWAR1", {"goal": "Recover me"})
            self.watch_once(root, identity, expected=0)
            self.expire_lease(root, "LWAR1", published["task_id"])
            _, recovered = self.run_module(
                "pao_runtime.oa_cli", "recover", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.assertEqual(recovered["count"], 1)
            self.assertEqual(recovered["tasks"][0]["attempt"], 2)
            incoming = list((root / "mailbox" / "LWAR1" / "incoming").glob("*.json"))
            self.assertEqual(len(incoming), 1)
            task = json.loads(incoming[0].read_text(encoding="utf-8"))
            self.assertEqual(task["attempt"], 2)

    def test_dead_letter_after_retry_budget_exhausted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, published = self.send_task(root, "LWAR1", {"goal": "Fail forever", "max_retries": 1})
            self.watch_once(root, identity, expected=0)
            self.expire_lease(root, "LWAR1", published["task_id"])
            _, recovered = self.run_module(
                "pao_runtime.oa_cli", "recover", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.assertEqual(recovered["count"], 0)
            self.assertEqual(len(recovered["dead_lettered"]), 1)
            self.assertEqual(recovered["dead_lettered"][0]["task_id"], published["task_id"])
            dead = [
                path for path in (root / "mailbox" / "LWAR1" / "dead").glob("*.json")
                if not path.name.endswith(".error.json")
            ]
            self.assertEqual(len(dead), 1)
            error = json.loads(dead[0].with_suffix(".error.json").read_text(encoding="utf-8"))
            self.assertEqual(error["reason"], "retry_budget_exhausted")
            ledger = list((root / "var" / "tasks").glob(f"*/{published['task_id']}.json"))
            self.assertEqual(len(ledger), 1)
            self.assertEqual(json.loads(ledger[0].read_text(encoding="utf-8"))["status"], "dead")

    def test_dead_requeue_keeps_attempt_monotonic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, published = self.send_task(root, "LWAR1", {"goal": "Second chance", "max_retries": 1})
            self.watch_once(root, identity, expected=0)
            self.expire_lease(root, "LWAR1", published["task_id"])
            self.run_module("pao_runtime.oa_cli", "recover", "--lwar-id", "LWAR1", "--root", str(root), expected=0)
            _, listed = self.run_module(
                "pao_runtime.oa_cli", "dead", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.assertEqual(listed["count"], 1)
            _, requeued = self.run_module(
                "pao_runtime.oa_cli",
                "dead", "--lwar-id", "LWAR1", "--requeue", published["task_id"],
                "--root", str(root),
                expected=0,
            )
            self.assertEqual(requeued["event"], "dead_requeued")
            incoming = list((root / "mailbox" / "LWAR1" / "incoming").glob("*.json"))
            self.assertEqual(len(incoming), 1)
            task = json.loads(incoming[0].read_text(encoding="utf-8"))
            # attempt is the collect-side fencing key: manual requeue continues
            # the counter (1 claim → 2 recover → 3 requeue), never resets it.
            self.assertEqual(task["attempt"], 3)
            self.assertEqual(
                list((root / "mailbox" / "LWAR1" / "dead").glob("*.json")),
                [],
            )


class ResultGuardTests(PaoTestCase):
    def test_stale_identity_result_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            outgoing = root / "mailbox" / "LWAR1" / "outgoing" / "task-stale.result.json"
            outgoing.write_text(
                json.dumps(
                    {
                        "schema_version": "pao.result.v1",
                        "task_id": "task-stale",
                        "workflow_id": "workflow-stale",
                        "lwar_id": "LWAR1",
                        "instance_id": identity["instance_id"],
                        "generation": 999,
                        "status": "succeeded",
                        "summary": "stale replay",
                        "evidence": {},
                    }
                ),
                encoding="utf-8",
            )
            _, collected = self.run_module(
                "pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.assertEqual(collected["count"], 0)
            self.assertEqual(len(collected["quarantined"]), 1)
            self.assertEqual(collected["quarantined"][0]["reason"], "stale_identity_result")
            quarantine = root / "mailbox" / "LWAR1" / "quarantine"
            self.assertEqual(len(list(quarantine.glob("*.result.json"))), 1)

    def test_duplicate_result_is_quarantined_after_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, published = self.send_task(root, "LWAR1", {"goal": "Once only"})
            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, published["task_id"])
            _, first = self.run_module(
                "pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--archive", "--root", str(root), expected=0
            )
            self.assertEqual(first["count"], 1)
            workflow_id = first["results"][0]["result"]["workflow_id"]
            replay = root / "mailbox" / "LWAR1" / "outgoing" / f"{published['task_id']}.result.json"
            replay.write_text(
                json.dumps(
                    {
                        "schema_version": "pao.result.v1",
                        "task_id": published["task_id"],
                        "workflow_id": workflow_id,
                        "lwar_id": "LWAR1",
                        "instance_id": identity["instance_id"],
                        "generation": identity["generation"],
                        "status": "succeeded",
                        "summary": "replayed",
                        "evidence": {},
                    }
                ),
                encoding="utf-8",
            )
            _, second = self.run_module(
                "pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.assertEqual(second["count"], 0)
            self.assertEqual(second["quarantined"][0]["reason"], "duplicate_result")

    def test_recollect_without_archive_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, published = self.send_task(root, "LWAR1", {"goal": "Read twice"})
            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, published["task_id"])
            # First collect accepts the result; a re-collect (no --archive leaves
            # it in outgoing/) must NOT re-report or re-record it — genuine
            # idempotency, not the old "count 1 every poll" that also grew the
            # ledger history without bound.
            _, first = self.run_module(
                "pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.assertEqual(first["count"], 1)
            self.assertEqual(first["quarantined"], [])
            ledger_path = next((root / "var" / "tasks").glob(f"*/{published['task_id']}.json"))
            history_len = len(json.loads(ledger_path.read_text(encoding="utf-8"))["history"])
            _, second = self.run_module(
                "pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.assertEqual(second["count"], 0)
            self.assertEqual(second["quarantined"], [])
            self.assertEqual(
                len(json.loads(ledger_path.read_text(encoding="utf-8"))["history"]),
                history_len,
                "re-collect must not append another history entry",
            )


class LeaseAlignmentTests(PaoTestCase):
    def test_lease_extends_to_task_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, published = self.send_task(root, "LWAR1", {"goal": "Long task", "timeout_s": 600})
            self.watch_once(root, identity, lease_seconds=30, expected=0)
            lease_path = root / "mailbox" / "LWAR1" / "leases" / f"{published['task_id']}.json"
            lease = json.loads(lease_path.read_text(encoding="utf-8"))
            self.assertEqual(lease["effective_lease_s"], 630)

    def test_short_timeout_keeps_default_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, published = self.send_task(root, "LWAR1", {"goal": "Short task", "timeout_s": 5})
            self.watch_once(root, identity, lease_seconds=180, expected=0)
            lease_path = root / "mailbox" / "LWAR1" / "leases" / f"{published['task_id']}.json"
            lease = json.loads(lease_path.read_text(encoding="utf-8"))
            self.assertEqual(lease["effective_lease_s"], 180)


if __name__ == "__main__":
    unittest.main()
