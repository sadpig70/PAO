from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .common import (
    atomic_write_json,
    claim_file,
    emit,
    ensure_mailbox,
    load_json,
    utc_now,
)


def load_verified_identity(root: Path, identity_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    identity = load_json(identity_path)
    registry_path = root / "var" / "registry" / "lwar_registry.json"
    if not registry_path.is_file():
        raise ValueError("dynamic registry does not exist")
    registry = load_json(registry_path)
    slot = registry.get("slots", {}).get(identity["lwar_id"])
    if slot is None:
        raise ValueError("LWAR is not registered")
    if slot["instance_id"] != identity["instance_id"] or slot["generation"] != identity["generation"]:
        raise ValueError("LWAR identity tuple does not match registry")
    return identity, slot


def heartbeat(mailbox: Path, identity: dict[str, Any], status: str, task_id: str | None) -> None:
    atomic_write_json(
        mailbox / "heartbeat.json",
        {
            "schema_version": "pao.heartbeat.v1",
            "lwar_id": identity["lwar_id"],
            "instance_id": identity["instance_id"],
            "generation": identity["generation"],
            "status": status,
            "current_task_id": task_id,
            "last_seen": utc_now(),
        },
    )


def claim_control(mailbox: Path, identity: dict[str, Any]) -> dict[str, Any] | None:
    for source in sorted((mailbox / "control").glob("*.json")):
        destination = mailbox / "control_claimed" / source.name
        if not claim_file(source, destination):
            continue
        try:
            message = load_json(destination)
        except Exception as error:
            failed = mailbox / "failed" / f"control_{destination.name}"
            destination.replace(failed)
            atomic_write_json(
                failed.with_suffix(".error.json"),
                {"reason": f"invalid_control_json:{error}", "failed_at": utc_now()},
            )
            continue
        if (
            message.get("lwar_id") != identity["lwar_id"]
            or message.get("instance_id") != identity["instance_id"]
            or message.get("generation") != identity["generation"]
        ):
            failed = mailbox / "failed" / f"control_{destination.name}"
            destination.replace(failed)
            atomic_write_json(failed.with_suffix(".error.json"), {"reason": "stale_control_identity", "failed_at": utc_now()})
            continue
        if message.get("command") not in {"shutdown", "ping", "cancel", "drain"}:
            failed = mailbox / "failed" / f"control_{destination.name}"
            destination.replace(failed)
            atomic_write_json(failed.with_suffix(".error.json"), {"reason": "invalid_control_command", "failed_at": utc_now()})
            continue
        archive = mailbox / "archive" / "control" / destination.name
        archive.parent.mkdir(parents=True, exist_ok=True)
        destination.replace(archive)
        return message
    return None


def reject_task(mailbox: Path, source: Path, reason: str) -> None:
    failed = mailbox / "failed" / source.name
    if claim_file(source, failed):
        atomic_write_json(failed.with_suffix(".error.json"), {"reason": reason, "failed_at": utc_now()})


def claim_task(mailbox: Path, identity: dict[str, Any], lease_s: int) -> tuple[dict[str, Any], Path] | None:
    for source in sorted((mailbox / "incoming").glob("*.json")):
        try:
            task = load_json(source)
        except Exception as error:
            reject_task(mailbox, source, f"invalid_json:{error}")
            continue
        required = ("task_id", "lwar_id", "instance_id", "generation", "goal")
        if any(field not in task for field in required):
            reject_task(mailbox, source, "missing_required_field")
            continue
        if task["lwar_id"] != identity["lwar_id"]:
            reject_task(mailbox, source, "wrong_lwar_id")
            continue
        if task["instance_id"] != identity["instance_id"] or task["generation"] != identity["generation"]:
            reject_task(mailbox, source, "stale_identity")
            continue
        destination = mailbox / "claimed" / source.name
        if not claim_file(source, destination):
            continue
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_s)
        atomic_write_json(
            mailbox / "leases" / f"{task['task_id']}.json",
            {
                "schema_version": "pao.lease.v1",
                "task_id": task["task_id"],
                "lwar_id": identity["lwar_id"],
                "instance_id": identity["instance_id"],
                "generation": identity["generation"],
                "claimed_file": destination.name,
                "leased_at": utc_now(),
                "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
            },
        )
        return task, destination
    return None


def watch(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    identity_path = Path(args.identity_file).resolve()
    try:
        identity, slot = load_verified_identity(root, identity_path)
    except Exception as error:
        emit({"event": "adp_error", "error": str(error), "action": "stop"})
        return 30
    mailbox = ensure_mailbox(root, identity["lwar_id"])
    deadline = time.monotonic() + args.timeout

    while time.monotonic() < deadline:
        try:
            identity, slot = load_verified_identity(root, identity_path)
        except Exception as error:
            emit({"event": "adp_error", "error": str(error), "action": "stop"})
            return 30

        control = claim_control(mailbox, identity)
        if control is not None:
            heartbeat(mailbox, identity, "control", None)
            emit({"event": "control", "command": control.get("command"), "message": control})
            return 20

        heartbeat(mailbox, identity, "watching" if slot["state"] == "on" else slot["state"], None)
        if slot["state"] == "on":
            claimed = claim_task(mailbox, identity, args.lease_seconds)
            if claimed is not None:
                task, claimed_path = claimed
                heartbeat(mailbox, identity, "running", task["task_id"])
                emit(
                    {
                        "event": "task_received",
                        "lwar_id": identity["lwar_id"],
                        "task_id": task["task_id"],
                        "message_file": str(claimed_path),
                        "task": task,
                        "action": "execute_then_submit_result",
                    }
                )
                return 0
        time.sleep(args.interval)

    heartbeat(mailbox, identity, "idle" if slot["state"] == "on" else slot["state"], None)
    emit(
        {
            "event": "idle_timeout" if slot["state"] == "on" else "state_wait",
            "lwar_id": identity["lwar_id"],
            "state": slot["state"],
            "waited_s": args.timeout,
            "action": "watch_again",
        }
    )
    return 10


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adp-watch", description="One ADP mailbox watch slice")
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--lease-seconds", type=int, default=180)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.interval <= 0 or args.timeout <= 0 or args.lease_seconds <= 0:
        raise SystemExit("interval, timeout, and lease-seconds must be positive")
    return watch(args)


if __name__ == "__main__":
    raise SystemExit(main())
