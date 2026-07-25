import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pao_helpers import PaoTestCase, REPO, RUNTIME_HOME


class HostTimeoutRecoveryTests(PaoTestCase):
    def test_response_replay_redelivers_same_claim_without_reregistering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, requested = self.run_module(
                "pao_runtime.lwar_cli",
                "register",
                "--runtime-name",
                "Test Agent",
                "--model",
                "Test Model",
                "--adapter-id",
                "test_agent",
                "--vendor-family",
                "test_vendor",
                "--interface",
                "agent",
                "--capability",
                "coding",
                "--root",
                str(root),
                expected=0,
            )
            self.run_module(
                "pao_runtime.oa_cli",
                "reconcile",
                "--root",
                str(root),
                expected=0,
            )
            _, published = self.send_task(
                root,
                "LWAR1",
                {
                    "goal": "Survive a host tool timeout",
                    "workflow_id": "workflow-host-timeout",
                    "timeout_s": 60,
                },
            )

            resident_args = (
                "response",
                requested["request_id"],
                "--resident",
                "--interval",
                "0.01",
                "--timeout",
                "0.2",
                "--lease-seconds",
                "90",
                "--root",
                str(root),
            )
            _, first_delivery = self.run_module(
                "pao_runtime.lwar_cli", *resident_args, expected=0
            )
            self.assertEqual(first_delivery["event"], "task_received")
            self.assertEqual(first_delivery["task_id"], published["task_id"])
            self.assertNotIn("recovered_claim", first_delivery)
            self.assertEqual(first_delivery["invocation_epoch"], 1)
            self.assertEqual(
                first_delivery["execution_id"],
                first_delivery["task"]["execution_id"],
            )

            # Simulate a host blocking-call timeout: stdout (including the
            # identity handle and TaskContract) is discarded, while the claim
            # and lease remain durable. The adapter retains only request_id and
            # the explicit bus root from before the blocking call.
            _, recovered = self.run_module(
                "pao_runtime.lwar_cli", *resident_args, expected=0
            )
            self.assertTrue(recovered["recovered_claim"])
            self.assertEqual(recovered["invocation_epoch"], 2)
            self.assertNotEqual(
                recovered["invocation_id"], first_delivery["invocation_id"]
            )
            self.assertEqual(recovered["identity_file"], first_delivery["identity_file"])
            self.assertEqual(recovered["message_file"], first_delivery["message_file"])
            self.assertEqual(
                recovered["task"]["claim_token"],
                first_delivery["task"]["claim_token"],
            )

            registry = json.loads(
                (root / "var" / "registry" / "lwar_registry.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(registry["registry_version"], 1)
            self.assertEqual(registry["slots"]["LWAR1"]["generation"], 1)
            self.assertEqual(
                len(list((root / "var" / "identities").glob("*.json"))),
                1,
            )
            self.assertEqual(
                len(list((root / "mailbox" / "LWAR1" / "claimed").glob("*.json"))),
                1,
            )

            identity = {
                "lwar_id": "LWAR1",
                "identity_file": recovered["identity_file"],
            }
            self.complete_task(root, identity, published["task_id"])
            _, collected = self.run_module(
                "pao_runtime.oa_cli",
                "collect",
                "--lwar-id",
                "LWAR1",
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(collected["count"], 1)
            self.assertEqual(collected["quarantined"], [])
            self.assertEqual(
                collected["results"][0]["result"]["claim_token"],
                recovered["task"]["claim_token"],
            )

            events = [
                json.loads(line)["event"]
                for line in (root / "var" / "audit" / "events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(events.count("task_redelivered"), 1)

    def test_delayed_orphan_delivery_is_epoch_fenced_and_one_begin_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, published = self.send_task(
                root,
                "LWAR1",
                {
                    "goal": "Fence a delayed orphan delivery",
                    "workflow_id": "workflow-orphan-fence",
                    "timeout_s": 60,
                },
            )

            fault_code = (
                "import contextlib,sys,time\n"
                "from pao_runtime import adp_watch\n"
                "from pao_runtime.transport import FileTransport\n"
                "real=FileTransport.invocation_delivery_guard\n"
                "@contextlib.contextmanager\n"
                "def delayed(self,identity,invocation):\n"
                " time.sleep(1.0)\n"
                " with real(self,identity,invocation) as current:\n"
                "  yield current\n"
                "FileTransport.invocation_delivery_guard=delayed\n"
                "sys.argv=['adp-watch','--identity-file',sys.argv[1],"
                "'--interval','0.01','--timeout','2','--lease-seconds','90',"
                "'--root',sys.argv[2]]\n"
                "raise SystemExit(adp_watch.main())\n"
            )
            orphan = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    fault_code,
                    identity["identity_file"],
                    str(root),
                ],
                cwd=REPO,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env={
                    **os.environ,
                    "PAO_OA_ID": "oa-test",
                    "PYTHONPATH": str(RUNTIME_HOME),
                },
            )
            claimed_dir = root / "mailbox" / "LWAR1" / "claimed"
            deadline = time.monotonic() + 5
            while not list(claimed_dir.glob("*.json")) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(list(claimed_dir.glob("*.json")), "fault process never claimed task")

            _, replay = self.watch_once(root, identity, lease_seconds=90, expected=0)
            self.assertTrue(replay["recovered_claim"])
            self.assertEqual(replay["task_id"], published["task_id"])
            orphan_stdout, orphan_stderr = orphan.communicate(timeout=5)
            self.assertEqual(orphan.returncode, 40, orphan_stderr + orphan_stdout)
            orphan_event = json.loads(orphan_stdout)
            self.assertEqual(orphan_event["event"], "invocation_superseded")
            self.assertLess(
                orphan_event["invocation_epoch"], replay["invocation_epoch"]
            )

            begin_args = (
                "begin",
                "--identity-file",
                identity["identity_file"],
                "--task-id",
                replay["task_id"],
                "--claim-token",
                replay["task"]["claim_token"],
                "--execution-id",
                replay["execution_id"],
                "--invocation-id",
                replay["invocation_id"],
                "--root",
                str(root),
            )
            _, began = self.run_module(
                "pao_runtime.lwar_cli", *begin_args, expected=0
            )
            _, repeated = self.run_module(
                "pao_runtime.lwar_cli", *begin_args, expected=0
            )
            self.assertTrue(repeated["recovered"])
            self.assertEqual(
                began["execution_token"], repeated["execution_token"]
            )

            # A new replay is current for delivery, but cannot steal execution
            # from the invocation that already passed begin.
            _, later = self.watch_once(root, identity, lease_seconds=90, expected=0)
            self.assertGreater(later["invocation_epoch"], replay["invocation_epoch"])
            _, fenced = self.run_module(
                "pao_runtime.lwar_cli",
                "begin",
                "--identity-file",
                identity["identity_file"],
                "--task-id",
                later["task_id"],
                "--claim-token",
                later["task"]["claim_token"],
                "--execution-id",
                later["execution_id"],
                "--invocation-id",
                later["invocation_id"],
                "--root",
                str(root),
                expected=4,
            )
            self.assertEqual(fenced["event"], "execution_fenced")

            result_path = root / "winning-result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "status": "succeeded",
                        "summary": "one execution grant",
                        "evidence": {"execution_tokens": 1},
                        "artifacts": [],
                        "exit_code": 0,
                    }
                ),
                encoding="utf-8",
            )
            self.run_module(
                "pao_runtime.lwar_cli",
                "complete",
                "--identity-file",
                identity["identity_file"],
                "--task-id",
                replay["task_id"],
                "--claim-token",
                replay["task"]["claim_token"],
                "--execution-token",
                began["execution_token"],
                "--result-file",
                str(result_path),
                "--root",
                str(root),
                expected=0,
            )
            _, collected = self.run_module(
                "pao_runtime.oa_cli",
                "collect",
                "--lwar-id",
                "LWAR1",
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(collected["count"], 1)
            self.assertEqual(collected["quarantined"], [])


if __name__ == "__main__":
    unittest.main()
