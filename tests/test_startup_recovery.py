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


class StartupRecoveryTests(PaoTestCase):
    def make_overdue(self, root: Path) -> None:
        heartbeat_path = root / "mailbox" / "LWAR1" / "heartbeat.json"
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        heartbeat["last_seen"] = (
            datetime.now(timezone.utc) - timedelta(seconds=60)
        ).isoformat().replace("+00:00", "Z")
        heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")

    def reap(self, root: Path, identity: dict, expected: int = 0, **overrides):
        return self.run_module(
            "pao_runtime.oa_cli",
            "recover",
            "--reap-startup",
            "--lwar-id",
            "LWAR1",
            "--instance-id",
            overrides.get("instance_id", identity["instance_id"]),
            "--generation",
            str(overrides.get("generation", identity["generation"])),
            "--startup-deadline",
            str(overrides.get("startup_deadline", 30)),
            "--root",
            str(root),
            expected=expected,
        )

    def test_overdue_starting_slot_is_reaped_and_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.make_overdue(root)

            _, outcome = self.reap(root, identity)

            self.assertEqual(outcome["event"], "startup_slot_reaped")
            self.assertTrue(outcome["deadline_missed"])
            registry = json.loads(
                (root / "var" / "registry" / "lwar_registry.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("LWAR1", registry["slots"])
            tombstones = json.loads(
                (root / "var" / "registry" / "tombstones.json").read_text(encoding="utf-8")
            )
            tombstone = tombstones["entries"]["LWAR1"]
            self.assertEqual(tombstone["instance_id"], identity["instance_id"])
            self.assertEqual(tombstone["last_generation"], identity["generation"])
            events = [
                json.loads(line)["event"]
                for line in (root / "var" / "audit" / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertIn("startup_deadline_missed", events)
            self.assertIn("startup_slot_reaped", events)

            _, replay = self.reap(root, identity)
            self.assertEqual(replay["reason"], "already_reaped")

    def test_crash_after_tombstone_before_registry_write_converges_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.make_overdue(root)
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
                "sys.argv=['oa','recover','--reap-startup','--lwar-id','LWAR1',"
                "'--instance-id',sys.argv[2],'--generation',sys.argv[3],'--root',sys.argv[1]]\n"
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
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PAO_OA_ID": "oa-test", "PYTHONPATH": str(RUNTIME_HOME)},
                check=False,
            )
            self.assertEqual(crashed.returncode, 97, crashed.stderr + crashed.stdout)

            registry_path = root / "var" / "registry" / "lwar_registry.json"
            tombstones_path = root / "var" / "registry" / "tombstones.json"
            partial_registry = json.loads(registry_path.read_text(encoding="utf-8"))
            partial_tombstones = json.loads(tombstones_path.read_text(encoding="utf-8"))
            self.assertIn("LWAR1", partial_registry["slots"])
            self.assertEqual(partial_registry["registry_version"], 1)
            self.assertEqual(
                partial_tombstones["entries"]["LWAR1"]["last_generation"],
                identity["generation"],
            )

            orphan_locks = [
                root / "var" / "oa" / ".command.lock",
                root / "var" / "registry" / ".registry.lock",
            ]
            old = time.time() - 60
            for lock_path in orphan_locks:
                self.assertTrue(lock_path.is_file())
                os.utime(lock_path, (old, old))

            _, recovered = self.reap(root, identity)

            self.assertEqual(recovered["event"], "startup_slot_reaped")
            final_registry = json.loads(registry_path.read_text(encoding="utf-8"))
            final_tombstones = json.loads(tombstones_path.read_text(encoding="utf-8"))
            self.assertNotIn("LWAR1", final_registry["slots"])
            self.assertEqual(final_registry["registry_version"], 2)
            self.assertEqual(
                final_tombstones["entries"]["LWAR1"]["last_generation"],
                identity["generation"],
            )
            self.assertTrue(all(not path.exists() for path in orphan_locks))

            _, replay = self.reap(root, identity)
            self.assertEqual(replay["reason"], "already_reaped")

    def test_crash_after_registry_write_before_response_replays_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.make_overdue(root)
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
                "sys.argv=['oa','recover','--reap-startup','--lwar-id','LWAR1',"
                "'--instance-id',sys.argv[2],'--generation',sys.argv[3],'--root',sys.argv[1]]\n"
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
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PAO_OA_ID": "oa-test", "PYTHONPATH": str(RUNTIME_HOME)},
                check=False,
            )
            self.assertEqual(crashed.returncode, 98, crashed.stderr + crashed.stdout)

            registry_path = root / "var" / "registry" / "lwar_registry.json"
            tombstones_path = root / "var" / "registry" / "tombstones.json"
            committed_registry = registry_path.read_bytes()
            committed_tombstones = tombstones_path.read_bytes()
            registry_state = json.loads(committed_registry)
            tombstone_state = json.loads(committed_tombstones)
            self.assertNotIn("LWAR1", registry_state["slots"])
            self.assertEqual(registry_state["registry_version"], 2)
            self.assertEqual(
                tombstone_state["entries"]["LWAR1"]["last_generation"],
                identity["generation"],
            )

            audit_path = root / "var" / "audit" / "events.jsonl"
            before_events = [json.loads(line)["event"] for line in audit_path.read_text(
                encoding="utf-8"
            ).splitlines()]
            self.assertNotIn("startup_deadline_missed", before_events)
            self.assertNotIn("startup_slot_reaped", before_events)

            orphan_locks = [
                root / "var" / "oa" / ".command.lock",
                root / "var" / "registry" / ".registry.lock",
            ]
            old = time.time() - 60
            for lock_path in orphan_locks:
                self.assertTrue(lock_path.is_file())
                os.utime(lock_path, (old, old))

            _, replay = self.reap(root, identity)

            self.assertEqual(replay["reason"], "already_reaped")
            self.assertEqual(replay["registry_version"], 2)
            self.assertEqual(registry_path.read_bytes(), committed_registry)
            self.assertEqual(tombstones_path.read_bytes(), committed_tombstones)
            self.assertTrue(all(not path.exists() for path in orphan_locks))
            after_events = [json.loads(line)["event"] for line in audit_path.read_text(
                encoding="utf-8"
            ).splitlines()]
            self.assertEqual(after_events.count("startup_deadline_missed"), 1)
            self.assertEqual(after_events.count("startup_slot_reaped"), 1)

    def test_crash_between_startup_audits_replays_only_missing_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.make_overdue(root)
            fault_code = (
                "import os,sys\n"
                "from pao_runtime import audit,oa_cli\n"
                "real_record_once=audit.record_once\n"
                "calls=0\n"
                "def crash_after_first(*args,**kwargs):\n"
                " global calls\n"
                " result=real_record_once(*args,**kwargs)\n"
                " calls+=1\n"
                " if calls==1:\n"
                "  os._exit(99)\n"
                " return result\n"
                "audit.record_once=crash_after_first\n"
                "sys.argv=['oa','recover','--reap-startup','--lwar-id','LWAR1',"
                "'--instance-id',sys.argv[2],'--generation',sys.argv[3],'--root',sys.argv[1]]\n"
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
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PAO_OA_ID": "oa-test", "PYTHONPATH": str(RUNTIME_HOME)},
                check=False,
            )
            self.assertEqual(crashed.returncode, 99, crashed.stderr + crashed.stdout)

            registry_path = root / "var" / "registry" / "lwar_registry.json"
            tombstones_path = root / "var" / "registry" / "tombstones.json"
            committed_registry = registry_path.read_bytes()
            committed_tombstones = tombstones_path.read_bytes()
            audit_path = root / "var" / "audit" / "events.jsonl"
            partial_audit = [json.loads(line) for line in audit_path.read_text(
                encoding="utf-8"
            ).splitlines()]
            self.assertEqual(
                sum(event.get("event") == "startup_deadline_missed" for event in partial_audit),
                1,
            )
            self.assertEqual(
                sum(event.get("event") == "startup_slot_reaped" for event in partial_audit),
                0,
            )

            command_lock = root / "var" / "oa" / ".command.lock"
            self.assertTrue(command_lock.is_file())
            old = time.time() - 60
            os.utime(command_lock, (old, old))

            _, replay = self.reap(root, identity)

            self.assertEqual(replay["reason"], "already_reaped")
            self.assertEqual(registry_path.read_bytes(), committed_registry)
            self.assertEqual(tombstones_path.read_bytes(), committed_tombstones)
            recovered_audit = [json.loads(line) for line in audit_path.read_text(
                encoding="utf-8"
            ).splitlines()]
            deadline_events = [
                event for event in recovered_audit
                if event.get("event") == "startup_deadline_missed"
            ]
            reaped_events = [
                event for event in recovered_audit
                if event.get("event") == "startup_slot_reaped"
            ]
            self.assertEqual(len(deadline_events), 1)
            self.assertEqual(len(reaped_events), 1)
            self.assertNotEqual(
                deadline_events[0]["idempotency_key"],
                reaped_events[0]["idempotency_key"],
            )

            stable_audit = audit_path.read_bytes()
            _, second_replay = self.reap(root, identity)
            self.assertEqual(second_replay["reason"], "already_reaped")
            self.assertEqual(audit_path.read_bytes(), stable_audit)

    def test_fresh_starting_slot_is_not_reaped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)

            _, outcome = self.reap(root, identity, expected=2)

            self.assertEqual(outcome["reason"], "startup_deadline_not_missed")
            registry = json.loads(
                (root / "var" / "registry" / "lwar_registry.json").read_text(encoding="utf-8")
            )
            self.assertIn("LWAR1", registry["slots"])

    def test_stale_operator_identity_cannot_reap_current_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.make_overdue(root)

            _, outcome = self.reap(
                root, identity, expected=2, generation=identity["generation"] + 1
            )

            self.assertEqual(outcome["reason"], "identity_mismatch")
            registry = json.loads(
                (root / "var" / "registry" / "lwar_registry.json").read_text(encoding="utf-8")
            )
            self.assertIn("LWAR1", registry["slots"])

    def test_active_mailbox_work_blocks_reap_without_data_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.make_overdue(root)
            _, published = self.send_task(root, "LWAR1", {"goal": "preserve me"})

            _, outcome = self.reap(root, identity, expected=2)

            self.assertEqual(outcome["reason"], "active_mailbox_work")
            self.assertEqual(outcome["active_work"], {"incoming": 1})
            incoming = list((root / "mailbox" / "LWAR1" / "incoming").glob("*.json"))
            self.assertEqual(len(incoming), 1)
            self.assertIn(published["task_id"], incoming[0].name)

    def test_every_active_mailbox_channel_blocks_reap(self):
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
                self.make_overdue(root)
                marker = root / "mailbox" / "LWAR1" / channel / "active.json"
                marker.write_text("{}", encoding="utf-8")

                _, outcome = self.reap(root, identity, expected=2)

                self.assertEqual(outcome["reason"], "active_mailbox_work")
                self.assertEqual(outcome["active_work"], {channel: 1})
                self.assertTrue(marker.is_file())

    def test_started_runtime_cannot_be_reaped_as_startup_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            FileTransport(root).write_heartbeat(identity, "idle", None)

            _, outcome = self.reap(root, identity, expected=2, startup_deadline=0.001)

            self.assertEqual(outcome["reason"], "heartbeat_not_starting")
            registry = json.loads(
                (root / "var" / "registry" / "lwar_registry.json").read_text(encoding="utf-8")
            )
            self.assertIn("LWAR1", registry["slots"])


if __name__ == "__main__":
    unittest.main()
