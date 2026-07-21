from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from . import audit
from .common import emit, load_json, require_local_filesystem, resolve_identity_root, resolve_root
from .contracts import validate_contract
from .transport import FileTransport


def load_verified_identity(root: Path, identity_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    identity = load_json(identity_path)
    validate_contract(identity, "identity.schema.json")
    registry_path = root / "var" / "registry" / "lwar_registry.json"
    if not registry_path.is_file():
        # A missing --root/PAO_ROOT resolves to the cwd and lands here looking
        # like a bus fault — name the resolved root so the trap is visible.
        raise ValueError(
            f"dynamic registry does not exist under {root} — "
            "verify --root (or PAO_ROOT) points at the bus root"
        )
    registry = load_json(registry_path)
    validate_contract(registry, "registry-state.schema.json")
    slot = registry.get("slots", {}).get(identity["lwar_id"])
    if slot is None:
        raise ValueError("LWAR is not registered")
    if slot["instance_id"] != identity["instance_id"] or slot["generation"] != identity["generation"]:
        raise ValueError("LWAR identity tuple does not match registry")
    return identity, slot


def validate_watch_args(args: argparse.Namespace) -> None:
    """Validate watcher timing arguments for both CLI entry paths."""
    if args.interval <= 0 or args.timeout <= 0 or args.lease_seconds <= 0:
        raise SystemExit("interval, timeout, and lease-seconds must be positive")
    if args.interval > args.timeout:
        raise SystemExit("--interval must be <= --timeout (a longer interval would overshoot the slice)")
    if args.state_wait_backoff_max is not None and args.state_wait_backoff_max < args.interval:
        raise SystemExit("--state-wait-backoff-max must be >= --interval")


def watch(args: argparse.Namespace) -> int:
    root = resolve_root(args.root)
    identity_path = Path(args.identity_file).resolve()
    audit_root: Path | None = None
    try:
        identity_snapshot = load_json(identity_path)
        validate_contract(identity_snapshot, "identity.schema.json")
        root = resolve_identity_root(identity_snapshot, identity_path, args.root)
        require_local_filesystem(root)
        audit_root = root
        transport = FileTransport(root)
        identity, slot = load_verified_identity(root, identity_path)
    except Exception as error:
        # Never write an error record to an explicit/env root that conflicts
        # with the adopted identity. Until root binding succeeds, stdout is the
        # only safe error channel.
        if audit_root is not None:
            audit.record(audit_root, "adp", {"event": "adp_error", "error": str(error)})
        emit(
            {
                "event": "adp_error",
                "error": str(error),
                "identity_file": str(identity_path),
                "action": "stop",
            }
        )
        return 30
    deadline = time.monotonic() + args.timeout
    wait_s = args.interval
    consecutive_errors = 0

    while True:
        now = time.monotonic()
        if now >= deadline:
            if args.resident:
                # Cross the idle slice boundary inside the same watcher
                # process. The next poll re-verifies identity/registry state
                # and refreshes heartbeat, so agent scheduling latency cannot
                # turn a live resident session into a stale LWAR.
                deadline = now + args.timeout
                wait_s = args.interval
                consecutive_errors = 0
                continue
            break
        try:
            identity, slot = load_verified_identity(root, identity_path)
        except Exception as error:
            # Identity no longer verifies (revoked, generation bump, gone) — a
            # genuinely fatal condition. Stop and report.
            audit.record(root, "adp", {"event": "adp_error", "error": str(error)})
            emit(
                {
                    "event": "adp_error",
                    "error": str(error),
                    "identity_file": str(identity_path),
                    "action": "stop",
                }
            )
            return 30

        try:
            control = transport.claim_control(identity)
            if control is not None:
                transport.write_heartbeat(identity, "control", None)
                # A cancel carrying a task_id is persisted as a tombstone BEFORE
                # the event reaches the agent, so cancellation of a not-yet-
                # claimed task no longer depends on the agent remembering it.
                if control.get("command") == "cancel" and control.get("task_id"):
                    transport.write_cancel_tombstone(
                        identity, control["task_id"], control.get("control_id")
                    )
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
                emit(
                    {
                        "event": "control",
                        "command": control.get("command"),
                        "identity_file": str(identity_path),
                        "message": control,
                    }
                )
                try:
                    transport.ack_control(identity, control)
                except (OSError, TimeoutError) as error:
                    # Delivery already reached stdout. Leave control_claimed in
                    # place for at-least-once redelivery on the next slice.
                    audit.record(
                        root,
                        "adp",
                        {"event": "control_ack_failed", "error": str(error), "control_id": control.get("control_id")},
                    )
                return 20

            transport.write_heartbeat(
                identity, "watching" if slot["state"] == "on" else slot["state"], None
            )
            if slot["state"] == "on":
                wait_s = args.interval
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
                            "identity_file": str(identity_path),
                            "message_file": str(claimed_path),
                            "task": task,
                            "action": "execute_then_submit_result",
                        }
                    )
                    return 0
            elif args.state_wait_backoff_max:
                # Bounded backoff while the slot is not `on`: doubles per poll up
                # to the cap, resets as soon as the state returns to `on`.
                wait_s = min(wait_s * 2, args.state_wait_backoff_max)
            consecutive_errors = 0
        except Exception as error:
            # A transient fault in one poll (a momentary sharing violation, a
            # file that vanished mid-read) must not crash the whole slice with an
            # uncatchable traceback the agent's loop cannot dispatch on. Retry
            # the next poll within this slice; only a run of consecutive failures
            # is treated as a fatal adp_error.
            consecutive_errors += 1
            audit.record(
                root,
                "adp",
                {"event": "adp_error", "error": str(error), "consecutive": consecutive_errors},
            )
            if consecutive_errors >= 3:
                emit(
                    {
                        "event": "adp_error",
                        "error": str(error),
                        "identity_file": str(identity_path),
                        "action": "stop",
                    }
                )
                return 30
            wait_s = args.interval

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        # Clamp the sleep to the slice deadline so a large --interval can never
        # overshoot the intended --timeout and starve control messages.
        time.sleep(min(wait_s if slot["state"] != "on" else args.interval, remaining))

    transport.write_heartbeat(identity, "idle" if slot["state"] == "on" else slot["state"], None)
    emit(
        {
            "event": "idle_timeout" if slot["state"] == "on" else "state_wait",
            "lwar_id": identity["lwar_id"],
            "identity_file": str(identity_path),
            "state": slot["state"],
            "waited_s": args.timeout,
            "action": "watch_again",
        }
    )
    return 10


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adp-watch", description="ADP mailbox watcher")
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--root", default=None)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--lease-seconds", type=int, default=180)
    parser.add_argument(
        "--resident",
        action="store_true",
        help=(
            "stay inside the watcher across idle slice boundaries; return only "
            "for a task, control event, or fatal error"
        ),
    )
    parser.add_argument(
        "--state-wait-backoff-max",
        type=float,
        default=None,
        help="cap for the doubling poll interval while the slot is not on (default: no backoff)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    validate_watch_args(args)
    return watch(args)


if __name__ == "__main__":
    raise SystemExit(main())
