#!/usr/bin/env python3
"""Build and preregister the isolated LWAR4 ordering recovery suite."""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.run_heterogeneous_lwar_ab import (
    OPENCODE_FINITE_VERIFIER_VERSION,
    build_finite_correction_feedback,
)

PRIOR_SUITES = (
    REPO / "benchmarks" / "canary-online-suite-v1.json",
    REPO / "benchmarks" / "lwar4-remediation-suite-v1.json",
)
OUTPUT = REPO / "benchmarks" / "lwar4-ordering-recovery-suite-v1.json"
REGISTRATION = (
    REPO
    / "benchmarks"
    / "lwar4-ordering-recovery-preregistration-v1.json"
)
PRODUCTION_PROMPT_SHA256 = (
    "74e4eb62a53e3b10b229ea9f8db6bd320bd6ae9181cbca58c60cfbf857a35f4a"
)
BASE_ADAPTER_CONTRACT_SHA256 = (
    "1c788666dd70081aefcceaa3c83d868c692449cdb6ce2369ed1ec31ecd05bf41"
)
ORDERINGS = (
    "EGBDAFC",
    "BFDAGEC",
    "DAFCGBE",
    "CGEAFBD",
    "HCEAGBDF",
    "BDFHACEG",
    "EAGCHFDB",
    "GDBFAEHC",
    "IFCADGBEH",
    "BDHAFICEG",
    "EIGCBFHAD",
    "GACEIHDBF",
)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def build_suite() -> dict[str, Any]:
    tasks = {}
    for index, answer in enumerate(ORDERINGS, start=1):
        last = max(answer)
        constraints = [
            f"{answer[position]} is immediately before {answer[position + 1]}"
            for position in range(len(answer) - 1)
        ]
        rotation = (index * 3) % len(constraints)
        constraints = constraints[rotation:] + constraints[:rotation]
        tasks[f"OR{index:02d}"] = {
            "task_class": "constraint_ordering",
            "prompt": (
                "Do not use tools. Return one JSON object and no prose. Arrange "
                f"A-{last} using all constraints: "
                + "; ".join(constraints)
                + '. Return {"answer":"<letters in order with no spaces>"}'
                "."
            ),
            "expected": {"answer": answer},
        }
    prompts = [task["prompt"] for task in tasks.values()]
    if len(prompts) != len(set(prompts)):
        raise RuntimeError("ordering recovery suite contains duplicate prompts")
    prior_prompts = set()
    for path in PRIOR_SUITES:
        prior = json.loads(path.read_text(encoding="utf-8"))
        prior_prompts.update(task["prompt"] for task in prior["tasks"].values())
    overlap = sorted(prior_prompts & set(prompts))
    if overlap:
        raise RuntimeError("ordering recovery suite overlaps a prior suite")
    if PRODUCTION_PROMPT_SHA256 in {
        prompt_sha256(prompt) for prompt in prompts
    }:
        raise RuntimeError("ordering recovery suite reuses the production prompt")
    return {
        "schema_version": "pao.benchmark-suite.v1",
        "suite_id": "lwar4-ordering-recovery-suite-v1",
        "claim_scope": (
            "preregistered_nonoverlapping_isolated_adapter_shadow_recovery"
        ),
        "tasks": tasks,
    }


def build_registration(suite: dict[str, Any]) -> dict[str, Any]:
    answer_key = {
        name: task["expected"] for name, task in suite["tasks"].items()
    }
    feedback_source = inspect.getsource(build_finite_correction_feedback)
    adapter_contract = {
        "adapter_id": "opencode_zai",
        "base_adapter_contract_sha256": BASE_ADAPTER_CONTRACT_SHA256,
        "finite_verifier_version": OPENCODE_FINITE_VERIFIER_VERSION,
        "correction_feedback_version": "finite-json-feedback-v2",
        "correction_feedback_source_sha256": hashlib.sha256(
            feedback_source.encode("utf-8")
        ).hexdigest(),
        "max_internal_verification_attempts": 2,
        "provider_receives_expected_answer": False,
        "timeout_telemetry_policy": "exclude_not_estimate",
    }
    return {
        "schema_version": "pao.benchmark-preregistration.v1",
        "preregistration_id": "lwar4-ordering-recovery-preregistration-v1",
        "created_at": "2026-07-26T02:25:29.7401812Z",
        "suite_sha256": canonical_sha256(suite),
        "answer_key_sha256": canonical_sha256(answer_key),
        "prior_suite_sha256": {
            path.name: canonical_sha256(
                json.loads(path.read_text(encoding="utf-8"))
            )
            for path in PRIOR_SUITES
        },
        "excluded_production_prompt_sha256": PRODUCTION_PROMPT_SHA256,
        "adapter_contract": adapter_contract,
        "adapter_contract_sha256": canonical_sha256(adapter_contract),
        "task_counts": {"constraint_ordering": len(suite["tasks"])},
        "prompt_overlap_with_prior": 0,
        "execution_rule": (
            "isolated_adapter_shadow_one_run_opencode_call_per_task"
        ),
        "max_suite_executions": 1,
        "production_circuit_policy": "preserve_open_no_reset",
        "sealed_before_provider_execution": True,
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    suite = build_suite()
    registration = build_registration(suite)
    write_json(OUTPUT, suite)
    write_json(REGISTRATION, registration)
    print(
        json.dumps(
            {
                "event": "lwar4_ordering_recovery_suite_preregistered",
                "preregistration": str(REGISTRATION),
                "suite": str(OUTPUT),
                "suite_sha256": registration["suite_sha256"],
                "tasks": len(suite["tasks"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
