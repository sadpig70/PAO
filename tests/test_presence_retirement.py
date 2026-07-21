import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pao_helpers import PaoTestCase
from pao_runtime.oa_cli import _next_renewal_deadline
from pao_runtime.presence import OA_PRESENCE_MAX_REFRESH_S, OA_PRESENCE_REFRESH_S


class PresenceAndRetirementTests(PaoTestCase):
    def test_presence_deadline_is_fixed_rate_with_scheduling_margin(self):
        self.assertEqual(OA_PRESENCE_REFRESH_S, 25.0)
        self.assertLess(OA_PRESENCE_REFRESH_S, OA_PRESENCE_MAX_REFRESH_S)
        # A renewal that finishes four seconds after its 125s deadline still
        # targets 150s. Fixed-delay scheduling would drift to 154s.
        self.assertEqual(_next_renewal_deadline(125.0, 25.0, 129.0), 150.0)
        # If several periods were missed, skip to the next future grid point
        # instead of busy-looping through old deadlines.
        self.assertEqual(_next_renewal_deadline(125.0, 25.0, 181.0), 200.0)
        with self.assertRaises(ValueError):
            _next_renewal_deadline(125.0, 0.0, 129.0)

    def test_lwar_can_start_first_and_adopt_after_oa_arrives(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bus"
            _, unavailable = self.run_module(
                "pao_runtime.lwar_cli", "oa-status", "--root", str(root), expected=2
            )
            self.assertEqual(unavailable["status"], "missing")

            args = [
                "register",
                "--runtime-name", "Test TUI",
                "--model", "Test Model",
                "--adapter-id", "test_tui",
                "--vendor-family", "test_vendor",
                "--interface", "tui",
                "--capability", "testing",
                "--root", str(root),
            ]
            _, requested = self.run_module("pao_runtime.lwar_cli", *args, expected=0)
            _, pending = self.run_module(
                "pao_runtime.lwar_cli",
                "response",
                requested["request_id"],
                "--root",
                str(root),
                expected=2,
            )
            self.assertEqual(pending["event"], "registration_pending")

            self.run_module(
                "pao_runtime.oa_cli", "presence", "--root", str(root), expected=0
            )
            self.run_module(
                "pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0
            )
            _, adopted = self.run_module(
                "pao_runtime.lwar_cli",
                "response",
                requested["request_id"],
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(adopted["event"], "identity_adopted")

    def test_lwar_can_distinguish_missing_live_stale_and_invalid_oa(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bus"
            _, missing = self.run_module(
                "pao_runtime.lwar_cli", "oa-status", "--root", str(root), expected=2
            )
            self.assertEqual(missing["status"], "missing")
            self.assertFalse(missing["live"])

            self.run_module(
                "pao_runtime.oa_cli", "presence", "--root", str(root), expected=0
            )
            _, live = self.run_module(
                "pao_runtime.lwar_cli", "oa-status", "--root", str(root), expected=0
            )
            self.assertEqual(live["status"], "live")
            self.assertTrue(live["live"])

            presence_path = root / "var" / "oa" / "presence.json"
            payload = json.loads(presence_path.read_text(encoding="utf-8"))
            stale_seen = datetime.now(timezone.utc) - timedelta(seconds=100)
            payload["last_seen"] = stale_seen.isoformat().replace("+00:00", "Z")
            payload["expires_at"] = (stale_seen + timedelta(seconds=90)).isoformat().replace(
                "+00:00", "Z"
            )
            presence_path.write_text(json.dumps(payload), encoding="utf-8")
            _, stale = self.run_module(
                "pao_runtime.lwar_cli", "oa-status", "--root", str(root), expected=2
            )
            self.assertEqual(stale["status"], "stale")

            presence_path.write_text("{broken", encoding="utf-8")
            _, invalid = self.run_module(
                "pao_runtime.lwar_cli", "oa-status", "--root", str(root), expected=3
            )
            self.assertEqual(invalid["status"], "invalid")

    def test_oa_mutation_refreshes_presence_and_lwar_status_surfaces_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bus"
            _, adopted = self.register_lwar(root)
            _, status = self.run_module(
                "pao_runtime.lwar_cli",
                "status",
                "--identity-file",
                adopted["identity_file"],
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(status["oa"]["status"], "live")
            self.assertEqual(status["oa"]["presence"]["oa_id"], "oa-test")

    def test_retire_control_returns_registry_slot_without_duplicate_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bus"
            _, adopted = self.register_lwar(root)
            identity_file = adopted["identity_file"]

            self.run_module(
                "pao_runtime.oa_cli",
                "control",
                "--lwar-id",
                "LWAR1",
                "--command",
                "retire",
                "--root",
                str(root),
                expected=0,
            )
            _, control = self.watch_once(root, adopted, expected=20)
            self.assertEqual(control["command"], "retire")

            _, first = self.run_module(
                "pao_runtime.lwar_cli",
                "retire",
                "--identity-file",
                identity_file,
                "--root",
                str(root),
                expected=2,
            )
            self.assertEqual(first["requested_state"], "draining")
            _, waiting = self.run_module(
                "pao_runtime.lwar_cli",
                "retire",
                "--identity-file",
                identity_file,
                "--root",
                str(root),
                expected=2,
            )
            self.assertEqual(waiting["event"], "retire_waiting")
            self.assertEqual(
                len(list((root / "control" / "lifecycle" / "requests").glob("*.json"))), 1
            )

            for expected_state in ("off", "deregistered"):
                self.run_module(
                    "pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0
                )
                _, progress = self.run_module(
                    "pao_runtime.lwar_cli",
                    "retire",
                    "--identity-file",
                    identity_file,
                    "--root",
                    str(root),
                    expected=2,
                )
                self.assertEqual(progress["requested_state"], expected_state)

            self.run_module(
                "pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0
            )
            _, retired = self.run_module(
                "pao_runtime.lwar_cli",
                "retire",
                "--identity-file",
                identity_file,
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(retired["event"], "lwar_retired")
            _, oa_status = self.run_module(
                "pao_runtime.oa_cli", "status", "--root", str(root), expected=0
            )
            self.assertEqual(oa_status["lwars"], [])

    def test_accepted_result_is_archived_once_after_lwar_retirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "bus"
            _, adopted = self.register_lwar(root)
            _, sent = self.send_task(
                root,
                "LWAR1",
                {"goal": "archive accepted result", "completion_criteria": ["result accepted"]},
            )
            self.watch_once(root, adopted, expected=0)
            self.complete_task(root, adopted, sent["task_id"])

            _, first_collect = self.run_module(
                "pao_runtime.oa_cli", "collect", "--root", str(root), expected=0
            )
            self.assertEqual(first_collect["count"], 1)
            outgoing = Path(first_collect["results"][0]["result_file"])
            self.assertTrue(outgoing.is_file())
            self.run_module(
                "pao_runtime.oa_cli",
                "validate",
                "--task-id",
                sent["task_id"],
                "--record",
                "--decision",
                "accepted",
                "--reason",
                "verified",
                "--root",
                str(root),
                expected=0,
            )

            _, retiring = self.run_module(
                "pao_runtime.lwar_cli",
                "retire",
                "--identity-file",
                adopted["identity_file"],
                "--root",
                str(root),
                expected=2,
            )
            self.assertEqual(retiring["requested_state"], "draining")
            for expected_state in ("off", "deregistered"):
                self.run_module(
                    "pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0
                )
                _, progress = self.run_module(
                    "pao_runtime.lwar_cli",
                    "retire",
                    "--identity-file",
                    adopted["identity_file"],
                    "--root",
                    str(root),
                    expected=2,
                )
                self.assertEqual(progress["requested_state"], expected_state)
            self.run_module(
                "pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0
            )
            self.run_module(
                "pao_runtime.lwar_cli",
                "retire",
                "--identity-file",
                adopted["identity_file"],
                "--root",
                str(root),
                expected=0,
            )

            _, reconciled = self.run_module(
                "pao_runtime.oa_cli", "collect", "--root", str(root), expected=0
            )
            self.assertEqual(reconciled["count"], 0)
            self.assertEqual(reconciled["quarantined"], [])
            self.assertEqual(len(reconciled["archived_reconciled"]), 1)
            archived = Path(reconciled["archived_reconciled"][0]["result_file"])
            self.assertFalse(outgoing.exists())
            self.assertTrue(archived.is_file())
            self.assertEqual(list((root / "mailbox" / "LWAR1" / "quarantine").glob("*")), [])
            ledger_path = next((root / "var" / "tasks").glob(f"*/{sent['task_id']}.json"))
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            self.assertEqual(ledger["result_file"], str(archived))
            self.assertEqual(ledger["validation"]["semantic_verdict"], "accepted")

            _, idempotent = self.run_module(
                "pao_runtime.oa_cli", "collect", "--root", str(root), expected=0
            )
            self.assertEqual(idempotent["count"], 0)
            self.assertEqual(idempotent["quarantined"], [])
            self.assertEqual(idempotent["archived_reconciled"], [])

            changed = json.loads(archived.read_text(encoding="utf-8"))
            changed["summary"] = "changed after acceptance"
            outgoing.write_text(json.dumps(changed), encoding="utf-8")
            _, fenced = self.run_module(
                "pao_runtime.oa_cli", "collect", "--root", str(root), expected=0
            )
            self.assertEqual(fenced["count"], 0)
            self.assertEqual(fenced["archived_reconciled"], [])
            self.assertEqual(fenced["quarantined"][0]["reason"], "stale_identity_result")


if __name__ == "__main__":
    import unittest

    unittest.main()
