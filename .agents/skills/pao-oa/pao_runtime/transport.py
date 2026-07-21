from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .common import (
    atomic_write_json,
    authority_denied_reason,
    claim_file,
    ensure_mailbox,
    load_json,
    mailbox_root,
    new_id,
    parse_utc,
    quarantine_corrupt,
    require_local_filesystem,
    safe_load_json,
    utc_now,
    validate_task_id,
)
from .contracts import validate_contract


LEASE_MARGIN_S = 30

CONTROL_COMMANDS = {"shutdown", "retire", "ping", "cancel", "drain"}

PRUNE_CATEGORIES = (
    "archive/tasks",
    "archive/results",
    "archive/control",
    "failed",
    "quarantine",
    "cancelled",
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

    def ack_control(self, identity: dict[str, Any], message: dict[str, Any]) -> Path | None: ...

    def write_cancel_tombstone(
        self, identity: dict[str, Any], task_id: str, control_id: str | None
    ) -> Path | None: ...

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

    def orphaned_claims(
        self, lwar_id: str, now: datetime, grace_s: float = ...
    ) -> list[tuple[Path, dict[str, Any]]]: ...

    def expired_incoming(
        self, lwar_id: str, now: datetime, delivery_timeout_s: float
    ) -> list[tuple[Path, dict[str, Any]]]: ...

    def result_exists(self, lwar_id: str, task_id: str) -> bool: ...

    def claimed_task_for_lease(
        self, lwar_id: str, lease: dict[str, Any]
    ) -> tuple[Path, dict[str, Any]] | None: ...

    def requeue_claimed(self, lwar_id: str, claimed_path: Path, task: dict[str, Any]) -> Path | None: ...

    def dead_letter(
        self, lwar_id: str, claimed_path: Path, task: dict[str, Any], reason: str
    ) -> Path | None: ...

    def list_dead(self, lwar_id: str) -> list[tuple[Path, dict[str, Any]]]: ...

    def find_dead_task(self, lwar_id: str, task_id: str) -> tuple[Path, dict[str, Any]] | None: ...

    def requeue_dead(
        self, lwar_id: str, task_id: str, attempt: int | None = None
    ) -> dict[str, Any] | None: ...

    def incoming_backlog(self, lwar_id: str) -> int: ...

    def list_lwar_ids(self) -> list[str]: ...

    def prune(self, lwar_id: str, older_than: datetime) -> dict[str, int]: ...


class FileTransport:
    """Local filesystem implementation of the PAO message plane."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        require_local_filesystem(self.root)

    def _mailbox(self, lwar_id: str) -> Path:
        return mailbox_root(self.root, lwar_id)

    # -- publication -------------------------------------------------------

    def publish_task(self, task: dict[str, Any]) -> Path:
        validate_contract(task, "task.schema.json")
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
        validate_contract(message, "control.schema.json")
        mailbox = ensure_mailbox(self.root, message["lwar_id"])
        target = mailbox / "control" / f"{message['control_id']}.json"
        atomic_write_json(target, message)
        return target

    # -- claiming ----------------------------------------------------------

    def claim_control(self, identity: dict[str, Any]) -> dict[str, Any] | None:
        mailbox = ensure_mailbox(self.root, identity["lwar_id"])
        candidates = list(sorted((mailbox / "control_claimed").glob("*.json")))
        candidates += list(sorted((mailbox / "control").glob("*.json")))
        for source in candidates:
            if source.parent.name == "control":
                destination = mailbox / "control_claimed" / source.name
                if not claim_file(source, destination):
                    continue
            else:
                destination = source
            try:
                message = load_json(destination)
                validate_contract(message, "control.schema.json")
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
            return message
        return None

    def ack_control(self, identity: dict[str, Any], message: dict[str, Any]) -> Path | None:
        mailbox = ensure_mailbox(self.root, identity["lwar_id"])
        name = f"{message['control_id']}.json"
        source = mailbox / "control_claimed" / name
        archive = mailbox / "archive" / "control" / name
        archive.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            if not claim_file(source, archive):
                return None
            return archive
        return archive if archive.is_file() else None

    # -- cancel tombstones -------------------------------------------------

    def _cancel_tombstone(self, lwar_id: str, task_id: str) -> Path:
        return self._mailbox(lwar_id) / "cancelled" / f"{task_id}.json"

    def write_cancel_tombstone(
        self, identity: dict[str, Any], task_id: str, control_id: str | None
    ) -> Path | None:
        """Record a cancel tombstone so the watcher can auto-cancel the task
        deterministically whenever it is claimed, without the agent having to
        remember the cancel across watch slices. First-writer-wins keeps a
        duplicate cancel harmless; an unroutable task_id is ignored."""
        try:
            validate_task_id(task_id)
        except ValueError:
            return None
        mailbox = ensure_mailbox(self.root, identity["lwar_id"])
        target = mailbox / "cancelled" / f"{task_id}.json"
        if target.exists():
            return target
        atomic_write_json(
            target,
            {
                "schema_version": "pao.cancel-tombstone.v1",
                "task_id": task_id,
                "control_id": control_id,
                "lwar_id": identity["lwar_id"],
                "cancelled_at": utc_now(),
            },
        )
        return target

    def _cancelled_result(
        self, identity: dict[str, Any], task: dict[str, Any], tombstone: dict[str, Any]
    ) -> dict[str, Any]:
        """Terminal ResultContract for a task auto-cancelled by its tombstone.
        Mirrors lwar_cli.command_complete's normalization; attempt and
        claim_token are echoed from the claimed task file, never fabricated."""
        return {
            "schema_version": "pao.result.v1",
            "task_id": task["task_id"],
            "workflow_id": task.get("workflow_id"),
            "lwar_id": identity["lwar_id"],
            "instance_id": identity["instance_id"],
            "generation": identity["generation"],
            "registry_version": identity.get("registry_version"),
            "status": "cancelled",
            "summary": (
                f"auto-cancelled before execution by tombstone "
                f"cancelled/{task['task_id']}.json (control {tombstone.get('control_id')})"
            ),
            "evidence": {
                "cancelled_by": "watcher_tombstone",
                "control_id": tombstone.get("control_id"),
                "tombstone": f"cancelled/{task['task_id']}.json",
            },
            "artifacts": [],
            "next_action": "validate",
            "exit_code": 1,
            "error": None,
            "attempt": int(task.get("attempt", 1)),
            "claim_token": task.get("claim_token"),
            "submitted_at": utc_now(),
        }

    def _auto_cancel(self, identity: dict[str, Any], claimed_path: Path, task: dict[str, Any]) -> None:
        """Consume the tombstone by submitting a deterministic cancelled result
        through the normal result pipeline, then remove the tombstone.

        The tombstone's mere existence is the cancel signal; its content is only
        metadata, so an unreadable/corrupt tombstone still cancels (safe_load →
        {}) rather than crashing the watcher slice mid-claim."""
        tombstone = safe_load_json(self._cancel_tombstone(identity["lwar_id"], task["task_id"])) or {}
        result = self._cancelled_result(identity, task, tombstone)
        self.submit_result(identity, claimed_path, result)
        self._cancel_tombstone(identity["lwar_id"], task["task_id"]).unlink(missing_ok=True)

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
                validate_contract(task, "task.schema.json")
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
            # Defense in depth against hand-planted tasks: the deny-set only —
            # cwd existence is send's responsibility (lazy workspaces are legal).
            if task.get("cwd"):
                denied = authority_denied_reason(Path(task["cwd"]), self.root)
                if denied:
                    self._reject_task(mailbox, source, f"authority_violation:{denied}")
                    continue
            destination = mailbox / "claimed" / source.name
            if not claim_file(source, destination):
                continue
            # The claimed file is exclusively ours until the lease exists
            # (recover only acts on expired leases), so stamping the claim
            # token here is race-free.
            task["claim_token"] = new_id("claim")
            atomic_write_json(destination, task)
            # A tombstoned task is auto-cancelled deterministically and never
            # handed to the agent: the watcher submits its terminal `cancelled`
            # result here, consumes the tombstone, and keeps scanning.
            if self._cancel_tombstone(identity["lwar_id"], task["task_id"]).is_file():
                self._auto_cancel(identity, destination, task)
                continue
            lease_s = effective_lease_seconds(task, default_lease_s)
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=lease_s)
            lease = {
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
                }
            validate_contract(lease, "lease.schema.json")
            atomic_write_json(mailbox / "leases" / f"{task['task_id']}.json", lease)
            return task, destination
        return None

    # -- heartbeat ---------------------------------------------------------

    def write_heartbeat(self, identity: dict[str, Any], status: str, task_id: str | None) -> None:
        mailbox = ensure_mailbox(self.root, identity["lwar_id"])
        heartbeat = {
                "schema_version": "pao.heartbeat.v1",
                "lwar_id": identity["lwar_id"],
                "instance_id": identity["instance_id"],
                "generation": identity["generation"],
                "status": status,
                "current_task_id": task_id,
                "last_seen": utc_now(),
            }
        validate_contract(heartbeat, "heartbeat.schema.json")
        atomic_write_json(mailbox / "heartbeat.json", heartbeat)

    def read_heartbeat(self, lwar_id: str) -> dict[str, Any] | None:
        path = self._mailbox(lwar_id) / "heartbeat.json"
        # A corrupt heartbeat reads as absent (None) rather than crashing every
        # caller — routing and status treat None as "stale/unknown" already.
        heartbeat = safe_load_json(path) if path.is_file() else None
        if heartbeat is None:
            return None
        try:
            validate_contract(heartbeat, "heartbeat.schema.json")
        except ValueError:
            return None
        return heartbeat

    # -- results -----------------------------------------------------------

    def find_claimed_task(self, lwar_id: str, task_id: str) -> tuple[Path, dict[str, Any]]:
        mailbox = self._mailbox(lwar_id)
        for path in sorted((mailbox / "claimed").glob("*.json")):
            task = safe_load_json(path)
            if task is None:
                quarantine_corrupt(path, "corrupt_claimed_task")
                continue
            if task.get("task_id") == task_id:
                return path, task
        raise FileNotFoundError(f"claimed task not found: {task_id}")

    def submit_result(self, identity: dict[str, Any], claimed_path: Path, result: dict[str, Any]) -> Path:
        validate_contract(result, "result.schema.json")
        mailbox = self._mailbox(identity["lwar_id"])
        outgoing = mailbox / "outgoing" / f"{result['task_id']}.result.json"
        # Publish the result FIRST. Once `outgoing` exists, result_exists() is
        # True, and every recovery path (expired-lease AND orphaned-claim) backs
        # off instead of re-queuing — so no window here can duplicate execution.
        # Only THEN retire the lease (before the archive move, so a retried
        # archive cannot resurrect it). Writing the result last would instead
        # leave a lease-less claim visible to orphaned_claims mid-submit.
        atomic_write_json(outgoing, result)
        (mailbox / "leases" / f"{result['task_id']}.json").unlink(missing_ok=True)
        archive = mailbox / "archive" / "tasks" / claimed_path.name
        archive.parent.mkdir(parents=True, exist_ok=True)
        # claim_file retries transient Windows sharing violations and returns
        # False only when the source is already gone (FileNotFoundError) —
        # meaning OA recovery re-queued this claim between our result write and
        # the archive move. That re-queued attempt is canonical; this submission
        # is superseded and will be quarantined by the attempt fence.
        if not claim_file(claimed_path, archive):
            raise RuntimeError(
                f"claim superseded during submission: {result['task_id']} "
                "(lease expired and the task was re-queued; do not retry)"
            )
        return outgoing

    def outgoing_results(self, lwar_id: str) -> list[Path]:
        return sorted((self._mailbox(lwar_id) / "outgoing").glob("*.json"))

    def archived_results(self, lwar_id: str) -> list[Path]:
        return sorted((self._mailbox(lwar_id) / "archive" / "results").glob("*.json"))

    def archived_task(self, lwar_id: str, task_id: str) -> dict[str, Any] | None:
        for path in sorted((self._mailbox(lwar_id) / "archive" / "tasks").glob("*.json")):
            task = safe_load_json(path)
            if task is None:
                quarantine_corrupt(path, "corrupt_archived_task")
                continue
            if task.get("task_id") == task_id:
                return task
        return None

    def provenance_task(
        self, lwar_id: str, task_id: str, claim_token: str | None, attempt: int | None
    ) -> dict[str, Any] | None:
        mailbox = self._mailbox(lwar_id)
        candidates = list(sorted((mailbox / "claimed").glob("*.json")))
        candidates += list(sorted((mailbox / "archive" / "tasks").glob("*.json")))
        for path in candidates:
            task = safe_load_json(path)
            if task is None or task.get("task_id") != task_id:
                continue
            if task.get("claim_token") != claim_token:
                continue
            if int(task.get("attempt", 1)) != int(attempt or 1):
                continue
            return task
        return None

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
            lease = safe_load_json(lease_path)
            if lease is None or "expires_at" not in lease:
                # One corrupt lease must not wedge recovery for every other
                # lease of this LWAR: quarantine it and keep sweeping.
                quarantine_corrupt(lease_path, "corrupt_lease")
                continue
            try:
                validate_contract(lease, "lease.schema.json")
            except ValueError:
                quarantine_corrupt(lease_path, "invalid_lease_contract")
                continue
            try:
                expires = parse_utc(lease["expires_at"])
            except (ValueError, TypeError):
                quarantine_corrupt(lease_path, "unparseable_lease_expiry")
                continue
            if expires <= now:
                expired.append((lease_path, lease))
        return expired

    def orphaned_claims(
        self, lwar_id: str, now: datetime, grace_s: float = 120.0
    ) -> list[tuple[Path, dict[str, Any]]]:
        """Claimed tasks that have NO lease file and are older than grace_s.

        The lease write failed (disk fault, a sharing violation) or the process
        died between the atomic claim-move and the lease write, so lease-based
        recovery can never see them — they would sit in claimed/ forever. The
        grace window avoids racing a live claim that is still writing its lease.
        """
        mailbox = self._mailbox(lwar_id)
        leases_dir = mailbox / "leases"
        orphans = []
        cutoff = now.timestamp() - grace_s
        for path in sorted((mailbox / "claimed").glob("*.json")):
            task = safe_load_json(path)
            if task is None:
                quarantine_corrupt(path, "corrupt_claimed_task")
                continue
            task_id = task.get("task_id")
            if not task_id or (leases_dir / f"{task_id}.json").is_file():
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
            except FileNotFoundError:
                continue
            orphans.append((path, task))
        return orphans

    def expired_incoming(
        self, lwar_id: str, now: datetime, delivery_timeout_s: float
    ) -> list[tuple[Path, dict[str, Any]]]:
        expired = []
        cutoff = now.timestamp() - delivery_timeout_s
        for path in sorted((self._mailbox(lwar_id) / "incoming").glob("*.json")):
            task = safe_load_json(path)
            if task is None:
                quarantine_corrupt(path, "corrupt_incoming_task")
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
            except FileNotFoundError:
                continue
            expired.append((path, task))
        return expired

    def find_pending_task(self, lwar_id: str, task_id: str) -> tuple[Path, dict[str, Any]] | None:
        suffix = f"_{task_id}.json"
        for directory in ("incoming", "claimed"):
            for path in sorted((self._mailbox(lwar_id) / directory).glob("*.json")):
                if not path.name.endswith(suffix):
                    continue
                task = safe_load_json(path)
                if task is not None:
                    return path, task
        return None

    def claimed_task_for_lease(
        self, lwar_id: str, lease: dict[str, Any]
    ) -> tuple[Path, dict[str, Any]] | None:
        claimed_file = lease.get("claimed_file")
        if not claimed_file:
            return None
        path = self._mailbox(lwar_id) / "claimed" / claimed_file
        if not path.is_file():
            return None
        task = safe_load_json(path)
        if task is None:
            quarantine_corrupt(path, "corrupt_claimed_task")
            return None
        return path, task

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

    def dead_letter(
        self, lwar_id: str, claimed_path: Path, task: dict[str, Any], reason: str
    ) -> Path | None:
        destination = self._mailbox(lwar_id) / "dead" / claimed_path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Move the existing claimed file first. If it is already gone (a racing
        # submit_result archived it), do NOT recreate it — rewriting-then-moving
        # would resurrect a completed task as a spurious dead-letter.
        if not claim_file(claimed_path, destination):
            return None
        atomic_write_json(destination, task)
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
            task = safe_load_json(path)
            if task is None:
                quarantine_corrupt(path, "corrupt_dead_task")
                continue
            entries.append((path, task))
        return entries

    def find_dead_task(self, lwar_id: str, task_id: str) -> tuple[Path, dict[str, Any]] | None:
        for path, task in self.list_dead(lwar_id):
            if task.get("task_id") == task_id:
                return path, task
        return None

    def requeue_dead(
        self, lwar_id: str, task_id: str, attempt: int | None = None
    ) -> dict[str, Any] | None:
        found = self.find_dead_task(lwar_id, task_id)
        if found is not None:
            path, task = found
            # attempt is the collect-side fencing key and must stay monotonic:
            # resetting it would let a superseded result match a future attempt.
            task["attempt"] = (
                int(attempt) if attempt is not None else int(task.get("attempt", 1)) + 1
            )
            atomic_write_json(path, task)
            incoming = self._mailbox(lwar_id) / "incoming" / path.name
            if not claim_file(path, incoming):
                return None
            path.with_suffix(".error.json").unlink(missing_ok=True)
            return task
        return None

    def failed_entries(self, lwar_id: str) -> list[tuple[Path, dict[str, Any], str | None]]:
        """Rejected tasks parked in failed/ with their rejection reasons."""
        entries = []
        for error_path in sorted((self._mailbox(lwar_id) / "failed").glob("*.error.json")):
            task_path = error_path.parent / (error_path.name[: -len(".error.json")] + ".json")
            if not task_path.is_file():
                continue
            try:
                task = load_json(task_path)
                error = load_json(error_path)
            except Exception:
                continue
            entries.append((task_path, task, error.get("reason")))
        return entries

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
                    try:
                        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                    except FileNotFoundError:
                        # Concurrent prune/cleanup removed it between glob and
                        # stat — nothing to do, keep pruning the rest.
                        continue
                    if modified <= older_than:
                        if category == "cancelled" and self.task_pending(lwar_id, path.stem):
                            continue
                        path.unlink(missing_ok=True)
                        removed += 1
            counts[category] = removed
        return counts
