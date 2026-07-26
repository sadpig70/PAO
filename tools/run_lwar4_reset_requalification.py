#!/usr/bin/env python3
"""Run the preregistered LWAR4 reset and requalification campaign."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
LWAR_BUNDLE = REPO / ".agents" / "skills" / "pao-lwar"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(LWAR_BUNDLE) not in sys.path:
    sys.path.insert(0, str(LWAR_BUNDLE))

from pao_runtime.canary_routing import (
    current_generation_observations,
    load_canary_policy,
    load_circuit_state,
    load_routing_observations,
    select_confidence_canary,
)
from pao_runtime.predictive_routing import (
    canonical_sha256,
    load_routing_profile,
)
from tools.build_lwar4_reset_requalification_suite import RESET_REASON
from tools.run_heterogeneous_lwar_ab import (
    extract_json_object,
    reported_tokens,
    run_codex,
    run_opencode,
)

LWAR_SCRIPT = LWAR_BUNDLE / "scripts" / "lwar.py"
ADP_SCRIPT = LWAR_BUNDLE / "scripts" / "adp_watch.py"
OA_SCRIPT = REPO / ".agents" / "skills" / "pao-oa" / "scripts" / "oa.py"
OA_ID = "oa-lwar4-reset-requalification-01"
DEFAULT_EVIDENCE_PATH = (
    REPO / "benchmarks" / "lwar4-reset-requalification-evidence-v1.json"
)
CAMPAIGN_ID = "reset-v1"
RUNNERS: dict[str, Callable[[str, Path], dict[str, Any]]] = {
    "LWAR1": run_codex,
    "LWAR4": run_opencode,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_stdout_json(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(completed.stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError(
        f"command emitted no JSON object: {completed.stderr[-1000:]}"
    )


def run_pao(
    script: Path,
    *args: str,
    expected: int = 0,
    timeout: int = 90,
) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PAO_OA_ID": OA_ID},
        check=False,
        timeout=timeout,
    )
    if completed.returncode != expected:
        raise RuntimeError(
            f"{script.name} returned {completed.returncode}, expected "
            f"{expected}: {completed.stderr[-1000:]}{completed.stdout[-1000:]}"
        )
    return parse_stdout_json(completed)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def grade(task: dict[str, Any], answer: str) -> dict[str, Any]:
    try:
        parsed = extract_json_object(answer)
    except Exception as error:
        return {
            "score": 0,
            "valid_json": False,
            "reason": f"invalid_json:{error}",
        }
    expected = task["expected"]["answer"]
    answer_value = parsed.get("answer")
    alphabet_valid = (
        isinstance(answer_value, str)
        and len(answer_value) == len(expected)
        and sorted(answer_value) == sorted(expected)
    )
    exact = alphabet_valid and answer_value == expected
    return {
        "score": 1 if exact else 0,
        "valid_json": True,
        "alphabet_valid": alphabet_valid,
        "reason": "exact_match" if exact else "objective_mismatch",
        "parsed": parsed,
    }


def trusted_identities(root: Path) -> dict[str, str]:
    experiment = load_json(root / "experiment.json")
    identities = {}
    for event in experiment.get("shutdowns", []):
        message = event.get("message") or {}
        alias = message.get("lwar_id")
        path = event.get("identity_file")
        if alias in RUNNERS and isinstance(path, str):
            identities[alias] = path
    if set(identities) != set(RUNNERS):
        raise RuntimeError("trusted handoff identities are incomplete")
    status = run_pao(OA_SCRIPT, "status", "--root", str(root))
    roster = {item["lwar_id"]: item for item in status["lwars"]}
    for alias, path_text in identities.items():
        path = Path(path_text)
        identity = load_json(path)
        slot = roster.get(alias)
        if slot is None:
            raise RuntimeError(f"trusted identity slot is absent: {alias}")
        for field in ("lwar_id", "instance_id", "generation"):
            if identity[field] != slot[field]:
                raise RuntimeError(
                    f"trusted identity mismatch for {alias}: {field}"
                )
    return identities


def activate(root: Path, identity_file: str) -> dict[str, Any]:
    event = run_pao(
        ADP_SCRIPT,
        "--identity-file",
        identity_file,
        "--interval",
        "0.01",
        "--timeout",
        "0.2",
        "--lease-seconds",
        "300",
        "--root",
        str(root),
        expected=10,
    )
    if event.get("event") != "idle_timeout":
        raise RuntimeError(f"identity activation did not idle: {event}")
    return event


def task_id(alias: str, task_name: str) -> str:
    return f"task-{CAMPAIGN_ID}-{alias.lower()}-{task_name.lower()}"


def workflow_id(phase: str) -> str:
    return f"workflow-{CAMPAIGN_ID}-{phase.replace('_', '-')}"


def publish(
    *,
    root: Path,
    campaign_dir: Path,
    work_dir: Path,
    profile: Path,
    policy: Path,
    alias: str | None,
    task_name: str,
    task: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    selected_alias = alias or "AUTO"
    draft = {
        "task_id": task_id(selected_alias, task_name),
        "workflow_id": workflow_id(task["phase"]),
        "goal": f"Solve sealed ordering task {task_name}",
        "instructions": task["prompt"],
        "completion_criteria": [
            "Return one JSON object",
            "Use every required letter exactly once with no spaces",
            "Match all ordering constraints",
        ],
        "cwd": str(work_dir),
        "timeout_s": 240,
        "permissions": {"read": [], "write": [], "network": False},
        "max_retries": 0,
        "priority": 5,
    }
    draft_path = campaign_dir / "drafts" / f"{selected_alias}-{task_name}.json"
    write_json(draft_path, draft)
    args = [
        "send",
        "--auto",
        "--require-capability",
        "blind-evaluation",
        "--routing-profile",
        str(profile),
        "--routing-class",
        "constraint_ordering",
        "--canary-policy",
        str(policy),
    ]
    if mode == "shadow":
        if alias is None:
            raise RuntimeError("shadow publication requires an alias")
        args += ["--routing-shadow-lwar-id", alias]
    elif mode == "recovery_shadow":
        if alias is None:
            raise RuntimeError("recovery publication requires an alias")
        args += ["--routing-recovery-shadow-lwar-id", alias]
    elif mode != "auto":
        raise RuntimeError(f"unknown publication mode: {mode}")
    args += ["--task-file", str(draft_path), "--root", str(root)]
    return run_pao(OA_SCRIPT, *args)


def claim_and_begin(
    root: Path,
    identity_file: str,
    expected_task_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    delivery = run_pao(
        ADP_SCRIPT,
        "--identity-file",
        identity_file,
        "--resident",
        "--interval",
        "0.01",
        "--timeout",
        "0.2",
        "--lease-seconds",
        "300",
        "--root",
        str(root),
        timeout=60,
    )
    if delivery.get("task_id") != expected_task_id:
        raise RuntimeError(f"unexpected task delivery: {delivery}")
    grant = run_pao(
        LWAR_SCRIPT,
        "begin",
        "--identity-file",
        identity_file,
        "--task-id",
        delivery["task_id"],
        "--claim-token",
        delivery["task"]["claim_token"],
        "--execution-id",
        delivery["execution_id"],
        "--invocation-id",
        delivery["invocation_id"],
        "--root",
        str(root),
    )
    if grant.get("event") != "execution_began":
        raise RuntimeError(f"execution fence refused task: {grant}")
    return delivery, grant


def wait_with_presence(
    root: Path,
    futures: set[Future[dict[str, Any]]],
) -> None:
    pending = set(futures)
    while pending:
        _, pending = wait(pending, timeout=20)
        if pending:
            run_pao(OA_SCRIPT, "presence", "--root", str(root))


def run_provider_batch(
    root: Path,
    work_dir: Path,
    requests: dict[str, str],
) -> dict[str, dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=len(requests)) as executor:
        futures = {
            alias: executor.submit(
                RUNNERS[alias],
                prompt,
                work_dir / alias,
            )
            for alias, prompt in requests.items()
        }
        wait_with_presence(root, set(futures.values()))
        results = {}
        for alias, future in futures.items():
            try:
                results[alias] = future.result()
            except Exception as error:
                results[alias] = {
                    "adapter": "unavailable",
                    "ok": False,
                    "duration_s": 0,
                    "error": str(error),
                    "answer": "",
                    "metrics": {"telemetry_complete": False},
                }
        return results


def complete_and_validate(
    *,
    root: Path,
    campaign_dir: Path,
    identity_file: str,
    alias: str,
    task_name: str,
    task: dict[str, Any],
    delivery: dict[str, Any],
    grant: dict[str, Any],
    provider: dict[str, Any],
    publication: dict[str, Any],
    record_routing_tokens: bool,
) -> dict[str, Any]:
    outcome = (
        grade(task, provider["answer"])
        if provider.get("ok")
        else {
            "score": 0,
            "valid_json": False,
            "reason": provider.get("error") or "provider_failed",
        }
    )
    answer = provider.get("answer", "")
    answer_sha256 = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    raw_path = campaign_dir / "raw" / alias / f"{task_name}.txt"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(answer, encoding="utf-8", newline="")
    result = {
        "status": "succeeded" if outcome["score"] == 1 else "failed",
        "summary": (
            "Sealed ordering task passed objective verification"
            if outcome["score"] == 1
            else "Sealed ordering task failed objective verification"
        ),
        "evidence": {
            "task": task_name,
            "phase": task["phase"],
            "score": outcome["score"],
            "valid_json": outcome["valid_json"],
            "response_sha256": answer_sha256,
        },
        "artifacts": [],
        "next_action": "validate",
        "exit_code": 0 if outcome["score"] == 1 else 1,
        "error": None if outcome["score"] == 1 else outcome["reason"],
    }
    result_path = campaign_dir / "results" / alias / f"{task_name}.json"
    write_json(result_path, result)
    run_pao(
        LWAR_SCRIPT,
        "complete",
        "--identity-file",
        identity_file,
        "--task-id",
        delivery["task_id"],
        "--claim-token",
        delivery["task"]["claim_token"],
        "--execution-token",
        grant["execution_token"],
        "--result-file",
        str(result_path),
        "--root",
        str(root),
    )
    collected = run_pao(
        OA_SCRIPT,
        "collect",
        "--lwar-id",
        alias,
        "--archive",
        "--root",
        str(root),
    )
    if collected.get("count") != 1 or collected.get("quarantined"):
        raise RuntimeError(f"collection invariant failed: {collected}")
    tokens = reported_tokens(provider.get("metrics") or {})
    validation_args = [
        "validate",
        "--task-id",
        delivery["task_id"],
        "--workflow-id",
        workflow_id(task["phase"]),
        "--record",
        "--decision",
        "accepted" if outcome["score"] == 1 else "rejected",
        "--reason",
        f"sealed_objective_{outcome['reason']}",
        "--root",
        str(root),
    ]
    if record_routing_tokens and tokens is not None:
        validation_args += ["--routing-reported-tokens", str(tokens)]
    validation = run_pao(OA_SCRIPT, *validation_args)
    public_provider = {
        key: value for key, value in provider.items() if key != "answer"
    }
    public_provider["reported_tokens"] = tokens
    return {
        "alias": alias,
        "task_id": delivery["task_id"],
        "task": task_name,
        "phase": task["phase"],
        "accepted": outcome["score"] == 1,
        "grade": outcome,
        "provider": public_provider,
        "response_sha256": answer_sha256,
        "receipt": {
            "routing_mode": publication.get("routing_mode"),
            "routing_reason": publication.get("routing_reason"),
            "routing_receipt_sha256": publication.get(
                "routing_receipt_sha256"
            ),
        },
        "validation": {
            "semantic_verdict": (
                "accepted" if outcome["score"] == 1 else "rejected"
            ),
            "routing_observation": validation.get("routing_observation"),
            "routing_observation_sha256": validation.get(
                "routing_observation_sha256"
            ),
        },
        "execution": {
            "execution_id": delivery["execution_id"],
            "invocation_epoch": delivery["invocation_epoch"],
            "claim_token_sha256": hashlib.sha256(
                delivery["task"]["claim_token"].encode("utf-8")
            ).hexdigest(),
        },
    }


def execute_task(
    *,
    root: Path,
    campaign_dir: Path,
    work_dir: Path,
    profile: Path,
    policy: Path,
    identities: dict[str, str],
    alias: str,
    task_name: str,
    task: dict[str, Any],
    mode: str,
    record_routing_tokens: bool,
) -> dict[str, Any]:
    for identity_alias in ("LWAR1", "LWAR4"):
        activate(root, identities[identity_alias])
    publication = publish(
        root=root,
        campaign_dir=campaign_dir,
        work_dir=work_dir,
        profile=profile,
        policy=policy,
        alias=alias if mode != "auto" else None,
        task_name=task_name,
        task=task,
        mode=mode,
    )
    selected_alias = publication["lwar_id"]
    if mode != "auto" and selected_alias != alias:
        raise RuntimeError(f"explicit routing selected {selected_alias}, not {alias}")
    delivery, grant = claim_and_begin(
        root, identities[selected_alias], publication["task_id"]
    )
    provider = run_provider_batch(
        root, work_dir, {selected_alias: task["prompt"]}
    )[selected_alias]
    return complete_and_validate(
        root=root,
        campaign_dir=campaign_dir,
        identity_file=identities[selected_alias],
        alias=selected_alias,
        task_name=task_name,
        task=task,
        delivery=delivery,
        grant=grant,
        provider=provider,
        publication=publication,
        record_routing_tokens=record_routing_tokens,
    )


def execute_pair(
    *,
    root: Path,
    campaign_dir: Path,
    work_dir: Path,
    profile: Path,
    policy: Path,
    identities: dict[str, str],
    task_name: str,
    task: dict[str, Any],
) -> list[dict[str, Any]]:
    publications = {}
    for alias, mode in (("LWAR1", "shadow"), ("LWAR4", "recovery_shadow")):
        activate(root, identities[alias])
        publications[alias] = publish(
            root=root,
            campaign_dir=campaign_dir,
            work_dir=work_dir,
            profile=profile,
            policy=policy,
            alias=alias,
            task_name=task_name,
            task=task,
            mode=mode,
        )
        if publications[alias]["lwar_id"] != alias:
            raise RuntimeError(f"paired routing invariant failed for {alias}")
    deliveries = {}
    grants = {}
    for alias in ("LWAR1", "LWAR4"):
        deliveries[alias], grants[alias] = claim_and_begin(
            root, identities[alias], publications[alias]["task_id"]
        )
    providers = run_provider_batch(
        root,
        work_dir,
        {alias: task["prompt"] for alias in ("LWAR1", "LWAR4")},
    )
    return [
        complete_and_validate(
            root=root,
            campaign_dir=campaign_dir,
            identity_file=identities[alias],
            alias=alias,
            task_name=task_name,
            task=task,
            delivery=deliveries[alias],
            grant=grants[alias],
            provider=providers[alias],
            publication=publications[alias],
            record_routing_tokens=alias == "LWAR4",
        )
        for alias in ("LWAR1", "LWAR4")
    ]


def active_work_counts(root: Path) -> dict[str, int]:
    counts = {}
    for name in ("incoming", "claimed", "leases", "outgoing"):
        counts[name] = sum(
            1
            for path in (root / "mailbox").glob(f"LWAR*/{name}/*")
            if path.is_file()
        )
    return counts


def current_production_decision(
    root: Path,
    profile_path: Path,
    policy_path: Path,
) -> dict[str, Any]:
    status = run_pao(OA_SCRIPT, "status", "--root", str(root))
    active = [
        item
        for item in status["lwars"]
        if item["state"] == "on"
        and item["runtime_status"] == "active"
        and item["heartbeat_identity_match"]
    ]
    eligible = sorted(item["lwar_id"] for item in active)
    identities = {
        item["lwar_id"]: {
            "instance_id": item["instance_id"],
            "generation": item["generation"],
            "registry_version": status["registry_version"],
        }
        for item in active
    }
    observations = current_generation_observations(
        load_routing_observations(root), identities
    )
    return select_confidence_canary(
        load_routing_profile(profile_path),
        load_canary_policy(policy_path),
        observations,
        load_circuit_state(root),
        "constraint_ordering",
        eligible,
        identities,
    )


def shutdown(root: Path, identities: dict[str, str]) -> list[dict[str, Any]]:
    events = []
    for alias in ("LWAR1", "LWAR4"):
        activate(root, identities[alias])
        run_pao(
            OA_SCRIPT,
            "control",
            "--lwar-id",
            alias,
            "--command",
            "shutdown",
            "--root",
            str(root),
        )
        events.append(
            run_pao(
                ADP_SCRIPT,
                "--identity-file",
                identities[alias],
                "--interval",
                "0.01",
                "--timeout",
                "0.2",
                "--root",
                str(root),
                expected=20,
            )
        )
    return events


def verify_preregistration_commit(suite: Path, preregistration: Path) -> str:
    completed = subprocess.run(
        ["git", "diff", "--exit-code", "HEAD", "--", str(suite), str(preregistration)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("suite and preregistration must be committed")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    upstream = subprocess.run(
        ["git", "rev-parse", "@{upstream}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if upstream.returncode != 0 or upstream.stdout.strip() != head:
        raise RuntimeError("preregistration commit must be pushed before execution")
    return head


def main() -> int:
    global CAMPAIGN_ID
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--preregistration", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--campaign-id", default=CAMPAIGN_ID)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE_PATH)
    args = parser.parse_args()

    CAMPAIGN_ID = args.campaign_id
    root = args.root.resolve()
    campaign_dir = args.campaign_dir.resolve()
    suite_path = args.suite.resolve()
    preregistration_path = args.preregistration.resolve()
    profile = args.profile.resolve()
    policy = args.policy.resolve()
    evidence_path = args.evidence.resolve()
    work_dir = campaign_dir / "workspace"
    work_dir.mkdir(parents=True, exist_ok=True)
    if evidence_path.exists():
        raise SystemExit(f"evidence already exists: {evidence_path}")

    preregistration_commit = verify_preregistration_commit(
        suite_path, preregistration_path
    )
    suite = load_json(suite_path)
    preregistration = load_json(preregistration_path)
    if canonical_sha256(suite) != preregistration["suite_sha256"]:
        raise RuntimeError("suite hash does not match preregistration")
    circuit_path = root / "var" / "routing" / "canary-circuits.json"
    if raw_sha256(circuit_path) != preregistration["source_bus"][
        "circuit_file_sha256"
    ]:
        raise RuntimeError("source circuit fingerprint changed after preregistration")

    doctor = run_pao(
        REPO / ".agents" / "skills" / "pao-oa" / "scripts" / "pao.py",
        "doctor",
        "--role",
        "oa",
        "--root",
        str(root),
    )
    if not doctor.get("healthy"):
        raise RuntimeError(f"OA doctor failed: {doctor}")
    audit_before = run_pao(OA_SCRIPT, "audit-health", "--root", str(root))
    if audit_before["status"] != "healthy":
        raise RuntimeError(f"audit is not healthy: {audit_before}")
    identities = trusted_identities(root)
    run_pao(OA_SCRIPT, "presence", "--root", str(root))

    records: list[dict[str, Any]] = []
    circuit_sha_before = raw_sha256(circuit_path)
    recovery_tasks = [
        (name, task)
        for name, task in suite["tasks"].items()
        if task["phase"] == "recovery_pair"
    ]
    for name, task in recovery_tasks:
        records.extend(
            execute_pair(
                root=root,
                campaign_dir=campaign_dir,
                work_dir=work_dir,
                profile=profile,
                policy=policy,
                identities=identities,
                task_name=name,
                task=task,
            )
        )
        write_json(campaign_dir / "progress.json", {"records": records})

    recovery = [row for row in records if row["phase"] == "recovery_pair"]
    recovery_gate = {
        "lwar1_accepted": sum(
            row["accepted"] for row in recovery if row["alias"] == "LWAR1"
        ),
        "lwar4_accepted": sum(
            row["accepted"] for row in recovery if row["alias"] == "LWAR4"
        ),
        "telemetry_complete": all(
            bool(row["provider"]["metrics"].get("telemetry_complete"))
            and row["provider"]["reported_tokens"] is not None
            for row in recovery
        ),
        "circuit_unchanged": raw_sha256(circuit_path) == circuit_sha_before,
        "audit_healthy": run_pao(
            OA_SCRIPT, "audit-health", "--root", str(root)
        )["status"]
        == "healthy",
        "active_work": active_work_counts(root),
    }
    recovery_gate["passed"] = (
        recovery_gate["lwar1_accepted"] == 12
        and recovery_gate["lwar4_accepted"] == 12
        and recovery_gate["telemetry_complete"]
        and recovery_gate["circuit_unchanged"]
        and recovery_gate["audit_healthy"]
        and all(value == 0 for value in recovery_gate["active_work"].values())
    )
    if not recovery_gate["passed"]:
        shutdowns = shutdown(root, identities)
        evidence = {
            "schema_version": "pao.reset-requalification-evidence.v1",
            "completed_at": utc_now(),
            "preregistration_commit": preregistration_commit,
            "suite_sha256": canonical_sha256(suite),
            "records": records,
            "recovery_gate": recovery_gate,
            "shutdowns": shutdowns,
            "verdict": "recovery_gate_failed_circuit_preserved_open",
        }
        write_json(evidence_path, evidence)
        print(json.dumps({"verdict": evidence["verdict"]}, sort_keys=True))
        return 2

    reset = run_pao(
        OA_SCRIPT,
        "routing-circuit-reset",
        "--lwar-id",
        "LWAR4",
        "--routing-class",
        "constraint_ordering",
        "--reason",
        RESET_REASON,
        "--root",
        str(root),
    )
    reset_state = load_circuit_state(root)
    reset_at = reset_state["resets"]["constraint_ordering::LWAR4"]["reset_at"]

    post_reset_tasks = [
        (name, task)
        for name, task in suite["tasks"].items()
        if task["phase"] == "post_reset_shadow"
    ]
    post_reset_failed = False
    for name, task in post_reset_tasks:
        record = execute_task(
            root=root,
            campaign_dir=campaign_dir,
            work_dir=work_dir,
            profile=profile,
            policy=policy,
            identities=identities,
            alias="LWAR4",
            task_name=name,
            task=task,
            mode="shadow",
            record_routing_tokens=True,
        )
        records.append(record)
        write_json(campaign_dir / "progress.json", {"records": records})
        if not record["accepted"]:
            post_reset_failed = True
            break

    post_reset = [
        row for row in records if row["phase"] == "post_reset_shadow"
    ]
    post_reset_gate = {
        "reset_at": reset_at,
        "executed": len(post_reset),
        "accepted": sum(row["accepted"] for row in post_reset),
        "rejected": sum(not row["accepted"] for row in post_reset),
        "all_have_observations": all(
            row["validation"]["routing_observation"] for row in post_reset
        ),
        "circuit_open": (
            "constraint_ordering::LWAR4"
            in load_circuit_state(root)["circuits"]
        ),
    }
    post_reset_gate["passed"] = (
        not post_reset_failed
        and post_reset_gate["executed"] == 13
        and post_reset_gate["accepted"] == 13
        and post_reset_gate["all_have_observations"]
        and not post_reset_gate["circuit_open"]
    )

    production_record = None
    fallback_record = None
    if post_reset_gate["passed"]:
        for alias in ("LWAR1", "LWAR4"):
            activate(root, identities[alias])
        production_decision = current_production_decision(
            root, profile, policy
        )
        if (
            production_decision["selected_lwar_id"] != "LWAR4"
            or production_decision["route_mode"] != "live"
            or production_decision["reason"] != "confidence_qualified_live"
        ):
            post_reset_gate["passed"] = False
            post_reset_gate["production_decision"] = production_decision
        else:
            post_reset_gate["production_decision"] = production_decision
    if post_reset_gate["passed"]:
        name, task = next(
            (name, task)
            for name, task in suite["tasks"].items()
            if task["phase"] == "production_canary"
        )
        production_record = execute_task(
            root=root,
            campaign_dir=campaign_dir,
            work_dir=work_dir,
            profile=profile,
            policy=policy,
            identities=identities,
            alias="LWAR4",
            task_name=name,
            task=task,
            mode="auto",
            record_routing_tokens=True,
        )
        if (
            production_record["alias"] != "LWAR4"
            or production_record["receipt"]["routing_mode"] != "live"
            or production_record["receipt"]["routing_reason"]
            != "confidence_qualified_live"
        ):
            raise RuntimeError(
                f"production route invariant failed: {production_record}"
            )
        records.append(production_record)
        if not production_record["accepted"]:
            name, task = next(
                (name, task)
                for name, task in suite["tasks"].items()
                if task["phase"] == "fallback_probe"
            )
            fallback_record = execute_task(
                root=root,
                campaign_dir=campaign_dir,
                work_dir=work_dir,
                profile=profile,
                policy=policy,
                identities=identities,
                alias="LWAR1",
                task_name=name,
                task=task,
                mode="auto",
                record_routing_tokens=True,
            )
            records.append(fallback_record)

    shutdowns = shutdown(root, identities)
    audit_after = run_pao(OA_SCRIPT, "audit-health", "--root", str(root))
    counts = active_work_counts(root)
    final_state = load_circuit_state(root)
    if not post_reset_gate["passed"]:
        verdict = "post_reset_requalification_failed_production_not_run"
    elif production_record and production_record["accepted"]:
        verdict = "production_requalified_canary_accepted"
    elif fallback_record and fallback_record["accepted"]:
        verdict = "production_canary_rejected_sticky_fallback_verified"
    else:
        verdict = "production_canary_rejected_fallback_unproven"
    evidence = {
        "schema_version": "pao.reset-requalification-evidence.v1",
        "completed_at": utc_now(),
        "preregistration_commit": preregistration_commit,
        "suite_sha256": canonical_sha256(suite),
        "source_circuit_sha256": circuit_sha_before,
        "recovery_gate": recovery_gate,
        "reset": reset,
        "post_reset_gate": post_reset_gate,
        "production_canary": production_record,
        "fallback_probe": fallback_record,
        "records": records,
        "final_circuit_state": final_state,
        "closeout": {
            "audit_status": audit_after["status"],
            "active_work": counts,
            "shutdowns_consumed": len(shutdowns),
        },
        "verdict": verdict,
    }
    write_json(evidence_path, evidence)
    print(
        json.dumps(
            {
                "evidence": str(evidence_path),
                "records": len(records),
                "verdict": verdict,
            },
            sort_keys=True,
        )
    )
    return 0 if verdict == "production_requalified_canary_accepted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
