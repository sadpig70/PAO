import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pao_helpers import PaoTestCase, REPO
from pao_runtime import __version__


class V1ProtocolTests(PaoTestCase):
    def test_runtime_reports_v1(self):
        self.assertEqual(__version__, "1.0.0")

    def test_unknown_permission_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.register_lwar(root)
            completed, _ = self.send_task(
                root,
                "LWAR1",
                {
                    "goal": "strict permission schema",
                    "permissions": {
                        "read": [str(root)],
                        "write": [str(root)],
                        "network": False,
                        "shell": True,
                    },
                },
                expected=None,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("unexpected keys ['shell']", completed.stderr)

    def test_unknown_result_field_is_quarantined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            self.send_task(root, "LWAR1", {"goal": "strict result schema"})
            _, event = self.watch_once(root, adopted, expected=0)
            task_id = event["task_id"]
            self.complete_task(root, adopted, task_id)
            outgoing = (
                root
                / "mailbox"
                / "LWAR1"
                / "outgoing"
                / f"{task_id}.result.json"
            )
            result = json.loads(outgoing.read_text(encoding="utf-8"))
            result["legacy_extension"] = True
            outgoing.write_text(json.dumps(result), encoding="utf-8")
            _, report = self.run_module(
                "pao_runtime.oa_cli",
                "collect",
                "--root",
                str(root),
                expected=0,
            )
            self.assertEqual(report["count"], 0)
            self.assertIn(
                "unexpected keys ['legacy_extension']",
                report["quarantined"][0]["reason"],
            )

    def test_doctor_rejects_pre_v1_registration_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = (
                root
                / "control"
                / "registration"
                / "archive"
                / "lwar-reg-legacy.json"
            )
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": "pao.lwar-registration-request.v1",
                        "request_id": "lwar-reg-" + "0" * 32,
                    }
                ),
                encoding="utf-8",
            )
            completed, report = self.run_module(
                "pao_runtime.pao_cli",
                "doctor",
                "--role",
                "lwar",
                "--root",
                str(root),
            )
            self.assertEqual(completed.returncode, 1)
            check = {
                item["check"]: item for item in report["checks"]
            }["v1_bus_contract"]
            self.assertFalse(check["ok"])
            self.assertIn("missing_runtime_version", check["detail"][0])

    def test_current_v1_flow_keeps_doctor_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, adopted = self.register_lwar(root)
            self.send_task(root, "LWAR1", {"goal": "v1 doctor flow"})
            _, event = self.watch_once(root, adopted, expected=0)
            self.complete_task(root, adopted, event["task_id"])
            self.run_module(
                "pao_runtime.oa_cli",
                "collect",
                "--root",
                str(root),
                expected=0,
            )
            _, report = self.run_module(
                "pao_runtime.pao_cli",
                "doctor",
                "--role",
                "lwar",
                "--root",
                str(root),
                expected=0,
            )
            self.assertTrue(report["healthy"])


class CredentialWorkflowAuthorityTests(unittest.TestCase):
    def test_audit_pat_and_issue_write_are_separate_jobs(self):
        workflow = (
            REPO / ".github" / "workflows" / "repository-policy-audit.yml"
        ).read_text(encoding="utf-8")
        lifecycle = workflow.split("  credential-lifecycle:", 1)[1]
        self.assertIn("issues: write", lifecycle)
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", lifecycle)
        self.assertNotIn("REPOSITORY_POLICY_AUDIT_TOKEN", lifecycle)
        self.assertIn("continue-on-error: true", workflow)
        self.assertIn("if: always()", workflow)


class CleanCopyDogfoodTests(unittest.TestCase):
    def test_two_skill_release_dogfood(self):
        completed = subprocess.run(
            [sys.executable, str(REPO / "tools" / "dogfood_v1_release.py")],
            check=False,
            capture_output=True,
            text=True,
            cwd=REPO,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr + completed.stdout,
        )
        evidence = json.loads(completed.stdout)
        self.assertEqual(evidence["runtime_version"], "1.0.0")
        self.assertEqual(evidence["lwar_ids"], ["LWAR1", "LWAR2"])
        self.assertTrue(evidence["oa_doctor"])
        self.assertTrue(evidence["lwar_doctor"])


if __name__ == "__main__":
    unittest.main()
