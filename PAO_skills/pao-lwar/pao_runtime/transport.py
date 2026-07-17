from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .common import (
    atomic_write_json,
    claim_file,
    ensure_mailbox,
    load_json,
    mailbox_root,
    new_id,
    parse_utc,
    utc_now,
)


LEASE_MARGIN_S = 30

CONTROL_COMMANDS = {"shutdown", "ping", "cancel", "drain"}

PRUNE_CATEGORIES = (
    "archive/tasks",
    "archive/results",
    "archive/control",
    "failed",
    "quarantine",
)


def effective_lease_seconds(task: dict[str, Any], default_lease_s: int, margin_s: int = LEASE_MARGIN_S) -> int:
    """Align the claim lease with the task's own execution timeout."""
    try:
        timeout_s = int(task.get("timeout_s", 0))
    except (TypeError, ValueError):
        timeout_s = 0
    if timeout_s <= 0:
        return default_lease_s
    return max(default_lease_s, timeout_s + margin_s)


class Transport(Protocol):
    """Message-plane contract.

    Implementations own every bus layout detail; orchestration code must not
    touch message paths directly. Replacing the file bus (MCP, SQLite, ...)
    means providing another implementation of this protocol.
    """

    def publish_task(self, task: dict[str, Any]) -> Path: ...

    def task_pending(self, lwar_id: str, task_id: str) -> bool: ...

    def publish_control(self, message: dict[str, Any]) -> Path: ...

    def claim_control(self, identity: dict[str, Any]) -> dict[str, Any] | None: ...

    def claim_task(
        self, identity: dict[str, Any], default_lease_s: int
    ) -> tuple[dict[str, Any], Path] | None: ...

    def write_heartbeat(self, identity: dict[str, Any], status: str, task_id: str | None) -> None: ...

    def read_heartbeat(self, lwar_id: str) -> dict[str, Any] | None: ...

    def find_claimed_task(self, lwar_id: str, task_id: str) -> tuple[Path, dict[str, Any]]: ...

    def submit_result(self, identity: dict[str, Any], claimed_path: Path, result: dict[str, Any]) -> Path: ...

    def outgoing_results(self, lwar_id: str) -> list[Path]: ...

    def archive_result(self, lwar_id: str, path: Path) -> Path: ...

    def quarantine_result(self, lwar_id: str, path: Path, reason: str) -> Path: ...

    def expired_leases(self, lwar_id: str, now: datetime) -> list[tuple[Path, dict[str, Any]]]: ...

    def result_exists(self, lwar_id: str, task_id: str) -> bool: ...

    def claimed_task_for_lease(
        self, lwar_id: str, lease: dict[str, Any]
    ) -> tuple[Path, dict[str, Any]] | None: ...

    def requeue_claimed(self, lwar_id: str, claimed_path: Path, task: dict[str, Any]) -> Path | None: ...

    def dead_letter(self, lwar_id: str, claimed_path: Path, task: dict[str, Any], reason: str) -> Path: ...

    def list_dead(self, lwar_id: str) -> list[tuple[Path, dict[str, Any]]]: ...

    def requeue_dead(self, lwar_id: str, task_id: str) -> dict[str, Any] | None: ...

    def incoming_backlog(self, lwar_id: str) -> int: ...

    def list_lwar_ids(self) -> list[str]: ...

    def prune(self, lwar_id: str, older_than: datetime) -> dict[str, int]: ...


class FileTransport:
    """Local filesystem implementation of the PAO message plane."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def _mailbox(self, lwar_id: str) -> Path:
        return mailbox_root(self.root, lwar_id)

    # -- publication -------------------------------------------------------

    def publish_task(self, task: dict[str, Any]) -> Path:
        mailbox = ensure_mailbox(self.root, task["lwar_id"])
        priority = int(task.get("priority", 5))
        target = mailbox / "incoming" / f"{priority:03d}_{task['task_id']}.json"
        atomic_write_json(target, task)
        return target

    def task_pending(self, lwar_id: str, task_id: str) -> bool:
        mailbox = ensure_mailbox(self.root, lwar_id)
        suffix = f"_{task_id}.json"
        for directory in ("incoming", "claimed"):
            if any(path.name.endswith(suffix) for path in (mailbox / directory).glob("*.json")):
                return True
        return False

    def publish_control(self, message: dict[str, Any]) -> Path:
        mailbox = ensure_mailbox(self.root, message["lwar_id"])
        target = mailbox / "control" / f"{message['control_id']}.json"
        atomic_write_json(target, message)
        return target

    # -- claiming ----------------------------------------------------------

    def claim_control(self, identity: dict[str, Any]) -> dict[str, Any] | None:
        mailbox = ensure_mailbox(self.root, identity["lwar_id"])
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
                atomic_write_json(
                    failed.with_suffix(".error.json"),
                    {"reason": "stale_control_identity", "failed_at": utc_now()},
                )
                continue
            if message.get("command") not in CONTROL_COMMANDS:
                failed = mailbox / "failed" / f"control_{destination.name}"
                destination.replace(failed)
                atomic_write_json(
                    failed.with_suffix(".error.json"),
                    {"reason": "invalid_control_command", "failed_at": utc_now()},
                )
                continue
            archive = mailbox / "archive" / "control" / destination.name
            archive.parent.mkdir(parents=True, exist_ok=True)
            destination.replace(archive)
            return message
        return None

    def _reject_task(self, mailbox: Path, source: Path, reason: str) -> None:
        failed = mailbox / "failed" / source.name
        if claim_file(source, failed):
            atomic_write_json(failed.with_suffix(".error.json"), {"reason": reason, "failed_at": utc_now()})

    def claim_task(
        self, identity: dict[str, Any], default_lease_s: int
    ) -> tuple[dict[str, Any], Path] | None:
        mailbox = ensure_mailbox(self.root, identity["lwar_id"])
        for source in sorted((mailbox / "incoming").glob("*.json")):
            try:
                task = load_json(source)
            except Exception as error:
                self._reject_task(mailbox, source, f"invalid_json:{error}")
                continue
            required = ("task_id", "lwar_id", "instance_id", "generation", "goal")
            if any(field not in task for field in required):
                self._reject_task(mailbox, source, "missing_required_field")
                continue
            if task["lwar_id"] != identity["lwar_id"]:
                self._reject_task(mailbox, source, "wrong_lwar_id")
                continue
            if task["instance_id"] != identity["instance_id"] or task["generation"] != identity["generation"]:
                self._reject_task(mailbox, source, "stale_identity")
                continue
            destination = mailbox / "claimed" / source.name
            if not claim_file(source, destination):
                continue
            # The claimed file is exclusively ours until the lease exists
            # (recover only acts on expired leases), so stamping the claim
            # token here is race-free.
            task["claim_token"] = new_id("claim")
            atomic_write_json(destination, task)
            lease_s = effective_lease_seconds(task, default_lease_s)
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_s)
            atomic_write_json(
                mailbox / "leases" / f"{task['task_id']}.json",
                {
                    "schema_version": "pao.lease.v1",
                    "task_id": task["task_id"],
                    "lwar_id": identity["lwar_id"],
                    "instance_id": identity["instance_id"],
                    "generation": identity["generation"],
                    "claim_token": task["claim_token"],
                    "claimed_file": destination.name,
                    "effective_lease_s": lease_s,
                    "leased_at": utc_now(),
                    "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                },
            )
            return task, destination
        return None

    # -- heartbeat ---------------------------------------------------------

    def write_heartbeat(self, identity: dict[str, Any], status: str, task_id: str | None) -> None:
        mailbox = ensure_mailbox(self.root, identity["lwar_id"])
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

    def read_heartbeat(self, lwar_id: str) -> dict[str, Any] | None:
        path = self._mailbox(lwar_id) / "heartbeat.json"
        return load_json(path) if path.is_file() else None

    # -- results -----------------------------------------------------------

    def find_claimed_task(self, lwar_id: str, task_id: str) -> tuple[Path, dict[str, Any]]:
        mailbox = self._mailbox(lwar_id)
        for path in sorted((mailbox / "claimed").glob("*.json")):
            task = load_json(path)
            if task.get("task_id") == task_id:
                return path, task
        raise FileNotFoundError(f"claimed task not found: {task_id}")

    def submit_result(self, identity: dict[str, Any], claimed_path: Path, result: dict[str, Any]) -> Path:
        mailbox = self._mailbox(identity["lwar_id"])
        outgoing = mailbox / "outgoing" / f"{result['task_id']}.result.json"
        atomic_write_json(outgoing, result)
        archive = mailbox / "archive" / "tasks" / claimed_path.name
        archive.parent.mkdir(parents=True, exist_ok=True)
        try:
            claimed_path.replace(archive)
        except FileNotFoundError:
            # OA recovery re-queued this claim between our result write and the
            # archive move: the re-queued attempt is canonical, this submission
            # is superseded and will be quarantined by the attempt fence.
            raise RuntimeError(
                f"claim superseded during submission: {result['task_id']} "
                "(lease expired and the task was re-queued; do not retry)"
            )
        (mailbox / "leases" / f"{result['task_id']}.json").unlink(missing_ok=True)
        return outgoing

    def outgoing_results(self, lwar_id: str) -> list[Path]:
        return sorted((self._mailbox(lwar_id) / "outgoing").glob("*.json"))

    def archive_result(self, lwar_id: str, path: Path) -> Path:
        destination = self._mailbox(lwar_id) / "archive" / "results" / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.replace(destination)
        return destination

    def quarantine_result(self, lwar_id: str, path: Path, reason: str) -> Path:
        destination = self._mailbox(lwar_id) / "quarantine" / path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.replace(destination)
        atomic_write_json(
            destination.with_suffix(".error.json"),
            {"reason": reason, "quarantined_at": utc_now()},
        )
        return destination

    def result_exists(self, lwar_id: str, task_id: str) -> bool:
        return (self._mailbox(lwar_id) / "outgoing" / f"{task_id}.result.json").exists()

    # -- recovery ----------------------------------------------------------

    def expired_leases(self, lwar_id: str, now: datetime) -> list[tuple[Path, dict[str, Any]]]:
        expired = []
        for lease_path in sorted((self._mailbox(lwar_id) / "leases").glob("*.json")):
            lease = load_json(lease_path)
            if parse_utc(lease["expires_at"]) <= now:
                expired.append((lease_path, lease))
        return expired

    def claimed_task_for_lease(
        self, lwar_id: str, lease: dict[str, Any]
    ) -> tuple[Path, dict[str, Any]] | None:
        path = self._mailbox(lwar_id) / "claimed" / lease["claimed_file"]
        if not path.is_file():
            return None
        return path, load_json(path)

    def requeue_claimed(self, lwar_id: str, claimed_path: Path, task: dict[str, Any]) -> Path | None:
        incoming = self._mailbox(lwar_id) / "incoming" / claimed_path.name
        if incoming.exists():
            return None
        # Claim the file atomically BEFORE rewriting: if the LWAR archived it
        # via submit_result in the meantime, recreating it here would fork the
        # task into a duplicate execution. The loser of this replace backs off.
        try:
            os.replace(claimed_path, incoming)
        except FileNotFoundError:
            return None
        atomic_write_json(incoming, task)
        return incoming

    def dead_letter(self, lwar_id: str, claimed_path: Path, task: dict[str, Any], reason: str) -> Path:
        destination = self._mailbox(lwar_id) / "dead" / claimed_path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(claimed_path, task)
        claimed_path.replace(destination)
        atomic_write_json(
            destination.with_suffix(".error.json"),
            {
                "reason": reason,
                "task_id": task.get("task_id"),
                "attempt": task.get("attempt"),
                "max_retries": task.get("max_retries"),
                "dead_lettered_at": utc_now(),
            },
        )
        return destination

    def list_dead(self, lwar_id: str) -> list[tuple[Path, dict[str, Any]]]:
        entries = []
        for path in sorted((self._mailbox(lwar_id) / "dead").glob("*.json")):
            if path.name.endswith(".error.json"):
                continue
            entries.append((path, load_json(path)))
        return entries

    def requeue_dead(self, lwar_id: str, task_id: str) -> dict[str, Any] | None:
        for path, task in self.list_dead(lwar_id):
            if task.get("task_id") != task_id:
                continue
            # attempt is the collect-side fencing key and must stay monotonic:
            # resetting it would let a superseded result match a future attempt.
            task["attempt"] = int(task.get("attempt", 1)) + 1
            atomic_write_json(path, task)
            incoming = self._mailbox(lwar_id) / "incoming" / path.name
            path.replace(incoming)
            path.with_suffix(".error.json").unlink(missing_ok=True)
            return task
        return None

    # -- observation and maintenance ----------------------------------------

    def incoming_backlog(self, lwar_id: str) -> int:
        return len(list((self._mailbox(lwar_id) / "incoming").glob("*.json")))

    def list_lwar_ids(self) -> list[str]:
        base = self.root / "mailbox"
        if not base.is_dir():
            return []
        return [path.name for path in sorted(base.glob("LWAR*")) if path.is_dir()]

    def prune(self, lwar_id: str, older_than: datetime) -> dict[str, int]:
        counts: dict[str, int] = {}
        mailbox = self._mailbox(lwar_id)
        for category in PRUNE_CATEGORIES:
            removed = 0
            directory = mailbox / category
            if directory.is_dir():
                for path in sorted(directory.glob("*")):
                    if not path.is_file():
                        continue
                    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                    if modified <= older_than:
                        path.unlink(missing_ok=True)
                        removed += 1
            counts[category] = removed
        return counts
