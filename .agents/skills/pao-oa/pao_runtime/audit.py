from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO

from .common import FileLock, _replace_retry, utc_now


AUDIT_SEGMENT_MAX_BYTES = 10 * 1024 * 1024
AUDIT_REPAIR_SEGMENT_RE = re.compile(r"(?:events(?:\.\d+)?|degraded)\.jsonl")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _durable_flush(handle: IO[Any]) -> None:
    """Push appended audit bytes through the operating-system durability barrier."""
    handle.flush()
    os.fsync(handle.fileno())


def _line_key(line: str) -> str | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    key = value.get("idempotency_key") if isinstance(value, dict) else None
    return key if isinstance(key, str) and key else None


def _line_has_key(line: str, idempotency_key: str) -> bool:
    return _line_key(line) == idempotency_key


def _malformed_line_numbers(data: bytes) -> list[int]:
    malformed: list[int] = []
    for index, raw in enumerate(data.splitlines(), start=1):
        try:
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("audit line must be a JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            malformed.append(index)
    return malformed


def _quarantine_truncated_tail(path: Path, data: bytes) -> None:
    boundary = data.rfind(b"\n") + 1
    fragment = data[boundary:]
    if not fragment:
        raise OSError(f"no truncated tail to repair: {path}")
    corrupt = path.parent / ".corrupt"
    corrupt.mkdir(parents=True, exist_ok=True)
    quarantine = corrupt / f"{path.name}.{time.time_ns()}.tail"
    with open(quarantine, "xb") as handle:
        handle.write(fragment)
        _durable_flush(handle)
    with open(path, "r+b") as handle:
        handle.truncate(boundary)
        _durable_flush(handle)


def _validated_lines(path: Path, repair_truncated_tail: bool = False) -> list[str] | None:
    """Read valid JSON-object lines; optionally quarantine one crash-truncated tail."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    raw_lines = data.splitlines()
    terminated = not data or data.endswith(b"\n")
    lines: list[str] = []
    for index, raw in enumerate(raw_lines):
        try:
            line = raw.decode("utf-8")
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("audit line must be a JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            if repair_truncated_tail and index == len(raw_lines) - 1 and not terminated:
                try:
                    _quarantine_truncated_tail(path, data)
                except OSError:
                    return None
                return _validated_lines(path, repair_truncated_tail=False)
            return None
        lines.append(line)
    return lines


def _file_keys(path: Path) -> set[str] | None:
    lines = _validated_lines(path, repair_truncated_tail=path.name == "events.jsonl")
    if lines is None:
        return None
    return {key for line in lines if (key := _line_key(line))}


def _logged_keys(directory: Path) -> set[str]:
    """Return a complete key snapshot or fail closed on an unreadable segment."""
    committed_keys: set[str] = set()
    for candidate in sorted(directory.glob("events*.jsonl")):
        keys = _file_keys(candidate)
        if keys is None:
            raise OSError(f"unreadable audit segment: {candidate}")
        committed_keys.update(keys)
    return committed_keys


def _replayable_backlog(backlog: list[str], committed_keys: set[str]) -> list[str]:
    """Keep unkeyed lines, but replay each uncommitted deterministic key once."""
    seen_keys = set(committed_keys)
    replayable: list[str] = []
    for pending in backlog:
        key = _line_key(pending)
        if key is None:
            replayable.append(pending)
        elif key not in seen_keys:
            replayable.append(pending)
            seen_keys.add(key)
    return replayable


def _spool_degraded(degraded: Path, line: str, idempotency_key: str | None) -> None:
    """Best-effort append to the degraded spool, once per deterministic key."""
    with FileLock(degraded.parent / ".degraded.lock"):
        existing: list[str] = []
        if degraded.is_file():
            validated = _validated_lines(degraded, repair_truncated_tail=True)
            if validated is None:
                raise OSError(f"corrupt degraded audit spool: {degraded}")
            existing = validated
        if idempotency_key and any(
            _line_has_key(pending, idempotency_key) for pending in existing
        ):
            return
        with open(degraded, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            _durable_flush(handle)


def _record(
    root: Path,
    actor: str,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
) -> bool:
    """Append one audit event, optionally once per deterministic key.

    Never raises into the caller's control flow beyond filesystem errors;
    stdout is reserved for `emit`, so audit stays file-only.
    """
    path = root.resolve() / "var" / "audit" / "events.jsonl"
    degraded = path.parent / "degraded.jsonl"
    committed = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        event = {"schema_version": "pao.audit-event.v1", "ts": utc_now(), "actor": actor, **payload}
        if idempotency_key is not None:
            event["idempotency_key"] = idempotency_key
        line = json.dumps(
            event,
            ensure_ascii=False,
            sort_keys=True,
        )
        with FileLock(path.parent / ".audit.lock"):
            with FileLock(path.parent / ".degraded.lock"):
                if path.is_file() and path.stat().st_size >= AUDIT_SEGMENT_MAX_BYTES:
                    path.replace(path.parent / f"events.{time.time_ns()}.jsonl")
                backlog: list[str] = []
                if degraded.is_file():
                    validated = _validated_lines(degraded, repair_truncated_tail=True)
                    if validated is None:
                        raise OSError(f"corrupt degraded audit spool: {degraded}")
                    backlog = validated
                backlog_keys = {key for pending in backlog if (key := _line_key(pending))}
                committed_keys = (
                    _logged_keys(path.parent)
                    if idempotency_key is not None or backlog_keys
                    else set()
                )
                replayable = _replayable_backlog(backlog, committed_keys)
                duplicate = bool(
                    idempotency_key
                    and (idempotency_key in committed_keys or idempotency_key in backlog_keys)
                )
                with open(path, "a", encoding="utf-8") as handle:
                    for pending in replayable:
                        handle.write(pending + "\n")
                    if not duplicate:
                        handle.write(line + "\n")
                    _durable_flush(handle)
                committed = True
                if backlog:
                    degraded.unlink(missing_ok=True)
        return True
    except (OSError, TimeoutError) as error:
        # A transient failure of the active append target must not affect the
        # command. Preserve a best-effort backlog in a separate segment; the
        # next successful record replays it into the canonical audit stream.
        if not committed:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                _spool_degraded(degraded, line, idempotency_key)
            except (OSError, TimeoutError):
                pass
        print(
            json.dumps(
                {"event": "audit_write_failed", "actor": actor, "error": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return False


def record(root: Path, actor: str, payload: dict[str, Any]) -> bool:
    """Append one non-idempotent audit event."""
    return _record(root, actor, payload)


def record_once(
    root: Path,
    actor: str,
    payload: dict[str, Any],
    idempotency_key: str,
) -> bool:
    """Append once across active, rotated, and degraded audit segments."""
    if not idempotency_key:
        raise ValueError("idempotency_key must not be empty")
    return _record(root, actor, payload, idempotency_key)


def _inspect_jsonl(path: Path, kind: str, directory: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "path": path.relative_to(directory).as_posix(),
        "kind": kind,
        "status": "healthy",
        "line_count": 0,
        "keyed_count": 0,
        "malformed_lines": [],
        "repairable_truncated_tail": False,
        "sha256": None,
    }
    try:
        data = path.read_bytes()
    except OSError as error:
        report.update({"status": "unreadable", "error": str(error)})
        return report
    raw_lines = data.splitlines()
    report["sha256"] = hashlib.sha256(data).hexdigest()
    terminated = not data or data.endswith(b"\n")
    report["line_count"] = len(raw_lines)
    for index, raw in enumerate(raw_lines, start=1):
        try:
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("audit line must be a JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            report["malformed_lines"].append(index)
            if index == len(raw_lines) and not terminated and kind in {"active", "degraded"}:
                report["repairable_truncated_tail"] = True
            continue
        key = value.get("idempotency_key")
        if isinstance(key, str) and key:
            report["keyed_count"] += 1
    if report["malformed_lines"]:
        report["status"] = "malformed"
    return report


def health(root: Path) -> dict[str, Any]:
    """Inspect audit health without acquiring locks or changing bus state."""
    directory = root.resolve() / "var" / "audit"
    segments = [
        _inspect_jsonl(path, "active" if path.name == "events.jsonl" else "rotated", directory)
        for path in sorted(directory.glob("events*.jsonl"))
        if path.is_file()
    ] if directory.is_dir() else []
    degraded_path = directory / "degraded.jsonl"
    degraded = (
        _inspect_jsonl(degraded_path, "degraded", directory)
        if degraded_path.is_file()
        else {
            "path": "degraded.jsonl",
            "kind": "degraded",
            "status": "absent",
            "line_count": 0,
            "keyed_count": 0,
            "malformed_lines": [],
            "repairable_truncated_tail": False,
            "sha256": None,
        }
    )
    quarantined = []
    corrupt = directory / ".corrupt"
    if corrupt.is_dir():
        for path in sorted(corrupt.iterdir()):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            quarantined.append(
                {"path": path.relative_to(directory).as_posix(), "bytes": size}
            )

    unhealthy_segments = [item for item in segments if item["status"] != "healthy"]
    degraded_unhealthy = degraded["status"] not in {"absent", "healthy"}
    pending_count = degraded["line_count"] if degraded["status"] == "healthy" else 0
    keyed_append_blocked = bool(unhealthy_segments or degraded_unhealthy)
    blocked_replay = degraded["status"] != "absent" and keyed_append_blocked
    repair_candidates = [
        {
            "segment": item["path"],
            "expected_sha256": item["sha256"],
            "drop_lines": item["malformed_lines"],
        }
        for item in [*segments, degraded]
        if item.get("status") == "malformed"
        and not item.get("repairable_truncated_tail")
        and item.get("sha256")
    ]
    if keyed_append_blocked:
        status = "blocked"
    elif pending_count or quarantined:
        status = "attention"
    else:
        status = "healthy"

    guidance: list[str] = []
    reports = [*unhealthy_segments]
    if degraded_unhealthy:
        reports.append(degraded)
    if any(item.get("status") == "unreadable" for item in reports):
        guidance.append("Restore local read access, then rerun audit-health before keyed mutations.")
    if any(item.get("repairable_truncated_tail") for item in reports):
        guidance.append(
            "Retry the original OA operation after confirming no writer is active; bounded tail repair will quarantine raw bytes before truncation."
        )
    if any(
        item.get("status") == "malformed" and not item.get("repairable_truncated_tail")
        for item in reports
    ):
        guidance.append(
            "Stop keyed audit mutations; no automatic repair is safe for terminated, interior, or rotated corruption. Use audit-repair with the exact repair_candidates fingerprint and line set."
        )
    if pending_count and not blocked_replay:
        guidance.append("Retry the original OA operation to replay the healthy degraded backlog.")
    if quarantined:
        guidance.append("Retain .corrupt fragments for incident review; they are never replayed.")
    if not guidance:
        guidance.append("No action required.")
    return {
        "event": "audit_health",
        "status": status,
        "keyed_append_blocked": keyed_append_blocked,
        "blocked_replay": blocked_replay,
        "segments": segments,
        "degraded": degraded,
        "pending_count": pending_count,
        "repair_candidates": repair_candidates,
        "quarantined_fragments": quarantined,
        "guidance": guidance,
    }


def repair(
    root: Path,
    segment: str,
    expected_sha256: str,
    drop_lines: list[int],
) -> dict[str, Any]:
    """Replace one corrupt audit segment under an exact operator fence.

    The operator must name every malformed line and no valid line. The original
    bytes are durably preserved before an atomic replacement becomes visible.
    """
    if not isinstance(segment, str) or not AUDIT_REPAIR_SEGMENT_RE.fullmatch(segment):
        raise ValueError(
            "segment must be events.jsonl, events.<digits>.jsonl, or degraded.jsonl"
        )
    expected = expected_sha256.casefold() if isinstance(expected_sha256, str) else ""
    if not SHA256_RE.fullmatch(expected):
        raise ValueError("expected_sha256 must be exactly 64 hexadecimal characters")
    if not drop_lines or any(not isinstance(line, int) or line <= 0 for line in drop_lines):
        raise ValueError("drop_lines must contain positive line numbers")
    if len(set(drop_lines)) != len(drop_lines):
        raise ValueError("drop_lines must not contain duplicates")
    selected = sorted(drop_lines)
    selected_set = set(selected)

    directory = root.resolve() / "var" / "audit"
    target = directory / segment
    with FileLock(directory / ".audit.lock"):
        with FileLock(directory / ".degraded.lock"):
            try:
                original = target.read_bytes()
            except OSError as error:
                raise OSError(f"cannot read audit segment {segment}: {error}") from error
            original_digest = hashlib.sha256(original).hexdigest()
            if original_digest != expected:
                raise ValueError(
                    f"audit segment fingerprint changed: expected {expected}, found {original_digest}"
                )

            malformed = _malformed_line_numbers(original)
            if selected != malformed:
                raise ValueError(
                    "drop_lines must exactly match every malformed line; "
                    f"selected={selected}, malformed={malformed}"
                )
            raw_lines = original.splitlines(keepends=True)
            candidate = b"".join(
                raw for index, raw in enumerate(raw_lines, start=1) if index not in selected_set
            )
            if candidate and not candidate.endswith(b"\n"):
                candidate += b"\n"
            remaining_malformed = _malformed_line_numbers(candidate)
            if remaining_malformed:
                raise ValueError(
                    f"repair candidate still contains malformed lines: {remaining_malformed}"
                )

            corrupt = directory / ".corrupt"
            corrupt.mkdir(parents=True, exist_ok=True)
            backup = corrupt / f"{segment}.{original_digest}.repair-original"
            try:
                with open(backup, "xb") as handle:
                    handle.write(original)
                    _durable_flush(handle)
            except FileExistsError:
                if backup.read_bytes() != original:
                    raise OSError(f"repair backup collision: {backup}")

            temporary = ""
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=directory,
                    prefix=f".{segment}.repair-",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = handle.name
                    handle.write(candidate)
                    _durable_flush(handle)
                _replace_retry(temporary, target)
                temporary = ""
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)

            repaired = target.read_bytes()
            if repaired != candidate:
                raise OSError(f"audit segment verification failed after repair: {segment}")
            repaired_digest = hashlib.sha256(repaired).hexdigest()

    return {
        "event": "audit_segment_repaired",
        "segment": segment,
        "original_sha256": original_digest,
        "repaired_sha256": repaired_digest,
        "dropped_lines": selected,
        "original_bytes": len(original),
        "repaired_bytes": len(repaired),
        "backup": backup.relative_to(directory).as_posix(),
    }


def prune_rotated(root: Path, older_than: datetime) -> int:
    """Remove safe rotated segments while preserving pending replay evidence."""
    directory = root.resolve() / "var" / "audit"
    removed = 0
    if not directory.is_dir():
        return removed
    with FileLock(directory / ".audit.lock"):
        with FileLock(directory / ".degraded.lock"):
            degraded = directory / "degraded.jsonl"
            pending_keys: set[str] = set()
            if degraded.is_file():
                keys = _file_keys(degraded)
                if keys is None:
                    return removed
                pending_keys = keys
            for path in sorted(directory.glob("events.*.jsonl")):
                try:
                    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                except FileNotFoundError:
                    continue
                if modified > older_than:
                    continue
                if pending_keys:
                    segment_keys = _file_keys(path)
                    if segment_keys is None or segment_keys & pending_keys:
                        continue
                path.unlink(missing_ok=True)
                removed += 1
    return removed
