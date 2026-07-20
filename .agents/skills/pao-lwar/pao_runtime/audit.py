from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import FileLock, utc_now


def record(root: Path, actor: str, payload: dict[str, Any]) -> None:
    """Append one audit event to var/audit/events.jsonl (append-only).

    Never raises into the caller's control flow beyond filesystem errors;
    stdout is reserved for `emit`, so audit stays file-only.
    """
    path = root.resolve() / "var" / "audit" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"schema_version": "pao.audit-event.v1", "ts": utc_now(), "actor": actor, **payload},
        ensure_ascii=False,
        sort_keys=True,
    )
    with FileLock(path.parent / ".audit.lock"):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
