import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_repository_policy", REPO / "tools" / "verify_repository_policy.py"
)
verify_repository_policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify_repository_policy
SPEC.loader.exec_module(verify_repository_policy)


class RepositoryPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy_document = json.loads(
            (REPO / ".github" / "repository-policy.json").read_text(
                encoding="utf-8"
            )
        )
        cls.policy = verify_repository_policy.parse_policy(cls.policy_document)

    def live_policy(self):
        return {
            "required_status_checks": {
                "strict": True,
                "checks": [
                    {"context": check.context, "app_id": check.app_id}
                    for check in self.policy.checks
                ],
            },
            "enforce_admins": {"enabled": True},
        }

    def test_checked_in_policy_is_valid(self):
        self.assertEqual(self.policy.branch, "main")
        self.assertEqual(len(self.policy.checks), 3)
        self.assertTrue(self.policy.strict)
        self.assertTrue(self.policy.enforce_admins)

    def test_matching_live_policy_passes(self):
        self.assertEqual(
            verify_repository_policy.compare_policy(
                self.policy, self.live_policy()
            ),
            [],
        )

    def test_missing_required_check_fails(self):
        live = self.live_policy()
        live["required_status_checks"]["checks"].pop()
        errors = verify_repository_policy.compare_policy(self.policy, live)
        self.assertIn(
            "required check is missing: Verify (windows-latest)",
            errors,
        )

    def test_unexpected_required_check_fails(self):
        live = self.live_policy()
        live["required_status_checks"]["checks"].append(
            {"context": "Unapproved Check", "app_id": 15368}
        )
        errors = verify_repository_policy.compare_policy(self.policy, live)
        self.assertIn("unexpected required check: Unapproved Check", errors)

    def test_app_id_drift_fails(self):
        live = self.live_policy()
        live["required_status_checks"]["checks"][0]["app_id"] = 1
        errors = verify_repository_policy.compare_policy(self.policy, live)
        self.assertIn(
            "required check app_id drift: PR Evidence; expected 15368, observed 1",
            errors,
        )

    def test_strict_mode_drift_fails(self):
        live = self.live_policy()
        live["required_status_checks"]["strict"] = False
        errors = verify_repository_policy.compare_policy(self.policy, live)
        self.assertIn(
            "strict status-check policy drift: expected true, observed false",
            errors,
        )

    def test_administrator_enforcement_drift_fails(self):
        live = self.live_policy()
        live["enforce_admins"]["enabled"] = False
        errors = verify_repository_policy.compare_policy(self.policy, live)
        self.assertIn(
            "administrator enforcement drift: expected true, observed false",
            errors,
        )

    def test_duplicate_contract_context_fails_closed(self):
        document = json.loads(json.dumps(self.policy_document))
        document["required_status_checks"]["checks"].append(
            document["required_status_checks"]["checks"][0]
        )
        with self.assertRaisesRegex(ValueError, "duplicate required check context"):
            verify_repository_policy.parse_policy(document)

    def test_policy_rejects_unknown_keys(self):
        document = json.loads(json.dumps(self.policy_document))
        document["typo"] = True
        with self.assertRaisesRegex(ValueError, "unexpected keys: typo"):
            verify_repository_policy.parse_policy(document)

    def test_fetch_uses_encoded_branch_and_bearer_token(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        response.__exit__.return_value = False
        with mock.patch.object(
            verify_repository_policy.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            result = verify_repository_policy.fetch_branch_protection(
                "owner/repo",
                "release/test",
                token="secret",
            )
        self.assertEqual(result, {})
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/release%2Ftest/protection"))
        self.assertEqual(request.headers["Authorization"], "Bearer secret")

    def test_cli_live_file_reports_drift(self):
        live = self.live_policy()
        live["enforce_admins"]["enabled"] = False
        with tempfile.TemporaryDirectory() as directory:
            live_path = Path(directory) / "live.json"
            live_path.write_text(json.dumps(live), encoding="utf-8")
            output = io.StringIO()
            with mock.patch.dict(
                verify_repository_policy.os.environ,
                {"GITHUB_ACTIONS": "false"},
            ), mock.patch("sys.stderr", output), mock.patch("sys.stdout", io.StringIO()):
                result = verify_repository_policy.main(
                    [
                        "--policy",
                        str(REPO / ".github" / "repository-policy.json"),
                        "--live-file",
                        str(live_path),
                    ]
                )
        self.assertEqual(result, 1)
        self.assertIn("administrator enforcement drift", output.getvalue())

    def test_cli_live_audit_requires_token(self):
        output = io.StringIO()
        with mock.patch.dict(
            verify_repository_policy.os.environ,
            {"GITHUB_ACTIONS": "false", "GITHUB_TOKEN": ""},
        ), mock.patch("sys.stderr", output):
            result = verify_repository_policy.main(
                [
                    "--policy",
                    str(REPO / ".github" / "repository-policy.json"),
                    "--repository",
                    "owner/repo",
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("GITHUB_TOKEN is required", output.getvalue())


if __name__ == "__main__":
    unittest.main()
