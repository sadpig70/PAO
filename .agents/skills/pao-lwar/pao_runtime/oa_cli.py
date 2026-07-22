from __future__ import annotations

import argparse
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import audit
from .common import (
    FileLock,
    atomic_write_json,
    authority_denied_reason,
    emit,
    identity_leaks,
    identity_terms,
    new_id,
    parse_utc,
    require_local_filesystem,
    resolve_root,
    safe_load_json,
    sha256_file,
    utc_now,
    validate_lwar_id,
    validate_task_id,
    validate_workflow_id,
)
from .contracts import ContractError, validate_contract
from .ledger import TaskLedger
from .presence import OA_PRESENCE_REFRESH_S, publish_oa_presence
from .registry import RegistryService
from .routing import (
    STARTUP_DEADLINE_S_DEFAULT,
    STALE_AFTER_S_DEFAULT,
    auto_route,
    classify_lwar_runtime,
    heartbeat_age_s,
)
from .transport import FileTransport


OA_WRITER_TTL_S = 900
OA_COMMAND_LOCK_TIMEOUT_S = 30
OA_COMMAND_LOCK_STALE_S = 30


def _next_renewal_deadline(deadline: float, interval_s: float, now: float) -> float:
    """Advance a fixed-rate deadline without accumulating command delay."""
    if interval_s <= 0:
        raise ValueError("renewal interval must be positive")
    deadline += interval_s
    if deadline <= now:
        missed = int((now - deadline) // interval_s) + 1
        deadline += missed * interval_s
    return deadline


def _require_int(value: Any, field: str) -> int:
    """Coerce a task-file field to int, or fail with a clean SystemExit instead
    of dumping a raw ValueError traceback."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SystemExit(f"{field} must be an integer")


def ensure_oa_writer(root: Path, ttl_s: int = OA_WRITER_TTL_S) -> dict[str, Any]:
    """Single-writer guard for mutating OA commands.

    The OA identity is `PAO_OA_ID` (set once per OA session). Mutations fail
    closed when it is absent. A renewable command guard refreshes the TTL.
    """
    require_local_filesystem(root)
    oa_id = os.environ.get("PAO_OA_ID", "").strip()
    if not oa_id:
        raise SystemExit(
            "PAO_OA_ID is required for mutating OA commands; set one unique id per OA session"
        )
    lease_path = root / "var" / "oa" / "writer_lease.json"
    now = datetime.now(timezone.utc)
    with FileLock(lease_path.parent / ".writer.lock"):
        lease = safe_load_json(lease_path) if lease_path.is_file() else None
        if lease is not None and "expires_at" in lease:
            # A corrupt/keyless lease is treated as absent and overwritten below
            # rather than wedging every mutating command until an operator
            # deletes it by hand.
            try:
                held = parse_utc(lease["expires_at"]) > now
            except (ValueError, TypeError):
                held = False
            if held and lease.get("oa_id") != oa_id:
                raise SystemExit(
                    f"another OA holds the writer lease: {lease.get('oa_id')} until "
                    f"{lease['expires_at']} — this session is a read-only observer "
                    "(set the matching PAO_OA_ID or wait for expiry)"
                )
        expires_at = (now + timedelta(seconds=ttl_s)).isoformat().replace("+00:00", "Z")
        lease = {
            "schema_version": "pao.oa-writer-lease.v1",
            "oa_id": oa_id,
            "exclusive": True,
            "refreshed_at": utc_now(),
            "expires_at": expires_at,
        }
        validate_contract(lease, "oa-writer-lease.schema.json")
        atomic_write_json(lease_path, lease)
    return lease


@contextmanager
def renewable_oa_writer(root: Path, ttl_s: int = OA_WRITER_TTL_S):
    """Serialize one mutating OA command and renew ownership until it exits."""
    # Check the writer identity before waiting so a different OA fails fast.
    lease = ensure_oa_writer(root, ttl_s)
    try:
        with FileLock(
            root / "var" / "oa" / ".command.lock",
            timeout_s=OA_COMMAND_LOCK_TIMEOUT_S,
            stale_s=OA_COMMAND_LOCK_STALE_S,
        ):
            # Revalidate after waiting: ownership could have changed while the
            # prior same-id command was finishing.
            lease = ensure_oa_writer(root, ttl_s)
            publish_oa_presence(root, lease["oa_id"])
            stop = threading.Event()
            renewal_errors: list[BaseException] = []

            def renew() -> None:
                interval_s = max(1.0, min(ttl_s / 3, OA_PRESENCE_REFRESH_S))
                deadline = time.monotonic() + interval_s
                while not stop.wait(max(0.0, deadline - time.monotonic())):
                    try:
                        renewed = ensure_oa_writer(root, ttl_s)
                        publish_oa_presence(root, renewed["oa_id"])
                    except BaseException as error:  # propagate to the command boundary
                        renewal_errors.append(error)
                        stop.set()
                        return
                    deadline = _next_renewal_deadline(deadline, interval_s, time.monotonic())

            thread = threading.Thread(target=renew, name="pao-oa-lease-renew", daemon=True)
            thread.start()
            try:
                yield lease
                if renewal_errors:
                    raise SystemExit(f"OA writer lease renewal failed: {renewal_errors[0]}")
            finally:
                stop.set()
                thread.join(timeout=2)
    except TimeoutError as error:
        raise SystemExit(f"OA mutation command lock unavailable: {error}")


def writer_guard(handler):
    def guarded(args: argparse.Namespace) -> int:
        root = resolve_root(args.root)
        with renewable_oa_writer(root):
            return handler(args)

    return guarded


def conditional_writer_guard(handler, predicate):
    """Guard commands whose read form is safe but an option enables mutation."""
    def guarded(args: argparse.Namespace) -> int:
        if not predicate(args):
            return handler(args)
        root = resolve_root(args.root)
        with renewable_oa_writer(root):
            return handler(args)

    return guarded


def command_presence(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    oa_id = os.environ["PAO_OA_ID"].strip()
    try:
        payload = publish_oa_presence(root, oa_id, args.ttl)
    except ValueError as error:
        raise SystemExit(str(error))
    emit({"event": "oa_presence_published", **payload})
    return 0


def load_active_slot(root: Path, lwar_id: str, require_on: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    registry_path = root / "var" / "registry" / "lwar_registry.json"
    if not registry_path.is_file():
        raise SystemExit("dynamic registry does not exist; reconcile a registration first")
    registry = safe_load_json(registry_path)
    if registry is None:
        raise SystemExit("registry is unreadable or corrupt; run `pao doctor` and inspect var/registry/")
    try:
        validate_contract(registry, "registry-state.schema.json")
    except ContractError as error:
        raise SystemExit(f"registry contract invalid: {error}")
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
        decision = entry.get("validation") or {}
        if decision.get("semantic_verdict") != "accepted":
            raise SystemExit(
                f"dependency not satisfied: {dependency} semantic validation="
                f"{decision.get('semantic_verdict') or 'missing'}"
            )
        criteria = decision.get("criteria", [])
        if any(item.get("verdict") != "passed" for item in criteria):
            raise SystemExit(f"dependency not satisfied: {dependency} has unpassed criteria")


def command_send(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ensure_oa_writer(root)
    transport = FileTransport(root)
    ledger = TaskLedger(root)
    source = safe_load_json(Path(args.task_file).resolve())
    if source is None:
        raise SystemExit(f"task file is missing or not valid JSON: {args.task_file}")

    if args.auto:
        registry_path = root / "var" / "registry" / "lwar_registry.json"
        if not registry_path.is_file():
            raise SystemExit("dynamic registry does not exist; reconcile a registration first")
        registry = safe_load_json(registry_path)
        if registry is None:
            raise SystemExit("registry is unreadable or corrupt; run `pao doctor`")
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
    priority = _require_int(source.get("priority", 5), "priority")
    if priority < 0 or priority > 999:
        raise SystemExit("priority must be between 0 and 999")
    if not source.get("goal"):
        raise SystemExit("task file requires a non-empty goal")
    timeout_s = _require_int(source.get("timeout_s", 90), "timeout_s")
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

    workflow_id = validate_workflow_id(source.get("workflow_id") or new_id("workflow"))
    terms = identity_terms(slot.get("profile"))
    public_id_leaks = identity_leaks({"task_id": task_id, "workflow_id": workflow_id}, terms)
    if public_id_leaks:
        raise SystemExit(f"public task identifiers expose runtime identity terms: {public_id_leaks}")
    task = {
        "schema_version": "pao.task.v1",
        "task_id": task_id,
        "workflow_id": workflow_id,
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
        "max_retries": _require_int(source.get("max_retries", 3), "max_retries"),
        "priority": priority,
        "adapter_options": source.get("adapter_options", {}),
        "attempt": _require_int(source.get("attempt", 1), "attempt"),
        "created_at": utc_now(),
    }
    task["permissions"] = permissions if permissions is not None else {
        "read": [task["cwd"]],
        "write": [task["cwd"]],
        "network": False,
    }
    if task["max_retries"] < 0:
        raise SystemExit("max_retries must be non-negative")
    if task["attempt"] < 1:
        raise SystemExit("attempt must be positive")
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
    validate_contract(task, "task.schema.json")
    if transport.task_pending(lwar_id, task_id):
        raise SystemExit(f"task already exists for {lwar_id}: {task_id}")
    existing = ledger.get(task["task_id"], task["workflow_id"])
    if existing is not None:
        raise SystemExit(
            f"task already has a ledger entry ({existing['status']}): {task_id} — use a new task_id"
        )
    # Record the ledger entry BEFORE making the task claimable: a crash between
    # the two then leaves a benign `published` entry with no incoming task,
    # never an untracked live task that recovery cannot see.
    ledger.record_publishing(task)
    target = transport.publish_task(task)
    ledger.transition(
        task_id,
        "published",
        workflow_id=task["workflow_id"],
        detail="mailbox_published",
        message_file=str(target),
    )
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
    archived_reconciled = []

    def identity_matches(slot: dict[str, Any] | None, result: dict[str, Any]) -> bool:
        return bool(
            slot is not None
            and slot.get("instance_id") == result.get("instance_id")
            and slot.get("generation") == result.get("generation")
        )

    def inspect_result(
        lwar_id: str, slot: dict[str, Any] | None, result: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None, dict[str, Any] | None, bool]:
        try:
            validate_contract(result, "result.schema.json")
        except ContractError as error:
            return None, f"invalid_result_schema:{error}", None, False
        entry = ledger.get(result["task_id"], result.get("workflow_id"))
        accepted_terminal_replay = bool(
            entry is not None
            and entry.get("status") == "completed"
            and entry.get("lwar_id") == lwar_id
            and entry.get("result") == result
            and (entry.get("validation") or {}).get("semantic_verdict") == "accepted"
        )
        # A retired LWAR has no active registry slot. Permit only the exact,
        # already-accepted terminal payload to finish its interrupted archive
        # transition; all new, changed, or unaccepted payloads remain fenced.
        if not identity_matches(slot, result) and not accepted_terminal_replay:
            return entry, "stale_identity_result", None, False
        ledger_attempt = entry.get("attempt") if entry else None
        result_attempt = result.get("attempt")
        if ledger_attempt is not None and result_attempt is not None:
            try:
                attempt_mismatch = int(result_attempt) != int(ledger_attempt)
            except (TypeError, ValueError):
                return entry, "invalid_result_attempt", None, False
            if attempt_mismatch:
                return entry, "stale_attempt_result", None, False
        if entry is not None and entry.get("task_contract") is not None:
            claim_token = result.get("claim_token")
            if not claim_token:
                return entry, "missing_claim_token", None, False
            provenance = transport.provenance_task(
                lwar_id, result["task_id"], claim_token, result.get("attempt")
            )
            if provenance is None:
                return entry, "claim_token_mismatch", None, False
            if int(provenance.get("attempt", 1)) != int(result.get("attempt", 1)):
                return entry, "claim_attempt_mismatch", None, False
        verification = artifact_verification(root, result.get("artifacts"))
        if not verification["verified"]:
            return entry, "artifact_tampered", verification, False
        return entry, None, verification, accepted_terminal_replay

    def quarantine(
        lwar_id: str, path: Path, result: dict[str, Any] | None, reason: str,
        verification: dict[str, Any] | None = None,
    ) -> None:
        destination = transport.quarantine_result(lwar_id, path, reason)
        item = {
            "lwar_id": lwar_id,
            "task_id": result.get("task_id") if result else None,
            "reason": reason,
            "file": str(destination),
        }
        if verification and verification.get("failures"):
            item["failures"] = verification["failures"]
        quarantined.append(item)

    for lwar_id in targets:
        slot = registry.get("slots", {}).get(lwar_id)
        for path in transport.outgoing_results(lwar_id):
            result = safe_load_json(path)
            if result is None or "task_id" not in result:
                quarantine(lwar_id, path, result, "invalid_result_json")
                continue
            entry, reason, verification, accepted_terminal_replay = inspect_result(
                lwar_id, slot, result
            )
            if reason:
                quarantine(lwar_id, path, result, reason, verification)
                continue
            if (
                entry is not None
                and entry.get("status") == "completed"
                and entry.get("result_file")
            ):
                if entry.get("result") != result:
                    quarantine(lwar_id, path, result, "duplicate_result")
                    continue
                if entry["result_file"] == str(path):
                    # Already collected this exact file on a prior run (a
                    # collect without --archive leaves the result in outgoing/).
                    # A later `--archive` pass should still move it out of
                    # outgoing/; a plain re-collect skips silently instead of
                    # re-verifying and re-growing the ledger history every poll.
                    reconcile_retired = (
                        accepted_terminal_replay and not identity_matches(slot, result)
                    )
                    if args.archive or reconcile_retired:
                        final_path = transport.archive_result(lwar_id, path)
                        ledger.update_result_file(result["task_id"], result["workflow_id"], str(final_path))
                        item = {"lwar_id": lwar_id, "result_file": str(final_path), "result": result}
                        if args.archive:
                            collected.append(item)
                        else:
                            archived_reconciled.append(item)
                    continue
                quarantine(lwar_id, path, result, "duplicate_result")
                continue
            # Canonical ledger commit happens before optional archival cleanup.
            ledger.record_completed_result(result, str(path))
            final_path = path
            if args.archive:
                final_path = transport.archive_result(lwar_id, path)
                ledger.update_result_file(result["task_id"], result["workflow_id"], str(final_path))
            collected.append({"lwar_id": lwar_id, "result_file": str(final_path), "result": result})

        # Repair either side of an interrupted archive transition. Archived
        # results are durable inputs, not invisible cleanup debris.
        for path in transport.archived_results(lwar_id):
            result = safe_load_json(path)
            if result is None or "task_id" not in result:
                quarantine(lwar_id, path, result, "invalid_archived_result_json")
                continue
            entry, reason, verification, accepted_terminal_replay = inspect_result(
                lwar_id, slot, result
            )
            if reason:
                quarantine(lwar_id, path, result, reason, verification)
                continue
            if entry is None or entry.get("status") != "completed":
                ledger.record_completed_result(result, str(path))
                collected.append({"lwar_id": lwar_id, "result_file": str(path), "result": result})
            elif entry.get("result_file") != str(path):
                ledger.update_result_file(result["task_id"], result["workflow_id"], str(path))
                if accepted_terminal_replay and not identity_matches(slot, result):
                    archived_reconciled.append(
                        {"lwar_id": lwar_id, "result_file": str(path), "result": result}
                    )
    audit.record(
        root,
        "oa",
        {
            "event": "results_collected",
            "count": len(collected),
            "quarantined": len(quarantined),
            "archived_reconciled": len(archived_reconciled),
        },
    )
    emit(
        {
            "event": "results_collected",
            "count": len(collected),
            "results": collected,
            "quarantined": quarantined,
            "archived_reconciled": archived_reconciled,
        }
    )
    return 0


def command_recover(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ensure_oa_writer(root)
    if args.reap_startup:
        if not args.lwar_id or not args.instance_id or args.generation is None:
            raise SystemExit(
                "--reap-startup requires --lwar-id, --instance-id, and --generation"
            )
        if args.startup_deadline <= 0:
            raise SystemExit("--startup-deadline must be positive")
        service = RegistryService(root)
        outcome = service.reap_startup(
            args.lwar_id,
            args.instance_id,
            args.generation,
            args.startup_deadline,
        )
        audit_operation = (
            f"startup-reap:{args.lwar_id}:{args.instance_id}:{args.generation}"
        )
        if outcome.get("deadline_missed"):
            audit.record_once(
                root,
                "oa",
                {
                    "event": "startup_deadline_missed",
                    "lwar_id": args.lwar_id,
                    "instance_id": args.instance_id,
                    "generation": args.generation,
                    "heartbeat_age_s": outcome.get("heartbeat_age_s"),
                    "startup_deadline_s": args.startup_deadline,
                },
                f"{audit_operation}:startup_deadline_missed",
            )
        event = "startup_slot_reaped" if outcome["accepted"] else "startup_slot_reap_rejected"
        audit_payload = {
            "event": event,
            "lwar_id": args.lwar_id,
            "instance_id": args.instance_id,
            "generation": args.generation,
            "reason": outcome.get("reason"),
            "active_work": outcome.get("active_work", {}),
        }
        if outcome["accepted"]:
            audit.record_once(root, "oa", audit_payload, f"{audit_operation}:{event}")
        else:
            audit.record(root, "oa", audit_payload)
        emit({"event": event, "lwar_id": args.lwar_id, **outcome})
        return 0 if outcome["accepted"] else 2
    if args.instance_id or args.generation is not None:
        raise SystemExit("--instance-id and --generation are valid only with --reap-startup")
    if args.delivery_timeout <= 0:
        raise SystemExit("--delivery-timeout must be positive")
    transport = FileTransport(root)
    ledger = TaskLedger(root)
    targets = [args.lwar_id] if args.lwar_id else transport.list_lwar_ids()
    recovered = []
    dead_lettered = []
    failed_reconciled = []
    publication_repaired = []
    incoming_expired = []
    now = datetime.now(timezone.utc)

    registry = RegistryService(root).load_registry()

    # Repair interrupted publication and requeue transitions before sweeping
    # leases. Transitional ledger states are durable outbox markers.
    for entry in ledger.all_entries():
        lwar_id = entry.get("lwar_id")
        if not lwar_id or (args.lwar_id and lwar_id != args.lwar_id):
            continue
        task_id = entry.get("task_id")
        workflow_id = entry.get("workflow_id")
        status = entry.get("status")
        if status in {"publishing", "published"}:
            task = entry.get("task_contract")
            if not isinstance(task, dict):
                continue
            if transport.task_pending(lwar_id, task_id):
                if status == "publishing":
                    ledger.transition(task_id, "published", workflow_id, detail="publication_reconciled")
                continue
            if transport.result_exists(lwar_id, task_id):
                continue
            slot = registry.get("slots", {}).get(lwar_id)
            if (
                slot is None
                or slot.get("state") != "on"
                or slot.get("instance_id") != task.get("instance_id")
                or slot.get("generation") != task.get("generation")
            ):
                ledger.transition(
                    task_id,
                    "failed",
                    workflow_id,
                    detail="publication_target_unavailable",
                )
                failed_reconciled.append(
                    {"lwar_id": lwar_id, "task_id": task_id, "reason": "publication_target_unavailable"}
                )
                continue
            target = transport.publish_task(task)
            ledger.transition(
                task_id,
                "published",
                workflow_id,
                detail="publication_repaired",
                message_file=str(target),
            )
            publication_repaired.append({"lwar_id": lwar_id, "task_id": task_id})
        elif status == "requeueing":
            pending = transport.find_pending_task(lwar_id, task_id)
            if pending is None:
                dead = transport.find_dead_task(lwar_id, task_id)
                if dead is not None:
                    desired_attempt = int(entry.get("attempt", dead[1].get("attempt", 1)))
                    repaired_task = transport.requeue_dead(lwar_id, task_id, desired_attempt)
                    if repaired_task is not None:
                        ledger.transition(
                            task_id,
                            "requeued",
                            workflow_id,
                            detail="dead_requeue_reconciled",
                            attempt=desired_attempt,
                        )
                        recovered.append(
                            {"lwar_id": lwar_id, "task_id": task_id, "attempt": desired_attempt}
                        )
                        continue
            if pending is None:
                if not transport.result_exists(lwar_id, task_id):
                    ledger.transition(
                        task_id,
                        "failed",
                        workflow_id,
                        detail="requeue_state_lost",
                    )
                    failed_reconciled.append(
                        {"lwar_id": lwar_id, "task_id": task_id, "reason": "requeue_state_lost"}
                    )
                continue
            pending_path, pending_task = pending
            desired_attempt = int(entry.get("attempt", pending_task.get("attempt", 1)))
            if pending_path.parent.name == "incoming" and int(pending_task.get("attempt", 1)) != desired_attempt:
                pending_task["attempt"] = desired_attempt
                atomic_write_json(pending_path, pending_task)
            elif pending_path.parent.name == "claimed":
                # A new claimant won the race. Its claim token is canonical;
                # align the ledger to the attempt actually being executed.
                desired_attempt = int(pending_task.get("attempt", desired_attempt))
            ledger.transition(
                task_id,
                "requeued",
                workflow_id,
                detail="requeue_reconciled",
                attempt=desired_attempt,
            )

    def recover_one(lwar_id: str, claimed_path: Path, task: dict[str, Any], detail: str) -> None:
        """Requeue or dead-letter one claimed task, from either an expired lease
        or an orphaned (lease-less) claim. Retires a leftover claim silently if a
        result was already submitted, so recovery never duplicates execution."""
        task_id = task.get("task_id")
        if task_id and transport.result_exists(lwar_id, task_id):
            claimed_path.unlink(missing_ok=True)
            return
        entry = ledger.get(task_id, task.get("workflow_id"))
        task_attempt = int(task.get("attempt", 1))
        if entry and entry.get("status") == "requeueing" and int(entry.get("attempt", 0)) > task_attempt:
            attempt = int(entry["attempt"])
        else:
            attempt = max(task_attempt, int((entry or {}).get("attempt", task_attempt))) + 1
        max_retries = int(task.get("max_retries", 3))
        # The interrupted terminal is recorded by OA, never inferred as a
        # submitted result: the LWAR may have died without submitting.
        interruption = {
            "status": "interrupted",
            "reason": detail,
            "recorded_by": "oa_reconciler",
            "recorded_at": utc_now(),
        }
        if attempt > max_retries:
            ledger.transition(
                task["task_id"],
                "dead_lettering",
                workflow_id=task.get("workflow_id"),
                detail="retry_budget_exhausted",
                attempt=attempt,
                interruption=interruption,
            )
            task["attempt"] = attempt
            if transport.dead_letter(lwar_id, claimed_path, task, "retry_budget_exhausted") is None:
                # A racing submit_result archived the claim first — superseded.
                return
            ledger.transition(
                task["task_id"],
                "dead",
                workflow_id=task.get("workflow_id"),
                detail="retry_budget_exhausted",
                attempt=attempt,
                interruption=interruption,
            )
            dead_lettered.append({"lwar_id": lwar_id, "task_id": task["task_id"], "attempt": attempt})
            return
        ledger.transition(
            task["task_id"],
            "requeueing",
            workflow_id=task.get("workflow_id"),
            detail=detail,
            attempt=attempt,
            interruption=interruption,
        )
        task["attempt"] = attempt
        moved = transport.requeue_claimed(lwar_id, claimed_path, task)
        if moved is not None:
            ledger.transition(
                task["task_id"],
                "requeued",
                workflow_id=task.get("workflow_id"),
                detail=detail,
                attempt=attempt,
                interruption=interruption,
            )
            recovered.append({"lwar_id": lwar_id, "task_id": task["task_id"], "attempt": attempt})

    for lwar_id in targets:
        for incoming_path, task in transport.expired_incoming(lwar_id, now, args.delivery_timeout):
            task_id = task.get("task_id")
            if not task_id or transport.result_exists(lwar_id, task_id):
                continue
            if transport.dead_letter(
                lwar_id, incoming_path, task, "delivery_timeout_unclaimed"
            ) is None:
                continue
            ledger.transition(
                task_id,
                "dead",
                workflow_id=task.get("workflow_id"),
                detail="delivery_timeout_unclaimed",
            )
            item = {"lwar_id": lwar_id, "task_id": task_id, "reason": "delivery_timeout_unclaimed"}
            incoming_expired.append(item)
            dead_lettered.append(item)
        for lease_path, lease in transport.expired_leases(lwar_id, now):
            if transport.result_exists(lwar_id, lease["task_id"]):
                lease_path.unlink(missing_ok=True)
                continue
            claimed = transport.claimed_task_for_lease(lwar_id, lease)
            if claimed is None:
                lease_path.unlink(missing_ok=True)
                continue
            recover_one(lwar_id, claimed[0], claimed[1], "lease_expired")
            lease_path.unlink(missing_ok=True)
        # A claim whose lease was never written (crash/disk fault between the
        # atomic claim-move and the lease write) is invisible to lease recovery
        # and would sit in claimed/ forever — sweep those orphans too.
        for claimed_path, task in transport.orphaned_claims(lwar_id, now):
            recover_one(lwar_id, claimed_path, task, "orphaned_claim_no_lease")
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
        # Dead-lettered tasks whose ledger entry is still non-terminal: no other
        # pass reconciles dead/, so a crash between dead_letter and the ledger
        # transition would otherwise leave the entry stuck non-terminal forever.
        for _dead_path, task in transport.list_dead(lwar_id):
            task_id = task.get("task_id")
            if not task_id:
                continue
            entry = ledger.get(task_id, task.get("workflow_id"))
            if entry is None or entry.get("status") in {"dead", "completed", "failed"}:
                continue
            ledger.transition(
                task_id,
                "dead",
                workflow_id=task.get("workflow_id"),
                detail="dead_letter_reconciled",
                attempt=entry.get("attempt"),
            )
            dead_lettered.append({"lwar_id": lwar_id, "task_id": task_id, "attempt": entry.get("attempt")})
    audit.record(
        root,
        "oa",
        {
            "event": "stale_leases_recovered",
            "count": len(recovered),
            "dead_lettered": len(dead_lettered),
            "failed_reconciled": len(failed_reconciled),
            "publication_repaired": len(publication_repaired),
            "incoming_expired": len(incoming_expired),
        },
    )
    emit(
        {
            "event": "stale_leases_recovered",
            "count": len(recovered),
            "tasks": recovered,
            "dead_lettered": dead_lettered,
            "failed_reconciled": failed_reconciled,
            "publication_repaired": publication_repaired,
            "incoming_expired": incoming_expired,
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
        runtime = classify_lwar_runtime(
            slot,
            heartbeat,
            now,
            args.stale_after,
            args.startup_deadline,
        )
        states.append(
            {
                "lwar_id": lwar_id,
                "instance_id": slot["instance_id"],
                "generation": slot["generation"],
                "state": slot["state"],
                "profile": slot["profile"],
                "heartbeat": heartbeat,
                "heartbeat_age_s": round(age, 3) if age is not None else None,
                **{
                    key: round(value, 3) if key == "startup_age_s" and value is not None else value
                    for key, value in runtime.items()
                },
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
        task_id = validate_task_id(args.requeue)
        found = transport.find_dead_task(args.lwar_id, task_id)
        if found is None:
            raise SystemExit(f"dead task not found: {args.requeue}")
        dead_task = found[1]
        entry = ledger.get(task_id, dead_task.get("workflow_id"))
        attempt = max(
            int(dead_task.get("attempt", 1)),
            int((entry or {}).get("attempt", dead_task.get("attempt", 1))),
        ) + 1
        ledger.transition(
            task_id,
            "requeueing",
            workflow_id=dead_task.get("workflow_id"),
            detail="manual_requeue",
            attempt=attempt,
        )
        task = transport.requeue_dead(args.lwar_id, task_id, attempt)
        if task is None:
            raise SystemExit(f"dead task requeue interrupted; run oa recover: {args.requeue}")
        ledger.transition(
            task["task_id"],
            "requeued",
            workflow_id=task.get("workflow_id"),
            detail="manual_requeue",
            attempt=int(task.get("attempt", 1)),
        )
        audit.record(
            root,
            "oa",
            {"event": "dead_requeued", "lwar_id": args.lwar_id, "task_id": task["task_id"]},
        )
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
    semantic_verdict = args.decision or "undecidable"
    criterion_verdict = "passed" if semantic_verdict == "accepted" else "manual_check_required"
    criteria = [
        {"criterion": criterion, "verdict": criterion_verdict}
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
        if semantic_verdict == "accepted" and not mechanical_pass:
            raise SystemExit("cannot record accepted: mechanical validation requires attention")
        decided_by = os.environ.get("PAO_OA_ID", "").strip()
        if not decided_by:
            raise SystemExit(
                "PAO_OA_ID is required for validate --record; set one unique id per OA session"
            )
        decision = {
            "schema_version": "pao.validation-decision.v1",
            "verdict": verdict,
            "semantic_verdict": semantic_verdict,
            "reason": args.reason,
            "checks": checks,
            "criteria": criteria,
            "artifact_verification": verification,
            "decided_by": decided_by,
            "decided_at": utc_now(),
        }
        validate_contract(decision, "validation-decision.schema.json")
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
            "semantic_verdict": semantic_verdict,
            "recorded": bool(args.record),
            "verdict": verdict,
        }
    )
    return 0


def command_prune(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    ensure_oa_writer(root)
    transport = FileTransport(root)
    ledger = TaskLedger(root)
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
    referenced = ledger.referenced_artifacts()
    artifact_removed = 0
    artifact_store = root / "var" / "artifacts"
    if artifact_store.is_dir():
        for path in sorted(artifact_store.glob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative in referenced:
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except FileNotFoundError:
                continue
            if modified <= cutoff:
                path.unlink(missing_ok=True)
                artifact_removed += 1
    audit_removed = audit.prune_rotated(root, cutoff)
    total += artifact_removed + audit_removed
    audit.record(
        root,
        "oa",
        {
            "event": "pruned",
            "total": total,
            "artifact_removed": artifact_removed,
            "audit_segments_removed": audit_removed,
        },
    )
    emit(
        {
            "event": "pruned",
            "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
            "counts": counts,
            "total": total,
            "artifact_removed": artifact_removed,
            "audit_segments_removed": audit_removed,
        }
    )
    return 0


def command_audit_health(args: argparse.Namespace) -> int:
    report = audit.health(resolve_root(args.root))
    emit(report)
    return 2 if report["status"] == "blocked" else 0


def command_audit_repair(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    try:
        report = audit.repair(
            root,
            segment=args.segment,
            expected_sha256=args.expected_sha256,
            drop_lines=args.drop_lines,
        )
    except (OSError, TimeoutError, ValueError) as error:
        raise SystemExit(f"audit repair refused: {error}")
    audit_committed = audit.record_once(
        root,
        "oa",
        {
            "event": "audit_repair_committed",
            "segment": report["segment"],
            "original_sha256": report["original_sha256"],
            "repaired_sha256": report["repaired_sha256"],
            "dropped_lines": report["dropped_lines"],
            "backup": report["backup"],
        },
        f"audit-repair:{report['segment']}:{report['original_sha256']}",
    )
    current_health = audit.health(root)
    report.update(
        {
            "audit_event_committed": audit_committed,
            "health_status": current_health["status"],
            "keyed_append_blocked": current_health["keyed_append_blocked"],
            "blocked_replay": current_health["blocked_replay"],
            "pending_count": current_health["pending_count"],
        }
    )
    emit(report)
    return 2 if current_health["status"] == "blocked" else 0


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

    presence = subparsers.add_parser("presence")
    presence.add_argument("--root", default=None)
    presence.add_argument("--ttl", type=float, default=90.0)
    presence.set_defaults(handler=writer_guard(command_presence))

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--root", default=None)
    reconcile.add_argument("--tombstone-retention", type=int, default=300)
    reconcile.set_defaults(handler=writer_guard(command_reconcile))

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
    send.set_defaults(handler=writer_guard(command_send))

    control = subparsers.add_parser("control")
    control.add_argument("--lwar-id", required=True)
    control.add_argument(
        "--command", required=True, choices=("shutdown", "retire", "ping", "cancel", "drain")
    )
    control.add_argument("--task-id")
    control.add_argument("--reason")
    control.add_argument("--root", default=None)
    control.set_defaults(handler=writer_guard(command_control))

    collect = subparsers.add_parser("collect")
    collect.add_argument("--lwar-id")
    collect.add_argument("--archive", action="store_true")
    collect.add_argument("--root", default=None)
    collect.set_defaults(handler=writer_guard(command_collect))

    recover = subparsers.add_parser("recover")
    recover.add_argument("--lwar-id")
    recover.add_argument(
        "--reap-startup",
        action="store_true",
        help="reclaim one deadline-missed starting slot using exact identity fencing",
    )
    recover.add_argument("--instance-id")
    recover.add_argument("--generation", type=int)
    recover.add_argument("--startup-deadline", type=float, default=STARTUP_DEADLINE_S_DEFAULT)
    recover.add_argument(
        "--delivery-timeout",
        type=float,
        default=300.0,
        help="dead-letter incoming tasks not claimed within this many seconds",
    )
    recover.add_argument("--root", default=None)
    recover.set_defaults(handler=writer_guard(command_recover))

    status = subparsers.add_parser("status")
    status.add_argument("--root", default=None)
    status.add_argument("--stale-after", type=float, default=STALE_AFTER_S_DEFAULT)
    status.add_argument("--startup-deadline", type=float, default=STARTUP_DEADLINE_S_DEFAULT)
    status.set_defaults(handler=command_status)

    dead = subparsers.add_parser("dead")
    dead.add_argument("--lwar-id")
    dead.add_argument("--requeue", metavar="TASK_ID")
    dead.add_argument("--root", default=None)
    dead.set_defaults(handler=conditional_writer_guard(command_dead, lambda args: bool(args.requeue)))

    validate = subparsers.add_parser("validate")
    validate.add_argument("--task-id", required=True)
    validate.add_argument("--workflow-id")
    validate.add_argument(
        "--record",
        action="store_true",
        help="persist the decision into the task ledger (mutating: takes the writer lease)",
    )
    validate.add_argument(
        "--decision",
        choices=("accepted", "rejected", "undecidable"),
        help="OA semantic decision to persist with --record (default: undecidable)",
    )
    validate.add_argument("--reason", help="reason for the semantic decision")
    validate.add_argument("--root", default=None)
    validate.set_defaults(
        handler=conditional_writer_guard(command_validate, lambda args: bool(args.record))
    )

    prune = subparsers.add_parser("prune")
    prune.add_argument("--older-than-days", type=float, required=True)
    prune.add_argument("--lwar-id")
    prune.add_argument("--root", default=None)
    prune.set_defaults(handler=writer_guard(command_prune))

    audit_health = subparsers.add_parser("audit-health")
    audit_health.add_argument("--root", default=None)
    audit_health.set_defaults(handler=command_audit_health)

    audit_repair = subparsers.add_parser("audit-repair")
    audit_repair.add_argument("--segment", required=True)
    audit_repair.add_argument("--expected-sha256", required=True)
    audit_repair.add_argument(
        "--drop-line",
        dest="drop_lines",
        action="append",
        type=int,
        required=True,
        help="malformed 1-based line to remove (repeat for every malformed line)",
    )
    audit_repair.add_argument("--root", default=None)
    audit_repair.set_defaults(handler=writer_guard(command_audit_repair))

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
