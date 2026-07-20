from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from . import __version__, audit
from .common import (
    atomic_write_json,
    emit,
    new_id,
    path_within,
    resolve_root,
    safe_load_json,
    snapshot_artifact,
    utc_now,
    validate_instance_id,
    validate_lwar_id,
    validate_task_id,
)
from .registry import ALLOWED_TRANSITIONS
from .transport import FileTransport


SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def _load_or_exit(path: Path, label: str) -> dict[str, Any]:
    """Read a JSON object, failing with a clean SystemExit (not a raw
    traceback) when the file is missing, truncated, or not an object."""
    data = safe_load_json(path)
    if data is None:
        raise SystemExit(f"cannot read or parse {label}: {path}")
    return data


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def slug(value: str) -> str:
    if not SLUG_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("value must be a lowercase slug")
    return value


def command_register(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    request_id = new_id("lwar-reg")
    instance_id = args.instance_id or new_id("lwar-instance")
    validate_instance_id(instance_id)
    requested_lwar_id = f"LWAR{args.number}" if args.number else None
    profile = {
        "runtime_name": args.runtime_name,
        "model": args.model,
        "adapter_id": args.adapter_id,
        "vendor_family": args.vendor_family,
        "interface": args.interface,
        "capabilities": sorted(set(args.capability)),
    }
    request = {
        "schema_version": "pao.lwar-registration-request.v1",
        "request_id": request_id,
        "instance_id": instance_id,
        "requested_lwar_id": requested_lwar_id,
        "allocation_mode": "explicit" if requested_lwar_id else "auto",
        "requested_state": "on",
        "profile": profile,
        "behavior_contract": "lwar-runtime.v2-adp",
        "runtime_version": __version__,
        "created_at": utc_now(),
    }
    request_path = root / "control" / "registration" / "requests" / f"{request_id}.json"
    pending_path = root / "var" / "identities" / f"{instance_id}.pending.json"
    atomic_write_json(request_path, request)
    atomic_write_json(pending_path, {"request_id": request_id, "instance_id": instance_id, "profile": profile})
    audit.record(
        root,
        "lwar",
        {"event": "registration_requested", "request_id": request_id, "instance_id": instance_id},
    )
    emit(
        {
            "event": "registration_requested",
            "request_id": request_id,
            "instance_id": instance_id,
            "requested_lwar_id": requested_lwar_id or "auto",
            "request_file": str(request_path),
            "next_action": "wait_for_oa_reconcile",
        }
    )
    return 0


def command_response(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    response_path = root / "control" / "registration" / "responses" / f"{args.request_id}.json"
    if not response_path.is_file():
        emit({"event": "registration_pending", "request_id": args.request_id})
        return 2
    response = _load_or_exit(response_path, "registration response")
    if response.get("accepted") is not True:
        emit({"event": "registration_rejected", **response})
        return 3
    instance_id = validate_instance_id(response["instance_id"])
    pending_path = root / "var" / "identities" / f"{instance_id}.pending.json"
    pending = safe_load_json(pending_path) if pending_path.is_file() else {}
    pending = pending or {}
    identity = {
        "schema_version": "pao.lwar-identity.v1",
        "lwar_id": response["lwar_id"],
        "instance_id": instance_id,
        "generation": response["generation"],
        "registry_version": response["registry_version"],
        "state": response["state"],
        "behavior_contract": response["behavior_contract"],
        "profile": pending.get("profile", {}),
        "adopted_at": utc_now(),
    }
    identity_path = root / "var" / "identities" / f"{instance_id}.json"
    atomic_write_json(identity_path, identity)
    pending_path.unlink(missing_ok=True)
    audit.record(
        root,
        "lwar",
        {"event": "identity_adopted", "lwar_id": identity["lwar_id"], "generation": identity["generation"]},
    )
    emit({"event": "identity_adopted", "identity_file": str(identity_path), **identity})
    return 0


def command_state(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    identity = _load_or_exit(Path(args.identity_file).resolve(), "identity file")
    lwar_id = validate_lwar_id(identity["lwar_id"])
    # Locally reject a definitively stale or illegal transition before writing
    # the request (OA reconcile is still authoritative). When the registry is
    # momentarily unreadable we cannot check, so we proceed and let OA decide.
    registry_path = root / "var" / "registry" / "lwar_registry.json"
    registry = safe_load_json(registry_path) if registry_path.is_file() else None
    if registry is not None:
        slot = registry.get("slots", {}).get(lwar_id)
        if slot is None:
            raise SystemExit(
                f"cannot request '{args.state}': {lwar_id} is not in the registry "
                "(register first, or run status)"
            )
        if slot.get("instance_id") != identity["instance_id"] or slot.get("generation") != identity["generation"]:
            raise SystemExit(
                f"cannot request '{args.state}': your identity is stale versus the registry "
                "(re-register from a fresh session)"
            )
        if args.state != slot["state"] and args.state not in ALLOWED_TRANSITIONS.get(slot["state"], set()):
            raise SystemExit(f"illegal lifecycle transition: {slot['state']} → {args.state}")
    request_id = new_id("lwar-state")
    request = {
        "schema_version": "pao.lwar-lifecycle-request.v1",
        "request_id": request_id,
        "lwar_id": lwar_id,
        "instance_id": validate_instance_id(identity["instance_id"]),
        "generation": identity["generation"],
        "registry_version": identity["registry_version"],
        "requested_state": args.state,
        "created_at": utc_now(),
    }
    request_path = root / "control" / "lifecycle" / "requests" / f"{request_id}.json"
    atomic_write_json(request_path, request)
    audit.record(
        root,
        "lwar",
        {"event": "lifecycle_requested", "lwar_id": request["lwar_id"], "requested_state": args.state},
    )
    emit({"event": "lifecycle_requested", "request_id": request_id, "requested_state": args.state})
    return 0


def command_status(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    identity_path = Path(args.identity_file).resolve()
    identity = _load_or_exit(identity_path, "identity file")
    registry_path = root / "var" / "registry" / "lwar_registry.json"
    if not registry_path.is_file():
        emit({"event": "registry_unavailable"})
        return 2
    registry = safe_load_json(registry_path)
    if registry is None:
        emit({"event": "registry_unavailable"})
        return 2
    slot = registry.get("slots", {}).get(identity["lwar_id"])
    if not slot:
        emit({"event": "unregistered", "lwar_id": identity["lwar_id"]})
        return 3
    if slot["instance_id"] != identity["instance_id"] or slot["generation"] != identity["generation"]:
        emit({"event": "identity_mismatch", "lwar_id": identity["lwar_id"]})
        return 4
    # Refresh the local snapshot, but never move registry_version BACKWARDS: a
    # concurrent status/re-adoption may already have written a fresher one, and
    # a stale reader must not clobber it.
    if registry["registry_version"] >= identity.get("registry_version", 0):
        identity["state"] = slot["state"]
        identity["registry_version"] = registry["registry_version"]
        atomic_write_json(identity_path, identity)
    emit(
        {
            "event": "lwar_status",
            "lwar_id": identity["lwar_id"],
            "state": slot["state"],
            "generation": slot["generation"],
            "registry_version": registry["registry_version"],
            "heartbeat": FileTransport(root).read_heartbeat(identity["lwar_id"]),
        }
    )
    return 0


def normalize_artifacts(
    root: Path, task: dict[str, Any], artifacts: list[Any]
) -> tuple[list[Any], list[str]]:
    """Resolve, bound-check, and snapshot declared artifacts.

    Every declared artifact must exist as a regular file. Bounds (cwd +
    permissions.write roots) are enforced when the task declares write roots;
    tasks published by pre-0.6 OAs (write=[]) get a string passthrough with a
    warning instead — the optional-first rollout pattern. Snapshots are
    content-addressed under var/artifacts/, so later verification never
    depends on the live workspace file.
    """
    cwd = Path(task.get("cwd", ".")).resolve()
    permissions = task.get("permissions") or {}
    write_roots = [Path(p) for p in permissions.get("write", []) if isinstance(p, str)]
    max_bytes = permissions.get("max_artifact_bytes")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        max_bytes = None
    store = root / "var" / "artifacts"
    entries: list[Any] = []
    warnings: list[str] = []
    for item in artifacts:
        raw = item.get("path") if isinstance(item, dict) else item
        if not isinstance(raw, str) or not raw:
            raise SystemExit("artifact entries must be non-empty path strings")
        resolved = (Path(raw) if os.path.isabs(raw) else cwd / raw).resolve()
        if not resolved.is_file():
            raise SystemExit(f"declared artifact is not a regular file: {resolved}")
        in_bounds = path_within(resolved, cwd) or any(
            path_within(resolved, write_root) for write_root in write_roots
        )
        if not in_bounds:
            if not write_roots:
                warnings.append(f"outside_declared_roots:{resolved}")
                entries.append(str(resolved))
                continue
            raise SystemExit(f"artifact outside allowed write roots: {resolved}")
        try:
            digest, size, snapshot = snapshot_artifact(resolved, store, max_bytes)
        except ValueError as error:
            raise SystemExit(str(error))
        entries.append(
            {
                "path": str(resolved),
                "sha256": digest,
                "size_bytes": size,
                "snapshot": snapshot.relative_to(root).as_posix(),
            }
        )
    return entries, warnings


def command_complete(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    transport = FileTransport(root)
    identity = _load_or_exit(Path(args.identity_file).resolve(), "identity file")
    lwar_id = validate_lwar_id(identity["lwar_id"])
    task_id = validate_task_id(args.task_id)
    try:
        claimed_path, task = transport.find_claimed_task(lwar_id, task_id)
    except FileNotFoundError:
        # Distinguish the three "no claim to complete" cases with a clean error
        # instead of a raw traceback: already submitted, superseded/requeued, or
        # never claimed / mistyped id.
        if transport.result_exists(lwar_id, task_id):
            raise SystemExit(f"task already has a submitted result (already completed): {task_id}")
        raise SystemExit(
            f"no claimed task to complete for {task_id} — it was superseded/requeued "
            "by OA recovery, or never claimed (check the task id)"
        )
    if task["instance_id"] != identity["instance_id"] or task["generation"] != identity["generation"]:
        raise SystemExit("task identity does not match this LWAR identity")
    result = _load_or_exit(Path(args.result_file).resolve(), "result file")
    for required in ("status", "summary", "evidence"):
        if required not in result:
            raise SystemExit(f"result missing required field: {required}")
    terminal_statuses = {
        "succeeded",
        "failed",
        "blocked",
        "cancelled",
        "interrupted",
        "timed_out",
        "protocol_error",
    }
    if result["status"] not in terminal_statuses:
        raise SystemExit(f"result status must be one of: {', '.join(sorted(terminal_statuses))}")
    if not isinstance(result["evidence"], dict):
        raise SystemExit("result evidence must be an object")
    if not isinstance(result.get("artifacts", []), list):
        raise SystemExit("result artifacts must be an array")
    artifacts, artifact_warnings = normalize_artifacts(root, task, result.get("artifacts", []))
    normalized = {
        "schema_version": "pao.result.v1",
        "task_id": task_id,
        "workflow_id": task.get("workflow_id"),
        "lwar_id": lwar_id,
        "instance_id": identity["instance_id"],
        "generation": identity["generation"],
        "registry_version": identity["registry_version"],
        "status": result["status"],
        "summary": result["summary"],
        "evidence": result["evidence"],
        "artifacts": artifacts,
        "next_action": result.get("next_action", "validate"),
        "exit_code": result.get("exit_code", 0 if result["status"] == "succeeded" else 1),
        "error": result.get("error"),
        # Fencing echo: both come from the claimed task file the bus wrote,
        # never from the caller's result draft.
        "attempt": int(task.get("attempt", 1)),
        "claim_token": task.get("claim_token"),
        "submitted_at": utc_now(),
    }
    if artifact_warnings:
        normalized["artifact_warnings"] = artifact_warnings
    try:
        outgoing = transport.submit_result(identity, claimed_path, normalized)
    except RuntimeError as error:
        audit.record(root, "lwar", {"event": "result_superseded", "lwar_id": lwar_id, "task_id": task_id})
        raise SystemExit(str(error))
    transport.write_heartbeat(identity, "idle", None)
    audit.record(
        root,
        "lwar",
        {"event": "result_submitted", "lwar_id": lwar_id, "task_id": task_id, "status": normalized["status"]},
    )
    emit({"event": "result_submitted", "task_id": task_id, "result_file": str(outgoing), "action": "watch_again"})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lwar", description="LWAR registration, lifecycle, and ADP result tool")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register")
    register.add_argument("number", nargs="?", type=positive_int)
    register.add_argument("--runtime-name", required=True)
    register.add_argument("--model", required=True)
    register.add_argument("--adapter-id", required=True, type=slug)
    register.add_argument("--vendor-family", required=True, type=slug)
    register.add_argument("--interface", required=True, choices=("cli", "tui", "agent", "build"))
    register.add_argument("--capability", action="append", default=[], type=slug)
    register.add_argument("--instance-id")
    register.add_argument("--root", default=None)
    register.set_defaults(handler=command_register)

    response = subparsers.add_parser("response")
    response.add_argument("request_id")
    response.add_argument("--root", default=None)
    response.set_defaults(handler=command_response)

    state = subparsers.add_parser("state")
    state.add_argument("state", choices=("on", "draining", "off", "deregistered"))
    state.add_argument("--identity-file", required=True)
    state.add_argument("--root", default=None)
    state.set_defaults(handler=command_state)

    status = subparsers.add_parser("status")
    status.add_argument("--identity-file", required=True)
    status.add_argument("--root", default=None)
    status.set_defaults(handler=command_status)

    complete = subparsers.add_parser("complete")
    complete.add_argument("--identity-file", required=True)
    complete.add_argument("--task-id", required=True)
    complete.add_argument("--result-file", required=True)
    complete.add_argument("--root", default=None)
    complete.set_defaults(handler=command_complete)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
