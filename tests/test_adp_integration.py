import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO = Path(__file__).parents[1]
RUNTIME_HOME = REPO / "PAO_skills" / "pao-lwar"


class ADPIntegrationTests(unittest.TestCase):
    def run_module(self, module, *args, expected=None):
        completed = subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(RUNTIME_HOME)},
        )
        if expected is not None:
            self.assertEqual(completed.returncode, expected, completed.stderr + completed.stdout)
        payload = json.loads(completed.stdout) if completed.stdout.strip() else None
        return completed, payload

    def register_lwar(self, root, number=None):
        args = ["register"]
        if number is not None:
            args.append(str(number))
        args += [
            "--runtime-name", "Test TUI",
            "--model", "Test Model",
            "--adapter-id", "test_tui",
            "--vendor-family", "test_vendor",
            "--interface", "tui",
            "--capability", "coding",
            "--root", str(root),
        ]
        _, requested = self.run_module("pao_runtime.lwar_cli", *args, expected=0)
        self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
        _, adopted = self.run_module(
            "pao_runtime.lwar_cli",
            "response", requested["request_id"],
            "--root", str(root),
            expected=0,
        )
        return requested, adopted

    def test_full_oa_adp_lwar_result_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.assertEqual(identity["lwar_id"], "LWAR1")

            task_draft = root / "task.json"
            task_draft.write_text(
                json.dumps(
                    {
                        "goal": "Create a verified artifact",
                        "instructions": "Write the artifact and report evidence",
                        "completion_criteria": ["artifact exists"],
                        "cwd": str(root),
                        "timeout_s": 30,
                    }
                ),
                encoding="utf-8",
            )
            _, published = self.run_module(
                "pao_runtime.oa_cli",
                "send", "--lwar-id", "LWAR1", "--task-file", str(task_draft),
                "--root", str(root),
                expected=0,
            )
            _, event = self.run_module(
                "pao_runtime.adp_watch",
                "--identity-file", identity["identity_file"],
                "--interval", "0.01", "--timeout", "0.5", "--lease-seconds", "30",
                "--root", str(root),
                expected=0,
            )
            self.assertEqual(event["event"], "task_received")
            self.assertEqual(event["task_id"], published["task_id"])

            # Declared artifacts must actually exist: complete snapshots them.
            (root / "artifact.txt").write_text("artifact body", encoding="utf-8")
            result_draft = root / "result.json"
            result_draft.write_text(
                json.dumps(
                    {
                        "status": "succeeded",
                        "summary": "Artifact created",
                        "evidence": {"artifact_exists": True},
                        "artifacts": ["artifact.txt"],
                        "exit_code": 0,
                    }
                ),
                encoding="utf-8",
            )
            self.run_module(
                "pao_runtime.lwar_cli",
                "complete", "--identity-file", identity["identity_file"],
                "--task-id", published["task_id"], "--result-file", str(result_draft),
                "--root", str(root),
                expected=0,
            )
            _, collected = self.run_module(
                "pao_runtime.oa_cli", "collect", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.assertEqual(collected["count"], 1)
            self.assertEqual(collected["results"][0]["result"]["status"], "succeeded")

    def test_idle_timeout_exits_with_watch_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            _, event = self.run_module(
                "pao_runtime.adp_watch",
                "--identity-file", identity["identity_file"],
                "--interval", "0.01", "--timeout", "0.05",
                "--root", str(root),
                expected=10,
            )
            self.assertEqual(event["event"], "idle_timeout")
            self.assertEqual(event["action"], "watch_again")

    def test_explicit_alias_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root, number=1)
            args = [
                "register", "1", "--runtime-name", "Second", "--model", "Second Model",
                "--adapter-id", "second", "--vendor-family", "second", "--interface", "cli",
                "--root", str(root),
            ]
            _, request = self.run_module("pao_runtime.lwar_cli", *args, expected=0)
            self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
            _, response = self.run_module(
                "pao_runtime.lwar_cli", "response", request["request_id"], "--root", str(root), expected=3
            )
            self.assertEqual(response["reason"], "lwar_id_in_use")

    def test_off_state_blocks_task_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.run_module(
                "pao_runtime.lwar_cli", "state", "off", "--identity-file", identity["identity_file"],
                "--root", str(root), expected=0
            )
            self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
            task_draft = root / "task.json"
            task_draft.write_text(json.dumps({"goal": "Must not publish"}), encoding="utf-8")
            completed, _ = self.run_module(
                "pao_runtime.oa_cli", "send", "--lwar-id", "LWAR1", "--task-file", str(task_draft),
                "--root", str(root)
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("not on", completed.stderr)

    def test_expired_lease_returns_claimed_task_to_incoming(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            task_draft = root / "task.json"
            task_draft.write_text(json.dumps({"goal": "Recover me"}), encoding="utf-8")
            _, published = self.run_module(
                "pao_runtime.oa_cli", "send", "--lwar-id", "LWAR1", "--task-file", str(task_draft),
                "--root", str(root), expected=0
            )
            self.run_module(
                "pao_runtime.adp_watch", "--identity-file", identity["identity_file"],
                "--interval", "0.01", "--timeout", "0.2", "--lease-seconds", "30",
                "--root", str(root), expected=0
            )
            lease_path = root / "mailbox" / "LWAR1" / "leases" / f"{published['task_id']}.json"
            lease = json.loads(lease_path.read_text(encoding="utf-8"))
            lease["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            lease_path.write_text(json.dumps(lease), encoding="utf-8")
            _, recovered = self.run_module(
                "pao_runtime.oa_cli", "recover", "--lwar-id", "LWAR1", "--root", str(root), expected=0
            )
            self.assertEqual(recovered["count"], 1)
            incoming = list((root / "mailbox" / "LWAR1" / "incoming").glob("*.json"))
            self.assertEqual(len(incoming), 1)

    def test_shutdown_control_reaches_resident_lwar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, identity = self.register_lwar(root)
            self.run_module(
                "pao_runtime.oa_cli", "control", "--lwar-id", "LWAR1", "--command", "shutdown",
                "--root", str(root), expected=0
            )
            _, event = self.run_module(
                "pao_runtime.adp_watch", "--identity-file", identity["identity_file"],
                "--interval", "0.01", "--timeout", "0.2", "--root", str(root), expected=20
            )
            self.assertEqual(event["event"], "control")
            self.assertEqual(event["command"], "shutdown")

    def test_reused_alias_increments_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, first = self.register_lwar(root)
            self.assertEqual(first["generation"], 1)
            self.run_module(
                "pao_runtime.lwar_cli", "state", "off", "--identity-file", first["identity_file"],
                "--root", str(root), expected=0
            )
            self.run_module(
                "pao_runtime.oa_cli", "reconcile", "--tombstone-retention", "0", "--root", str(root), expected=0
            )
            self.run_module(
                "pao_runtime.lwar_cli", "state", "deregistered", "--identity-file", first["identity_file"],
                "--root", str(root), expected=0
            )
            self.run_module(
                "pao_runtime.oa_cli", "reconcile", "--tombstone-retention", "0", "--root", str(root), expected=0
            )
            _, second = self.register_lwar(root)
            self.assertEqual(second["lwar_id"], "LWAR1")
            self.assertEqual(second["generation"], 2)


if __name__ == "__main__":
    unittest.main()
