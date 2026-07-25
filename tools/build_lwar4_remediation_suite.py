#!/usr/bin/env python3
"""Build and preregister the non-overlapping LWAR4 remediation suite."""

from __future__ import annotations

import hashlib
import itertools
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.run_heterogeneous_lwar_ab import (
    OPENCODE_FINITE_VERIFIER_VERSION,
    OPENCODE_VERIFICATION_PREAMBLE,
)


PRIOR_SUITE = REPO / "benchmarks" / "canary-online-suite-v1.json"
OUTPUT = REPO / "benchmarks" / "lwar4-remediation-suite-v1.json"
REGISTRATION = (
    REPO / "benchmarks" / "lwar4-remediation-preregistration-v1.json"
)

ORDERINGS = (
    "FABCDE",
    "BDFACE",
    "CEAFDB",
    "DAFCEB",
    "EBCFAD",
    "ACFDEB",
    "FEDCBA",
    "CBAFDE",
    "DEABFC",
    "AFBDCE",
    "BECADF",
    "CDFBEA",
)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ordering_tasks() -> dict[str, dict[str, Any]]:
    tasks = {}
    for index, answer in enumerate(ORDERINGS, start=1):
        constraints = [
            f"{answer[position]} is immediately before {answer[position + 1]}"
            for position in range(len(answer) - 1)
        ]
        rotation = (index * 2) % len(constraints)
        constraints = constraints[rotation:] + constraints[:rotation]
        tasks[f"RCO{index:02d}"] = {
            "task_class": "constraint_ordering",
            "prompt": (
                "Do not use tools. Return one JSON object and no prose. Arrange "
                "A-F using all constraints: "
                + "; ".join(constraints)
                + '. Return {"answer":"<six letters in order>"}.'
            ),
            "expected": {"answer": answer},
        }
    return tasks


def bounded_value(index: int, name: str, field: str, minimum: int, width: int) -> int:
    digest = hashlib.sha256(
        f"lwar4-remediation-v1:{index}:{name}:{field}".encode("utf-8")
    ).digest()
    return minimum + int.from_bytes(digest[:4], "big") % width


def optimization_tasks() -> dict[str, dict[str, Any]]:
    tasks = {}
    for index in range(1, 13):
        items = [
            {
                "name": name,
                "cost": bounded_value(index, name, "cost", 2, 7),
                "risk": bounded_value(index, name, "risk", 1, 5),
                "value": bounded_value(index, name, "value", 4, 12),
            }
            for name in "ABCDEF"
        ]
        cost_limit = 12 + index % 5
        risk_limit = 7 + index % 3
        feasible = []
        for count in range(1, len(items) + 1):
            for subset in itertools.combinations(items, count):
                cost = sum(item["cost"] for item in subset)
                risk = sum(item["risk"] for item in subset)
                if cost <= cost_limit and risk <= risk_limit:
                    feasible.append(
                        {
                            "selection": [item["name"] for item in subset],
                            "value": sum(item["value"] for item in subset),
                            "cost": cost,
                            "risk": risk,
                        }
                    )
        winner = min(
            feasible,
            key=lambda item: (
                -item["value"],
                item["cost"],
                item["risk"],
                item["selection"],
            ),
        )
        catalog = ", ".join(
            f"{item['name']}(cost{item['cost']},risk{item['risk']},value{item['value']})"
            for item in items
        )
        tasks[f"RBO{index:02d}"] = {
            "task_class": "bounded_optimization",
            "prompt": (
                "Do not use tools. Return one JSON object and no prose. Choose a "
                f"subset with total cost <={cost_limit} and risk <={risk_limit} "
                "that maximizes value; tie-break by lower cost, then lower risk, "
                f"then lexicographically sorted selection. Items: {catalog}. "
                'Return {"selection":["sorted letters"],"value":<int>,'
                '"cost":<int>,"risk":<int>}.'
            ),
            "expected": winner,
        }
    return tasks


def build_suite() -> dict[str, Any]:
    tasks = {}
    tasks.update(ordering_tasks())
    tasks.update(optimization_tasks())
    prompts = [task["prompt"] for task in tasks.values()]
    if len(prompts) != len(set(prompts)):
        raise RuntimeError("remediation suite contains duplicate prompts")
    prior = json.loads(PRIOR_SUITE.read_text(encoding="utf-8"))
    prior_prompts = {
        task["prompt"] for task in prior["tasks"].values()
    }
    overlap = sorted(prior_prompts & set(prompts))
    if overlap:
        raise RuntimeError("remediation suite overlaps the prior suite")
    return {
        "schema_version": "pao.benchmark-suite.v1",
        "suite_id": "lwar4-remediation-suite-v1",
        "claim_scope": "preregistered_nonoverlapping_blind_shadow_evidence",
        "tasks": tasks,
    }


def build_registration(suite: dict[str, Any]) -> dict[str, Any]:
    prior = json.loads(PRIOR_SUITE.read_text(encoding="utf-8"))
    adapter_contract = {
        "adapter_id": "opencode_zai",
        "model": "zai-coding-plan/glm-4.7",
        "variant": "high",
        "verification_preamble_sha256": hashlib.sha256(
            OPENCODE_VERIFICATION_PREAMBLE.encode("utf-8")
        ).hexdigest(),
        "timeout_s": 180,
        "max_attempts": 2,
        "finite_verifier_version": OPENCODE_FINITE_VERIFIER_VERSION,
        "max_internal_verification_attempts": 2,
        "timeout_telemetry_policy": "exclude_not_estimate",
    }
    answer_key = {
        name: task["expected"] for name, task in suite["tasks"].items()
    }
    counts = {}
    for task in suite["tasks"].values():
        task_class = task["task_class"]
        counts[task_class] = counts.get(task_class, 0) + 1
    return {
        "schema_version": "pao.benchmark-preregistration.v1",
        "preregistration_id": "lwar4-remediation-preregistration-v1",
        "created_at": "2026-07-25T16:15:00Z",
        "suite_sha256": canonical_sha256(suite),
        "answer_key_sha256": canonical_sha256(answer_key),
        "prior_suite_sha256": canonical_sha256(prior),
        "adapter_contract": adapter_contract,
        "adapter_contract_sha256": canonical_sha256(adapter_contract),
        "task_counts": dict(sorted(counts.items())),
        "prompt_overlap_with_prior": 0,
        "promotion_policy": "benchmarks/canary-policy-v1.json",
        "execution_rule": "four_alias_current_generation_explicit_shadow",
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
                "event": "lwar4_remediation_suite_preregistered",
                "suite": str(OUTPUT),
                "preregistration": str(REGISTRATION),
                "suite_sha256": registration["suite_sha256"],
                "tasks": len(suite["tasks"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
