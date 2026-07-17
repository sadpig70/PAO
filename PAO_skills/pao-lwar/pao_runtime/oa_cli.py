from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import audit
from .common import (
    FileLock,
    atomic_write_json,
    authority_denied_reason,
    emit,
    load_json,
    new_id,
    parse_utc,
    resolve_root,
    sha256_file,
    utc_now,
    validate_lwar_id,
    validate_task_id,
)
from .ledger import TaskLedger
from .registry import RegistryService
from .routing import (
    STALE_AFTER_S_DEFAULT,
    auto_route,
    heartbeat_age_s,
    heartbeat_stale,
)
from .transport import FileTransport


OA_WRITER_TTL_S = 900


def ensure_oa_writer(root: Path, ttl_s: int = OA_WRITER_TTL_S) -> dict[str, Any]:
    """Single-writer guard for mutating OA commands.

    The OA identity is `PAO_OA_ID` (set once per OA session). Sessions that do
    not set it share the `oa-default` holder, so exclusion is only effective
    between sessions with distinct ids. Best-effort: the TTL, not the lock,
    bounds a crashed writer — a command outliving the TTL can be superseded.
    """
    oa_id = os.environ.get("PAO_OA_ID", "").strip() or "oa-default"
    lease_path = root / "var" / "oa" / "writer_lease.json"
    now = datetime.now(timezone.utc)
    with FileLock(lease_path.parent / ".writer.lock"):
        if lease_path.is_file():
            lease = load_json(lease_path)
            if lease.get("oa_id") != oa_id and parse_utc(lease["expires_at"]) > now:
                raise SystemExit(
                    f"another OA holds the writer lease: {lease.get('oa_id')} until "
                    f"{lease['expires_at']} — this session is a read-only observer "
                    "(set the matching PAO_OA_ID or wait for expiry)"
                )
        expires_at = (now + timedelta(seconds=ttl_s)).isoformat().replace("+00:00", "Z")
        lease = {
            "schema_version": "pao.oa-writer-lease.v1",
            "oa_id": oa_id,
            "exclusive": oa_id != "oa-default",
            "refreshed_at": utc_now(),
            "expires_at": expires_at,
        }
        atomic_write_json(lease_path, lease)
    return lease


def load_active_slot(root: Path, lwar_id: str, require_on: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    registry_path = root / "var" / "registry" / "lwar_registry.json"
    if not registry_path.is_file():
        raise SystemExit("dynamic registry does not exist; reconcile a registration first")
    registry = load_json(registry_path)
    slot = registry.get("slots", {}).get(validate_lwar_id(lwar_id))
    if slot is None:
        raise SystemExit(f"LWAR is not registered: {lwar_id}")
    if require_on and slot["state"] != "on":
        raise SystemExit(f"LWAR is not on: {lwar_id} state={slot['state']}")
    return registry, slot


def command_reconcile(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ensure_oa_writer(root)
    service = RegistryService(root, tombstone_retention_s=args.tombstone_retention)
    counts = service.reconcile()
    audit.record(root, "oa", {"event": "oa_reconcile_complete", **counts})
    emit({"event": "oa_reconcile_complete", **counts})
    return 0


def _check_dependencies(ledger: TaskLedger, depends_on: list[str]) -> None:
    for dependency in depends_on:
        validate_task_id(dependency)
        entry = ledger.get(dependency)
        if entry is None:
            raise SystemExit(f"dependency not satisfied: {dependency} has no ledger entry")
        if entry.get("status") != "completed":
            raise SystemExit(f"dependency not satisfied: {dependency} status={entry.get('status')}")
        result = entry.get("result") or {}
        if result.get("status") != "succeeded":
            raise SystemExit(
                f"dependency not satisfied: {dependency} result status={result.get('status')}"
            )


def command_send(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ensure_oa_writer(root)
    transport = FileTransport(root)
    ledger = TaskLedger(root)
    source = load_json(Path(args.task_file).resolve())

    if args.auto:
        registry_path = root / "var" / "registry" / "lwar_registry.json"
        if not registry_path.is_file():
            raise SystemExit("dynamic registry does not exist; reconcile a registration first")
        registry = load_json(registry_path)
        require = set(args.require_capability)
        lwar_id = auto_route(
            registry, transport, require, datetime.now(timezone.utc), stale_after_s=args.stale_after
        )
        if lwar_id is None:
            raise SystemExit(f"no eligible LWAR for capabilities: {sorted(require) or 'any'}")
    elif args.lwar_id:
        lwar_id = args.lwar_id
    else:
        raise SystemExit("either --lwar-id or --auto is required")

    registry, slot = load_active_slot(root, lwar_id, require_on=True)
    task_id = validate_task_id(source.get("task_id") or new_id("task"))
    priority = int(source.get("priority", 5))
    if priority < 0 or priority > 999:
        raise SystemExit("priority must be between 0 and 999")
    if not source.get("goal"):
        raise SystemExit("task file requires a non-empty goal")
    timeout_s = int(source.get("timeout_s", 90))
    if timeout_s <= 0:
        raise SystemExit("timeout_s must be positive")
    if not isinstance(source.get("completion_criteria", []), list):
        raise SystemExit("completion_criteria must be an array")
    permissions = source.get("permissions")
    if permissions is not None:
        if not isinstance(permissions, dict):
            raise SystemExit("permissions must be an object")
        for key in ("read", "write"):
            entries = permissions.get(key, [])
            if not isinstance(entries, list) or any(not isinstance(e, str) for e in entries):
                raise SystemExit(f"permissions.{key} must be an array of paths")
        if "network" in permissions and not isinstance(permissions["network"], bool):
            raise SystemExit("permissions.network must be a boolean")
        max_artifact_bytes = permissions.get("max_artifact_bytes")
        if max_artifact_bytes is not None and (
            not isinstance(max_artifact_bytes, int) or max_artifact_bytes <= 0
        ):
            raise SystemExit("permissions.max_artifact_bytes must be a positive integer")
    depends_on = source.get("depends_on", [])
    if not isinstance(depends_on, list):
        raise SystemExit("depends_on must be an array of task ids")
    _check_dependencies(ledger, depends_on)

    task = {
        "schema_version": "pao.task.v1",
        "task_id": task_id,
        "workflow_id": source.get("workflow_id") or new_id("workflow"),
        "parent_task_id": source.get("parent_task_id"),
        "depends_on": depends_on,
        "lwar_id": lwar_id,
        "instance_id": slot["instance_id"],
        "generation": slot["generation"],
        "registry_version": registry["registry_version"],
        "role": source.get("role", "worker"),
        "goal": source["goal"],
        "instructions": source.get("instructions", source["goal"]),
        "completion_criteria": source.get("completion_criteria", []),
        "cwd": str(Path(source.get("cwd", str(root))).resolve()),
        "input_files": source.get("input_files", []),
        "expected_output": source.get("expected_output", "ResultContract"),
        "timeout_s": timeout_s,
        "max_retries": int(source.get("max_retries", 3)),
        "priority": priority,
        "adapter_options": source.get("adapter_options", {}),
        "attempt": int(source.get("attempt", 1)),
        "created_at": utc_now(),
    }
    task["permissions"] = permissions if permissions is not None else {
        "read": [task["cwd"]],
        "write": [task["cwd"]],
        "network": False,
    }
    if not Path(task["cwd"]).is_dir():
        raise SystemExit(f"task cwd does not exist: {task['cwd']}")
    denied = authority_denied_reason(Path(task["cwd"]), root)
    if denied:
        raise SystemExit(f"task cwd violates authority bounds: {denied}")
    for key in ("read", "write"):
        for entry in task["permissions"].get(key, []):
            denied = authority_denied_reason(Path(entry), root)
            if denied:
                raise SystemExit(f"permissions.{key} path violates authority bounds: {denied} ({entry})")
    if transport.task_pending(lwar_id, task_id):
        raise SystemExit(f"task already exists for {lwar_id}: {task_id}")
    target = transport.publish_task(task)
    ledger.record_published(task)
    audit.record(
        root,
        "oa",
        {"event": "task_published", "lwar_id": lwar_id, "task_id": task_id, "workflow_id": task["workflow_id"]},
    )
    emit({"event": "task_published", "lwar_id": lwar_id, "task_id": task_id, "message_file": str(target)})
    return 0


def command_control(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ensure_oa_writer(root)
    transport = FileTransport(root)
    registry, slot = load_active_slot(root, args.lwar_id)
    control_id = new_id("control")
    message = {
        "schema_version": "pao.control.v1",
        "control_id": control_id,
        "lwar_id": args.lwar_id,
        "instance_id": slot["instance_id"],
        "generation": slot["generation"],
        "registry_version": registry["registry_version"],
        "command": args.command,
        "task_id": args.task_id,
        "reason": args.reason,
        "created_at": utc_now(),
    }
    path = transport.publish_control(message)
    audit.record(
        root,
        "oa",
        {"event": "control_published", "lwar_id": args.lwar_id, "command": args.command, "control_id": control_id},
    )
    emit({"event": "control_published", "lwar_id": args.lwar_id, "command": args.command, "message_file": str(path)})
    return 0


def artifact_verification(root: Path, artifacts: list[Any]) -> dict[str, Any]:
    """Verify the immutable snapshots recorded by `complete`.

    Legacy string artifacts carry no snapshot and are skipped. The size is
    compared before hashing so a swapped store file cannot force an unbounded
    read (DoS guard).
    """
    failures = []
    checked = 0
    for item in artifacts or []:
        if not isinstance(item, dict):
            continue
        checked += 1
        snapshot_rel = item.get("snapshot")
        snapshot = (root / snapshot_rel) if snapshot_rel else None
        if snapshot is None or not snapshot.is_file():
            failures.append(f"artifact_snapshot_missing:{item.get('path')}")
            continue
        if snapshot.stat().st_size != item.get("size_bytes"):
            failures.append(f"artifact_size_mismatch:{item.get('path')}")
            continue
        if sha256_file(snapshot) != item.get("sha256"):
            failures.append(f"artifact_hash_mismatch:{item.get('path')}")
    return {"verified": not failures, "checked": checked, "failures": failures}


def command_collect(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ensure_oa_writer(root)
    transport = FileTransport(root)
    ledger = TaskLedger(root)
    registry = RegistryService(root).load_registry()
    targets = [args.lwar_id] if args.lwar_id else transport.list_lwar_ids()
    collected = []
    quarantined = []
    for lwar_id in targets:
        slot = registry.get("slots", {}).get(lwar_id)
        for path in transport.outgoing_results(lwar_id):
            result = load_json(path)
            if (
                slot is None
                or slot["instance_id"] != result.get("instance_id")
                or slot["generation"] != result.get("generation")
            ):
                destination = transport.quarantine_result(lwar_id, path, "stale_identity_result")
                quarantined.append(
                    {"lwar_id": lwar_id, "task_id": result.get("task_id"), "reason": "stale_identity_result", "file": str(destination)}
                )
                continue
            entry = ledger.get(result["task_id"], result.get("workflow_id"))
            ledger_attempt = entry.get("attempt") if entry else None
            result_attempt = result.get("attempt")
            if (
                ledger_attempt is not None
                and result_attempt is not None
                and int(result_attempt) != int(ledger_attempt)
            ):
                # Attempt fence: the ledger's attempt is bumped by recover and
                # dead-requeue, so a mismatched echo means this result belongs
                # to a superseded claim. Legacy results without the echo skip
                # the fence (optional-first rollout).
                destination = transport.quarantine_result(lwar_id, path, "stale_attempt_result")
                quarantined.append(
                    {"lwar_id": lwar_id, "task_id": result["task_id"], "reason": "stale_attempt_result", "file": str(destination)}
                )
                continue
            if (
                entry is not None
                and entry.get("status") == "completed"
                and entry.get("result_file")
                and entry["result_file"] != str(path)
            ):
                destination = transport.quarantine_result(lwar_id, path, "duplicate_result")
                quarantined.append(
                    {"lwar_id": lwar_id, "task_id": result["task_id"], "reason": "duplicate_result", "file": str(destination)}
                )
                continue
            verification = artifact_verification(root, result.get("artifacts"))
            if not verification["verified"]:
                destination = transport.quarantine_result(lwar_id, path, "artifact_tampered")
                quarantined.append(
                    {
                        "lwar_id": lwar_id,
                        "task_id": result["task_id"],
                        "reason": "artifact_tampered",
                        "failures": verification["failures"],
                        "file": str(destination),
                    }
                )
                continue
            final_path = path
            if args.archive:
                final_path = transport.archive_result(lwar_id, path)
            ledger.record_completed_result(result, str(final_path))
            collected.append({"lwar_id": lwar_id, "result_file": str(final_path), "result": result})
    audit.record(
        root,
        "oa",
        {"event": "results_collected", "count": len(collected), "quarantined": len(quarantined)},
    )
    emit({"event": "results_collected", "count": len(collected), "results": collected, "quarantined": quarantined})
    return 0


def command_recover(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ensure_oa_writer(root)
    transport = FileTransport(root)
    ledger = TaskLedger(root)
    targets = [args.lwar_id] if args.lwar_id else transport.list_lwar_ids()
    recovered = []
    dead_lettered = []
    failed_reconciled = []
    now = datetime.now(timezone.utc)
    for lwar_id in targets:
        for lease_path, lease in transport.expired_leases(lwar_id, now):
            if transport.result_exists(lwar_id, lease["task_id"]):
                lease_path.unlink(missing_ok=True)
                continue
            claimed = transport.claimed_task_for_lease(lwar_id, lease)
            if claimed is None:
                lease_path.unlink(missing_ok=True)
                continue
            claimed_path, task = claimed
            attempt = int(task.get("attempt", 1)) + 1
            max_retries = int(task.get("max_retries", 3))
            task["attempt"] = attempt
            # The interrupted terminal is recorded by OA, never inferred as a
            # submitted result: the LWAR may have died without submitting.
            interruption = {
                "status": "interrupted",
                "reason": "lease_expired",
                "recorded_by": "oa_reconciler",
                "recorded_at": utc_now(),
            }
            if attempt > max_retries:
                transport.dead_letter(lwar_id, claimed_path, task, "retry_budget_exhausted")
                ledger.transition(
                    task["task_id"],
                    "dead",
                    workflow_id=task.get("workflow_id"),
                    detail="retry_budget_exhausted",
                    attempt=attempt,
                    interruption=interruption,
                )
                dead_lettered.append({"lwar_id": lwar_id, "task_id": task["task_id"], "attempt": attempt})
            else:
                moved = transport.requeue_claimed(lwar_id, claimed_path, task)
                if moved is not None:
                    ledger.transition(
                        task["task_id"],
                        "requeued",
                        workflow_id=task.get("workflow_id"),
                        detail="lease_expired",
                        attempt=attempt,
                        interruption=interruption,
                    )
                    recovered.append({"lwar_id": lwar_id, "task_id": task["task_id"], "attempt": attempt})
            lease_path.unlink(missing_ok=True)
        # Reconcile rejected tasks parked in failed/ so their ledger entries do
        # not sit at `published` forever (claim-guard and schema rejections).
        for _task_path, task, reason in transport.failed_entries(lwar_id):
            task_id = task.get("task_id")
            if not task_id:
                continue
            entry = ledger.get(task_id, task.get("workflow_id"))
            if entry is None or entry.get("status") in {"failed", "dead", "completed"}:
                continue
            ledger.transition(
                task_id,
                "failed",
                workflow_id=task.get("workflow_id"),
                detail=f"rejected:{reason}",
            )
            failed_reconciled.append({"lwar_id": lwar_id, "task_id": task_id, "reason": reason})
    audit.record(
        root,
        "oa",
        {
            "event": "stale_leases_recovered",
            "count": len(recovered),
            "dead_lettered": len(dead_lettered),
            "failed_reconciled": len(failed_reconciled),
        },
    )
    emit(
        {
            "event": "stale_leases_recovered",
            "count": len(recovered),
            "tasks": recovered,
            "dead_lettered": dead_lettered,
            "failed_reconciled": failed_reconciled,
        }
    )
    return 0


def command_status(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    transport = FileTransport(root)
    service = RegistryService(root)
    registry = service.load_registry()
    now = datetime.now(timezone.utc)
    states = []
    for lwar_id, slot in sorted(registry["slots"].items()):
        heartbeat = transport.read_heartbeat(lwar_id)
        age = heartbeat_age_s(heartbeat, now)
        states.append(
            {
                "lwar_id": lwar_id,
                "instance_id": slot["instance_id"],
                "generation": slot["generation"],
                "state": slot["state"],
                "profile": slot["profile"],
                "heartbeat": heartbeat,
                "heartbeat_age_s": round(age, 3) if age is not None else None,
                "heartbeat_stale": heartbeat_stale(heartbeat, now, args.stale_after),
            }
        )
    emit({"event": "oa_status", "registry_version": registry["registry_version"], "lwars": states})
    return 0


def command_dead(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    transport = FileTransport(root)
    ledger = TaskLedger(root)
    if args.requeue:
        if not args.lwar_id:
            raise SystemExit("--requeue requires --lwar-id")
        ensure_oa_writer(root)
        task = transport.requeue_dead(args.lwar_id, validate_task_id(args.requeue))
        if task is None:
            raise SystemExit(f"dead task not found: {args.requeue}")
        ledger.transition(
            task["task_id"],
            "requeued",
            workflow_id=task.get("workflow_id"),
            detail="manual_requeue",
            attempt=int(task.get("attempt", 1)),
        )
        audit.record(root, "oa", {"event": "dead_requeued", "lwar_id": args.lwar_id, "task_id": task["task_id"]})
        emit({"event": "dead_requeued", "lwar_id": args.lwar_id, "task_id": task["task_id"]})
        return 0
    targets = [args.lwar_id] if args.lwar_id else transport.list_lwar_ids()
    entries = []
    for lwar_id in targets:
        for path, task in transport.list_dead(lwar_id):
            entries.append(
                {
                    "lwar_id": lwar_id,
                    "task_id": task.get("task_id"),
                    "attempt": task.get("attempt"),
                    "max_retries": task.get("max_retries"),
                    "goal": task.get("goal"),
                    "file": str(path),
                }
            )
    emit({"event": "dead_tasks", "count": len(entries), "tasks": entries})
    return 0


def command_validate(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ledger = TaskLedger(root)
    entry = ledger.get(validate_task_id(args.task_id), args.workflow_id)
    if entry is None:
        emit({"event": "validation_unavailable", "task_id": args.task_id, "reason": "no_ledger_entry"})
        return 3
    result = entry.get("result")
    if entry.get("status") != "completed" or not result:
        emit(
            {
                "event": "validation_unavailable",
                "task_id": args.task_id,
                "reason": f"task_not_completed:{entry.get('status')}",
            }
        )
        return 2
    exit_code = result.get("exit_code")
    checks = {
        "result_status": result.get("status"),
        "status_succeeded": result.get("status") == "succeeded",
        "exit_code": exit_code,
        "exit_code_matches_status": (exit_code == 0) == (result.get("status") == "succeeded"),
        "evidence_present": bool(result.get("evidence")),
        "artifacts": result.get("artifacts", []),
        "attempt": entry.get("attempt"),
    }
    criteria = [
        {"criterion": criterion, "verdict": "manual_check_required"}
        for criterion in entry.get("completion_criteria", [])
    ]
    verification = artifact_verification(root, result.get("artifacts"))
    mechanical_pass = (
        checks["status_succeeded"]
        and checks["evidence_present"]
        and checks["exit_code_matches_status"]
        and verification["verified"]
    )
    verdict = "ready_for_oa_review" if mechanical_pass else "attention_required"
    if args.record:
        # Persisting the decision is a mutation; plain reporting stays
        # read-only so observer OAs can validate freely.
        ensure_oa_writer(root)
        decision = {
            "schema_version": "pao.validation-decision.v1",
            "verdict": verdict,
            "checks": checks,
            "criteria": criteria,
            "artifact_verification": verification,
            "decided_by": os.environ.get("PAO_OA_ID", "").strip() or "oa-default",
            "decided_at": utc_now(),
        }
        ledger.record_validation(args.task_id, entry.get("workflow_id"), decision)
    audit.record(root, "oa", {"event": "validation_report", "task_id": args.task_id, "verdict": verdict})
    emit(
        {
            "event": "validation_report",
            "task_id": args.task_id,
            "workflow_id": entry.get("workflow_id"),
            "goal": entry.get("goal"),
            "checks": checks,
            "criteria": criteria,
            "artifact_verification": verification,
            "recorded": bool(args.record),
            "verdict": verdict,
        }
    )
    return 0


def command_prune(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ensure_oa_writer(root)
    transport = FileTransport(root)
    if args.older_than_days <= 0:
        raise SystemExit("--older-than-days must be positive")
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.older_than_days)
    targets = [args.lwar_id] if args.lwar_id else transport.list_lwar_ids()
    counts = {}
    total = 0
    for lwar_id in targets:
        removed = transport.prune(lwar_id, cutoff)
        counts[lwar_id] = removed
        total += sum(removed.values())
    audit.record(root, "oa", {"event": "pruned", "total": total})
    emit(
        {
            "event": "pruned",
            "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
            "counts": counts,
            "total": total,
        }
    )
    return 0


def command_workflow_status(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ledger = TaskLedger(root)
    entries = ledger.workflow_entries(args.workflow_id)
    by_status: dict[str, int] = {}
    tasks = []
    for entry in entries:
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
        result = entry.get("result") or {}
        tasks.append(
            {
                "task_id": entry["task_id"],
                "status": entry["status"],
                "lwar_id": entry.get("lwar_id"),
                "attempt": entry.get("attempt"),
                "depends_on": entry.get("depends_on", []),
                "result_status": result.get("status"),
            }
        )
    emit(
        {
            "event": "workflow_status",
            "workflow_id": args.workflow_id,
            "total": len(entries),
            "by_status": by_status,
            "tasks": tasks,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oa", description="OA control tool for PAO ADP file bus")
    subparsers = parser.add_subparsers(dest="command", required=True)

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--root", default=None)
    reconcile.add_argument("--tombstone-retention", type=int, default=300)
    reconcile.set_defaults(handler=command_reconcile)

    send = subparsers.add_parser("send")
    send.add_argument("--lwar-id")
    send.add_argument("--auto", action="store_true", help="route by capability and load instead of --lwar-id")
    send.add_argument(
        "--require-capability",
        action="append",
        default=[],
        help="capability required by --auto routing (repeatable)",
    )
    send.add_argument("--stale-after", type=float, default=STALE_AFTER_S_DEFAULT)
    send.add_argument("--task-file", required=True)
    send.add_argument("--root", default=None)
    send.set_defaults(handler=command_send)

    control = subparsers.add_parser("control")
    control.add_argument("--lwar-id", required=True)
    control.add_argument("--command", required=True, choices=("shutdown", "ping", "cancel", "drain"))
    control.add_argument("--task-id")
    control.add_argument("--reason")
    control.add_argument("--root", default=None)
    control.set_defaults(handler=command_control)

    collect = subparsers.add_parser("collect")
    collect.add_argument("--lwar-id")
    collect.add_argument("--archive", action="store_true")
    collect.add_argument("--root", default=None)
    collect.set_defaults(handler=command_collect)

    recover = subparsers.add_parser("recover")
    recover.add_argument("--lwar-id")
    recover.add_argument("--root", default=None)
    recover.set_defaults(handler=command_recover)

    status = subparsers.add_parser("status")
    status.add_argument("--root", default=None)
    status.add_argument("--stale-after", type=float, default=STALE_AFTER_S_DEFAULT)
    status.set_defaults(handler=command_status)

    dead = subparsers.add_parser("dead")
    dead.add_argument("--lwar-id")
    dead.add_argument("--requeue", metavar="TASK_ID")
    dead.add_argument("--root", default=None)
    dead.set_defaults(handler=command_dead)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--task-id", required=True)
    validate.add_argument("--workflow-id")
    validate.add_argument(
        "--record",
        action="store_true",
        help="persist the decision into the task ledger (mutating: takes the writer lease)",
    )
    validate.add_argument("--root", default=None)
    validate.set_defaults(handler=command_validate)

    prune = subparsers.add_parser("prune")
    prune.add_argument("--older-than-days", type=float, required=True)
    prune.add_argument("--lwar-id")
    prune.add_argument("--root", default=None)
    prune.set_defaults(handler=command_prune)

    workflow_status = subparsers.add_parser("workflow-status")
    workflow_status.add_argument("--workflow-id", required=True)
    workflow_status.add_argument("--root", default=None)
    workflow_status.set_defaults(handler=command_workflow_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
