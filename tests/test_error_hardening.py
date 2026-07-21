"""Error, exception, and edge-case hardening regression tests.

Each test pins one hardening fix so a regression to the old crash-or-wedge
behavior fails loudly.
"""
import json
import os
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from pao_helpers import PaoTestCase

import pao_runtime.common as common
from pao_runtime.common import FileLock, quarantine_corrupt, safe_load_json


INSTANCE = "lwar-instance-" + "a" * 32


class CommonPrimitiveTests(unittest.TestCase):
    def test_safe_load_json_returns_none_on_poison(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            good = root / "good.json"
            good.write_text('{"a": 1}', encoding="utf-8")
            self.assertEqual(safe_load_json(good), {"a": 1})
            for bad, text in (("empty.json", ""), ("trunc.json", '{"a":'), ("arr.json", "[1,2]")):
                path = root / bad
                path.write_text(text, encoding="utf-8")
                self.assertIsNone(safe_load_json(path), bad)
            self.assertIsNone(safe_load_json(root / "missing.json"))

    def test_quarantine_corrupt_moves_file_aside(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            bad = root / "bad.json"
            bad.write_text("garbage{", encoding="utf-8")
            moved = quarantine_corrupt(bad, "corrupt_test")
            self.assertIsNotNone(moved)
            self.assertFalse(bad.exists())
            self.assertTrue(moved.exists())
            # The `.corrupt/` sibling is not matched by the sweeps' *.json glob.
            self.assertEqual(list(root.glob("*.json")), [])

    def test_replace_retry_survives_transient_permission_error(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "target.json"
            real_replace = os.replace
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise PermissionError(5, "sharing violation")
                return real_replace(src, dst)

            with mock.patch.object(common.os, "replace", side_effect=flaky):
                common.atomic_write_json(path, {"ok": True})
            self.assertGreaterEqual(calls["n"], 3)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"ok": True})

    def test_filelock_release_does_not_delete_a_stolen_lock(self):
        with TemporaryDirectory() as directory:
            lock_path = Path(directory) / ".t.lock"
            lock = FileLock(lock_path)
            lock.__enter__()
            # A second holder steals the lock and stamps its own token.
            lock_path.write_text("99999 foreigntoken 2020-01-01T00:00:00Z\n", encoding="utf-8")
            lock.__exit__(None, None, None)
            self.assertTrue(lock_path.exists(), "must not delete a lock we no longer own")

    def test_authority_denies_bus_surfaces(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNotNone(common.authority_denied_reason(root / "mailbox" / "x", root))
            self.assertIsNone(common.authority_denied_reason(root / "work", root))


class RecoverySweepTests(PaoTestCase):
    def _claimed_path(self, root, task_id):
        return next((root / "mailbox" / "LWAR1" / "claimed").glob(f"*{task_id}*.json"))

    def test_one_corrupt_lease_does_not_wedge_recovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, good = self.send_task(root, "LWAR1", {"goal": "good", "task_id": "task-good"})
            self.watch_once(root, identity, expected=0)
            self.expire_lease(root, "LWAR1", "task-good")
            # Plant a poison lease next to the real one.
            (root / "mailbox" / "LWAR1" / "leases" / "task-poison.json").write_text(
                "{ not json", encoding="utf-8"
            )
            _, recovered = self.run_module(
                "pao_runtime.oa_cli", "recover", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            # The good expired lease is still recovered despite the poison file.
            self.assertEqual(recovered["count"], 1)
            self.assertEqual(recovered["tasks"][0]["task_id"], "task-good")

    def test_orphaned_claim_without_lease_is_recovered(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.send_task(root, "LWAR1", {"goal": "orphan", "task_id": "task-orphan"})
            self.watch_once(root, identity, expected=0)
            # Simulate a crash between the claim-move and the lease write: delete
            # the lease and age the claimed file past the grace window.
            (root / "mailbox" / "LWAR1" / "leases" / "task-orphan.json").unlink()
            claimed = self._claimed_path(root, "task-orphan")
            old = time.time() - 300
            os.utime(claimed, (old, old))
            _, recovered = self.run_module(
                "pao_runtime.oa_cli", "recover", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.assertEqual(recovered["count"], 1)
            self.assertEqual(recovered["tasks"][0]["task_id"], "task-orphan")
            incoming = list((root / "mailbox" / "LWAR1" / "incoming").glob("*task-orphan*.json"))
            self.assertEqual(len(incoming), 1)


class ReconcileHardeningTests(PaoTestCase):
    def test_corrupt_registration_request_is_quarantined_not_fatal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)  # establishes registry + a good flow
            requests = root / "control" / "registration" / "requests"
            requests.mkdir(parents=True, exist_ok=True)
            (requests / "poison.json").write_text("{ broken", encoding="utf-8")
            _, counts = self.run_module(
                "pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0
            )
            self.assertGreaterEqual(counts.get("quarantined", 0), 1)
            failed = list((root / "control" / "registration" / "failed").glob("poison.json"))
            self.assertEqual(len(failed), 1)

    def test_corrupt_writer_lease_does_not_wedge_mutations(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            lease = root / "var" / "oa" / "writer_lease.json"
            lease.parent.mkdir(parents=True, exist_ok=True)
            lease.write_text("not a lease", encoding="utf-8")
            # A mutating command must overwrite the corrupt lease, not crash.
            self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
            self.assertIn("oa_id", json.loads(lease.read_text(encoding="utf-8")))

    def test_replayed_registration_does_not_allocate_a_second_slot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def register_once():
                _, req = self.run_module(
                    "pao_runtime.lwar_cli", "register",
                    "--runtime-name", "R", "--model", "M", "--adapter-id", "r",
                    "--vendor-family", "v", "--interface", "tui",
                    "--instance-id", INSTANCE, "--root", str(root), expected=0,
                )
                self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
                _, resp = self.run_module(
                    "pao_runtime.lwar_cli", "response", req["request_id"], "--root", str(root), expected=0
                )
                return resp

            first = register_once()
            second = register_once()  # same instance_id, fresh request_id (a replay)
            self.assertEqual(first["lwar_id"], "LWAR1")
            self.assertEqual(second["lwar_id"], "LWAR1", "replay must reuse the slot, not allocate LWAR2")
            _, status = self.run_module(
                "pao_runtime.oa_cli", "status", "--root", str(root), expected=0
            )
            self.assertEqual([s["lwar_id"] for s in status["lwars"]], ["LWAR1"])


class SubmissionEdgeTests(PaoTestCase):
    def test_resend_of_a_terminal_task_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            draft = {"goal": "once", "task_id": "task-terminal", "workflow_id": "workflow-fixed"}
            self.send_task(root, "LWAR1", draft)
            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, "task-terminal")
            self.run_module("pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--root", str(root), expected=0)
            # Re-sending the same (workflow_id, task_id) after completion must be
            # refused rather than clobbering the ledger and re-executing.
            completed, _ = self.send_task(root, "LWAR1", draft, expected=1)
            self.assertIn("terminal", completed.stderr.lower())

    def test_double_complete_is_a_clean_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, pub = self.send_task(root, "LWAR1", {"goal": "one", "task_id": "task-dc"})
            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, "task-dc")
            completed = self.complete_task(root, identity, "task-dc", expected=1)[0]
            self.assertIn("already", completed.stderr.lower())
            self.assertNotIn("Traceback", completed.stderr)

    def test_corrupt_tombstone_still_cancels_without_crashing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.send_task(root, "LWAR1", {"goal": "cancel me", "task_id": "task-ct"})
            tombstone = root / "mailbox" / "LWAR1" / "cancelled" / "task-ct.json"
            tombstone.parent.mkdir(parents=True, exist_ok=True)
            tombstone.write_text("{{ not json", encoding="utf-8")
            # The watcher must auto-cancel (tombstone existence is the signal),
            # not crash on the unreadable content.
            self.watch_once(root, identity)
            result = root / "mailbox" / "LWAR1" / "outgoing" / "task-ct.result.json"
            self.assertTrue(result.is_file())
            self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["status"], "cancelled")


    def test_archive_pass_after_plain_collect_cleans_outgoing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.send_task(root, "LWAR1", {"goal": "cleanup", "task_id": "task-arch"})
            self.watch_once(root, identity, expected=0)
            self.complete_task(root, identity, "task-arch")
            outgoing = root / "mailbox" / "LWAR1" / "outgoing" / "task-arch.result.json"
            self.run_module("pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--root", str(root), expected=0)
            self.assertTrue(outgoing.is_file(), "plain collect leaves the result in outgoing/")
            # A later --archive pass over the already-collected result must still
            # move it out of outgoing/ (the O5 skip must not block --archive).
            _, archived = self.run_module(
                "pao_runtime.oa_cli", "collect", "--archive", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.assertEqual(archived["count"], 1)
            self.assertFalse(outgoing.is_file())
            self.assertTrue((root / "mailbox" / "LWAR1" / "archive" / "results" / "task-arch.result.json").is_file())


class WatcherGuardTests(PaoTestCase):
    def test_interval_greater_than_timeout_is_rejected(self):
        with TemporaryDirectory() as directory:
            completed, _ = self.run_module(
                "pao_runtime.adp_watch",
                "--identity-file", str(Path(directory) / "id.json"),
                "--interval", "10", "--timeout", "1",
                "--root", str(directory), expected=1,
            )
            self.assertIn("interval", completed.stderr.lower())


class RoutingHardeningTests(PaoTestCase):
    def test_corrupt_heartbeat_is_excluded_from_auto_routing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root, capabilities=("coding",))
            heartbeat = root / "mailbox" / "LWAR1" / "heartbeat.json"
            heartbeat.parent.mkdir(parents=True, exist_ok=True)
            heartbeat.write_text("not json", encoding="utf-8")
            draft = root / "auto.json"
            draft.write_text(json.dumps({"goal": "route me"}), encoding="utf-8")
            completed, _ = self.run_module(
                "pao_runtime.oa_cli", "send", "--auto", "--require-capability", "coding",
                "--task-file", str(draft), "--root", str(root),
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("no eligible LWAR", completed.stderr)


if __name__ == "__main__":
    unittest.main()
