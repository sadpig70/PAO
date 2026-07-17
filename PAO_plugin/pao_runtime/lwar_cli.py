from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from . import audit
from .common import (
    atomic_write_json,
    emit,
    load_json,
    new_id,
    resolve_root,
    utc_now,
    validate_instance_id,
    validate_lwar_id,
    validate_task_id,
)
from .transport import FileTransport


SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


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
    response = load_json(response_path)
    if response.get("accepted") is not True:
        emit({"event": "registration_rejected", **response})
        return 3
    instance_id = validate_instance_id(response["instance_id"])
    pending_path = root / "var" / "identities" / f"{instance_id}.pending.json"
    pending = load_json(pending_path) if pending_path.is_file() else {}
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
    identity = load_json(Path(args.identity_file).resolve())
    request_id = new_id("lwar-state")
    request = {
        "schema_version": "pao.lwar-lifecycle-request.v1",
        "request_id": request_id,
        "lwar_id": validate_lwar_id(identity["lwar_id"]),
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
    identity = load_json(identity_path)
    registry_path = root / "var" / "registry" / "lwar_registry.json"
    if not registry_path.is_file():
        emit({"event": "registry_unavailable"})
        return 2
    registry = load_json(registry_path)
    slot = registry.get("slots", {}).get(identity["lwar_id"])
    if not slot:
        emit({"event": "unregistered", "lwar_id": identity["lwar_id"]})
        return 3
    if slot["instance_id"] != identity["instance_id"] or slot["generation"] != identity["generation"]:
        emit({"event": "identity_mismatch", "lwar_id": identity["lwar_id"]})
        return 4
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


def command_complete(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    transport = FileTransport(root)
    identity = load_json(Path(args.identity_file).resolve())
    lwar_id = validate_lwar_id(identity["lwar_id"])
    task_id = validate_task_id(args.task_id)
    claimed_path, task = transport.find_claimed_task(lwar_id, task_id)
    if task["instance_id"] != identity["instance_id"] or task["generation"] != identity["generation"]:
        raise SystemExit("task identity does not match this LWAR identity")
    result = load_json(Path(args.result_file).resolve())
    for required in ("status", "summary", "evidence"):
        if required not in result:
            raise SystemExit(f"result missing required field: {required}")
    if result["status"] not in {"succeeded", "failed", "blocked", "cancelled"}:
        raise SystemExit("result status must be succeeded, failed, blocked, or cancelled")
    if not isinstance(result["evidence"], dict):
        raise SystemExit("result evidence must be an object")
    if not isinstance(result.get("artifacts", []), list):
        raise SystemExit("result artifacts must be an array")
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
        "artifacts": result.get("artifacts", []),
        "next_action": result.get("next_action", "validate"),
        "exit_code": result.get("exit_code", 0 if result["status"] == "succeeded" else 1),
        "error": result.get("error"),
        "submitted_at": utc_now(),
    }
    outgoing = transport.submit_result(identity, claimed_path, normalized)
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
