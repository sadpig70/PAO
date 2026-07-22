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
from pao_runtime.common import FileLock


class OACommandMutexTests(PaoTestCase):
    def subprocess_env(self, oa_id="oa-test"):
        return {**os.environ, "PAO_OA_ID": oa_id, "PYTHONPATH": str(RUNTIME_HOME)}

    def start_holder(self, root, oa_id="oa-mutex", hold_s=60):
        holder_code = (
            "import sys,time\n"
            "from pathlib import Path\n"
            "from pao_runtime.oa_cli import renewable_oa_writer\n"
            "with renewable_oa_writer(Path(sys.argv[1])):\n"
            " print('locked', flush=True)\n"
            " time.sleep(float(sys.argv[2]))\n"
        )
        holder = subprocess.Popen(
            [sys.executable, "-c", holder_code, str(root), str(hold_s)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.subprocess_env(oa_id),
        )
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        return holder

    def test_same_oa_id_process_waits_for_active_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            holder = self.start_holder(root, hold_s=1.0)

            started = time.monotonic()
            self.run_module(
                "pao_runtime.oa_cli",
                "presence",
                "--root",
                str(root),
                env={"PAO_OA_ID": "oa-mutex"},
                expected=0,
            )
            waited_s = time.monotonic() - started
            _, holder_error = holder.communicate(timeout=5)

            self.assertEqual(holder.returncode, 0, holder_error)
            self.assertGreaterEqual(waited_s, 0.7)
            self.assertFalse((root / "var" / "oa" / ".command.lock").exists())

    def test_live_holder_is_not_stolen_even_when_lock_age_is_old(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            holder = self.start_holder(root)
            lock_path = root / "var" / "oa" / ".command.lock"
            old = time.time() - 60
            os.utime(lock_path, (old, old))
            try:
                with self.assertRaises(TimeoutError):
                    with FileLock(lock_path, timeout_s=0.2, stale_s=0.1):
                        self.fail("a live holder's command lock was stolen")
                self.assertIsNone(holder.poll())
            finally:
                holder.kill()
                holder.communicate(timeout=5)

    def test_killed_holder_lock_is_reaped_and_mutation_resumes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            holder = self.start_holder(root, oa_id="oa-crash")
            lock_path = root / "var" / "oa" / ".command.lock"
            crashed_lock = lock_path.read_text(encoding="utf-8")
            holder.kill()
            holder.communicate(timeout=5)
            self.assertTrue(lock_path.is_file())
            old = time.time() - 60
            os.utime(lock_path, (old, old))

            _, resumed = self.run_module(
                "pao_runtime.oa_cli",
                "presence",
                "--root",
                str(root),
                env={"PAO_OA_ID": "oa-crash"},
                expected=0,
            )

            self.assertEqual(resumed["event"], "oa_presence_published")
            self.assertFalse(lock_path.exists())
            self.assertIn(str(holder.pid), crashed_lock)

    def test_concurrent_send_and_startup_reap_end_in_one_safe_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            heartbeat_path = root / "mailbox" / "LWAR1" / "heartbeat.json"
            heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            heartbeat["last_seen"] = (
                datetime.now(timezone.utc) - timedelta(seconds=60)
            ).isoformat().replace("+00:00", "Z")
            heartbeat_path.write_text(json.dumps(heartbeat), encoding="utf-8")
            draft = root / "race-task.json"
            draft.write_text(json.dumps({"goal": "mutex dogfood"}), encoding="utf-8")

            send_command = [
                sys.executable,
                "-m",
                "pao_runtime.oa_cli",
                "send",
                "--lwar-id",
                "LWAR1",
                "--task-file",
                str(draft),
                "--root",
                str(root),
            ]
            reap_command = [
                sys.executable,
                "-m",
                "pao_runtime.oa_cli",
                "recover",
                "--reap-startup",
                "--lwar-id",
                "LWAR1",
                "--instance-id",
                identity["instance_id"],
                "--generation",
                str(identity["generation"]),
                "--root",
                str(root),
            ]
            send = subprocess.Popen(
                send_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.subprocess_env(),
            )
            reap = subprocess.Popen(
                reap_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.subprocess_env(),
            )
            send_out, send_err = send.communicate(timeout=40)
            reap_out, reap_err = reap.communicate(timeout=40)

            self.assertEqual([send.returncode, reap.returncode].count(0), 1)
            registry = json.loads(
                (root / "var" / "registry" / "lwar_registry.json").read_text(encoding="utf-8")
            )
            incoming = list((root / "mailbox" / "LWAR1" / "incoming").glob("*.json"))
            if "LWAR1" in registry["slots"]:
                self.assertEqual(send.returncode, 0, send_err)
                self.assertEqual(reap.returncode, 2, reap_err)
                self.assertEqual(len(incoming), 1)
                self.assertEqual(json.loads(reap_out)["reason"], "active_mailbox_work")
            else:
                self.assertEqual(reap.returncode, 0, reap_err)
                self.assertNotEqual(send.returncode, 0, send_out)
                self.assertEqual(incoming, [])


if __name__ == "__main__":
    unittest.main()
