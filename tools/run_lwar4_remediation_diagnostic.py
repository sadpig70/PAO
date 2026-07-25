#!/usr/bin/env python3
"""Replay only historical LWAR4 failures as non-blind diagnostic evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.run_heterogeneous_lwar_ab import (
    extract_json_object,
    reported_tokens,
    run_opencode,
    run_provider_with_retry,
)


HISTORICAL_FAILURES = ("BO02", "BO04", "BO05", "BO10", "CO08")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def objective_grade(answer: str, expected: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = extract_json_object(answer)
    except Exception as error:
        return {
            "score": 0,
            "valid_json": False,
            "reason": f"invalid_json:{error}",
        }
    exact = all(parsed.get(key) == value for key, value in expected.items())
    return {
        "score": int(exact),
        "valid_json": True,
        "reason": "exact_match" if exact else "objective_mismatch",
        "parsed": parsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    suite = json.loads(args.suite.read_text(encoding="utf-8"))
    output = args.output.resolve()
    work = output / "workspace"
    work.mkdir(parents=True, exist_ok=True)
    records = []
    for task_name in HISTORICAL_FAILURES:
        task = suite["tasks"][task_name]
        provider = run_provider_with_retry(
            run_opencode,
            task["prompt"],
            work,
        )
        grade = (
            objective_grade(provider["answer"], task["expected"])
            if provider["ok"]
            else {
                "score": 0,
                "valid_json": False,
                "reason": provider["error"],
            }
        )
        raw = output / "raw" / f"{task_name}.txt"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(provider["answer"], encoding="utf-8", newline="\n")
        records.append(
            {
                "task": task_name,
                "task_class": task["task_class"],
                "expected": task["expected"],
                "provider": {
                    key: value
                    for key, value in provider.items()
                    if key != "answer"
                },
                "reported_tokens": reported_tokens(provider["metrics"]),
                "grade": grade,
            }
        )
        print(
            json.dumps(
                {
                    "event": "diagnostic_task_completed",
                    "task": task_name,
                    "accepted": bool(grade["score"]),
                    "provider_ok": provider["ok"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
    report = {
        "schema_version": "pao.lwar4-remediation-diagnostic.v1",
        "claim_scope": "historical_failures_not_blind_evidence",
        "records": records,
        "accepted": sum(record["grade"]["score"] for record in records),
        "total": len(records),
        "verdict": (
            "historical_failures_remediated"
            if all(record["grade"]["score"] for record in records)
            else "historical_failures_remediation_partial"
        ),
    }
    write_json(output / "diagnostic.json", report)
    print(
        json.dumps(
            {
                "event": "lwar4_remediation_diagnostic_completed",
                "accepted": report["accepted"],
                "total": report["total"],
                "verdict": report["verdict"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
