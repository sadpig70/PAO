import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from pao_helpers import PaoTestCase


class ControlFlowTests(PaoTestCase):
    def test_cancel_control_reaches_lwar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.run_module(
                "pao_runtime.oa_cli",
                "control", "--lwar-id", "LWAR1", "--command", "cancel", "--task-id", "task-abc",
                "--root", str(root),
                expected=0,
            )
            _, event = self.watch_once(root, identity, timeout="0.2", expected=20)
            self.assertEqual(event["event"], "control")
            self.assertEqual(event["command"], "cancel")
            self.assertEqual(event["message"]["task_id"], "task-abc")

    def test_priority_orders_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.send_task(root, "LWAR1", {"goal": "Low priority", "priority": 9, "task_id": "task-low"})
            self.send_task(root, "LWAR1", {"goal": "High priority", "priority": 1, "task_id": "task-high"})
            _, event = self.watch_once(root, identity, expected=0)
            self.assertEqual(event["task_id"], "task-high")

    def test_tombstoned_slot_blocks_explicit_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.run_module(
                "pao_runtime.lwar_cli", "state", "off", "--identity-file", identity["identity_file"],
                "--root", str(root), expected=0,
            )
            self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
            self.run_module(
                "pao_runtime.lwar_cli", "state", "deregistered", "--identity-file", identity["identity_file"],
                "--root", str(root), expected=0,
            )
            self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
            args = [
                "register", "1", "--runtime-name", "Reuse", "--model", "Reuse Model",
                "--adapter-id", "reuse", "--vendor-family", "reuse", "--interface", "cli",
                "--root", str(root),
            ]
            _, request = self.run_module("pao_runtime.lwar_cli", *args, expected=0)
            self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
            _, response = self.run_module(
                "pao_runtime.lwar_cli", "response", request["request_id"], "--root", str(root), expected=3
            )
            self.assertEqual(response["reason"], "lwar_id_tombstoned")


class MaintenanceTests(PaoTestCase):
    def test_prune_removes_old_archived_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, published = self.send_task(root, "LWAR1", {"goal": "Archive me"})
            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, published["task_id"])
            self.run_module(
                "pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--archive", "--root", str(root),
                expected=0,
            )
            old = time.time() - 3 * 86400
            archived = list((root / "mailbox" / "LWAR1" / "archive" / "results").glob("*.json"))
            self.assertEqual(len(archived), 1)
            os.utime(archived[0], (old, old))
            _, pruned = self.run_module(
                "pao_runtime.oa_cli", "prune", "--older-than-days", "1", "--lwar-id", "LWAR1",
                "--root", str(root),
                expected=0,
            )
            self.assertGreaterEqual(pruned["total"], 1)
            self.assertEqual(
                list((root / "mailbox" / "LWAR1" / "archive" / "results").glob("*.json")), []
            )

    def test_audit_log_records_lifecycle_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            self.send_task(root, "LWAR1", {"goal": "Audit me"})
            audit_path = root / "var" / "audit" / "events.jsonl"
            self.assertTrue(audit_path.is_file())
            lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
            events = {line["event"] for line in lines}
            self.assertIn("registration_requested", events)
            self.assertIn("oa_reconcile_complete", events)
            self.assertIn("task_published", events)
            for line in lines:
                self.assertEqual(line["schema_version"], "pao.audit-event.v1")
                self.assertIn(line["actor"], {"oa", "lwar", "adp"})


class WorkflowDagTests(PaoTestCase):
    def test_depends_on_gates_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, first = self.send_task(
                root, "LWAR1", {"goal": "Upstream", "workflow_id": "workflow-dag", "task_id": "task-up"}
            )
            completed, _ = self.send_task(
                root,
                "LWAR1",
                {
                    "goal": "Downstream",
                    "workflow_id": "workflow-dag",
                    "task_id": "task-down",
                    "depends_on": ["task-up"],
                },
                expected=None,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("dependency not satisfied", completed.stderr)

            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, first["task_id"])
            self.run_module(
                "pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.send_task(
                root,
                "LWAR1",
                {
                    "goal": "Downstream",
                    "workflow_id": "workflow-dag",
                    "task_id": "task-down",
                    "depends_on": ["task-up"],
                },
                expected=0,
            )

    def test_workflow_status_aggregates_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, first = self.send_task(
                root, "LWAR1", {"goal": "One", "workflow_id": "workflow-agg", "task_id": "task-one"}
            )
            self.send_task(
                root, "LWAR1", {"goal": "Two", "workflow_id": "workflow-agg", "task_id": "task-two"}
            )
            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, first["task_id"])
            self.run_module(
                "pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            _, status = self.run_module(
                "pao_runtime.oa_cli", "workflow-status", "--workflow-id", "workflow-agg",
                "--root", str(root),
                expected=0,
            )
            self.assertEqual(status["total"], 2)
            self.assertEqual(status["by_status"], {"completed": 1, "published": 1})


if __name__ == "__main__":
    unittest.main()
