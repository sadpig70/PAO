import importlib.util
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_pr_evidence", REPO / "tools" / "verify_pr_evidence.py"
)
verify_pr_evidence = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify_pr_evidence
SPEC.loader.exec_module(verify_pr_evidence)


class PREvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (REPO / ".github" / "pull_request_template.md").read_text(
            encoding="utf-8"
        )

    def valid_body(self):
        body = re.sub(r"<!--.*?-->", "Evidence provided.", self.template, flags=re.DOTALL)
        return body.replace("- [ ]", "- [x]")

    def test_repository_template_accepts_complete_evidence(self):
        self.assertEqual(
            verify_pr_evidence.validate_body(self.template, self.valid_body()),
            [],
        )

    def test_uppercase_checked_marker_is_accepted(self):
        body = self.valid_body().replace("- [x]", "- [X]")
        self.assertEqual(verify_pr_evidence.validate_body(self.template, body), [])

    def test_missing_required_section_fails(self):
        body = self.valid_body().replace(
            "## Risk assessment\n\nEvidence provided.\n\n",
            "",
        )
        self.assertIn(
            "missing section: Risk assessment",
            verify_pr_evidence.validate_body(self.template, body),
        )

    def test_duplicate_required_section_fails(self):
        body = self.valid_body() + "\n## Risk assessment\n\nDuplicate.\n"
        self.assertIn(
            "duplicate section: Risk assessment",
            verify_pr_evidence.validate_body(self.template, body),
        )

    def test_required_section_order_fails_closed(self):
        body = self.valid_body()
        first = body.index("## Problem and intended outcome")
        second = body.index("## Changed files and contracts")
        third = body.index("## Verification performed")
        problem = body[first:second]
        changed = body[second:third]
        reordered = body[:first] + changed + problem + body[third:]
        self.assertIn(
            "required sections are out of order",
            verify_pr_evidence.validate_body(self.template, reordered),
        )

    def test_template_comment_is_not_narrative_evidence(self):
        body = self.valid_body().replace(
            "## Problem and intended outcome\n\nEvidence provided.",
            (
                "## Problem and intended outcome\n\n"
                "<!-- Describe the problem, why it matters, and the observable outcome. -->"
            ),
        )
        self.assertIn(
            "section has no evidence: Problem and intended outcome",
            verify_pr_evidence.validate_body(self.template, body),
        )

    def test_unchecked_contract_checkbox_fails(self):
        body = self.valid_body().replace("- [x]", "- [ ]", 1)
        errors = verify_pr_evidence.validate_body(self.template, body)
        self.assertTrue(any(error.startswith("unchecked checkbox") for error in errors))

    def test_missing_contract_checkbox_fails(self):
        first_checkbox = re.search(r"^- \[x\].+$", self.valid_body(), re.MULTILINE)
        body = self.valid_body().replace(first_checkbox.group(0) + "\n", "", 1)
        errors = verify_pr_evidence.validate_body(self.template, body)
        self.assertTrue(any(error.startswith("missing checkbox") for error in errors))

    def test_empty_event_body_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(
                json.dumps({"pull_request": {"body": None}}),
                encoding="utf-8",
            )
            body = verify_pr_evidence.load_event_body(event)
        self.assertEqual(
            verify_pr_evidence.validate_body(self.template, body),
            ["pull request body is empty"],
        )

    def test_event_without_pull_request_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no pull_request object"):
                verify_pr_evidence.load_event_body(event)

    def test_failure_check_payload_binds_head_and_errors(self):
        sha = "a" * 40
        payload = verify_pr_evidence.build_check_payload(sha, ["missing evidence"])
        self.assertEqual(payload["head_sha"], sha)
        self.assertEqual(payload["conclusion"], "failure")
        self.assertIn("missing evidence", payload["output"]["summary"])

    def test_success_check_payload_is_completed(self):
        payload = verify_pr_evidence.build_check_payload("b" * 40, [])
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["conclusion"], "success")

    def test_pull_request_head_sha_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "no valid pull_request.head.sha"):
            verify_pr_evidence.pull_request_head_sha(
                {"pull_request": {"head": {"sha": "not-a-sha"}}}
            )

    def test_check_shas_include_head_and_synthetic_merge(self):
        shas = verify_pr_evidence.pull_request_check_shas(
            {
                "pull_request": {
                    "head": {"sha": "a" * 40},
                    "merge_commit_sha": "b" * 40,
                }
            }
        )
        self.assertEqual(shas, ("a" * 40, "b" * 40))

    def test_check_shas_reject_invalid_synthetic_merge(self):
        with self.assertRaisesRegex(
            ValueError, "no valid pull_request.merge_commit_sha"
        ):
            verify_pr_evidence.pull_request_check_shas(
                {
                    "pull_request": {
                        "head": {"sha": "a" * 40},
                        "merge_commit_sha": "invalid",
                    }
                }
            )

    def test_required_merge_sha_fails_closed_when_api_has_none(self):
        with self.assertRaisesRegex(ValueError, "no merge_commit_sha"):
            verify_pr_evidence.pull_request_check_shas(
                {
                    "pull_request": {
                        "head": {"sha": "a" * 40},
                        "merge_commit_sha": None,
                    }
                },
                require_merge=True,
            )

    def test_pull_request_number_must_be_positive(self):
        self.assertEqual(
            verify_pr_evidence.pull_request_number(
                {"pull_request": {"number": 6}}
            ),
            6,
        )
        with self.assertRaisesRegex(ValueError, "no valid pull_request.number"):
            verify_pr_evidence.pull_request_number(
                {"pull_request": {"number": 0}}
            )

    def test_fetch_pull_request_uses_trusted_api_metadata(self):
        observed = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "number": 6,
                        "body": "evidence",
                        "head": {"sha": "a" * 40},
                        "merge_commit_sha": "b" * 40,
                    }
                ).encode("utf-8")

        def opener(request, timeout):
            observed["url"] = request.full_url
            observed["method"] = request.method
            observed["timeout"] = timeout
            return Response()

        payload = verify_pr_evidence.fetch_pull_request(
            repository="owner/repository",
            number=6,
            token="secret-token",
            api_url="https://api.example.test",
            opener=opener,
        )
        self.assertEqual(
            observed["url"],
            "https://api.example.test/repos/owner/repository/pulls/6",
        )
        self.assertEqual(observed["method"], "GET")
        self.assertEqual(observed["timeout"], 15)
        self.assertEqual(payload["merge_commit_sha"], "b" * 40)

    def test_publish_check_uses_checks_api_without_exposing_token(self):
        observed = {}

        class Response:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def opener(request, timeout):
            observed["url"] = request.full_url
            observed["authorization"] = request.headers["Authorization"]
            observed["payload"] = json.loads(request.data)
            observed["timeout"] = timeout
            return Response()

        payload = verify_pr_evidence.build_check_payload("c" * 40, [])
        verify_pr_evidence.publish_check(
            payload,
            repository="owner/repository",
            token="secret-token",
            api_url="https://api.example.test",
            opener=opener,
        )
        self.assertEqual(
            observed["url"],
            "https://api.example.test/repos/owner/repository/check-runs",
        )
        self.assertEqual(observed["authorization"], "Bearer secret-token")
        self.assertEqual(observed["payload"]["head_sha"], "c" * 40)
        self.assertEqual(observed["timeout"], 15)


if __name__ == "__main__":
    unittest.main()
