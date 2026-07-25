from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from typing import Any

from .common import atomic_write_json, utc_now
from .contracts import ContractError, validate_contract


PROFILE_SCHEMA_VERSION = "pao.routing-profile.v1"
RECEIPT_SCHEMA_VERSION = "pao.routing-receipt.v1"


def _lwar_number(lwar_id: str) -> int:
    return int(lwar_id[len("LWAR"):])


def canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compile_routing_profile(
    observations: list[dict[str, Any]],
    *,
    profile_id: str,
    source_experiment_ids: list[str],
    min_observations: int = 5,
    max_quality_drop: float = 0.0,
    created_at: str | None = None,
) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in observations:
        item = {
            "task_id": raw["task_id"],
            "task_class": raw["task_class"],
            "lwar_id": raw["lwar_id"],
            "accepted": raw["accepted"],
            "reported_tokens": raw["reported_tokens"],
        }
        key = (item["task_id"], item["lwar_id"])
        if key in seen:
            raise ValueError(f"duplicate calibration observation: {key[0]} / {key[1]}")
        seen.add(key)
        normalized.append(item)
    normalized.sort(key=lambda item: (item["task_id"], _lwar_number(item["lwar_id"])))
    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": profile_id,
        "created_at": created_at or utc_now(),
        "source_experiment_ids": sorted(set(source_experiment_ids)),
        "policy": {
            "min_observations": min_observations,
            "max_quality_drop": max_quality_drop,
        },
        "observations": normalized,
    }
    validate_routing_profile(profile)
    return profile


def validate_routing_profile(profile: dict[str, Any]) -> None:
    validate_contract(profile, "routing-profile.schema.json")
    if profile["policy"]["max_quality_drop"] > 1:
        raise ContractError("routing max_quality_drop must be between 0 and 1")
    seen: set[tuple[str, str]] = set()
    class_by_task: dict[str, str] = {}
    for item in profile["observations"]:
        key = (item["task_id"], item["lwar_id"])
        if key in seen:
            raise ContractError(f"duplicate routing observation: {key[0]} / {key[1]}")
        seen.add(key)
        previous = class_by_task.setdefault(item["task_id"], item["task_class"])
        if previous != item["task_class"]:
            raise ContractError(f"task has multiple routing classes: {item['task_id']}")


def load_routing_profile(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"routing profile is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise ContractError("routing profile must be a JSON object")
    validate_routing_profile(value)
    return value


def calibration_task_ids(profile: dict[str, Any]) -> set[str]:
    return {item["task_id"] for item in profile["observations"]}


def _aggregate(
    observations: list[dict[str, Any]],
    eligible_lwar_ids: set[str],
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, int]] = defaultdict(
        lambda: {"observations": 0, "accepted": 0, "reported_tokens": 0}
    )
    for item in observations:
        alias = item["lwar_id"]
        if alias not in eligible_lwar_ids:
            continue
        bucket = buckets[alias]
        bucket["observations"] += 1
        bucket["accepted"] += int(item["accepted"])
        bucket["reported_tokens"] += item["reported_tokens"]
    result: dict[str, dict[str, Any]] = {}
    for alias, bucket in buckets.items():
        count = bucket["observations"]
        result[alias] = {
            **bucket,
            "acceptance_rate": bucket["accepted"] / count,
            "mean_reported_tokens": bucket["reported_tokens"] / count,
        }
    return result


def _quality_rank(item: tuple[str, dict[str, Any]]) -> tuple[Any, ...]:
    alias, stats = item
    return (
        -Fraction(stats["accepted"], stats["observations"]),
        -stats["observations"],
        stats["mean_reported_tokens"],
        _lwar_number(alias),
    )


def _global_quality_leader(
    profile: dict[str, Any],
    eligible_lwar_ids: set[str],
) -> tuple[str | None, dict[str, dict[str, Any]]]:
    stats = _aggregate(profile["observations"], eligible_lwar_ids)
    if not stats:
        return None, stats
    return min(stats.items(), key=_quality_rank)[0], stats


def select_predictive_lwar(
    profile: dict[str, Any],
    task_class: str,
    eligible_lwar_ids: list[str],
) -> dict[str, Any]:
    validate_routing_profile(profile)
    eligible = sorted(set(eligible_lwar_ids), key=_lwar_number)
    if not eligible:
        raise ValueError("predictive routing requires at least one eligible LWAR")
    eligible_set = set(eligible)
    leader, global_stats = _global_quality_leader(profile, eligible_set)
    if leader is None:
        return {
            "selected_lwar_id": None,
            "reason": "fallback_no_calibration_evidence",
            "eligible_lwar_ids": eligible,
            "class_stats": {},
            "global_stats": {},
        }

    class_observations = [
        item for item in profile["observations"] if item["task_class"] == task_class
    ]
    class_stats = _aggregate(class_observations, eligible_set)
    minimum = profile["policy"]["min_observations"]
    profiled_eligible = set(global_stats)
    incomplete_support = any(
        class_stats.get(alias, {}).get("observations", 0) < minimum
        for alias in profiled_eligible
    )
    supported = {
        alias: stats
        for alias, stats in class_stats.items()
        if stats["observations"] >= minimum
    }
    if not class_observations:
        selected, reason = leader, "fallback_unknown_class"
    elif incomplete_support or not supported:
        selected, reason = leader, "fallback_insufficient_support"
    else:
        best_rate = max(
            Fraction(stats["accepted"], stats["observations"])
            for stats in supported.values()
        )
        tolerance = profile["policy"]["max_quality_drop"]
        qualified = [
            (alias, stats)
            for alias, stats in supported.items()
            if float(best_rate - Fraction(stats["accepted"], stats["observations"]))
            <= tolerance
        ]
        selected, _ = min(
            qualified,
            key=lambda item: (item[1]["mean_reported_tokens"], _lwar_number(item[0])),
        )
        reason = "class_quality_qualified_lowest_tokens"
    return {
        "selected_lwar_id": selected,
        "reason": reason,
        "eligible_lwar_ids": eligible,
        "class_stats": class_stats,
        "global_stats": global_stats,
    }


def make_routing_receipt(
    *,
    task_id: str,
    task_class: str,
    profile: dict[str, Any],
    decision: dict[str, Any],
    decided_at: str | None = None,
) -> dict[str, Any]:
    if task_id in calibration_task_ids(profile):
        raise ValueError(f"held-out task is present in calibration profile: {task_id}")
    selected = decision.get("selected_lwar_id")
    if not selected:
        raise ValueError("cannot create a receipt without a selected LWAR")
    binding = {
        "task_id": task_id,
        "task_class": task_class,
        "profile_sha256": canonical_sha256(profile),
        "selected_lwar_id": selected,
        "eligible_lwar_ids": decision["eligible_lwar_ids"],
        "reason": decision["reason"],
    }
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "receipt_id": f"routing-{canonical_sha256(binding)[:32]}",
        "decided_at": decided_at or utc_now(),
        **binding,
        "decision": {
            "policy": profile["policy"],
            "class_stats": decision["class_stats"],
            "global_stats": decision["global_stats"],
        },
    }
    validate_contract(receipt, "routing-receipt.schema.json")
    return receipt


def write_routing_receipt(root: Path, receipt: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    validate_contract(receipt, "routing-receipt.schema.json")
    path = root / "var" / "routing" / "receipts" / f"{receipt['task_id']}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"routing receipt is unreadable: {path}") from error
        validate_contract(existing, "routing-receipt.schema.json")
        comparable = ("receipt_id", "task_id", "task_class", "profile_sha256",
                      "selected_lwar_id", "eligible_lwar_ids", "reason", "decision")
        if any(existing[key] != receipt[key] for key in comparable):
            raise RuntimeError(f"conflicting routing receipt exists: {path}")
        return path, existing
    atomic_write_json(path, receipt)
    return path, receipt
