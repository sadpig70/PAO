import importlib.util
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


REPO = Path(__file__).parents[1]
TOOLS = REPO / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
SPEC = importlib.util.spec_from_file_location(
    "reconcile_credential_issue", TOOLS / "reconcile_credential_issue.py"
)
credential_issue = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = credential_issue
SPEC.loader.exec_module(credential_issue)


class FakeIssues:
    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.created = []
        self.updated = []
        self.closed = []
        self.label_ensured = False

    def ensure_label(self):
        self.label_ensured = True

    def managed_open_issues(self):
        return list(self.existing)

    def create(self, body):
        issue = {"number": 101, "body": body}
        self.created.append(issue)
        return issue

    def update(self, number, body):
        self.updated.append((number, body))
        return {"number": number}

    def close(self, number):
        self.closed.append(number)
        return {"number": number}


class CredentialIssueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = json.loads(
            (REPO / ".github" / "repository-policy-credential.json").read_text(
                encoding="utf-8"
            )
        )

    def classify(self, date, outcome="success"):
        return credential_issue.classify_lifecycle(
            self.document,
            outcome,
            now=datetime.fromisoformat(date).replace(tzinfo=timezone.utc),
        )

    def test_healthy_before_warning_window(self):
        self.assertEqual(self.classify("2026-10-07T00:00:00").name, "healthy")

    def test_warning_at_fourteen_day_boundary(self):
        self.assertEqual(self.classify("2026-10-08T00:00:00").name, "warning")

    def test_expired_at_deadline(self):
        self.assertEqual(self.classify("2026-10-22T00:00:00").name, "expired")

    def test_audit_failure_overrides_healthy_deadline(self):
        self.assertEqual(
            self.classify("2026-07-25T00:00:00", outcome="failure").name,
            "audit_failed",
        )

    def test_warning_creates_one_managed_issue(self):
        github = FakeIssues()
        result = credential_issue.reconcile(
            self.classify("2026-10-08T00:00:00"),
            github,
        )
        self.assertTrue(github.label_ensured)
        self.assertEqual(result["action"], "created")
        self.assertEqual(len(github.created), 1)
        self.assertIn(credential_issue.MARKER, github.created[0]["body"])

    def test_repeated_warning_updates_and_closes_duplicates(self):
        github = FakeIssues([{"number": 7}, {"number": 8}])
        result = credential_issue.reconcile(
            self.classify("2026-10-09T00:00:00"),
            github,
        )
        self.assertEqual(result["action"], "updated")
        self.assertEqual(github.updated[0][0], 7)
        self.assertEqual(github.closed, [8])

    def test_healthy_closes_only_returned_managed_issues(self):
        github = FakeIssues([{"number": 7}])
        result = credential_issue.reconcile(
            self.classify("2026-07-25T00:00:00"),
            github,
        )
        self.assertEqual(result["action"], "closed")
        self.assertEqual(github.closed, [7])

    def test_issue_body_never_contains_token_like_prefix(self):
        body = credential_issue.issue_body(self.classify("2026-10-09T00:00:00"))
        self.assertNotIn("github_pat_", body)
        self.assertNotIn("gho_", body)

    def test_api_uses_bearer_without_echoing_token(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"[]"
        response.__exit__.return_value = False
        github = credential_issue.GitHubIssues("owner/repo", "secret-value")
        with mock.patch.object(
            credential_issue.urllib.request,
            "urlopen",
            return_value=response,
        ) as urlopen:
            self.assertEqual(github.managed_open_issues(), [])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.headers["Authorization"], "Bearer secret-value")


if __name__ == "__main__":
    unittest.main()
