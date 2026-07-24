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
from datetime import datetime, timedelta, timezone
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


@dataclass(frozen=True)
class CredentialRule:
    kind: str
    prefix: str
    not_after: datetime


@dataclass(frozen=True)
class CredentialPolicy:
    secret_name: str
    preferred_kind: str
    warn_before_days: int
    accepted_credentials: tuple[CredentialRule, ...]


@dataclass(frozen=True)
class CredentialValidation:
    kind: str | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


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


def _parse_utc_timestamp(value: Any, location: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{location} must be an ISO 8601 UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{location} must be a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{location} must use UTC")
    return parsed


def parse_credential_policy(document: Any) -> CredentialPolicy:
    """Validate the token-kind and maximum-use-date contract."""
    if not isinstance(document, dict):
        raise ValueError("credential policy must be a JSON object")
    _require_exact_keys(
        document,
        {
            "secret_name",
            "preferred_kind",
            "warn_before_days",
            "accepted_credentials",
        },
        "credential policy",
    )
    secret_name = document["secret_name"]
    preferred_kind = document["preferred_kind"]
    warn_before_days = document["warn_before_days"]
    raw_rules = document["accepted_credentials"]
    if not isinstance(secret_name, str) or not re.fullmatch(
        r"[A-Z][A-Z0-9_]*", secret_name
    ):
        raise ValueError("credential policy secret_name must be an uppercase identifier")
    if not isinstance(preferred_kind, str) or not preferred_kind:
        raise ValueError("credential policy preferred_kind must be a non-empty string")
    if (
        isinstance(warn_before_days, bool)
        or not isinstance(warn_before_days, int)
        or warn_before_days < 0
    ):
        raise ValueError("credential policy warn_before_days must be a non-negative integer")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError(
            "credential policy accepted_credentials must be a non-empty array"
        )

    rules: list[CredentialRule] = []
    seen_kinds: set[str] = set()
    seen_prefixes: set[str] = set()
    for index, raw_rule in enumerate(raw_rules):
        location = f"credential policy accepted_credentials[{index}]"
        if not isinstance(raw_rule, dict):
            raise ValueError(f"{location} must be a JSON object")
        _require_exact_keys(raw_rule, {"kind", "prefix", "not_after"}, location)
        kind = raw_rule["kind"]
        prefix = raw_rule["prefix"]
        if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", kind):
            raise ValueError(f"{location}.kind must be a snake_case identifier")
        if not isinstance(prefix, str) or len(prefix) < 4:
            raise ValueError(f"{location}.prefix must contain at least four characters")
        if kind in seen_kinds:
            raise ValueError(f"duplicate credential kind: {kind}")
        if prefix in seen_prefixes:
            raise ValueError(f"duplicate credential prefix: {prefix}")
        if any(
            prefix.startswith(existing) or existing.startswith(prefix)
            for existing in seen_prefixes
        ):
            raise ValueError(f"overlapping credential prefix: {prefix}")
        seen_kinds.add(kind)
        seen_prefixes.add(prefix)
        rules.append(
            CredentialRule(
                kind=kind,
                prefix=prefix,
                not_after=_parse_utc_timestamp(
                    raw_rule["not_after"], f"{location}.not_after"
                ),
            )
        )
    if preferred_kind not in seen_kinds:
        raise ValueError("credential policy preferred_kind is not accepted")
    return CredentialPolicy(
        secret_name=secret_name,
        preferred_kind=preferred_kind,
        warn_before_days=warn_before_days,
        accepted_credentials=tuple(rules),
    )


def validate_credential(
    policy: CredentialPolicy,
    token: str | None,
    *,
    now: datetime | None = None,
) -> CredentialValidation:
    """Classify a secret without exposing it and enforce its maximum use date."""
    if not token:
        return CredentialValidation(
            kind=None,
            errors=(f"{policy.secret_name} is missing",),
            warnings=(),
        )
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("credential validation time must be timezone-aware")
    observed_at = observed_at.astimezone(timezone.utc)
    matches = [
        rule for rule in policy.accepted_credentials if token.startswith(rule.prefix)
    ]
    if len(matches) != 1:
        return CredentialValidation(
            kind=None,
            errors=("credential type is not accepted by repository policy",),
            warnings=(),
        )

    rule = matches[0]
    errors: list[str] = []
    warnings: list[str] = []
    if observed_at >= rule.not_after:
        errors.append(
            f"credential {rule.kind} exceeded not_after "
            f"{rule.not_after.isoformat().replace('+00:00', 'Z')}"
        )
    else:
        remaining = rule.not_after - observed_at
        if remaining <= timedelta(days=policy.warn_before_days):
            warnings.append(
                f"credential {rule.kind} reaches not_after in "
                f"{max(0, remaining.days)} day(s)"
            )
    if rule.kind != policy.preferred_kind:
        warnings.append(
            f"credential {rule.kind} is transitional; "
            f"rotate to {policy.preferred_kind}"
        )
    return CredentialValidation(
        kind=rule.kind,
        errors=tuple(errors),
        warnings=tuple(warnings),
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


def _emit_warning(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::{message}")
    else:
        print(f"WARNING: {message}", file=sys.stderr)


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
        "--credential-policy",
        type=Path,
        help="token-kind and maximum-use-date policy JSON",
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
        credential_kind = None
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
            if args.credential_policy:
                credential_policy = parse_credential_policy(
                    load_json(args.credential_policy)
                )
                credential = validate_credential(credential_policy, token)
                for warning in credential.warnings:
                    _emit_warning(warning)
                if credential.errors:
                    raise ValueError("; ".join(credential.errors))
                credential_kind = credential.kind
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
        f"{len(policy.checks)} app-bound checks and administrator enforcement match"
        f"{f'; credential={credential_kind}' if credential_kind else ''}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
