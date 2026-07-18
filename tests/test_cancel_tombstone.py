import json
import tempfile
import unittest
from pathlib import Path

from pao_helpers import PaoTestCase


def outgoing_result(root, lwar_id, task_id):
    path = root / "mailbox" / lwar_id / "outgoing" / f"{task_id}.result.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def tombstone_path(root, lwar_id, task_id):
    return root / "mailbox" / lwar_id / "cancelled" / f"{task_id}.json"


def failed_errors(root, lwar_id):
    return list((root / "mailbox" / lwar_id / "failed").glob("*.error.json"))


class CancelTombstoneTests(PaoTestCase):
    def _publish_and_tombstone(self, root, adopted, goal):
        """Publish a task, then claim one cancel control so its tombstone lands
        (the control slice returns before any task is claimed)."""
        _, sent = self.send_task(root, "LWAR1", {"goal": goal})
        task_id = sent["task_id"]
        self.run_module(
            "pao_runtime.oa_cli", "control", "--lwar-id", "LWAR1",
            "--command", "cancel", "--task-id", task_id, "--root", str(root),
            expected=0,
        )
        _, control_event = self.watch_once(root, adopted, expected=20)
        self.assertEqual(control_event["event"], "control")
        self.assertEqual(control_event["message"]["command"], "cancel")
        self.assertTrue(tombstone_path(root, "LWAR1", task_id).is_file())
        return task_id

    def test_cancel_before_claim_auto_cancels_without_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            task_id = self._publish_and_tombstone(root, adopted, "cancel before claim")

            # The next watch slice claims the task, finds the tombstone, and
            # submits the cancelled result itself — no `complete` is ever run.
            _, event = self.watch_once(root, adopted, expected=10)
            self.assertEqual(event["event"], "idle_timeout")

            result = outgoing_result(root, "LWAR1", task_id)
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "cancelled")
            # attempt and claim_token are echoed from the claimed task file.
            self.assertEqual(result["attempt"], 1)
            self.assertIsNotNone(result["claim_token"])
            self.assertTrue(result["claim_token"].startswith("claim-"))
            self.assertEqual(result["instance_id"], adopted["instance_id"])
            self.assertEqual(result["generation"], adopted["generation"])
            self.assertEqual(result["evidence"]["cancelled_by"], "watcher_tombstone")

            # The tombstone is consumed and the task left neither queued nor claimed.
            self.assertFalse(tombstone_path(root, "LWAR1", task_id).is_file())
            self.assertEqual(
                list((root / "mailbox" / "LWAR1" / "incoming").glob("*.json")), []
            )
            self.assertEqual(
                list((root / "mailbox" / "LWAR1" / "claimed").glob("*.json")), []
            )
            self.assertEqual(failed_errors(root, "LWAR1"), [])

            # The cancelled result flows through OA collect uncontested.
            _, collected = self.run_module(
                "pao_runtime.oa_cli", "collect", "--root", str(root), expected=0
            )
            self.assertEqual(collected["count"], 1)
            self.assertEqual(collected["quarantined"], [])
            self.assertEqual(collected["results"][0]["result"]["status"], "cancelled")

    def test_duplicate_cancel_is_harmless(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            _, sent = self.send_task(root, "LWAR1", {"goal": "duplicate cancel"})
            task_id = sent["task_id"]

            # Two cancels for the same task, each claimed in its own slice.
            for _ in range(2):
                self.run_module(
                    "pao_runtime.oa_cli", "control", "--lwar-id", "LWAR1",
                    "--command", "cancel", "--task-id", task_id, "--root", str(root),
                    expected=0,
                )
            for _ in range(2):
                _, event = self.watch_once(root, adopted, expected=20)
                self.assertEqual(event["event"], "control")
            self.assertTrue(tombstone_path(root, "LWAR1", task_id).is_file())

            # The task is auto-cancelled exactly once; no error surfaces.
            _, event = self.watch_once(root, adopted, expected=10)
            self.assertEqual(event["event"], "idle_timeout")
            result = outgoing_result(root, "LWAR1", task_id)
            self.assertIsNotNone(result)
            self.assertEqual(result["status"], "cancelled")
            self.assertFalse(tombstone_path(root, "LWAR1", task_id).is_file())
            self.assertEqual(failed_errors(root, "LWAR1"), [])
            self.assertEqual(
                list((root / "mailbox" / "LWAR1" / "outgoing").glob("*.json")),
                [root / "mailbox" / "LWAR1" / "outgoing" / f"{task_id}.result.json"],
            )

    def test_tombstone_for_completed_task_is_harmless(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            _, sent = self.send_task(root, "LWAR1", {"goal": "already completed"})
            task_id = sent["task_id"]

            # Claim and complete the task normally.
            _, event = self.watch_once(root, adopted, expected=0)
            self.assertEqual(event["task_id"], task_id)
            self.complete_task(root, adopted, task_id)
            completed_result = outgoing_result(root, "LWAR1", task_id)
            self.assertEqual(completed_result["status"], "succeeded")

            # A cancel arriving after completion writes a tombstone but never
            # cancels anything — the task no longer reappears in incoming.
            self.run_module(
                "pao_runtime.oa_cli", "control", "--lwar-id", "LWAR1",
                "--command", "cancel", "--task-id", task_id, "--root", str(root),
                expected=0,
            )
            _, control_event = self.watch_once(root, adopted, expected=20)
            self.assertEqual(control_event["event"], "control")
            self.assertTrue(tombstone_path(root, "LWAR1", task_id).is_file())

            # A further slice is idle; the completed result is untouched and the
            # dangling tombstone is simply ignored (never consumed, never errors).
            _, event = self.watch_once(root, adopted, expected=10)
            self.assertEqual(event["event"], "idle_timeout")
            self.assertEqual(outgoing_result(root, "LWAR1", task_id)["status"], "succeeded")
            self.assertTrue(tombstone_path(root, "LWAR1", task_id).is_file())
            self.assertEqual(failed_errors(root, "LWAR1"), [])


if __name__ == "__main__":
    unittest.main()
