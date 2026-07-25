#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
RUNTIME_HOME = REPO / ".agents" / "skills" / "pao-lwar"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(RUNTIME_HOME) not in sys.path:
    sys.path.insert(0, str(RUNTIME_HOME))

from pao_runtime.canary_routing import (
    current_generation_observations,
    empty_circuit_state,
    load_canary_policy,
    load_circuit_state,
    load_routing_observations,
    select_confidence_canary,
)
from pao_runtime.predictive_routing import (
    canonical_sha256,
    load_routing_profile,
)


def load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def heldout_observations(
    experiment: dict[str, Any], *, source: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    observations = []
    skipped = []
    for record in experiment.get("records", []):
        alias = record["alias"]
        task = record["task"].lower()
        provider = record.get("provider") or {}
        tokens = provider.get("reported_tokens")
        if not isinstance(tokens, int) or tokens < 0:
            skipped.append(
                {"alias": alias, "task": record["task"], "reason": "missing_tokens"}
            )
            continue
        binding = {
            "source": source,
            "task": record["task"],
            "alias": alias,
            "grade": record.get("grade"),
        }
        digest = canonical_sha256(binding)
        alias_number = int(alias[4:])
        observation_binding = {
            "task_id": f"task-{source}-{task}-{alias.lower()}",
            "task_class": record["task_class"],
            "lwar_id": alias,
            "instance_id": f"lwar-instance-{alias_number:032x}",
            "generation": 1,
            "registry_version": 1,
            "receipt_id": f"canary-routing-{digest[32:64]}",
            "route_mode": "shadow",
            "accepted": record.get("grade", {}).get("score") == 1,
            "reported_tokens": tokens,
            "validation_sha256": canonical_sha256(
                {"grade": record.get("grade")}
            ),
        }
        observations.append(
            {
                "schema_version": "pao.routing-observation.v1",
                "observation_id": (
                    "routing-observation-"
                    + canonical_sha256(observation_binding)[:32]
                ),
                "observed_at": provider["started_at"],
                **observation_binding,
            }
        )
    observations.sort(key=lambda item: (item["observed_at"], item["observation_id"]))
    return observations, skipped


def analyze(
    profile: dict[str, Any],
    policy: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    state: dict[str, Any] | None = None,
    identities: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    aliases = sorted(
        {item["lwar_id"] for item in profile["observations"]},
        key=lambda alias: int(alias[4:]),
    )
    classes = sorted(
        {item["task_class"] for item in profile["observations"]}
        | {item["task_class"] for item in observations}
    )
    decisions = {}
    if identities is None:
        identities = {
            alias: {
                "instance_id": f"lwar-instance-{int(alias[4:]):032x}",
                "generation": 1,
                "registry_version": 1,
            }
            for alias in aliases
        }
    observations = current_generation_observations(observations, identities)
    circuit_state = state or empty_circuit_state()
    for task_class in classes:
        decisions[task_class] = select_confidence_canary(
            profile,
            policy,
            observations,
            circuit_state,
            task_class,
            aliases,
            identities,
        )
    promoted = [
        task_class
        for task_class, decision in decisions.items()
        if decision["route_mode"] == "live"
        and decision["candidate_lwar_id"] is not None
    ]
    return {
        "schema_version": "pao.canary-evidence-report.v1",
        "profile_sha256": canonical_sha256(profile),
        "policy_sha256": canonical_sha256(policy),
        "observations_sha256": canonical_sha256(
            {"observations": observations}
        ),
        "aliases": aliases,
        "classes": classes,
        "online_observations": len(observations),
        "decisions": decisions,
        "promoted_classes": promoted,
        "verdict": (
            "current_evidence_promotes_nonleader"
            if promoted
            else "current_evidence_remains_shadow_only"
        ),
    }


def markdown_report(report: dict[str, Any], skipped: list[dict[str, str]]) -> str:
    lines = [
        "# Canary Router Current-Evidence Report",
        "",
        f"- Verdict: `{report['verdict']}`",
        f"- Online observations with token telemetry: {report['online_observations']}",
        f"- Skipped missing-token records: {len(skipped)}",
        f"- Profile SHA-256: `{report['profile_sha256']}`",
        f"- Policy SHA-256: `{report['policy_sha256']}`",
        "",
        "| Task class | Incumbent | Candidate | Actual | Mode | Reason |",
        "|---|---|---|---|---|---|",
    ]
    for task_class, decision in sorted(report["decisions"].items()):
        lines.append(
            "| "
            + " | ".join(
                [
                    task_class,
                    str(decision["incumbent_lwar_id"]),
                    str(decision["candidate_lwar_id"]),
                    str(decision["selected_lwar_id"]),
                    decision["route_mode"],
                    decision["reason"],
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "No non-incumbent route may leave shadow mode unless every profiled",
            "eligible alias has ten accepted observations in the class and the",
            "candidate Wilson lower bound is non-inferior to the incumbent.",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate the canary promotion gate against held-out evidence"
    )
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--experiment", type=Path)
    source.add_argument(
        "--bus-root",
        type=Path,
        help="live PAO bus containing bound canary observations",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    profile = load_routing_profile(args.profile)
    policy = load_canary_policy(args.policy)
    if args.bus_root:
        bus_root = args.bus_root.resolve()
        registry = load_json_object(
            bus_root / "var" / "registry" / "lwar_registry.json"
        )
        aliases = sorted(
            {item["lwar_id"] for item in profile["observations"]},
            key=lambda alias: int(alias[4:]),
        )
        identities = {
            alias: {
                "instance_id": registry["slots"][alias]["instance_id"],
                "generation": registry["slots"][alias]["generation"],
                "registry_version": registry["registry_version"],
            }
            for alias in aliases
        }
        observations = load_routing_observations(bus_root)
        experiment_path = bus_root / "experiment.json"
        skipped = []
        if experiment_path.is_file():
            experiment = load_json_object(experiment_path)
            skipped = [
                {
                    "alias": record["alias"],
                    "task": record["task"],
                    "reason": "missing_tokens",
                }
                for record in experiment.get("records", [])
                if not (record.get("validation") or {}).get(
                    "routing_observation"
                )
            ]
        report = analyze(
            profile,
            policy,
            observations,
            state=load_circuit_state(bus_root),
            identities=identities,
        )
        report["source"] = "verified_bus"
    else:
        experiment = load_json_object(args.experiment)
        observations, skipped = heldout_observations(experiment, source="heldout")
        report = analyze(profile, policy, observations)
        report["source"] = "experiment_replay"
    report["skipped"] = skipped
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "canary-evidence.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (args.output / "report.md").write_text(
        markdown_report(report, skipped),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "event": "canary_evidence_analyzed",
                "output": str(args.output),
                "verdict": report["verdict"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
