from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import (
    atomic_write_json,
    emit,
    ensure_mailbox,
    load_json,
    mailbox_root,
    new_id,
    parse_utc,
    utc_now,
    validate_lwar_id,
    validate_task_id,
)
from .registry import RegistryService


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
    service = RegistryService(Path(args.root), tombstone_retention_s=args.tombstone_retention)
    counts = service.reconcile()
    emit({"event": "oa_reconcile_complete", **counts})
    return 0


def command_send(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    registry, slot = load_active_slot(root, args.lwar_id, require_on=True)
    source = load_json(Path(args.task_file).resolve())
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
    if not isinstance(source.get("permissions", {}), dict):
        raise SystemExit("permissions must be an object")
    task = {
        "schema_version": "pao.task.v1",
        "task_id": task_id,
        "workflow_id": source.get("workflow_id") or new_id("workflow"),
        "parent_task_id": source.get("parent_task_id"),
        "lwar_id": args.lwar_id,
        "instance_id": slot["instance_id"],
        "generation": slot["generation"],
        "registry_version": registry["registry_version"],
        "role": source.get("role", "worker"),
        "goal": source["goal"],
        "instructions": source.get("instructions", source["goal"]),
        "completion_criteria": source.get("completion_criteria", []),
        "cwd": source.get("cwd", str(root)),
        "input_files": source.get("input_files", []),
        "expected_output": source.get("expected_output", "ResultContract"),
        "timeout_s": timeout_s,
        "max_retries": int(source.get("max_retries", 3)),
        "priority": priority,
        "permissions": source.get(
            "permissions",
            {"read": [str(root)], "write": [], "network": False},
        ),
        "adapter_options": source.get("adapter_options", {}),
        "attempt": int(source.get("attempt", 1)),
        "created_at": utc_now(),
    }
    mailbox = ensure_mailbox(root, args.lwar_id)
    target = mailbox / "incoming" / f"{priority:03d}_{task_id}.json"
    if target.exists() or any(path.name.endswith(f"_{task_id}.json") for path in (mailbox / "claimed").glob("*.json")):
        raise SystemExit(f"task already exists for {args.lwar_id}: {task_id}")
    atomic_write_json(target, task)
    emit({"event": "task_published", "lwar_id": args.lwar_id, "task_id": task_id, "message_file": str(target)})
    return 0


def command_control(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
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
    mailbox = ensure_mailbox(root, args.lwar_id)
    path = mailbox / "control" / f"{control_id}.json"
    atomic_write_json(path, message)
    emit({"event": "control_published", "lwar_id": args.lwar_id, "command": args.command, "message_file": str(path)})
    return 0


def command_collect(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    targets = [args.lwar_id] if args.lwar_id else [path.name for path in sorted((root / "mailbox").glob("LWAR*")) if path.is_dir()]
    collected = []
    for lwar_id in targets:
        mailbox = mailbox_root(root, lwar_id)
        for path in sorted((mailbox / "outgoing").glob("*.json")):
            result = load_json(path)
            collected.append({"lwar_id": lwar_id, "result_file": str(path), "result": result})
            if args.archive:
                destination = mailbox / "archive" / "results" / path.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                path.replace(destination)
    emit({"event": "results_collected", "count": len(collected), "results": collected})
    return 0


def command_recover(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    targets = [args.lwar_id] if args.lwar_id else [path.name for path in sorted((root / "mailbox").glob("LWAR*")) if path.is_dir()]
    recovered = []
    now = datetime.now(timezone.utc)
    for lwar_id in targets:
        mailbox = mailbox_root(root, lwar_id)
        for lease_path in sorted((mailbox / "leases").glob("*.json")):
            lease = load_json(lease_path)
            if parse_utc(lease["expires_at"]) > now:
                continue
            if (mailbox / "outgoing" / f"{lease['task_id']}.result.json").exists():
                lease_path.unlink(missing_ok=True)
                continue
            claimed = mailbox / "claimed" / lease["claimed_file"]
            if claimed.is_file():
                incoming = mailbox / "incoming" / claimed.name
                if not incoming.exists():
                    claimed.replace(incoming)
                    recovered.append({"lwar_id": lwar_id, "task_id": lease["task_id"]})
            lease_path.unlink(missing_ok=True)
    emit({"event": "stale_leases_recovered", "count": len(recovered), "tasks": recovered})
    return 0


def command_status(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    service = RegistryService(root)
    registry = service.load_registry()
    states = []
    for lwar_id, slot in sorted(registry["slots"].items()):
        heartbeat_path = mailbox_root(root, lwar_id) / "heartbeat.json"
        states.append(
            {
                "lwar_id": lwar_id,
                "instance_id": slot["instance_id"],
                "generation": slot["generation"],
                "state": slot["state"],
                "profile": slot["profile"],
                "heartbeat": load_json(heartbeat_path) if heartbeat_path.is_file() else None,
            }
        )
    emit({"event": "oa_status", "registry_version": registry["registry_version"], "lwars": states})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oa", description="OA control tool for PAO ADP file bus")
    subparsers = parser.add_subparsers(dest="command", required=True)

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--root", default=".")
    reconcile.add_argument("--tombstone-retention", type=int, default=300)
    reconcile.set_defaults(handler=command_reconcile)

    send = subparsers.add_parser("send")
    send.add_argument("--lwar-id", required=True)
    send.add_argument("--task-file", required=True)
    send.add_argument("--root", default=".")
    send.set_defaults(handler=command_send)

    control = subparsers.add_parser("control")
    control.add_argument("--lwar-id", required=True)
    control.add_argument("--command", required=True, choices=("shutdown", "ping", "cancel", "drain"))
    control.add_argument("--task-id")
    control.add_argument("--reason")
    control.add_argument("--root", default=".")
    control.set_defaults(handler=command_control)

    collect = subparsers.add_parser("collect")
    collect.add_argument("--lwar-id")
    collect.add_argument("--archive", action="store_true")
    collect.add_argument("--root", default=".")
    collect.set_defaults(handler=command_collect)

    recover = subparsers.add_parser("recover")
    recover.add_argument("--lwar-id")
    recover.add_argument("--root", default=".")
    recover.set_defaults(handler=command_recover)

    status = subparsers.add_parser("status")
    status.add_argument("--root", default=".")
    status.set_defaults(handler=command_status)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
