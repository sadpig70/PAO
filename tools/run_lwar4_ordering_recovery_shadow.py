#!/usr/bin/env python3
"""Run one sealed isolated adapter-shadow recovery suite for LWAR4."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.build_lwar4_ordering_recovery_suite import canonical_sha256
from tools.run_heterogeneous_lwar_ab import (
    extract_json_object,
    reported_tokens,
    run_opencode,
    verify_finite_json_answer,
)


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def grade_result(
    task: dict[str, Any], provider: dict[str, Any]
) -> dict[str, Any]:
    failures = verify_finite_json_answer(
        task["prompt"], provider.get("answer", "")
    )
    parsed = None
    parse_error = None
    try:
        parsed = extract_json_object(provider.get("answer", ""))
    except Exception as error:
        parse_error = str(error)
    exact = parsed == task["expected"]
    accepted = bool(provider.get("ok") and not failures and exact)
    if accepted:
        reason = "exact_match"
    elif failures:
        reason = "deterministic_verification_failed:" + ",".join(failures)
    elif parse_error:
        reason = "invalid_json"
    else:
        reason = "objective_mismatch"
    return {
        "accepted": accepted,
        "deterministic_failures": failures,
        "exact_match": exact,
        "reason": reason,
    }


def build_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    return "\n".join(
        [
            "# LWAR4 Ordering Recovery Adapter Shadow",
            "",
            f"- tasks accepted: {summary['accepted']}/{summary['total']}",
            f"- tasks corrected internally: {summary['corrected']}",
            f"- reported tokens: {summary['reported_tokens']}",
            f"- telemetry exclusions: {summary['missing_telemetry']}",
            f"- verdict: `{payload['verdict']}`",
            "",
            "This is isolated adapter evidence. It does not reset the production",
            "circuit and does not count as current-generation PAO routing evidence.",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--preregistration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    suite_path = args.suite.resolve()
    preregistration_path = args.preregistration.resolve()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "raw").mkdir()

    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    preregistration = json.loads(
        preregistration_path.read_text(encoding="utf-8")
    )
    if canonical_sha256(suite) != preregistration["suite_sha256"]:
        raise SystemExit("suite hash does not match preregistration")
    if not preregistration["sealed_before_provider_execution"]:
        raise SystemExit("preregistration is not sealed")
    if preregistration["max_suite_executions"] != 1:
        raise SystemExit("preregistration must permit exactly one suite execution")

    records = []
    for task_name, task in suite["tasks"].items():
        with tempfile.TemporaryDirectory(
            prefix=f"pao-{task_name.lower()}-"
        ) as temporary:
            provider = run_opencode(task["prompt"], Path(temporary))
        grade = grade_result(task, provider)
        answer = provider.get("answer", "")
        (output / "raw" / f"{task_name}.txt").write_text(
            answer, encoding="utf-8", newline="\n"
        )
        attempts = int(
            provider.get("metrics", {}).get(
                "verification_attempt_count", 1
            )
        )
        record = {
            "task": task_name,
            "task_class": task["task_class"],
            "provider_ok": bool(provider.get("ok")),
            "duration_s": provider.get("duration_s"),
            "error": provider.get("error"),
            "reported_tokens": reported_tokens(
                provider.get("metrics") or {}
            ),
            "telemetry_complete": bool(
                (provider.get("metrics") or {}).get("telemetry_complete")
            ),
            "internal_attempts": attempts,
            "corrected": attempts > 1,
            "response_sha256": hashlib.sha256(
                answer.encode("utf-8")
            ).hexdigest(),
            "grade": grade,
        }
        records.append(record)
        write_json(
            output / "experiment.partial.json",
            {
                "schema_version": "pao.ordering-recovery-shadow.partial.v1",
                "records": records,
            },
        )
        print(
            json.dumps(
                {
                    "accepted": grade["accepted"],
                    "event": "ordering_recovery_shadow_task_completed",
                    "internal_attempts": attempts,
                    "task": task_name,
                },
                sort_keys=True,
            ),
            flush=True,
        )

    token_values = [record["reported_tokens"] for record in records]
    summary = {
        "accepted": sum(record["grade"]["accepted"] for record in records),
        "corrected": sum(record["corrected"] for record in records),
        "missing_telemetry": sum(value is None for value in token_values),
        "reported_tokens": (
            sum(value for value in token_values if value is not None)
            if all(value is not None for value in token_values)
            else None
        ),
        "total": len(records),
    }
    verdict = (
        "isolated_ordering_adapter_recovery_passed"
        if summary["accepted"] == summary["total"]
        else "isolated_ordering_adapter_recovery_incomplete"
    )
    payload = {
        "schema_version": "pao.ordering-recovery-shadow.v1",
        "completed_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "suite_sha256": preregistration["suite_sha256"],
        "adapter_contract_sha256": preregistration[
            "adapter_contract_sha256"
        ],
        "execution_scope": "isolated_adapter_shadow",
        "production_circuit_mutation": "none",
        "records": records,
        "summary": summary,
        "verdict": verdict,
    }
    write_json(output / "experiment.json", payload)
    (output / "report.md").write_text(
        build_report(payload), encoding="utf-8", newline="\n"
    )
    (output / "experiment.partial.json").unlink()
    print(
        json.dumps(
            {
                "accepted": summary["accepted"],
                "event": "ordering_recovery_shadow_completed",
                "output": str(output),
                "total": summary["total"],
                "verdict": verdict,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
