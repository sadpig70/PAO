#!/usr/bin/env python3
"""Reconcile one durable GitHub issue for PAO credential lifecycle health."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verify_repository_policy import (
    API_VERSION,
    REPOSITORY_RE,
    load_json,
    parse_credential_policy,
)


LABEL = "pao-credential-lifecycle"
TITLE = "[PAO] Repository policy audit credential requires attention"
MARKER = "<!-- pao-credential-lifecycle -->"


@dataclass(frozen=True)
class LifecycleState:
    name: str
    deadline: datetime
    audit_outcome: str


def classify_lifecycle(
    policy_document: Any,
    audit_outcome: str,
    *,
    now: datetime | None = None,
) -> LifecycleState:
    policy = parse_credential_policy(policy_document)
    observed = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    preferred = next(
        rule
        for rule in policy.accepted_credentials
        if rule.kind == policy.preferred_kind
    )
    if audit_outcome != "success":
        name = "audit_failed"
    elif observed >= preferred.not_after:
        name = "expired"
    elif preferred.not_after - observed <= timedelta(days=policy.warn_before_days):
        name = "warning"
    else:
        name = "healthy"
    return LifecycleState(
        name=name,
        deadline=preferred.not_after,
        audit_outcome=audit_outcome,
    )


def issue_body(state: LifecycleState) -> str:
    deadline = state.deadline.isoformat().replace("+00:00", "Z")
    urgency = {
        "audit_failed": "The live repository policy audit did not complete successfully.",
        "expired": "The checked-in credential deadline has been reached.",
        "warning": "The checked-in credential deadline is inside its warning window.",
    }[state.name]
    return "\n".join(
        [
            MARKER,
            "## PAO credential lifecycle alert",
            "",
            urgency,
            "",
            f"- State: `{state.name}`",
            f"- Audit outcome: `{state.audit_outcome}`",
            f"- Checked-in deadline: `{deadline}`",
            "",
            "Rotate `REPOSITORY_POLICY_AUDIT_TOKEN` with a repository-scoped",
            "fine-grained PAT granting only Administration: read and Metadata: read.",
            "Update the reviewed `not_after` ceiling in the same protected change,",
            "then manually dispatch Repository Policy Audit. A healthy run closes",
            "this PAO-managed issue automatically.",
            "",
            "Do not paste a token into this issue.",
        ]
    )


class GitHubIssues:
    def __init__(
        self,
        repository: str,
        token: str,
        *,
        api_url: str = "https://api.github.com",
    ):
        if not REPOSITORY_RE.fullmatch(repository):
            raise ValueError("repository must use the owner/name form")
        if not token:
            raise ValueError("GITHUB_TOKEN is required for issue reconciliation")
        self.repository = repository
        self.token = token
        self.api_url = api_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        allow_status: tuple[int, ...] = (),
    ) -> Any:
        data = (
            json.dumps(payload, sort_keys=True).encode("utf-8")
            if payload is not None
            else None
        )
        request = urllib.request.Request(
            f"{self.api_url}{path}",
            method=method,
            data=data,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "PAO-credential-lifecycle",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as error:
            if error.code in allow_status:
                return None
            raise

    def ensure_label(self) -> None:
        self.request(
            "POST",
            f"/repos/{self.repository}/labels",
            {
                "name": LABEL,
                "color": "B60205",
                "description": "Managed PAO credential lifecycle alert",
            },
            allow_status=(422,),
        )

    def managed_open_issues(self) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(LABEL, safe="")
        items = self.request(
            "GET",
            f"/repos/{self.repository}/issues?state=open&labels={encoded}&per_page=100",
        )
        return [
            item
            for item in items or []
            if "pull_request" not in item
            and item.get("title") == TITLE
            and MARKER in (item.get("body") or "")
        ]

    def create(self, body: str) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/repos/{self.repository}/issues",
            {"title": TITLE, "body": body, "labels": [LABEL]},
        )

    def update(self, number: int, body: str) -> dict[str, Any]:
        return self.request(
            "PATCH",
            f"/repos/{self.repository}/issues/{number}",
            {"body": body, "state": "open"},
        )

    def close(self, number: int) -> dict[str, Any]:
        return self.request(
            "PATCH",
            f"/repos/{self.repository}/issues/{number}",
            {"state": "closed", "state_reason": "completed"},
        )


def reconcile(state: LifecycleState, github: GitHubIssues) -> dict[str, Any]:
    github.ensure_label()
    existing = github.managed_open_issues()
    if state.name == "healthy":
        for issue in existing:
            github.close(int(issue["number"]))
        return {
            "event": "credential_issue_reconciled",
            "state": state.name,
            "action": "closed" if existing else "none",
            "issue_numbers": [int(issue["number"]) for issue in existing],
        }

    body = issue_body(state)
    if existing:
        primary = existing[0]
        github.update(int(primary["number"]), body)
        for duplicate in existing[1:]:
            github.close(int(duplicate["number"]))
        return {
            "event": "credential_issue_reconciled",
            "state": state.name,
            "action": "updated",
            "issue_numbers": [int(primary["number"])],
            "duplicates_closed": [int(issue["number"]) for issue in existing[1:]],
        }

    created = github.create(body)
    return {
        "event": "credential_issue_reconciled",
        "state": state.name,
        "action": "created",
        "issue_numbers": [int(created["number"])],
    }


def _parse_now(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--now must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open, update, or close the PAO credential lifecycle issue."
    )
    parser.add_argument(
        "--credential-policy",
        type=Path,
        default=Path(".github/repository-policy-credential.json"),
    )
    parser.add_argument("--audit-outcome", required=True)
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
    )
    parser.add_argument("--now", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        if not args.repository:
            raise ValueError("--repository or GITHUB_REPOSITORY is required")
        state = classify_lifecycle(
            load_json(args.credential_policy),
            args.audit_outcome,
            now=_parse_now(args.now),
        )
        github = GitHubIssues(
            args.repository,
            os.environ.get("GITHUB_TOKEN", ""),
            api_url=args.api_url,
        )
        result = reconcile(state, github)
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as error:
        print(f"ERROR: credential issue reconciliation failed: {error}", file=sys.stderr)
        return 2

    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
