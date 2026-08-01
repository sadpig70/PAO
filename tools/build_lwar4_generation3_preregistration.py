#!/usr/bin/env python3
"""Bind the live LWAR4 generation-3 (Kimi) identity before provider execution.

Generation 3 reuses the sealed generation-2 calibration suite (operator
decision) and replaces the failed generation-2 Qwen provider. The truthful
replacement profile must be the Kimi Code CLI (adapter kimi_cli, vendor
moonshot) and must differ in adapter and vendor from BOTH retired generations
(generation 1 OpenCode/z_ai and generation 2 Qwen/alibaba).
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

LWAR_BUNDLE = REPO / ".agents" / "skills" / "pao-lwar"
if str(LWAR_BUNDLE) not in sys.path:
    sys.path.insert(0, str(LWAR_BUNDLE))

from pao_runtime.predictive_routing import canonical_sha256, load_routing_profile
from pao_runtime.canary_routing import load_canary_policy
from tools.run_heterogeneous_lwar_ab import ADAPTERS, verify_finite_json_answer


# Operator decision: generation 3 REUSES the generation-2 sealed suite.
SUITE_PATH = REPO / "benchmarks" / "lwar4-generation2-calibration-suite-v1.json"
TARGET = (
    REPO
    / "benchmarks"
    / "lwar4-generation3-calibration-preregistration-v1.json"
)
# The immediately-retired generation is generation 2.
PREDECESSOR_PREREGISTRATION = (
    REPO / "benchmarks" / "lwar4-generation2-calibration-preregistration-v1.json"
)
CREATED_AT = "2026-08-01T11:00:00Z"
CIRCUIT_KEY = "constraint_ordering::LWAR4"
PROFILE_KEYS = ("runtime_name", "model", "adapter_id", "vendor_family")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_preregistration(
    *,
    identity: dict[str, Any],
    registry: dict[str, Any],
    circuit_state: dict[str, Any],
    suite: dict[str, Any],
    identity_file_sha256: str,
    registry_file_sha256: str,
    circuit_file_sha256: str,
    profile_sha256: str,
    policy_sha256: str,
    created_at: str = CREATED_AT,
) -> dict[str, Any]:
    slot = registry.get("slots", {}).get("LWAR4")
    if not isinstance(slot, dict):
        raise RuntimeError("LWAR4 registry slot is absent")
    for field in ("instance_id", "generation", "state", "profile"):
        if slot.get(field) != identity.get(field):
            raise RuntimeError(f"LWAR4 identity/registry mismatch: {field}")
    if identity.get("lwar_id") != "LWAR4" or identity.get("generation") != 3:
        raise RuntimeError("preregistration requires exact LWAR4 generation 3")
    profile = identity.get("profile")
    if not isinstance(profile, dict):
        raise RuntimeError("LWAR4 profile is missing")
    if (
        profile.get("adapter_id") != "kimi_cli"
        or profile.get("vendor_family") != "moonshot"
        or profile.get("interface") != "cli"
    ):
        raise RuntimeError("LWAR4 profile is not the truthful Kimi CLI profile")

    circuit = circuit_state.get("circuits", {}).get(CIRCUIT_KEY)
    if not isinstance(circuit, dict) or circuit.get("status") != "open":
        raise RuntimeError(f"required circuit is not open: {CIRCUIT_KEY}")
    if circuit.get("policy_sha256") != policy_sha256:
        raise RuntimeError("open circuit policy does not match campaign policy")

    predecessor = load_json(PREDECESSOR_PREREGISTRATION)
    # Generation 3 replaces generation 2 (Qwen/alibaba). Its truthful profile
    # is recorded from the predecessor preregistration's bound identity.
    retired_profile = {
        key: predecessor["target_identity"]["profile"][key] for key in PROFILE_KEYS
    }
    # Fail closed unless the replacement differs in adapter AND vendor from both
    # retired generations (generation 2 just above, generation 1 below).
    generation1_profile = {key: ADAPTERS["LWAR4"][key] for key in PROFILE_KEYS}
    for retired in (retired_profile, generation1_profile):
        if (
            profile["adapter_id"] == retired["adapter_id"]
            or profile["vendor_family"] == retired["vendor_family"]
        ):
            raise RuntimeError(
                "replacement provider duplicates a retired generation adapter/vendor"
            )

    expected = {
        name: task["expected"] for name, task in suite["tasks"].items()
    }
    verifier_source = inspect.getsource(verify_finite_json_answer)
    adapter_contract = {
        "adapter_id": profile["adapter_id"],
        "execution_mode": "resident_agent_begin_complete",
        "finite_verifier_source_sha256": hashlib.sha256(
            verifier_source.encode("utf-8")
        ).hexdigest(),
        "finite_verifier_version": "finite-json-v2",
        "max_provider_calls_per_task": 1,
        "provider_receives_expected_answer": False,
        "result_submission_contract": "lwar-runtime.v2-adp",
        "token_telemetry": {
            "accepted_source": "exact_runtime_report_only",
            "missing_policy": "exclude_not_estimate",
            "required_before_circuit_reset": True,
        },
    }
    target_identity = {
        "lwar_id": identity["lwar_id"],
        "instance_id": identity["instance_id"],
        "generation": identity["generation"],
        "registry_version": identity["registry_version"],
        "behavior_contract": identity["behavior_contract"],
        "state": identity["state"],
        "profile": profile,
        "identity_file_sha256": identity_file_sha256,
    }
    return {
        "schema_version": "pao.generation-calibration-preregistration.v1",
        "preregistration_id": "lwar4-generation3-calibration-v1",
        "created_at": created_at,
        "sealed_before_provider_execution": True,
        "max_campaign_executions": 1,
        "suite_reused_from": "lwar4-generation2-calibration-v1",
        "suite_sha256": canonical_sha256(suite),
        "answer_key_sha256": canonical_sha256(expected),
        "target_identity": target_identity,
        "adapter_contract": adapter_contract,
        "adapter_contract_sha256": canonical_sha256(adapter_contract),
        "replacement_evidence": {
            "retired_adapter_contract_sha256": predecessor[
                "adapter_contract_sha256"
            ],
            "retired_profile": retired_profile,
            "retired_profile_sha256": canonical_sha256(retired_profile),
            "profile_rule": "adapter_and_vendor_differ_exact_model_unreported",
            "claim_boundary": (
                "provider_family_heterogeneity_only_until_exact_model_reported"
            ),
        },
        "source_bus": {
            "root_name": "pao-lwar4-remediation-blind-01",
            "registry_file_sha256": registry_file_sha256,
            "registry_version": registry["registry_version"],
            "circuit_key": CIRCUIT_KEY,
            "circuit_status": circuit["status"],
            "circuit_reason": circuit["reason"],
            "circuit_policy_sha256": circuit["policy_sha256"],
            "circuit_file_sha256": circuit_file_sha256,
            "profile_sha256": profile_sha256,
            "policy_sha256": policy_sha256,
        },
        "authority": {"write": [], "network": False},
        "recovery_gate": {
            "tasks": 12,
            "required_lwar4_accepted": 12,
            "objective_reference": "answer_key_free_finite_verifier",
            "incumbent_provider_calls": 0,
            "required_exact_token_reports": 12,
            "on_missing_token_report": (
                "preserve_open_and_stop_before_circuit_reset"
            ),
            "required_circuit_mutation": "none",
            "required_audit_status": "healthy",
            "required_active_claims": 0,
            "required_active_leases": 0,
        },
        "reset_action": {
            "command": "routing-circuit-reset",
            "lwar_id": "LWAR4",
            "task_class": "constraint_ordering",
            "reason": (
                "LWAR4 generation 3 passed 12/12 sealed recovery tasks with "
                "exact telemetry and unchanged safety invariants"
            ),
        },
        "post_reset_gate": {
            "required_fresh_candidate_accepted": 13,
            "allowed_candidate_rejected": 0,
            "required_route_mode": "live",
            "required_reason": "confidence_qualified_live",
            "pre_reset_candidate_observations_count": False,
        },
        "production_gate": {
            "max_live_canaries": 1,
            "on_acceptance": "verify_circuit_closed",
            "on_rejection": "verify_sticky_circuit_and_incumbent_fallback",
            "fallback_probes": 1,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--created-at", default=CREATED_AT)
    args = parser.parse_args()

    root = args.root.resolve()
    registry_path = root / "var" / "registry" / "lwar_registry.json"
    circuit_path = root / "var" / "routing" / "canary-circuits.json"
    registry = load_json(registry_path)
    slot = registry.get("slots", {}).get("LWAR4")
    if not isinstance(slot, dict):
        raise SystemExit("LWAR4 registry slot is absent")
    identity_path = (
        root / "var" / "identities" / f"{slot['instance_id']}.json"
    )
    suite = load_json(SUITE_PATH)
    preregistration = build_preregistration(
        identity=load_json(identity_path),
        registry=registry,
        circuit_state=load_json(circuit_path),
        suite=suite,
        identity_file_sha256=raw_sha256(identity_path),
        registry_file_sha256=raw_sha256(registry_path),
        circuit_file_sha256=raw_sha256(circuit_path),
        profile_sha256=canonical_sha256(
            load_routing_profile(args.profile.resolve())
        ),
        policy_sha256=canonical_sha256(
            load_canary_policy(args.policy.resolve())
        ),
        created_at=args.created_at,
    )
    TARGET.write_text(
        json.dumps(preregistration, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        json.dumps(
            {
                "event": "generation3_preregistration_built",
                "path": str(TARGET.relative_to(REPO)).replace("\\", "/"),
                "sha256": canonical_sha256(preregistration),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
