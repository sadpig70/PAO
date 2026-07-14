import json
import tempfile
import unittest
from pathlib import Path

from pao_helpers import PaoTestCase


class TaskLedgerTests(PaoTestCase):
    def test_ledger_tracks_published_to_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, published = self.send_task(
                root, "LWAR1", {"goal": "Track me", "workflow_id": "workflow-ledger"}
            )
            ledger_path = root / "var" / "tasks" / "workflow-ledger" / f"{published['task_id']}.json"
            entry = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(entry["status"], "published")
            self.assertEqual(entry["attempt"], 1)

            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, published["task_id"])
            self.run_module(
                "pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            entry = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(entry["status"], "completed")
            self.assertEqual(entry["result"]["status"], "succeeded")
            statuses = [item["status"] for item in entry["history"]]
            self.assertEqual(statuses, ["published", "completed"])


class HeartbeatMonitorTests(PaoTestCase):
    def test_status_reports_missing_heartbeat_as_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            _, status = self.run_module("pao_runtime.oa_cli", "status", "--root", str(root), expected=0)
            self.assertTrue(status["lwars"][0]["heartbeat_stale"])
            self.assertIsNone(status["lwars"][0]["heartbeat_age_s"])

    def test_status_reports_fresh_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.watch_once(root, identity, timeout="0.05", expected=10)
            _, status = self.run_module("pao_runtime.oa_cli", "status", "--root", str(root), expected=0)
            self.assertFalse(status["lwars"][0]["heartbeat_stale"])
            self.assertLess(status["lwars"][0]["heartbeat_age_s"], 120)


class ValidateCommandTests(PaoTestCase):
    def test_validate_reports_mechanical_checks_and_criteria(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, published = self.send_task(
                root,
                "LWAR1",
                {"goal": "Validate me", "completion_criteria": ["artifact exists", "tests pass"]},
            )
            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, published["task_id"])
            self.run_module(
                "pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            _, report = self.run_module(
                "pao_runtime.oa_cli", "validate", "--task-id", published["task_id"], "--root", str(root),
                expected=0,
            )
            self.assertEqual(report["event"], "validation_report")
            self.assertEqual(report["verdict"], "ready_for_oa_review")
            self.assertTrue(report["checks"]["status_succeeded"])
            self.assertTrue(report["checks"]["evidence_present"])
            self.assertEqual(len(report["criteria"]), 2)
            self.assertEqual(report["criteria"][0]["verdict"], "manual_check_required")

    def test_validate_before_completion_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            _, published = self.send_task(root, "LWAR1", {"goal": "Not done yet"})
            _, report = self.run_module(
                "pao_runtime.oa_cli", "validate", "--task-id", published["task_id"], "--root", str(root),
                expected=2,
            )
            self.assertEqual(report["event"], "validation_unavailable")


class AutoRouteTests(PaoTestCase):
    def test_auto_route_matches_required_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root, capabilities=("coding",))
            self.register_lwar(root, capabilities=("coding", "testing"))
            draft = root / "auto_task.json"
            draft.write_text(json.dumps({"goal": "Route by capability"}), encoding="utf-8")
            _, published = self.run_module(
                "pao_runtime.oa_cli",
                "send", "--auto", "--require-capability", "testing",
                "--task-file", str(draft), "--root", str(root),
                expected=0,
            )
            self.assertEqual(published["lwar_id"], "LWAR2")

    def test_auto_route_prefers_lower_backlog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root, capabilities=("coding",))
            self.register_lwar(root, capabilities=("coding",))
            self.send_task(root, "LWAR1", {"goal": "Backlog one"})
            self.send_task(root, "LWAR1", {"goal": "Backlog two"})
            draft = root / "auto_task.json"
            draft.write_text(json.dumps({"goal": "Route by load"}), encoding="utf-8")
            _, published = self.run_module(
                "pao_runtime.oa_cli",
                "send", "--auto", "--require-capability", "coding",
                "--task-file", str(draft), "--root", str(root),
                expected=0,
            )
            self.assertEqual(published["lwar_id"], "LWAR2")

    def test_auto_route_without_eligible_lwar_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root, capabilities=("coding",))
            draft = root / "auto_task.json"
            draft.write_text(json.dumps({"goal": "Impossible route"}), encoding="utf-8")
            completed, _ = self.run_module(
                "pao_runtime.oa_cli",
                "send", "--auto", "--require-capability", "quantum",
                "--task-file", str(draft), "--root", str(root),
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("no eligible LWAR", completed.stderr)


if __name__ == "__main__":
    unittest.main()
