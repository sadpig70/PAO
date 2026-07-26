from __future__ import annotations

import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

from .common import atomic_write_json, utc_now
from .contracts import ContractError, validate_contract
from .predictive_routing import (
    _aggregate,
    _global_quality_leader,
    _lwar_number,
    calibration_task_ids,
    canonical_sha256,
    select_predictive_lwar,
    validate_routing_profile,
)


CANARY_POLICY_SCHEMA_VERSION = "pao.canary-policy.v1"
CANARY_RECEIPT_SCHEMA_VERSION = "pao.canary-routing-receipt.v1"
OBSERVATION_SCHEMA_VERSION = "pao.routing-observation.v1"
CIRCUIT_STATE_SCHEMA_VERSION = "pao.routing-circuit-state.v1"
EMPTY_STATE_TIME = "1970-01-01T00:00:00Z"


def validate_canary_policy(policy: dict[str, Any]) -> None:
    validate_contract(policy, "canary-policy.schema.json")


def load_canary_policy(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"canary policy is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise ContractError("canary policy must be a JSON object")
    validate_canary_policy(value)
    return value


def empty_circuit_state() -> dict[str, Any]:
    return {
        "schema_version": CIRCUIT_STATE_SCHEMA_VERSION,
        "updated_at": EMPTY_STATE_TIME,
        "circuits": {},
        "resets": {},
    }


def circuit_key(task_class: str, lwar_id: str) -> str:
    return f"{task_class}::{lwar_id}"


def validate_circuit_state(state: dict[str, Any]) -> None:
    validate_contract(state, "routing-circuit-state.schema.json")
    for key, item in state["circuits"].items():
        if key != circuit_key(item["task_class"], item["lwar_id"]):
            raise ContractError(f"routing circuit key mismatch: {key}")
    for key, item in state["resets"].items():
        if key != circuit_key(item["task_class"], item["lwar_id"]):
            raise ContractError(f"routing reset key mismatch: {key}")


def circuit_state_path(root: Path) -> Path:
    return root / "var" / "routing" / "canary-circuits.json"


def load_circuit_state(root: Path) -> dict[str, Any]:
    path = circuit_state_path(root)
    if not path.is_file():
        return empty_circuit_state()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"routing circuit state is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise ContractError("routing circuit state must be a JSON object")
    validate_circuit_state(value)
    return value


def write_circuit_state(root: Path, state: dict[str, Any]) -> Path:
    validate_circuit_state(state)
    path = circuit_state_path(root)
    atomic_write_json(path, state)
    return path


def validate_routing_observation(observation: dict[str, Any]) -> None:
    validate_contract(observation, "routing-observation.schema.json")
    binding = {
        key: observation[key]
        for key in (
            "task_id",
            "task_class",
            "lwar_id",
            "instance_id",
            "generation",
            "registry_version",
            "receipt_id",
            "route_mode",
            "accepted",
            "reported_tokens",
            "validation_sha256",
        )
    }
    expected = f"routing-observation-{canonical_sha256(binding)[:32]}"
    if observation["observation_id"] != expected:
        raise ContractError("routing observation id binding mismatch")


def validate_canary_receipt(receipt: dict[str, Any]) -> None:
    validate_contract(receipt, "canary-routing-receipt.schema.json")
    policy = receipt["decision"]["policy"]
    validate_canary_policy(policy)
    if canonical_sha256(policy) != receipt["policy_sha256"]:
        raise ContractError("canary routing receipt policy binding mismatch")
    eligible = set(receipt["eligible_lwar_ids"])
    if (
        receipt["selected_lwar_id"] not in eligible
        or receipt["incumbent_lwar_id"] not in eligible
        or (
            receipt["candidate_lwar_id"] is not None
            and receipt["candidate_lwar_id"] not in eligible
        )
    ):
        raise ContractError("canary routing receipt eligible binding mismatch")
    binding = {
        key: receipt[key]
        for key in (
            "task_id",
            "task_class",
            "profile_sha256",
            "policy_sha256",
            "observations_sha256",
            "circuit_state_sha256",
            "selected_lwar_id",
            "selected_instance_id",
            "selected_generation",
            "selected_registry_version",
            "incumbent_lwar_id",
            "candidate_lwar_id",
            "eligible_lwar_ids",
            "route_mode",
            "reason",
        )
    }
    expected = f"canary-routing-{canonical_sha256(binding)[:32]}"
    if receipt["receipt_id"] != expected:
        raise ContractError("canary routing receipt id binding mismatch")


def observation_path(root: Path, task_id: str) -> Path:
    return root / "var" / "routing" / "observations" / f"{task_id}.json"


def load_routing_observations(root: Path) -> list[dict[str, Any]]:
    directory = root / "var" / "routing" / "observations"
    if not directory.is_dir():
        return []
    observations = []
    seen_ids: set[str] = set()
    for path in sorted(directory.glob("task-*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractError(f"routing observation is unreadable: {path}") from error
        if not isinstance(value, dict):
            raise ContractError(f"routing observation must be an object: {path}")
        validate_routing_observation(value)
        if value["task_id"] != path.stem:
            raise ContractError(f"routing observation path mismatch: {path}")
        if value["observation_id"] in seen_ids:
            raise ContractError(
                f"duplicate routing observation id: {value['observation_id']}"
            )
        receipt_path = (
            root / "var" / "routing" / "receipts" / f"{value['task_id']}.json"
        )
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractError(
                f"routing observation receipt is unreadable: {receipt_path}"
            ) from error
        validate_canary_receipt(receipt)
        receipt_fields = {
            "receipt_id": "receipt_id",
            "task_id": "task_id",
            "task_class": "task_class",
            "lwar_id": "selected_lwar_id",
            "instance_id": "selected_instance_id",
            "generation": "selected_generation",
            "registry_version": "selected_registry_version",
            "route_mode": "route_mode",
        }
        for observation_field, receipt_field in receipt_fields.items():
            if value[observation_field] != receipt[receipt_field]:
                raise ContractError(
                    f"routing observation receipt binding mismatch: {observation_field}"
                )
        ledger_paths = sorted(
            (root / "var" / "tasks").glob(f"*/{value['task_id']}.json")
        )
        if len(ledger_paths) != 1:
            raise ContractError(
                f"routing observation requires one task ledger entry: {value['task_id']}"
            )
        try:
            ledger = json.loads(ledger_paths[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ContractError(
                f"routing observation ledger is unreadable: {ledger_paths[0]}"
            ) from error
        validate_contract(ledger, "task-ledger.schema.json")
        for field in ("lwar_id", "instance_id", "generation"):
            if ledger.get(field) != value[field]:
                raise ContractError(
                    f"routing observation ledger identity mismatch: {field}"
                )
        task_contract = ledger.get("task_contract") or {}
        if task_contract.get("registry_version") != value["registry_version"]:
            raise ContractError(
                "routing observation ledger identity mismatch: registry_version"
            )
        validation = ledger.get("validation")
        if not isinstance(validation, dict):
            raise ContractError(
                f"routing observation has no recorded validation: {value['task_id']}"
            )
        validate_contract(validation, "validation-decision.schema.json")
        if canonical_sha256(validation) != value["validation_sha256"]:
            raise ContractError(
                f"routing observation validation binding mismatch: {value['task_id']}"
            )
        if value["accepted"] != (
            validation["semantic_verdict"] == "accepted"
        ):
            raise ContractError(
                f"routing observation verdict binding mismatch: {value['task_id']}"
            )
        seen_ids.add(value["observation_id"])
        observations.append(value)
    observations.sort(key=lambda item: (item["observed_at"], item["observation_id"]))
    return observations


def current_generation_observations(
    observations: list[dict[str, Any]],
    eligible_identities: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    current = []
    for item in observations:
        validate_routing_observation(item)
        identity = eligible_identities.get(item["lwar_id"])
        if identity is None:
            continue
        if (
            item["instance_id"] == identity.get("instance_id")
            and item["generation"] == identity.get("generation")
        ):
            current.append(item)
    return current


def promotion_epoch_observations(
    observations: list[dict[str, Any]],
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return promotion rows that remain valid after alias-class resets."""
    validate_circuit_state(state)
    current = []
    for item in observations:
        validate_routing_observation(item)
        if item["route_mode"] == "recovery_shadow":
            continue
        key = circuit_key(item["task_class"], item["lwar_id"])
        reset_at = state["resets"].get(key, {}).get(
            "reset_at", EMPTY_STATE_TIME
        )
        if item["observed_at"] <= reset_at:
            continue
        current.append(item)
    return current


def wilson_interval(accepted: int, observations: int, z: float = 1.96) -> tuple[float, float]:
    if observations <= 0:
        return 0.0, 1.0
    rate = accepted / observations
    z2 = z * z
    denominator = 1 + z2 / observations
    center = (rate + z2 / (2 * observations)) / denominator
    margin = (
        z
        * math.sqrt(
            rate * (1 - rate) / observations
            + z2 / (4 * observations * observations)
        )
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)


def _with_confidence(
    stats: dict[str, dict[str, Any]], z: float
) -> dict[str, dict[str, Any]]:
    return {
        alias: {
            **item,
            "wilson_lower": wilson_interval(
                item["accepted"], item["observations"], z
            )[0],
            "wilson_upper": wilson_interval(
                item["accepted"], item["observations"], z
            )[1],
        }
        for alias, item in stats.items()
    }


def select_confidence_canary(
    profile: dict[str, Any],
    policy: dict[str, Any],
    online_observations: list[dict[str, Any]],
    state: dict[str, Any],
    task_class: str,
    eligible_lwar_ids: list[str],
    eligible_identities: dict[str, dict[str, Any]],
    *,
    shadow_execution: bool = False,
    shadow_lwar_id: str | None = None,
    recovery_shadow_lwar_id: str | None = None,
) -> dict[str, Any]:
    validate_routing_profile(profile)
    validate_canary_policy(policy)
    validate_circuit_state(state)
    for observation in online_observations:
        validate_routing_observation(observation)
        if observation["task_id"] in calibration_task_ids(profile):
            raise ContractError(
                f"online observation reuses calibration task: {observation['task_id']}"
            )
    eligible = sorted(set(eligible_lwar_ids), key=_lwar_number)
    if not eligible:
        raise ValueError("canary routing requires at least one eligible LWAR")
    eligible_set = set(eligible)
    shadow_modes = sum(
        (
            bool(shadow_execution),
            shadow_lwar_id is not None,
            recovery_shadow_lwar_id is not None,
        )
    )
    if shadow_modes > 1:
        raise ValueError(
            "choose one candidate, explicit, or recovery shadow mode"
        )
    if shadow_lwar_id is not None and shadow_lwar_id not in eligible_set:
        raise ValueError(f"explicit shadow LWAR is not eligible: {shadow_lwar_id}")
    if (
        recovery_shadow_lwar_id is not None
        and recovery_shadow_lwar_id not in eligible_set
    ):
        raise ValueError(
            "recovery shadow LWAR is not eligible: "
            f"{recovery_shadow_lwar_id}"
        )
    if set(eligible_identities) != eligible_set:
        raise ValueError("canary routing requires every eligible identity binding")
    online_observations = current_generation_observations(
        online_observations, eligible_identities
    )
    promotion_observations = promotion_epoch_observations(
        online_observations, state
    )
    incumbent, calibration_global = _global_quality_leader(profile, eligible_set)
    if incumbent is None:
        return {
            "selected_lwar_id": None,
            "incumbent_lwar_id": None,
            "candidate_lwar_id": None,
            "eligible_lwar_ids": eligible,
            "route_mode": "leader",
            "reason": "fallback_no_calibration_evidence",
            "class_stats": {},
            "global_stats": {},
            "confidence": {"balanced_accepted_ready": False},
        }

    predictive = select_predictive_lwar(profile, task_class, eligible)
    candidate = (
        predictive["selected_lwar_id"]
        if predictive["reason"] == "class_quality_qualified_lowest_tokens"
        else None
    )
    if candidate == incumbent:
        candidate = None

    class_rows = [
        item
        for item in promotion_observations
        if item["task_class"] == task_class
    ]
    class_stats = _with_confidence(
        _aggregate(class_rows, eligible_set), policy["confidence_z"]
    )
    global_stats = _with_confidence(
        _aggregate(promotion_observations, eligible_set),
        policy["confidence_z"],
    )
    profiled_eligible = set(calibration_global)
    minimum = policy["min_accepted_observations"]
    balanced_ready = bool(profiled_eligible) and all(
        class_stats.get(alias, {}).get("accepted", 0) >= minimum
        for alias in profiled_eligible
    )
    confidence = {
        "balanced_accepted_ready": balanced_ready,
        "min_accepted_observations": minimum,
        "candidate_wilson_lower": (
            class_stats.get(candidate, {}).get("wilson_lower")
            if candidate is not None
            else None
        ),
        "incumbent_wilson_lower": class_stats.get(incumbent, {}).get(
            "wilson_lower"
        ),
    }
    shadow_target = (
        shadow_lwar_id
        if shadow_lwar_id is not None
        else (candidate if shadow_execution else None)
    )
    if recovery_shadow_lwar_id is not None:
        recovery_key = circuit_key(task_class, recovery_shadow_lwar_id)
        recovery_circuit = state["circuits"].get(recovery_key)
        if recovery_circuit is None:
            raise ValueError(
                f"recovery shadow requires an open circuit: {recovery_key}"
            )
        if recovery_circuit["policy_sha256"] != canonical_sha256(policy):
            raise ValueError(
                "recovery shadow policy does not match open circuit: "
                f"{recovery_key}"
            )
        selected, route_mode, reason = (
            recovery_shadow_lwar_id,
            "recovery_shadow",
            "open_circuit_recovery_shadow",
        )
    elif (
        shadow_target is not None
        and circuit_key(task_class, shadow_target) in state["circuits"]
    ):
        selected, route_mode, reason = (
            incumbent,
            "circuit_open",
            "shadow_target_circuit_open",
        )
    elif shadow_target is not None:
        selected, route_mode, reason = (
            shadow_target,
            "shadow",
            (
                "explicit_shadow_target"
                if shadow_lwar_id is not None
                else (
                    "shadow_insufficient_accepted"
                    if not balanced_ready
                    else "shadow_confidence_not_qualified"
                )
            ),
        )
    elif candidate is None:
        selected, route_mode, reason = (
            incumbent,
            "leader",
            "no_nonleader_candidate",
        )
    elif circuit_key(task_class, candidate) in state["circuits"]:
        selected, route_mode, reason = incumbent, "circuit_open", "circuit_open"
    else:
        candidate_lower = class_stats.get(candidate, {}).get("wilson_lower", 0.0)
        incumbent_lower = class_stats.get(incumbent, {}).get("wilson_lower", 0.0)
        qualified = (
            balanced_ready
            and candidate_lower
            >= incumbent_lower - policy["max_quality_drop"]
        )
        if qualified:
            selected, route_mode, reason = (
                candidate,
                "live",
                "confidence_qualified_live",
            )
        else:
            selected, route_mode, reason = (
                incumbent,
                "leader",
                (
                    "shadow_insufficient_accepted"
                    if not balanced_ready
                    else "shadow_confidence_not_qualified"
                ),
            )
    return {
        "selected_lwar_id": selected,
        "incumbent_lwar_id": incumbent,
        "candidate_lwar_id": candidate,
        "eligible_lwar_ids": eligible,
        "route_mode": route_mode,
        "reason": reason,
        "class_stats": class_stats,
        "global_stats": global_stats,
        "confidence": confidence,
    }


def make_canary_routing_receipt(
    *,
    task_id: str,
    task_class: str,
    profile: dict[str, Any],
    policy: dict[str, Any],
    observations: list[dict[str, Any]],
    state: dict[str, Any],
    decision: dict[str, Any],
    selected_identity: dict[str, Any],
    decided_at: str | None = None,
) -> dict[str, Any]:
    if task_id in calibration_task_ids(profile):
        raise ValueError(f"held-out task is present in calibration profile: {task_id}")
    selected = decision.get("selected_lwar_id")
    incumbent = decision.get("incumbent_lwar_id")
    if not selected or not incumbent:
        raise ValueError("cannot create a canary receipt without a selected incumbent")
    binding = {
        "task_id": task_id,
        "task_class": task_class,
        "profile_sha256": canonical_sha256(profile),
        "policy_sha256": canonical_sha256(policy),
        "observations_sha256": canonical_sha256({"observations": observations}),
        "circuit_state_sha256": canonical_sha256(state),
        "selected_lwar_id": selected,
        "selected_instance_id": selected_identity["instance_id"],
        "selected_generation": selected_identity["generation"],
        "selected_registry_version": selected_identity["registry_version"],
        "incumbent_lwar_id": incumbent,
        "candidate_lwar_id": decision.get("candidate_lwar_id"),
        "eligible_lwar_ids": decision["eligible_lwar_ids"],
        "route_mode": decision["route_mode"],
        "reason": decision["reason"],
    }
    receipt = {
        "schema_version": CANARY_RECEIPT_SCHEMA_VERSION,
        "receipt_id": f"canary-routing-{canonical_sha256(binding)[:32]}",
        "decided_at": decided_at or utc_now(),
        **binding,
        "decision": {
            "policy": policy,
            "class_stats": decision["class_stats"],
            "global_stats": decision["global_stats"],
            "confidence": decision["confidence"],
        },
    }
    validate_canary_receipt(receipt)
    return receipt


def write_canary_routing_receipt(
    root: Path, receipt: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    validate_canary_receipt(receipt)
    path = root / "var" / "routing" / "receipts" / f"{receipt['task_id']}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"routing receipt is unreadable: {path}") from error
        validate_canary_receipt(existing)
        comparable = (
            "receipt_id",
            "task_id",
            "task_class",
            "profile_sha256",
            "policy_sha256",
            "observations_sha256",
            "circuit_state_sha256",
            "selected_lwar_id",
            "selected_instance_id",
            "selected_generation",
            "selected_registry_version",
            "incumbent_lwar_id",
            "candidate_lwar_id",
            "eligible_lwar_ids",
            "route_mode",
            "reason",
            "decision",
        )
        if any(existing[key] != receipt[key] for key in comparable):
            raise RuntimeError(f"conflicting routing receipt exists: {path}")
        return path, existing
    atomic_write_json(path, receipt)
    return path, receipt


def load_canary_receipt(root: Path, task_id: str) -> dict[str, Any]:
    path = root / "var" / "routing" / "receipts" / f"{task_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"canary routing receipt is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise ContractError("canary routing receipt must be a JSON object")
    validate_canary_receipt(value)
    return value


def make_routing_observation(
    receipt: dict[str, Any],
    validation: dict[str, Any],
    reported_tokens: int,
) -> dict[str, Any]:
    validate_canary_receipt(receipt)
    validate_contract(validation, "validation-decision.schema.json")
    verdict = validation["semantic_verdict"]
    if verdict not in {"accepted", "rejected"}:
        raise ValueError("routing observation requires accepted or rejected validation")
    if not isinstance(reported_tokens, int) or reported_tokens < 0:
        raise ValueError("reported routing tokens must be a nonnegative integer")
    binding = {
        "task_id": receipt["task_id"],
        "task_class": receipt["task_class"],
        "lwar_id": receipt["selected_lwar_id"],
        "instance_id": receipt["selected_instance_id"],
        "generation": receipt["selected_generation"],
        "registry_version": receipt["selected_registry_version"],
        "receipt_id": receipt["receipt_id"],
        "route_mode": receipt["route_mode"],
        "accepted": verdict == "accepted",
        "reported_tokens": reported_tokens,
        "validation_sha256": canonical_sha256(validation),
    }
    observation = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "observation_id": (
            f"routing-observation-{canonical_sha256(binding)[:32]}"
        ),
        "observed_at": validation["decided_at"],
        **binding,
    }
    validate_routing_observation(observation)
    return observation


def write_routing_observation(
    root: Path, observation: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    validate_routing_observation(observation)
    path = observation_path(root, observation["task_id"])
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"routing observation is unreadable: {path}") from error
        validate_routing_observation(existing)
        if existing != observation:
            raise RuntimeError(f"conflicting routing observation exists: {path}")
        return path, existing
    atomic_write_json(path, observation)
    return path, observation


def refresh_circuits(
    policy: dict[str, Any],
    observations: list[dict[str, Any]],
    state: dict[str, Any],
    *,
    opened_at: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    validate_canary_policy(policy)
    validate_circuit_state(state)
    for item in observations:
        validate_routing_observation(item)
    updated = deepcopy(state)
    events: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in observations:
        if item["route_mode"] not in {"shadow", "live"}:
            continue
        key = circuit_key(item["task_class"], item["lwar_id"])
        reset_at = updated["resets"].get(key, {}).get("reset_at", EMPTY_STATE_TIME)
        if item["observed_at"] <= reset_at:
            continue
        grouped.setdefault(key, []).append(item)
    for key in sorted(grouped):
        if key in updated["circuits"]:
            continue
        rows = sorted(
            grouped[key], key=lambda item: (item["observed_at"], item["observation_id"])
        )
        reason = None
        trigger = None
        if policy["trip_on_rejection"]:
            reset_at = updated["resets"].get(key, {}).get(
                "reset_at", EMPTY_STATE_TIME
            )
            trigger = next(
                (
                    item
                    for item in rows
                    if not item["accepted"]
                    and (
                        item["route_mode"] == "live"
                        or (
                            item["observed_at"] > reset_at
                            and reset_at != EMPTY_STATE_TIME
                        )
                    )
                ),
                None,
            )
            if trigger is not None:
                reason = "candidate_rejected"
        if reason is None and len(rows) >= 2 * policy["drift_window"]:
            width = policy["drift_window"]
            prior = rows[-2 * width : -width]
            recent = rows[-width:]
            prior_accepted = sum(item["accepted"] for item in prior)
            recent_accepted = sum(item["accepted"] for item in recent)
            prior_lower, _ = wilson_interval(
                prior_accepted, width, policy["confidence_z"]
            )
            _, recent_upper = wilson_interval(
                recent_accepted, width, policy["confidence_z"]
            )
            if (
                recent_upper + policy["max_drift_drop"]
                < prior_lower
            ):
                reason = "confidence_drift"
                trigger = recent[-1]
        if reason is None or trigger is None:
            continue
        entry = {
            "task_class": trigger["task_class"],
            "lwar_id": trigger["lwar_id"],
            "status": "open",
            "opened_at": opened_at or utc_now(),
            "reason": reason,
            "trigger_observation_id": trigger["observation_id"],
            "policy_sha256": canonical_sha256(policy),
        }
        updated["circuits"][key] = entry
        events.append({"key": key, **entry})
    if events:
        updated["updated_at"] = opened_at or utc_now()
    validate_circuit_state(updated)
    return updated, events


def reset_circuit(
    state: dict[str, Any],
    *,
    task_class: str,
    lwar_id: str,
    reason: str,
    decided_by: str,
    reset_at: str | None = None,
) -> dict[str, Any]:
    validate_circuit_state(state)
    if not reason.strip():
        raise ValueError("routing circuit reset requires a reason")
    if not decided_by.strip():
        raise ValueError("routing circuit reset requires an OA identity")
    key = circuit_key(task_class, lwar_id)
    if key not in state["circuits"]:
        raise ValueError(f"routing circuit is not open: {key}")
    updated = deepcopy(state)
    del updated["circuits"][key]
    at = reset_at or utc_now()
    updated["resets"][key] = {
        "task_class": task_class,
        "lwar_id": lwar_id,
        "reset_at": at,
        "reason": reason.strip(),
        "decided_by": decided_by.strip(),
    }
    updated["updated_at"] = at
    validate_circuit_state(updated)
    return updated
