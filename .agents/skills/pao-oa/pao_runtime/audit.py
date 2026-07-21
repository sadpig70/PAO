from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import FileLock, utc_now


AUDIT_SEGMENT_MAX_BYTES = 10 * 1024 * 1024


def record(root: Path, actor: str, payload: dict[str, Any]) -> bool:
    """Append one audit event to var/audit/events.jsonl (append-only).

    Never raises into the caller's control flow beyond filesystem errors;
    stdout is reserved for `emit`, so audit stays file-only.
    """
    path = root.resolve() / "var" / "audit" / "events.jsonl"
    degraded = path.parent / "degraded.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"schema_version": "pao.audit-event.v1", "ts": utc_now(), "actor": actor, **payload},
            ensure_ascii=False,
            sort_keys=True,
        )
        with FileLock(path.parent / ".audit.lock"):
            if path.is_file() and path.stat().st_size >= AUDIT_SEGMENT_MAX_BYTES:
                path.replace(path.parent / f"events.{time.time_ns()}.jsonl")
            backlog: list[str] = []
            if degraded.is_file():
                try:
                    backlog = degraded.read_text(encoding="utf-8").splitlines()
                except OSError:
                    backlog = []
            with open(path, "a", encoding="utf-8") as handle:
                for pending in backlog:
                    handle.write(pending + "\n")
                handle.write(line + "\n")
                handle.flush()
            if backlog:
                degraded.unlink(missing_ok=True)
        return True
    except (OSError, TimeoutError) as error:
        # A transient failure of the active append target must not affect the
        # command. Preserve a best-effort backlog in a separate segment; the
        # next successful record replays it into the canonical audit stream.
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(degraded, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
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


def prune_rotated(root: Path, older_than: datetime) -> int:
    """Remove rotated audit segments only; preserve the active append-only log."""
    directory = root.resolve() / "var" / "audit"
    removed = 0
    if not directory.is_dir():
        return removed
    for path in sorted(directory.glob("events.*.jsonl")):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except FileNotFoundError:
            continue
        if modified <= older_than:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
