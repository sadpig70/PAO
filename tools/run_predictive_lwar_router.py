#!/usr/bin/env python3
"""Prepare and analyze a calibration-only, held-out PAO routing experiment."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
RUNTIME_HOME = REPO / ".agents" / "skills" / "pao-lwar"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(RUNTIME_HOME) not in sys.path:
    sys.path.insert(0, str(RUNTIME_HOME))

from pao_runtime.common import utc_now  # noqa: E402
from pao_runtime.predictive_routing import (  # noqa: E402
    calibration_task_ids,
    canonical_sha256,
    compile_routing_profile,
    make_routing_receipt,
    select_predictive_lwar,
    write_routing_receipt,
)
from tools.run_heterogeneous_lwar_ab import (  # noqa: E402
    ADAPTERS,
    load_task_suite,
    write_json,
)


PRIOR_CLASSES = {
    "T1": "constraint_ordering",
    "T2": "code_review",
    "T3": "bounded_optimization",
}


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def experiment_observations(
    experiment: dict[str, Any],
    *,
    source_prefix: str,
    classes: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    result = []
    for record in experiment["records"]:
        task_name = record["task"]
        task_class = record.get("task_class") or (classes or {}).get(task_name)
        tokens = record["provider"].get("reported_tokens")
        if not task_class:
            raise ValueError(f"missing task class for {task_name}")
        if not isinstance(tokens, int):
            raise ValueError(f"missing reported token telemetry for {record['alias']} / {task_name}")
        result.append(
            {
                "task_id": f"task-{source_prefix}-{task_name.lower()}",
                "task_class": task_class,
                "lwar_id": record["alias"],
                "accepted": record["grade"]["score"] == 1,
                "reported_tokens": tokens,
            }
        )
    return result


def prepare(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"output must be absent or empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    prior = _read_json(args.prior_experiment.resolve())
    calibration = _read_json(args.calibration_experiment.resolve())
    heldout = load_task_suite(args.heldout_suite.resolve())
    observations = experiment_observations(
        prior,
        source_prefix="calibration-prior",
        classes=PRIOR_CLASSES,
    )
    observations += experiment_observations(
        calibration,
        source_prefix="calibration-new",
    )
    profile = compile_routing_profile(
        observations,
        profile_id=args.profile_id,
        source_experiment_ids=[
            str(args.prior_experiment.resolve()),
            str(args.calibration_experiment.resolve()),
        ],
        min_observations=args.min_observations,
        max_quality_drop=args.max_quality_drop,
    )
    profile_path = output / "routing-profile.json"
    write_json(profile_path, profile)

    aliases = list(ADAPTERS)
    receipts = []
    for task_name, task in heldout.items():
        task_id = f"task-heldout-{task_name.lower()}"
        decision = select_predictive_lwar(profile, task["task_class"], aliases)
        receipt = make_routing_receipt(
            task_id=task_id,
            task_class=task["task_class"],
            profile=profile,
            decision=decision,
        )
        receipt_path, stored = write_routing_receipt(output, receipt)
        receipts.append(
            {
                "task": task_name,
                "task_id": task_id,
                "task_class": task["task_class"],
                "selected_lwar_id": stored["selected_lwar_id"],
                "reason": stored["reason"],
                "path": receipt_path.relative_to(output).as_posix(),
                "sha256": _sha256_file(receipt_path),
                "decided_at": stored["decided_at"],
            }
        )
    heldout_ids = {item["task_id"] for item in receipts}
    overlap = heldout_ids & calibration_task_ids(profile)
    if overlap:
        raise RuntimeError(f"calibration and held-out task IDs overlap: {sorted(overlap)}")
    manifest = {
        "schema_version": "pao.predictive-precommit.v1",
        "completed_at": utc_now(),
        "profile": profile_path.name,
        "profile_sha256": canonical_sha256(profile),
        "calibration_task_ids": sorted(calibration_task_ids(profile)),
        "heldout_task_ids": sorted(heldout_ids),
        "receipts": receipts,
    }
    write_json(output / "precommit.json", manifest)
    print(
        json.dumps(
            {
                "event": "predictive_routes_precommitted",
                "output": str(output),
                "calibration_observations": len(observations),
                "heldout_routes": len(receipts),
                "profile_sha256": manifest["profile_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


def _score(records: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = [record["provider"].get("reported_tokens") for record in records]
    return {
        "accepted": sum(record["grade"]["score"] for record in records),
        "total": len(records),
        "reported_tokens": sum(tokens) if all(isinstance(value, int) for value in tokens) else None,
        "telemetry_complete": all(
            bool(record["provider"]["metrics"].get("telemetry_complete"))
            for record in records
        ),
        "per_task": {
            record["task"]: {
                "alias": record["alias"],
                "accepted": record["grade"]["score"] == 1,
                "reported_tokens": record["provider"].get("reported_tokens"),
            }
            for record in records
        },
    }


def _lifecycle(root: Path) -> dict[str, Any]:
    live = {}
    for name in ("incoming", "claimed", "outgoing", "executions"):
        live[name] = len(list((root / "mailbox").glob(f"LWAR*/{name}/*.json")))
    return {
        "live": live,
        "archived_results": len(
            list((root / "mailbox").glob("LWAR*/archive/results/*.json"))
        ),
    }


def analyze(args: argparse.Namespace) -> int:
    output = args.output.resolve()
    profile = _read_json(output / "routing-profile.json")
    precommit = _read_json(output / "precommit.json")
    heldout = _read_json(args.heldout_experiment.resolve())
    records = heldout["records"]
    by_key = {(record["task"], record["alias"]): record for record in records}
    aliases = list(ADAPTERS)
    task_names = [item["task"] for item in precommit["receipts"]]
    expected_keys = {(task, alias) for task in task_names for alias in aliases}
    if set(by_key) != expected_keys:
        missing = sorted(expected_keys - set(by_key))
        extra = sorted(set(by_key) - expected_keys)
        raise ValueError(f"held-out matrix mismatch; missing={missing}, extra={extra}")

    earliest_call = min(_iso(record["provider"]["started_at"]) for record in records)
    all_precommitted = (
        _iso(precommit["completed_at"]) < earliest_call
        and all(_iso(item["decided_at"]) < earliest_call for item in precommit["receipts"])
    )
    routed_records = [
        by_key[(item["task"], item["selected_lwar_id"])]
        for item in precommit["receipts"]
    ]
    global_decision = select_predictive_lwar(profile, "unknown_heldout_class", aliases)
    baseline_alias = global_decision["selected_lwar_id"]
    baseline_records = [by_key[(task, baseline_alias)] for task in task_names]
    router_score = _score(routed_records)
    baseline_score = _score(baseline_records)

    oracle_records = []
    for task in task_names:
        correct = [
            by_key[(task, alias)]
            for alias in aliases
            if by_key[(task, alias)]["grade"]["score"] == 1
            and isinstance(by_key[(task, alias)]["provider"].get("reported_tokens"), int)
        ]
        if correct:
            oracle_records.append(
                min(correct, key=lambda record: record["provider"]["reported_tokens"])
            )
    oracle_score = _score(oracle_records)
    router_savings = None
    if baseline_score["reported_tokens"] and router_score["reported_tokens"] is not None:
        router_savings = round(
            100
            * (baseline_score["reported_tokens"] - router_score["reported_tokens"])
            / baseline_score["reported_tokens"],
            1,
        )
    oracle_gap = None
    if router_score["reported_tokens"] and oracle_score["reported_tokens"] is not None:
        oracle_gap = round(
            100
            * (router_score["reported_tokens"] - oracle_score["reported_tokens"])
            / router_score["reported_tokens"],
            1,
        )
    lifecycle = _lifecycle(args.heldout_experiment.resolve().parent)
    audit_healthy = heldout["audit_health"].get("status") == "healthy"
    lifecycle_clean = all(value == 0 for value in lifecycle["live"].values())
    full_telemetry_count = sum(
        bool(record["provider"]["metrics"].get("telemetry_complete"))
        for record in records
    )
    full_telemetry_complete = full_telemetry_count == len(records)
    success = bool(
        all_precommitted
        and router_score["accepted"] >= baseline_score["accepted"]
        and router_score["reported_tokens"] is not None
        and baseline_score["reported_tokens"] is not None
        and router_score["reported_tokens"] < baseline_score["reported_tokens"]
        and router_score["telemetry_complete"]
        and baseline_score["telemetry_complete"]
        and full_telemetry_complete
        and audit_healthy
        and lifecycle_clean
        and len(heldout.get("shutdowns", [])) == len(aliases)
    )
    verdict = (
        "heldout_predictive_quality_preserved_token_reduction_validated"
        if success
        else "heldout_predictive_routing_not_validated"
    )
    result = {
        "schema_version": "pao.predictive-router-experiment.v1",
        "verdict": verdict,
        "preregistered_success": success,
        "preexecution_binding": {
            "all_routes_precommitted": all_precommitted,
            "precommit_completed_at": precommit["completed_at"],
            "earliest_provider_call_at": earliest_call.isoformat(),
            "profile_sha256": precommit["profile_sha256"],
        },
        "calibration": {
            "observations": len(profile["observations"]),
            "tasks": len(calibration_task_ids(profile)),
            "best_single_lwar": baseline_alias,
            "policy": profile["policy"],
        },
        "heldout": {
            "tasks": len(task_names),
            "full_panel_provider_calls": len(records),
            "router": router_score,
            "baseline": {**baseline_score, "alias": baseline_alias},
            "posthoc_oracle": oracle_score,
            "router_token_savings_percent": router_savings,
            "router_to_oracle_token_gap_percent": oracle_gap,
            "route_receipts": precommit["receipts"],
            "shadow_evaluation_tokens_excluded_from_policy_cost": True,
        },
        "integrity": {
            "audit_healthy": audit_healthy,
            "lifecycle": lifecycle,
            "shutdowns": len(heldout.get("shutdowns", [])),
            "telemetry_complete": full_telemetry_complete,
            "telemetry_complete_calls": full_telemetry_count,
            "telemetry_total_calls": len(records),
        },
        "claim_scope": (
            "empirical_heldout_result_for_nine_preregistered_tasks;"
            "not_a_population_level_statistical_guarantee;"
            "reported_tokens_are_provider_native_and_not_cost_equivalent"
        ),
    }
    write_json(output / "experiment.json", result)
    router_failures = [
        task for task, item in router_score["per_task"].items() if not item["accepted"]
    ]
    baseline_failures = [
        task for task, item in baseline_score["per_task"].items() if not item["accepted"]
    ]
    lines = [
        "# PAO Held-Out Predictive LWAR Router Report",
        "",
        f"- Verdict: `{verdict}`",
        f"- Routes committed before provider execution: **{all_precommitted}**",
        f"- Calibration: **{len(calibration_task_ids(profile))} tasks / {len(profile['observations'])} observations**",
        f"- Held-out: **{len(task_names)} tasks / {len(records)} full-panel calls**",
        f"- Calibration-selected best single: **{baseline_alias}**",
        f"- Router quality: **{router_score['accepted']}/{router_score['total']}**",
        f"- Baseline quality: **{baseline_score['accepted']}/{baseline_score['total']}**",
        f"- Router failures: **{router_failures or 'none'}**",
        f"- Baseline failures: **{baseline_failures or 'none'}**",
        f"- Router reported tokens: **{router_score['reported_tokens']}**",
        f"- Baseline reported tokens: **{baseline_score['reported_tokens']}**",
        f"- Router token reduction: **{router_savings}%**",
        f"- Post-hoc oracle tokens: **{oracle_score['reported_tokens']}**",
        f"- Remaining router-to-oracle token gap: **{oracle_gap}%**",
        f"- Audit: **{'healthy' if audit_healthy else 'not healthy'}**",
        f"- Full-panel token telemetry: **{full_telemetry_count}/{len(records)}**",
        f"- Residual live work: **{sum(lifecycle['live'].values())}**",
        "",
        "The policy cost includes only each precommitted selected call. The other",
        "full-panel calls are shadow evaluation overhead and are excluded from",
        "the routed-policy token total.",
        "",
        "This is an empirical result for nine preregistered held-out tasks, not a",
        "population-level guarantee. Reported-token fields are normalized from",
        "provider-native telemetry and are not equivalent to monetary cost.",
        "",
        "Machine-readable evidence: `experiment.json`",
    ]
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "event": "predictive_router_analyzed",
                "verdict": verdict,
                "success": success,
                "router_accepted": router_score["accepted"],
                "baseline_accepted": baseline_score["accepted"],
                "router_tokens": router_score["reported_tokens"],
                "baseline_tokens": baseline_score["reported_tokens"],
                "token_savings_percent": router_savings,
                "report": str(output / "report.md"),
            },
            sort_keys=True,
        )
    )
    return 0 if success else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--prior-experiment", type=Path, required=True)
    prepare_parser.add_argument("--calibration-experiment", type=Path, required=True)
    prepare_parser.add_argument("--heldout-suite", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument(
        "--profile-id",
        default="routing-profile-heldout-v1",
    )
    prepare_parser.add_argument("--min-observations", type=int, default=5)
    prepare_parser.add_argument("--max-quality-drop", type=float, default=0.0)
    prepare_parser.set_defaults(handler=prepare)

    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("--heldout-experiment", type=Path, required=True)
    analyze_parser.add_argument("--output", type=Path, required=True)
    analyze_parser.set_defaults(handler=analyze)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
