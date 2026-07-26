import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pao_helpers import PaoTestCase, RUNTIME_HOME
from pao_runtime.transport import FileTransport


class StaleRetirementTests(PaoTestCase):
    def make_heartbeat(
        self,
        root: Path,
        identity: dict,
        *,
        status: str = "idle",
        age_s: float = 300,
        current_task_id: str | None = None,
    ) -> str:
        transport = FileTransport(root)
        transport.write_heartbeat(identity, status, current_task_id)
        heartbeat_path = root / "mailbox" / identity["lwar_id"] / "heartbeat.json"
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        heartbeat["last_seen"] = (
            datetime.now(timezone.utc) - timedelta(seconds=age_s)
        ).isoformat().replace("+00:00", "Z")
        heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")
        return heartbeat["last_seen"]

    def retire(
        self,
        root: Path,
        identity: dict,
        expected_last_seen: str,
        *,
        expected: int = 0,
        **overrides,
    ):
        return self.run_module(
            "pao_runtime.oa_cli",
            "recover",
            "--retire-stale",
            "--lwar-id",
            overrides.get("lwar_id", identity["lwar_id"]),
            "--instance-id",
            overrides.get("instance_id", identity["instance_id"]),
            "--generation",
            str(overrides.get("generation", identity["generation"])),
            "--expected-last-seen",
            overrides.get("expected_last_seen", expected_last_seen),
            "--stale-after",
            str(overrides.get("stale_after", 120)),
            "--reason",
            overrides.get("reason", "replace failed provider generation"),
            "--root",
            str(root),
            expected=expected,
        )

    def test_exact_stale_idle_generation_is_retired_and_replay_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            last_seen = self.make_heartbeat(root, identity)

            _, outcome = self.retire(root, identity, last_seen)

            self.assertEqual(outcome["event"], "stale_slot_retired")
            self.assertTrue(outcome["stale_confirmed"])
            registry_path = root / "var" / "registry" / "lwar_registry.json"
            tombstones_path = root / "var" / "registry" / "tombstones.json"
            self.assertNotIn(
                identity["lwar_id"],
                json.loads(registry_path.read_text(encoding="utf-8"))["slots"],
            )
            tombstone = json.loads(
                tombstones_path.read_text(encoding="utf-8")
            )["entries"][identity["lwar_id"]]
            self.assertEqual(tombstone["retirement_mode"], "stale_idle_reap")
            self.assertEqual(tombstone["expected_last_seen"], last_seen)

            stable_registry = registry_path.read_bytes()
            stable_tombstones = tombstones_path.read_bytes()
            _, replay = self.retire(root, identity, last_seen)
            self.assertEqual(replay["reason"], "already_retired")
            self.assertEqual(registry_path.read_bytes(), stable_registry)
            self.assertEqual(tombstones_path.read_bytes(), stable_tombstones)

            events = [
                json.loads(line)
                for line in (root / "var" / "audit" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                sum(event.get("event") == "stale_identity_confirmed" for event in events),
                1,
            )
            self.assertEqual(
                sum(event.get("event") == "stale_slot_retired" for event in events),
                1,
            )

    def test_replay_requires_the_same_operator_fences(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            last_seen = self.make_heartbeat(root, identity)
            self.retire(root, identity, last_seen)

            _, threshold_drift = self.retire(
                root, identity, last_seen, expected=2, stale_after=121
            )
            _, reason_drift = self.retire(
                root,
                identity,
                last_seen,
                expected=2,
                reason="different operator decision",
            )

            self.assertEqual(threshold_drift["reason"], "lwar_not_registered")
            self.assertEqual(reason_drift["reason"], "lwar_not_registered")

    def test_fresh_heartbeat_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            last_seen = self.make_heartbeat(root, identity, age_s=0)

            _, outcome = self.retire(root, identity, last_seen, expected=2)

            self.assertEqual(outcome["reason"], "heartbeat_not_stale")
            registry = json.loads(
                (root / "var" / "registry" / "lwar_registry.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn(identity["lwar_id"], registry["slots"])

    def test_changed_heartbeat_observation_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            old_last_seen = self.make_heartbeat(root, identity)
            self.make_heartbeat(root, identity, age_s=250)

            _, outcome = self.retire(root, identity, old_last_seen, expected=2)

            self.assertEqual(outcome["reason"], "heartbeat_observation_changed")

    def test_running_or_task_bearing_heartbeat_is_preserved(self):
        cases = (
            ("running", None),
            ("idle", "task-still-running"),
        )
        for status, task_id in cases:
            with self.subTest(status=status, task_id=task_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, identity = self.register_lwar(root)
                last_seen = self.make_heartbeat(
                    root,
                    identity,
                    status=status,
                    current_task_id=task_id,
                )

                _, outcome = self.retire(root, identity, last_seen, expected=2)

                self.assertEqual(outcome["reason"], "heartbeat_not_idle")

    def test_starting_heartbeat_uses_the_startup_reap_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            heartbeat = json.loads(
                (root / "mailbox" / "LWAR1" / "heartbeat.json").read_text(
                    encoding="utf-8"
                )
            )

            _, outcome = self.retire(
                root, identity, heartbeat["last_seen"], expected=2, stale_after=0.001
            )

            self.assertEqual(outcome["reason"], "heartbeat_starting")

    def test_identity_mismatch_cannot_retire_current_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            last_seen = self.make_heartbeat(root, identity)

            _, outcome = self.retire(
                root,
                identity,
                last_seen,
                expected=2,
                generation=identity["generation"] + 1,
            )

            self.assertEqual(outcome["reason"], "identity_mismatch")

    def test_every_active_mailbox_channel_blocks_retirement(self):
        for channel in (
            "incoming",
            "claimed",
            "leases",
            "outgoing",
            "control",
            "control_claimed",
        ):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, identity = self.register_lwar(root)
                last_seen = self.make_heartbeat(root, identity)
                marker = root / "mailbox" / "LWAR1" / channel / "active.json"
                marker.write_text("{}", encoding="utf-8")

                _, outcome = self.retire(root, identity, last_seen, expected=2)

                self.assertEqual(outcome["reason"], "active_mailbox_work")
                self.assertEqual(outcome["active_work"], {channel: 1})
                self.assertTrue(marker.is_file())

    def test_tombstone_first_crash_converges_on_exact_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            last_seen = self.make_heartbeat(root, identity)
            fault_code = (
                "import os,sys\n"
                "from pathlib import Path\n"
                "from pao_runtime import oa_cli,registry\n"
                "real_write=registry.atomic_write_json\n"
                "registry_path=(Path(sys.argv[1])/'var'/'registry'/'lwar_registry.json').resolve()\n"
                "def crash_on_registry(path,payload):\n"
                " if Path(path).resolve()==registry_path:\n"
                "  os._exit(97)\n"
                " return real_write(path,payload)\n"
                "registry.atomic_write_json=crash_on_registry\n"
                "sys.argv=['oa','recover','--retire-stale','--lwar-id','LWAR1',"
                "'--instance-id',sys.argv[2],'--generation',sys.argv[3],"
                "'--expected-last-seen',sys.argv[4],'--stale-after','120',"
                "'--reason','replace failed provider generation','--root',sys.argv[1]]\n"
                "raise SystemExit(oa_cli.main())\n"
            )
            crashed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    fault_code,
                    str(root),
                    identity["instance_id"],
                    str(identity["generation"]),
                    last_seen,
                ],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PAO_OA_ID": "oa-test",
                    "PYTHONPATH": str(RUNTIME_HOME),
                },
                check=False,
            )
            self.assertEqual(crashed.returncode, 97, crashed.stderr + crashed.stdout)

            registry_path = root / "var" / "registry" / "lwar_registry.json"
            tombstones_path = root / "var" / "registry" / "tombstones.json"
            self.assertIn(
                "LWAR1",
                json.loads(registry_path.read_text(encoding="utf-8"))["slots"],
            )
            self.assertEqual(
                json.loads(tombstones_path.read_text(encoding="utf-8"))["entries"][
                    "LWAR1"
                ]["retirement_mode"],
                "stale_idle_reap",
            )

            old = time.time() - 60
            for lock_path in (
                root / "var" / "oa" / ".command.lock",
                root / "var" / "registry" / ".registry.lock",
            ):
                self.assertTrue(lock_path.is_file())
                os.utime(lock_path, (old, old))

            _, recovered = self.retire(root, identity, last_seen)
            self.assertEqual(recovered["event"], "stale_slot_retired")
            self.assertNotIn(
                "LWAR1",
                json.loads(registry_path.read_text(encoding="utf-8"))["slots"],
            )

    def test_post_registry_crash_replays_missing_audit_without_state_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            last_seen = self.make_heartbeat(root, identity)
            fault_code = (
                "import os,sys\n"
                "from pathlib import Path\n"
                "from pao_runtime import oa_cli,registry\n"
                "real_write=registry.atomic_write_json\n"
                "registry_path=(Path(sys.argv[1])/'var'/'registry'/'lwar_registry.json').resolve()\n"
                "def crash_after_registry(path,payload):\n"
                " real_write(path,payload)\n"
                " if Path(path).resolve()==registry_path:\n"
                "  os._exit(98)\n"
                "registry.atomic_write_json=crash_after_registry\n"
                "sys.argv=['oa','recover','--retire-stale','--lwar-id','LWAR1',"
                "'--instance-id',sys.argv[2],'--generation',sys.argv[3],"
                "'--expected-last-seen',sys.argv[4],'--stale-after','120',"
                "'--reason','replace failed provider generation','--root',sys.argv[1]]\n"
                "raise SystemExit(oa_cli.main())\n"
            )
            crashed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    fault_code,
                    str(root),
                    identity["instance_id"],
                    str(identity["generation"]),
                    last_seen,
                ],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PAO_OA_ID": "oa-test",
                    "PYTHONPATH": str(RUNTIME_HOME),
                },
                check=False,
            )
            self.assertEqual(crashed.returncode, 98, crashed.stderr + crashed.stdout)

            registry_path = root / "var" / "registry" / "lwar_registry.json"
            tombstones_path = root / "var" / "registry" / "tombstones.json"
            stable_registry = registry_path.read_bytes()
            stable_tombstones = tombstones_path.read_bytes()
            events_before = [
                json.loads(line)
                for line in (root / "var" / "audit" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertFalse(
                any(event.get("event") == "stale_slot_retired" for event in events_before)
            )

            command_lock = root / "var" / "oa" / ".command.lock"
            registry_lock = root / "var" / "registry" / ".registry.lock"
            old = time.time() - 60
            for lock_path in (command_lock, registry_lock):
                self.assertTrue(lock_path.is_file())
                os.utime(lock_path, (old, old))

            _, replay = self.retire(root, identity, last_seen)

            self.assertEqual(replay["reason"], "already_retired")
            self.assertEqual(registry_path.read_bytes(), stable_registry)
            self.assertEqual(tombstones_path.read_bytes(), stable_tombstones)
            events_after = [
                json.loads(line)
                for line in (root / "var" / "audit" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                sum(
                    event.get("event") == "stale_identity_confirmed"
                    for event in events_after
                ),
                1,
            )
            self.assertEqual(
                sum(event.get("event") == "stale_slot_retired" for event in events_after),
                1,
            )


if __name__ == "__main__":
    unittest.main()
