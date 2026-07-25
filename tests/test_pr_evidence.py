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

    def test_workflow_uses_evaluator_without_checks_write(self):
        workflow = (REPO / ".github" / "workflows" / "pr-evidence.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("name: PR Evidence Evaluator", workflow)
        self.assertNotIn("checks: write", workflow)
        self.assertNotIn("--publish-check", workflow)


if __name__ == "__main__":
    unittest.main()
