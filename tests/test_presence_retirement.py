import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pao_helpers import PaoTestCase


class PresenceAndRetirementTests(PaoTestCase):
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


if __name__ == "__main__":
    import unittest

    unittest.main()
