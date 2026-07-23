#!/usr/bin/env python3
"""Fail closed when live GitHub branch protection drifts from repository policy."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class RequiredCheck:
    context: str
    app_id: int


@dataclass(frozen=True)
class RepositoryPolicy:
    branch: str
    strict: bool
    checks: tuple[RequiredCheck, ...]
    enforce_admins: bool


def _require_exact_keys(
    value: dict[str, Any], expected: set[str], location: str
) -> None:
    missing = sorted(expected - value.keys())
    unexpected = sorted(value.keys() - expected)
    if missing:
        raise ValueError(f"{location} missing keys: {', '.join(missing)}")
    if unexpected:
        raise ValueError(f"{location} unexpected keys: {', '.join(unexpected)}")


def parse_policy(document: Any) -> RepositoryPolicy:
    """Validate and normalize the checked-in policy document."""
    if not isinstance(document, dict):
        raise ValueError("policy must be a JSON object")
    _require_exact_keys(
        document,
        {"branch", "required_status_checks", "enforce_admins"},
        "policy",
    )

    branch = document["branch"]
    if not isinstance(branch, str) or not branch.strip():
        raise ValueError("policy branch must be a non-empty string")
    if "/" in branch or branch in {".", ".."}:
        raise ValueError("policy branch must be a single safe branch name")

    required = document["required_status_checks"]
    if not isinstance(required, dict):
        raise ValueError("required_status_checks must be a JSON object")
    _require_exact_keys(required, {"strict", "checks"}, "required_status_checks")
    if not isinstance(required["strict"], bool):
        raise ValueError("required_status_checks.strict must be a boolean")

    raw_checks = required["checks"]
    if not isinstance(raw_checks, list) or not raw_checks:
        raise ValueError("required_status_checks.checks must be a non-empty array")

    checks: list[RequiredCheck] = []
    seen: set[str] = set()
    for index, raw_check in enumerate(raw_checks):
        location = f"required_status_checks.checks[{index}]"
        if not isinstance(raw_check, dict):
            raise ValueError(f"{location} must be a JSON object")
        _require_exact_keys(raw_check, {"context", "app_id"}, location)
        context = raw_check["context"]
        app_id = raw_check["app_id"]
        if not isinstance(context, str) or not context.strip():
            raise ValueError(f"{location}.context must be a non-empty string")
        if context in seen:
            raise ValueError(f"duplicate required check context: {context}")
        if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
            raise ValueError(f"{location}.app_id must be a positive integer")
        seen.add(context)
        checks.append(RequiredCheck(context=context, app_id=app_id))

    enforce_admins = document["enforce_admins"]
    if not isinstance(enforce_admins, bool):
        raise ValueError("enforce_admins must be a boolean")

    return RepositoryPolicy(
        branch=branch,
        strict=required["strict"],
        checks=tuple(checks),
        enforce_admins=enforce_admins,
    )


def load_json(path: Path) -> Any:
    """Load one UTF-8 JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def _live_enabled(live: dict[str, Any], key: str) -> bool | None:
    value = live.get(key)
    if not isinstance(value, dict):
        return None
    enabled = value.get("enabled")
    return enabled if isinstance(enabled, bool) else None


def compare_policy(policy: RepositoryPolicy, live: Any) -> list[str]:
    """Return stable drift messages; an empty list means the policy matches."""
    if not isinstance(live, dict):
        return ["live branch protection response is not a JSON object"]

    errors: list[str] = []
    required = live.get("required_status_checks")
    if not isinstance(required, dict):
        errors.append("live required_status_checks is missing or invalid")
        observed_strict = None
        raw_checks: Any = None
    else:
        observed_strict = required.get("strict")
        raw_checks = required.get("checks")

    if observed_strict is not policy.strict:
        errors.append(
            "strict status-check policy drift: "
            f"expected {str(policy.strict).lower()}, "
            f"observed {str(observed_strict).lower()}"
        )

    observed: dict[str, int] = {}
    if not isinstance(raw_checks, list):
        errors.append("live required status checks are missing or invalid")
    else:
        for index, raw_check in enumerate(raw_checks):
            if not isinstance(raw_check, dict):
                errors.append(f"live required check at index {index} is invalid")
                continue
            context = raw_check.get("context")
            app_id = raw_check.get("app_id")
            if not isinstance(context, str) or not context:
                errors.append(f"live required check at index {index} has no context")
                continue
            if context in observed:
                errors.append(f"live required check is duplicated: {context}")
                continue
            if isinstance(app_id, bool) or not isinstance(app_id, int):
                errors.append(f"live required check has invalid app_id: {context}")
                continue
            observed[context] = app_id

    expected = {check.context: check.app_id for check in policy.checks}
    for context, app_id in expected.items():
        if context not in observed:
            errors.append(f"required check is missing: {context}")
        elif observed[context] != app_id:
            errors.append(
                f"required check app_id drift: {context}; "
                f"expected {app_id}, observed {observed[context]}"
            )
    for context in sorted(observed.keys() - expected.keys()):
        errors.append(f"unexpected required check: {context}")

    observed_admins = _live_enabled(live, "enforce_admins")
    if observed_admins is not policy.enforce_admins:
        errors.append(
            "administrator enforcement drift: "
            f"expected {str(policy.enforce_admins).lower()}, "
            f"observed {str(observed_admins).lower()}"
        )
    return errors


def fetch_branch_protection(
    repository: str,
    branch: str,
    *,
    token: str | None,
    api_url: str = "https://api.github.com",
) -> Any:
    """Read live protection through the GitHub REST API."""
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError("repository must use the owner/name form")
    encoded_branch = urllib.parse.quote(branch, safe="")
    url = (
        f"{api_url.rstrip('/')}/repos/{repository}/branches/"
        f"{encoded_branch}/protection"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "PAO-repository-policy-audit",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _emit_error(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error::{message}")
    else:
        print(f"ERROR: {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit live GitHub branch protection against repository policy."
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path(".github/repository-policy.json"),
        help="checked-in repository policy JSON",
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="GitHub repository in owner/name form",
    )
    parser.add_argument(
        "--live-file",
        type=Path,
        help="use a captured API response instead of the GitHub API",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        help="GitHub API base URL",
    )
    args = parser.parse_args(argv)

    try:
        policy = parse_policy(load_json(args.policy))
        if args.live_file:
            live = load_json(args.live_file)
        else:
            if not args.repository:
                raise ValueError(
                    "--repository or GITHUB_REPOSITORY is required without --live-file"
                )
            token = os.environ.get("GITHUB_TOKEN")
            if not token:
                raise ValueError(
                    "GITHUB_TOKEN is required to read live branch protection"
                )
            live = fetch_branch_protection(
                args.repository,
                policy.branch,
                token=token,
                api_url=args.api_url,
            )
        errors = compare_policy(policy, live)
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ) as exc:
        _emit_error(f"repository policy audit could not run: {exc}")
        return 2

    if errors:
        for error in errors:
            _emit_error(error)
        print(f"Repository policy audit failed with {len(errors)} drift finding(s).")
        return 1

    print(
        f"Repository policy audit passed for {policy.branch}: "
        f"{len(policy.checks)} app-bound checks and administrator enforcement match."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
