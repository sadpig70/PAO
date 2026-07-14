from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LWAR_ID_RE = re.compile(r"^LWAR[1-9][0-9]*$")
INSTANCE_ID_RE = re.compile(r"^lwar-instance-[a-f0-9]{32}$")
TASK_ID_RE = re.compile(r"^task-[A-Za-z0-9][A-Za-z0-9._-]*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".pao-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def validate_lwar_id(value: str) -> str:
    if not LWAR_ID_RE.fullmatch(value):
        raise ValueError("lwar_id must match LWAR<positive integer>")
    return value


def validate_instance_id(value: str) -> str:
    if not INSTANCE_ID_RE.fullmatch(value):
        raise ValueError("instance_id must match lwar-instance-<32 lowercase hex>")
    return value


def validate_task_id(value: str) -> str:
    if not TASK_ID_RE.fullmatch(value):
        raise ValueError("task_id must start with task- and contain only safe filename characters")
    return value


MAILBOX_DIRS = (
    "incoming",
    "claimed",
    "outgoing",
    "control",
    "control_claimed",
    "leases",
    "archive/tasks",
    "archive/results",
    "archive/control",
    "failed",
    "work",
)


def mailbox_root(root: Path, lwar_id: str) -> Path:
    return root / "mailbox" / validate_lwar_id(lwar_id)


def ensure_mailbox(root: Path, lwar_id: str) -> Path:
    mailbox = mailbox_root(root, lwar_id)
    for relative in MAILBOX_DIRS:
        (mailbox / relative).mkdir(parents=True, exist_ok=True)
    return mailbox


def claim_file(source: Path, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
        return True
    except FileNotFoundError:
        return False


class FileLock:
    """Small cross-platform lockfile with stale-lock recovery."""

    def __init__(self, path: Path, timeout_s: float = 5.0, stale_s: float = 30.0):
        self.path = path
        self.timeout_s = timeout_s
        self.stale_s = stale_s
        self.acquired = False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(f"{os.getpid()} {utc_now()}\n")
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > self.stale_s:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock timeout: {self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False
