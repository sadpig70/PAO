import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


REPO = Path(__file__).parents[1]
# PAO_skills is the canonical source; PAO_plugin is frozen (kept only for
# frozen-artifact assertions in the packaging/installer tests).
PLUGIN = REPO / "PAO_plugin"
RUNTIME_HOME = REPO / "PAO_skills" / "pao-lwar"

if str(RUNTIME_HOME) not in sys.path:
    sys.path.insert(0, str(RUNTIME_HOME))


class PaoTestCase(unittest.TestCase):
    """Shared subprocess harness for PAO integration suites."""

    def run_module(self, module, *args, expected=None, env=None, cwd=None):
        merged_env = {**os.environ, **(env or {})}
        if "PYTHONPATH" not in (env or {}):
            merged_env["PYTHONPATH"] = str(RUNTIME_HOME)
        completed = subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=cwd or REPO,
            check=False,
            capture_output=True,
            text=True,
            env=merged_env,
        )
        if expected is not None:
            self.assertEqual(completed.returncode, expected, completed.stderr + completed.stdout)
        payload = json.loads(completed.stdout) if completed.stdout.strip() else None
        return completed, payload

    def register_lwar(self, root, number=None, capabilities=("coding",)):
        args = ["register"]
        if number is not None:
            args.append(str(number))
        args += [
            "--runtime-name", "Test TUI",
            "--model", "Test Model",
            "--adapter-id", "test_tui",
            "--vendor-family", "test_vendor",
            "--interface", "tui",
        ]
        for capability in capabilities:
            args += ["--capability", capability]
        args += ["--root", str(root)]
        _, requested = self.run_module("pao_runtime.lwar_cli", *args, expected=0)
        self.run_module("pao_runtime.oa_cli", "reconcile", "--root", str(root), expected=0)
        _, adopted = self.run_module(
            "pao_runtime.lwar_cli",
            "response", requested["request_id"],
            "--root", str(root),
            expected=0,
        )
        return requested, adopted

    def send_task(self, root, lwar_id, draft, expected=0):
        draft_path = root / f"draft_{abs(hash(json.dumps(draft, sort_keys=True)))}.json"
        draft_path.write_text(json.dumps(draft), encoding="utf-8")
        return self.run_module(
            "pao_runtime.oa_cli",
            "send", "--lwar-id", lwar_id, "--task-file", str(draft_path),
            "--root", str(root),
            expected=expected,
        )

    def watch_once(self, root, identity, lease_seconds=30, timeout="0.5", expected=None):
        return self.run_module(
            "pao_runtime.adp_watch",
            "--identity-file", identity["identity_file"],
            "--interval", "0.01", "--timeout", timeout,
            "--lease-seconds", str(lease_seconds),
            "--root", str(root),
            expected=expected,
        )

    def complete_task(self, root, identity, task_id, result=None, expected=0):
        payload = result or {
            "status": "succeeded",
            "summary": "done",
            "evidence": {"ok": True},
            "artifacts": [],
            "exit_code": 0,
        }
        result_path = root / f"result_{task_id}.json"
        result_path.write_text(json.dumps(payload), encoding="utf-8")
        return self.run_module(
            "pao_runtime.lwar_cli",
            "complete", "--identity-file", identity["identity_file"],
            "--task-id", task_id, "--result-file", str(result_path),
            "--root", str(root),
            expected=expected,
        )

    def expire_lease(self, root, lwar_id, task_id):
        from datetime import datetime, timedelta, timezone

        lease_path = root / "mailbox" / lwar_id / "leases" / f"{task_id}.json"
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        lease["expires_at"] = (
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        )
        lease_path.write_text(json.dumps(lease), encoding="utf-8")
        return lease
