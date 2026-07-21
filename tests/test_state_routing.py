import json
import tempfile
import unittest
from pathlib import Path

from pao_helpers import PaoTestCase
from pao_runtime.transport import FileTransport


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
            self.assertEqual(statuses, ["publishing", "published", "completed"])


class HeartbeatMonitorTests(PaoTestCase):
    def test_adoption_publishes_starting_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            heartbeat = json.loads(
                (root / "mailbox" / "LWAR1" / "heartbeat.json").read_text(encoding="utf-8")
            )
            self.assertEqual(heartbeat["status"], "starting")
            self.assertEqual(heartbeat["instance_id"], identity["instance_id"])
            self.assertEqual(heartbeat["generation"], identity["generation"])

    def test_response_replay_does_not_reset_active_heartbeat_or_adoption_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requested, identity = self.register_lwar(root)
            transport = FileTransport(root)
            transport.write_heartbeat(identity, "idle", None)
            heartbeat_path = root / "mailbox" / "LWAR1" / "heartbeat.json"
            before_heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            before_adopted_at = identity["adopted_at"]

            _, replay = self.run_module(
                "pao_runtime.lwar_cli", "response", requested["request_id"],
                "--root", str(root), expected=0,
            )
            after_heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            self.assertEqual(replay["adopted_at"], before_adopted_at)
            self.assertEqual(after_heartbeat, before_heartbeat)
            self.assertEqual(after_heartbeat["status"], "idle")

    def test_status_reports_registered_not_started(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, requested = self.run_module(
                "pao_runtime.lwar_cli",
                "register", "--runtime-name", "Test TUI", "--model", "Test Model",
                "--adapter-id", "test_tui", "--vendor-family", "test_vendor",
                "--interface", "tui", "--capability", "coding", "--root", str(root),
                expected=0,
            )
            self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
            _, status = self.run_module("pao_runtime.oa_cli", "status", "--root", str(root), expected=0)
            lwar = status["lwars"][0]
            self.assertEqual(lwar["runtime_status"], "registered_not_started")
            self.assertTrue(lwar["registered_not_started"])
            self.assertFalse(lwar["heartbeat_identity_match"])
            self.assertTrue(lwar["heartbeat_stale"])
            self.assertIsNone(lwar["heartbeat_age_s"])
            self.assertEqual(requested["event"], "registration_requested")

    def test_status_reports_starting_then_startup_deadline_missed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            _, starting = self.run_module(
                "pao_runtime.oa_cli", "status", "--root", str(root), expected=0
            )
            lwar = starting["lwars"][0]
            self.assertEqual(lwar["runtime_status"], "starting")
            self.assertTrue(lwar["registered_not_started"])
            self.assertTrue(lwar["heartbeat_identity_match"])
            self.assertFalse(lwar["startup_deadline_missed"])

            _, missed = self.run_module(
                "pao_runtime.oa_cli", "status", "--startup-deadline", "0",
                "--root", str(root), expected=0,
            )
            lwar = missed["lwars"][0]
            self.assertEqual(lwar["runtime_status"], "registered_not_started")
            self.assertTrue(lwar["startup_deadline_missed"])
            self.assertTrue(lwar["heartbeat_stale"])

    def test_status_reports_fresh_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.watch_once(root, identity, timeout="0.05", expected=10)
            _, status = self.run_module("pao_runtime.oa_cli", "status", "--root", str(root), expected=0)
            lwar = status["lwars"][0]
            self.assertEqual(lwar["runtime_status"], "active")
            self.assertFalse(lwar["registered_not_started"])
            self.assertFalse(lwar["heartbeat_stale"])
            self.assertLess(lwar["heartbeat_age_s"], 120)

    def test_status_reports_started_runtime_that_became_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            transport = FileTransport(root)
            transport.write_heartbeat(identity, "idle", None)
            heartbeat_path = root / "mailbox" / "LWAR1" / "heartbeat.json"
            heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            heartbeat["last_seen"] = "2000-01-01T00:00:00Z"
            heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")
            _, status = self.run_module(
                "pao_runtime.oa_cli", "status", "--root", str(root), expected=0
            )
            lwar = status["lwars"][0]
            self.assertEqual(lwar["runtime_status"], "stale")
            self.assertFalse(lwar["registered_not_started"])
            self.assertTrue(lwar["heartbeat_identity_match"])
            self.assertTrue(lwar["heartbeat_stale"])

    def test_status_distinguishes_old_generation_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            heartbeat_path = root / "mailbox" / "LWAR1" / "heartbeat.json"
            heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            heartbeat["generation"] = identity["generation"] + 1
            heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")
            _, status = self.run_module(
                "pao_runtime.oa_cli", "status", "--root", str(root), expected=0
            )
            lwar = status["lwars"][0]
            self.assertEqual(lwar["runtime_status"], "registered_not_started")
            self.assertFalse(lwar["heartbeat_identity_match"])


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
    def test_auto_route_rejects_starting_lwar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root, capabilities=("coding",))
            draft = root / "starting_task.json"
            draft.write_text(json.dumps({"goal": "Do not route yet"}), encoding="utf-8")
            completed, _ = self.run_module(
                "pao_runtime.oa_cli", "send", "--auto", "--require-capability", "coding",
                "--task-file", str(draft), "--root", str(root),
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("no eligible LWAR", completed.stderr)

    def test_auto_route_matches_required_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, first = self.register_lwar(root, capabilities=("coding",))
            _, second = self.register_lwar(root, capabilities=("coding", "testing"))
            transport = FileTransport(root)
            transport.write_heartbeat(first, "idle", None)
            transport.write_heartbeat(second, "idle", None)
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
            _, first = self.register_lwar(root, capabilities=("coding",))
            _, second = self.register_lwar(root, capabilities=("coding",))
            transport = FileTransport(root)
            transport.write_heartbeat(first, "idle", None)
            transport.write_heartbeat(second, "idle", None)
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
            _, identity = self.register_lwar(root, capabilities=("coding",))
            FileTransport(root).write_heartbeat(identity, "idle", None)
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
