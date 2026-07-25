#!/usr/bin/env python3
"""Validate pull-request evidence against the checked-in PR template."""

from __future__ import annotations

import argparse
import json
import re
import sys
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
