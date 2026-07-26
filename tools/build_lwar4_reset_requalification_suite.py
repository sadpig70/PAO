#!/usr/bin/env python3
"""Build the sealed LWAR4 reset and requalification campaign."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import itertools
import json
import random
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
LWAR_BUNDLE = REPO / ".agents" / "skills" / "pao-lwar"
if str(LWAR_BUNDLE) not in sys.path:
    sys.path.insert(0, str(LWAR_BUNDLE))

from pao_runtime.canary_routing import load_canary_policy, load_circuit_state
from pao_runtime.predictive_routing import canonical_sha256, load_routing_profile
from tools.run_heterogeneous_lwar_ab import (
    OPENCODE_FINITE_VERIFIER_VERSION,
    build_finite_correction_feedback,
    verify_finite_json_answer,
)

SUITE_PATH = (
    REPO / "benchmarks" / "lwar4-reset-requalification-suite-v1.json"
)
PREREGISTRATION_PATH = (
    REPO
    / "benchmarks"
    / "lwar4-reset-requalification-preregistration-v1.json"
)
BASE_ADAPTER_CONTRACT_SHA256 = (
    "1c788666dd70081aefcceaa3c83d868c692449cdb6ce2369ed1ec31ecd05bf41"
)
CAMPAIGN_CREATED_AT = "2026-07-26T10:00:00Z"
RESET_REASON = (
    "LWAR4 current-generation paired recovery passed 12/12 against "
    "LWAR1 12/12 with complete telemetry and unchanged safety invariants"
)
PHASE_COUNTS = {
    "recovery_pair": 12,
    "post_reset_shadow": 13,
    "production_canary": 1,
    "fallback_probe": 1,
}


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fact_catalog(
    symbols: tuple[str, ...], target: tuple[str, ...]
) -> list[tuple[str, Any]]:
    position = {symbol: index for index, symbol in enumerate(target)}
    facts: list[tuple[str, Any]] = []
    for symbol in symbols:
        index = position[symbol]
        facts.append(
            (
                f"{symbol} is in position {index + 1}",
                lambda candidate, s=symbol, i=index: candidate[i] == s,
            )
        )
    for left, right in itertools.permutations(symbols, 2):
        left_index = position[left]
        right_index = position[right]
        if left_index < right_index:
            facts.append(
                (
                    f"{left} is before {right}",
                    lambda candidate, l=left, r=right: candidate.index(l)
                    < candidate.index(r),
                )
            )
        if right_index == left_index + 1:
            facts.append(
                (
                    f"{left} is immediately before {right}",
                    lambda candidate, l=left, r=right: candidate.index(r)
                    == candidate.index(l) + 1,
                )
            )
    for left, right in itertools.combinations(symbols, 2):
        if abs(position[left] - position[right]) > 1:
            facts.append(
                (
                    f"{left} is not adjacent to {right}",
                    lambda candidate, l=left, r=right: abs(
                        candidate.index(l) - candidate.index(r)
                    )
                    > 1,
                )
            )
    return facts


def unique_constraints(ordering: str, seed: int) -> list[str]:
    symbols = tuple(sorted(ordering))
    target = tuple(ordering)
    candidates = list(itertools.permutations(symbols))
    facts = _fact_catalog(symbols, target)
    random.Random(seed).shuffle(facts)
    selected: list[tuple[str, Any]] = []
    remaining = candidates
    for fact in facts:
        narrowed = [candidate for candidate in remaining if fact[1](candidate)]
        if len(narrowed) < len(remaining):
            selected.append(fact)
            remaining = narrowed
        if remaining == [target]:
            break
    if remaining != [target]:
        raise RuntimeError(f"could not derive unique ordering: {ordering}")
    for fact in list(reversed(selected)):
        trial = [item for item in selected if item is not fact]
        valid = [
            candidate
            for candidate in candidates
            if all(item[1](candidate) for item in trial)
        ]
        if valid == [target]:
            selected = trial
    return [description for description, _ in selected]


def build_task(name: str, ordering: str, phase: str, seed: int) -> dict[str, Any]:
    symbols = "".join(sorted(ordering))
    constraints = unique_constraints(ordering, seed)
    prompt = (
        "Do not use tools. Return one JSON object and no prose. "
        f"Arrange A-{symbols[-1]} using every letter exactly once. "
        + "; ".join(constraints)
        + '. Return {"answer":"<letters with no spaces>",'
        '"reason":"<short proof>"}.'
    )
    return {
        "task_class": "constraint_ordering",
        "phase": phase,
        "prompt": prompt,
        "expected": {"answer": ordering},
    }


def build_suite(
    *,
    campaign_version: int = 1,
    seed_offset: int = 0,
) -> dict[str, Any]:
    tasks: dict[str, dict[str, Any]] = {}
    cursor = 0
    for phase, count in PHASE_COUNTS.items():
        prefix = {
            "recovery_pair": "RR",
            "post_reset_shadow": "RQ",
            "production_canary": "PC",
            "fallback_probe": "FB",
        }[phase]
        for index in range(1, count + 1):
            size = 6 + (cursor % 2)
            symbols = [chr(ord("A") + offset) for offset in range(size)]
            random.Random(8100 + seed_offset + cursor).shuffle(symbols)
            ordering = "".join(symbols)
            tasks[f"{prefix}{index:02d}"] = build_task(
                f"{prefix}{index:02d}",
                ordering,
                phase,
                9100 + seed_offset + cursor,
            )
            cursor += 1
    prompts = [task["prompt"] for task in tasks.values()]
    if len(prompts) != len(set(prompts)):
        raise RuntimeError("campaign contains duplicate prompts")
    return {
        "schema_version": "pao.benchmark-suite.v1",
        "suite_id": (
            f"lwar4-reset-requalification-suite-v{campaign_version}"
        ),
        "claim_scope": (
            "preregistered_current_generation_reset_requalification_campaign"
        ),
        "tasks": tasks,
    }


def prior_prompt_hashes(
    *, excluded_path: Path = SUITE_PATH
) -> tuple[set[str], dict[str, str]]:
    hashes: set[str] = set()
    sources = {}
    for path in sorted((REPO / "benchmarks").glob("*suite*.json")):
        if path == excluded_path:
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        tasks = value.get("tasks")
        if not isinstance(tasks, dict):
            continue
        sources[path.name] = canonical_sha256(value)
        for task in tasks.values():
            prompt = task.get("prompt")
            if isinstance(prompt, str):
                hashes.add(prompt_sha256(prompt))
    production = json.loads(
        (REPO / "benchmarks" / "lwar4-production-canary-preregistration-v1.json")
        .read_text(encoding="utf-8")
    )
    hashes.add(production["task"]["prompt_sha256"])
    return hashes, sources


def build_preregistration(
    suite: dict[str, Any],
    *,
    root: Path,
    profile_path: Path,
    policy_path: Path,
    campaign_version: int = 1,
    suite_path: Path = SUITE_PATH,
    predecessor_evidence_path: Path | None = None,
) -> dict[str, Any]:
    profile = load_routing_profile(profile_path)
    policy = load_canary_policy(policy_path)
    circuit_state = load_circuit_state(root)
    circuit_key = "constraint_ordering::LWAR4"
    circuit = circuit_state["circuits"].get(circuit_key)
    if not circuit or circuit["status"] != "open":
        raise RuntimeError(f"required circuit is not open: {circuit_key}")
    if circuit["policy_sha256"] != canonical_sha256(policy):
        raise RuntimeError("open circuit policy does not match campaign policy")
    prior_hashes, prior_sources = prior_prompt_hashes(
        excluded_path=suite_path
    )
    campaign_hashes = {
        prompt_sha256(task["prompt"]) for task in suite["tasks"].values()
    }
    overlap = sorted(campaign_hashes & prior_hashes)
    if overlap:
        raise RuntimeError(f"campaign prompt overlap: {overlap}")
    answer_key = {
        name: task["expected"] for name, task in suite["tasks"].items()
    }
    correction_source = inspect.getsource(build_finite_correction_feedback)
    verifier_source = inspect.getsource(verify_finite_json_answer)
    adapter_contract = {
        "adapter_id": "opencode_zai",
        "base_adapter_contract_sha256": BASE_ADAPTER_CONTRACT_SHA256,
        "finite_verifier_version": OPENCODE_FINITE_VERIFIER_VERSION,
        "finite_verifier_source_sha256": hashlib.sha256(
            verifier_source.encode("utf-8")
        ).hexdigest(),
        "correction_feedback_version": "finite-json-feedback-v2",
        "correction_feedback_source_sha256": hashlib.sha256(
            correction_source.encode("utf-8")
        ).hexdigest(),
        "max_internal_verification_attempts": 2,
        "provider_receives_expected_answer": False,
        "timeout_telemetry_policy": "exclude_not_estimate",
    }
    circuit_path = root / "var" / "routing" / "canary-circuits.json"
    preregistration = {
        "schema_version": "pao.reset-requalification-preregistration.v1",
        "preregistration_id": (
            f"lwar4-reset-requalification-v{campaign_version}"
        ),
        "created_at": (
            CAMPAIGN_CREATED_AT
            if campaign_version == 1
            else "2026-07-26T07:10:00Z"
        ),
        "sealed_before_provider_execution": True,
        "max_campaign_executions": 1,
        "suite_sha256": canonical_sha256(suite),
        "answer_key_sha256": canonical_sha256(answer_key),
        "prompt_overlap_with_prior": 0,
        "prior_suite_sha256": prior_sources,
        "source_bus": {
            "root_name": root.name,
            "circuit_key": circuit_key,
            "circuit_status": circuit["status"],
            "circuit_reason": circuit["reason"],
            "circuit_policy_sha256": circuit["policy_sha256"],
            "circuit_file_sha256": raw_sha256(circuit_path),
            "profile_sha256": canonical_sha256(profile),
            "policy_sha256": canonical_sha256(policy),
        },
        "authority": {"write": [], "network": False},
        "adapter_contract": adapter_contract,
        "adapter_contract_sha256": canonical_sha256(adapter_contract),
        "identity_policy": (
            "reuse_exact_trusted_handoff_identity_current_instance_generation"
        ),
        "recovery_gate": {
            "paired_tasks": PHASE_COUNTS["recovery_pair"],
            "required_lwar1_accepted": PHASE_COUNTS["recovery_pair"],
            "required_lwar4_accepted": PHASE_COUNTS["recovery_pair"],
            "required_complete_telemetry": True,
            "required_circuit_mutation": "none",
            "required_audit_status": "healthy",
            "required_active_claims": 0,
            "required_active_leases": 0,
        },
        "reset_action": {
            "command": "routing-circuit-reset",
            "lwar_id": "LWAR4",
            "task_class": "constraint_ordering",
            "reason": RESET_REASON,
        },
        "post_reset_gate": {
            "required_fresh_candidate_accepted": PHASE_COUNTS[
                "post_reset_shadow"
            ],
            "allowed_candidate_rejected": 0,
            "required_route_mode": "live",
            "required_reason": "confidence_qualified_live",
            "pre_reset_candidate_observations_count": False,
        },
        "production_gate": {
            "max_live_canaries": PHASE_COUNTS["production_canary"],
            "on_acceptance": "verify_circuit_closed",
            "on_rejection": "verify_sticky_circuit_and_incumbent_fallback",
            "fallback_probes": PHASE_COUNTS["fallback_probe"],
        },
    }
    if predecessor_evidence_path is not None:
        predecessor = json.loads(
            predecessor_evidence_path.read_text(encoding="utf-8")
        )
        preregistration["predecessor_evidence"] = {
            "path": predecessor_evidence_path.name,
            "sha256": canonical_sha256(predecessor),
            "verdict": predecessor["verdict"],
            "reuse_allowed": False,
        }
    return preregistration


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--campaign-version", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    version = args.campaign_version
    suite_path = (
        SUITE_PATH
        if version == 1
        else REPO
        / "benchmarks"
        / "lwar4-reset-requalification-suite-v2.json"
    )
    preregistration_path = (
        PREREGISTRATION_PATH
        if version == 1
        else REPO
        / "benchmarks"
        / "lwar4-reset-requalification-preregistration-v2.json"
    )
    predecessor = (
        None
        if version == 1
        else REPO
        / "benchmarks"
        / "lwar4-reset-requalification-evidence-v1.json"
    )
    if predecessor is not None and not predecessor.is_file():
        raise SystemExit(f"v2 requires preserved v1 evidence: {predecessor}")
    suite = build_suite(
        campaign_version=version,
        seed_offset=0 if version == 1 else 1000,
    )
    preregistration = build_preregistration(
        suite,
        root=args.root.resolve(),
        profile_path=args.profile.resolve(),
        policy_path=args.policy.resolve(),
        campaign_version=version,
        suite_path=suite_path,
        predecessor_evidence_path=predecessor,
    )
    write_json(suite_path, suite)
    write_json(preregistration_path, preregistration)
    print(
        json.dumps(
            {
                "suite": str(suite_path),
                "suite_sha256": canonical_sha256(suite),
                "preregistration": str(preregistration_path),
                "tasks": len(suite["tasks"]),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
