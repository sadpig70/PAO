#!/usr/bin/env python3
"""Normalize reset-campaign evidence paths while preserving source lineage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]


def normalize_paths(value: Any, repo: Path) -> Any:
    if isinstance(value, dict):
        return {
            key: normalize_paths(item, repo)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalize_paths(item, repo) for item in value]
    if not isinstance(value, str):
        return value
    prefix = str(repo.resolve()).replace("\\", "/").rstrip("/") + "/"
    portable_value = value.replace("\\", "/")
    if portable_value.lower().startswith(prefix.lower()):
        return portable_value[len(prefix) :]
    return value


def normalize_evidence(
    source: dict[str, Any],
    *,
    repo: Path,
    source_file_sha256: str,
    source_canonical_sha256: str,
    source_commit: str,
) -> dict[str, Any]:
    source_without_prior_lineage = dict(source)
    source_without_prior_lineage.pop("portability", None)
    normalized = normalize_paths(source_without_prior_lineage, repo)
    normalized["portability"] = {
        "normalized": True,
        "rule": "repository_absolute_paths_to_repository_relative_posix",
        "source_file_sha256": source_file_sha256.lower(),
        "source_canonical_sha256": source_canonical_sha256.lower(),
        "source_commit": source_commit,
    }
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--source-file-sha256", required=True)
    parser.add_argument("--source-canonical-sha256", required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    path = args.evidence.resolve()
    source = json.loads(path.read_text(encoding="utf-8"))
    normalized = normalize_evidence(
        source,
        repo=REPO,
        source_file_sha256=args.source_file_sha256,
        source_canonical_sha256=args.source_canonical_sha256,
        source_commit=args.source_commit,
    )
    path.write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "event": "reset_evidence_normalized",
                "evidence": str(path.relative_to(REPO)),
                "source_commit": args.source_commit,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
