#!/usr/bin/env python3
"""Validate pull-request evidence against the checked-in PR template."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.MULTILINE)
CHECKBOX_RE = re.compile(
    r"^[ \t]*-[ \t]+\[([ xX])\][ \t]+(.+?)[ \t]*$",
    re.MULTILINE,
)
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


@dataclass(frozen=True)
class Section:
    heading: str
    content: str


@dataclass(frozen=True)
class EvidenceContract:
    headings: tuple[str, ...]
    narrative_headings: tuple[str, ...]
    checkboxes: tuple[tuple[str, str], ...]


def normalize_text(value: str) -> str:
    """Collapse insignificant whitespace while retaining literal evidence."""
    return " ".join(value.strip().split())


def parse_sections(text: str) -> tuple[list[Section], list[str]]:
    """Return ordered H2 sections and duplicate-heading errors."""
    matches = list(HEADING_RE.finditer(text))
    sections: list[Section] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        heading = normalize_text(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if heading in seen:
            errors.append(f"duplicate section: {heading}")
        seen.add(heading)
        sections.append(Section(heading, text[match.end() : end]))
    return sections, errors


def section_checkboxes(section: Section) -> list[tuple[bool, str]]:
    """Return checkbox state and normalized label for one section."""
    return [
        (state.lower() == "x", normalize_text(label))
        for state, label in CHECKBOX_RE.findall(section.content)
    ]


def load_contract(template_text: str) -> EvidenceContract:
    """Derive the required evidence structure from the repository template."""
    sections, errors = parse_sections(template_text)
    if errors:
        raise ValueError("; ".join(errors))
    if not sections:
        raise ValueError("pull request template has no H2 sections")

    checkboxes: list[tuple[str, str]] = []
    narrative_headings: list[str] = []
    for section in sections:
        items = section_checkboxes(section)
        if items:
            labels = [label for _, label in items]
            if len(labels) != len(set(labels)):
                raise ValueError(
                    f"pull request template has duplicate checkboxes in: {section.heading}"
                )
            checkboxes.extend((section.heading, label) for label in labels)
        else:
            narrative_headings.append(section.heading)
    if not checkboxes:
        raise ValueError("pull request template has no checkboxes")
    return EvidenceContract(
        headings=tuple(section.heading for section in sections),
        narrative_headings=tuple(narrative_headings),
        checkboxes=tuple(checkboxes),
    )


def narrative_text(section: Section) -> str:
    """Remove template comments and checkbox lines before testing prose."""
    without_comments = COMMENT_RE.sub("", section.content)
    without_checkboxes = CHECKBOX_RE.sub("", without_comments)
    return normalize_text(without_checkboxes)


def validate_body(template_text: str, body: str | None) -> list[str]:
    """Return stable validation errors; an empty list means the body is valid."""
    contract = load_contract(template_text)
    if body is None or not body.strip():
        return ["pull request body is empty"]

    sections, errors = parse_sections(body)
    by_heading: dict[str, Section] = {}
    for section in sections:
        by_heading.setdefault(section.heading, section)

    missing = [heading for heading in contract.headings if heading not in by_heading]
    errors.extend(f"missing section: {heading}" for heading in missing)

    observed_order = [
        section.heading for section in sections if section.heading in contract.headings
    ]
    expected_present_order = [
        heading for heading in contract.headings if heading in by_heading
    ]
    if observed_order != expected_present_order:
        errors.append("required sections are out of order")

    for heading in contract.narrative_headings:
        section = by_heading.get(heading)
        if section is not None and not narrative_text(section):
            errors.append(f"section has no evidence: {heading}")

    for heading, required_label in contract.checkboxes:
        section = by_heading.get(heading)
        if section is None:
            continue
        matching = [
            checked
            for checked, label in section_checkboxes(section)
            if label == required_label
        ]
        if not matching:
            errors.append(f"missing checkbox in {heading}: {required_label}")
        elif len(matching) > 1:
            errors.append(f"duplicate checkbox in {heading}: {required_label}")
        elif not matching[0]:
            errors.append(f"unchecked checkbox in {heading}: {required_label}")
    return errors


def load_event_body(path: Path) -> str | None:
    """Load only pull_request.body from a GitHub event payload."""
    payload = load_event(path)
    pull_request = payload.get("pull_request")
    body = pull_request.get("body")
    if body is not None and not isinstance(body, str):
        raise ValueError("pull_request.body must be a string or null")
    return body


def load_event(path: Path) -> dict[str, Any]:
    """Load a GitHub pull-request event as a JSON object."""
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ValueError("event payload has no pull_request object")
    return payload


def pull_request_head_sha(payload: dict[str, Any]) -> str:
    """Return the PR head commit bound to the evidence check."""
    pull_request = payload["pull_request"]
    head = pull_request.get("head")
    sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise ValueError("event payload has no valid pull_request.head.sha")
    return sha.lower()


def pull_request_check_shas(payload: dict[str, Any]) -> tuple[str, ...]:
    """Return every commit GitHub may use for strict required-check evaluation."""
    pull_request = payload["pull_request"]
    head_sha = pull_request_head_sha(payload)
    merge_sha = pull_request.get("merge_commit_sha")
    if merge_sha is None:
        return (head_sha,)
    if not isinstance(merge_sha, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", merge_sha
    ):
        raise ValueError("event payload has no valid pull_request.merge_commit_sha")
    normalized_merge = merge_sha.lower()
    if normalized_merge == head_sha:
        return (head_sha,)
    return (head_sha, normalized_merge)


def build_check_payload(head_sha: str, errors: list[str]) -> dict[str, Any]:
    """Build one completed check run without embedding executable PR content."""
    passed = not errors
    summary = (
        "All required PR evidence is present and checked."
        if passed
        else "\n".join(["PR evidence validation failed:", *[f"- {item}" for item in errors]])
    )
    return {
        "name": "PR Evidence",
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "success" if passed else "failure",
        "output": {
            "title": "PR evidence valid" if passed else "PR evidence invalid",
            "summary": summary,
        },
    }


def publish_check(
    payload: dict[str, Any],
    *,
    repository: str,
    token: str,
    api_url: str,
    opener=urllib.request.urlopen,
) -> None:
    """Publish the trusted check run to the PR head through the Checks API."""
    if not re.fullmatch(r"[^/\s]+/[^/\s]+", repository):
        raise ValueError("GITHUB_REPOSITORY must be owner/name")
    if not token:
        raise ValueError("GITHUB_TOKEN is required to publish the check")
    endpoint = f"{api_url.rstrip('/')}/repos/{repository}/check-runs"
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with opener(request, timeout=15) as response:
        if response.status not in {200, 201}:
            raise OSError(f"Checks API returned HTTP {response.status}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate PR evidence against .github/pull_request_template.md"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--event", type=Path, help="GitHub event JSON path")
    source.add_argument("--body-file", type=Path, help="plain Markdown body path")
    parser.add_argument(
        "--template",
        type=Path,
        default=Path(".github/pull_request_template.md"),
    )
    parser.add_argument(
        "--publish-check",
        action="store_true",
        help="publish a trusted PR Evidence check to pull_request.head.sha",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        template_text = args.template.read_text(encoding="utf-8")
        event_payload = load_event(args.event) if args.event is not None else None
        body = (
            event_payload["pull_request"].get("body")
            if event_payload is not None
            else args.body_file.read_text(encoding="utf-8")
        )
        if body is not None and not isinstance(body, str):
            raise ValueError("pull_request.body must be a string or null")
        errors = validate_body(template_text, body)
        if args.publish_check:
            if event_payload is None:
                raise ValueError("--publish-check requires --event")
            for sha in pull_request_check_shas(event_payload):
                check_payload = build_check_payload(sha, errors)
                publish_check(
                    check_payload,
                    repository=os.environ.get("GITHUB_REPOSITORY", ""),
                    token=os.environ.get("GITHUB_TOKEN", ""),
                    api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"PR evidence validation error: {error}", file=sys.stderr)
        return 2

    if errors:
        print("PR evidence validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("PR evidence validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
