from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from . import audit
from .common import emit, load_json, resolve_root
from .transport import FileTransport


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


def watch(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    transport = FileTransport(root)
    identity_path = Path(args.identity_file).resolve()
    try:
        identity, slot = load_verified_identity(root, identity_path)
    except Exception as error:
        audit.record(root, "adp", {"event": "adp_error", "error": str(error)})
        emit({"event": "adp_error", "error": str(error), "action": "stop"})
        return 30
    deadline = time.monotonic() + args.timeout

    while time.monotonic() < deadline:
        try:
            identity, slot = load_verified_identity(root, identity_path)
        except Exception as error:
            audit.record(root, "adp", {"event": "adp_error", "error": str(error)})
            emit({"event": "adp_error", "error": str(error), "action": "stop"})
            return 30

        control = transport.claim_control(identity)
        if control is not None:
            transport.write_heartbeat(identity, "control", None)
            audit.record(
                root,
                "adp",
                {
                    "event": "control",
                    "lwar_id": identity["lwar_id"],
                    "command": control.get("command"),
                    "control_id": control.get("control_id"),
                },
            )
            emit({"event": "control", "command": control.get("command"), "message": control})
            return 20

        transport.write_heartbeat(identity, "watching" if slot["state"] == "on" else slot["state"], None)
        if slot["state"] == "on":
            claimed = transport.claim_task(identity, args.lease_seconds)
            if claimed is not None:
                task, claimed_path = claimed
                transport.write_heartbeat(identity, "running", task["task_id"])
                audit.record(
                    root,
                    "adp",
                    {"event": "task_received", "lwar_id": identity["lwar_id"], "task_id": task["task_id"]},
                )
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

    transport.write_heartbeat(identity, "idle" if slot["state"] == "on" else slot["state"], None)
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
    parser.add_argument("--root", default=None)
    parser.add_argument("--interval", type=float, default=5.0)
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
