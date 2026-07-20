from __future__ import annotations

import hashlib
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


def resolve_root(value: str | None) -> Path:
    """Resolve the bus root: explicit --root, then PAO_ROOT env, then a `.pao/`
    folder under the current directory.

    The `.pao/` default keeps all PAO state (mailbox/, var/, control/) namespaced
    in one hidden folder instead of scattering it across the project workspace —
    add `.pao/` to .gitignore. Set PAO_ROOT (or pass --root) to point at a
    central bus outside the project instead.
    """
    if value:
        return Path(value).resolve()
    env_value = os.environ.get("PAO_ROOT", "").strip()
    if env_value:
        return Path(env_value).resolve()
    return (Path.cwd() / ".pao").resolve()


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


def safe_load_json(path: Path) -> dict[str, Any] | None:
    """Lenient read: return None instead of raising on a missing, empty,
    truncated, non-object, or undecodable file.

    Sweeps over bus directories use this to skip-and-continue past a single
    poison file (crash-truncated, hand-edited, disk-faulted) rather than
    aborting the whole pass — a corrupt file must never wedge a subsystem.
    JSONDecodeError is a ValueError; FileNotFoundError/permission errors are
    OSError.
    """
    try:
        return load_json(path)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def quarantine_corrupt(path: Path, reason: str) -> Path | None:
    """Best-effort: move a corrupt file into a `.corrupt/` sibling with a
    reason marker so it stops re-tripping a sweep and stays inspectable.

    Returns the quarantine path, or None if it could not be moved (in which
    case the caller still skips it). The `.corrupt/` subdirectory is never
    matched by the `*.json` globs the sweeps use.
    """
    try:
        corrupt_dir = path.parent / ".corrupt"
        corrupt_dir.mkdir(parents=True, exist_ok=True)
        destination = corrupt_dir / f"{path.name}.{uuid.uuid4().hex[:8]}"
        os.replace(path, destination)
        try:
            atomic_write_json(
                destination.with_suffix(destination.suffix + ".error.json"),
                {"reason": reason, "original": str(path), "quarantined_at": utc_now()},
            )
        except OSError:
            pass
        return destination
    except OSError:
        return None


def _replace_retry(source: Any, destination: Any, attempts: int = 10, base_delay_s: float = 0.05) -> None:
    """`os.replace` with bounded backoff on Windows sharing violations.

    A concurrent reader or an antivirus scan holding the destination open makes
    `os.replace` raise `PermissionError` (WinError 5/32) transiently; retry a
    few times before surfacing it. `FileNotFoundError` (missing source) is a
    real absence and never retried.
    """
    last: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except FileNotFoundError:
            raise
        except PermissionError as error:
            last = error
            time.sleep(base_delay_s * (attempt + 1))
    if last is not None:
        raise last


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
        _replace_retry(temporary, path)
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


BUS_CONTROL_SUBDIRS = ("mailbox", "var", "control")


def path_within(child: Path, parent: Path) -> bool:
    """Case-insensitive-on-Windows containment check; cross-drive/UNC → False."""
    try:
        child_key = os.path.normcase(str(Path(child).resolve()))
        parent_key = os.path.normcase(str(Path(parent).resolve()))
    except OSError:
        return False
    if child_key == parent_key:
        return True
    return child_key.startswith(parent_key.rstrip("\\/") + os.sep)


def runtime_bundle_root() -> Path:
    return Path(__file__).resolve().parents[1]


def authority_denied_reason(path: Path, root: Path) -> str | None:
    """Deny the bus control surfaces and the runtime bundle — never their ancestors.

    Fail closed: a path that cannot even be resolved (OSError from resolve() —
    e.g. an over-long or malformed Windows path) is denied rather than waved
    through. A defense-in-depth check must never weaken to "couldn't verify, so
    allow".
    """
    try:
        Path(path).resolve()
    except OSError:
        return "unresolvable_path"
    for name in BUS_CONTROL_SUBDIRS:
        if path_within(path, root / name):
            return f"inside_bus_{name}"
    if path_within(path, runtime_bundle_root()):
        return "inside_runtime_bundle"
    return None


def snapshot_artifact(source: Path, store: Path, max_bytes: int | None) -> tuple[str, int, Path]:
    """Copy-while-hashing into the content-addressed store (single pass, capped)."""
    store.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    temporary = ""
    try:
        with source.open("rb") as reader, tempfile.NamedTemporaryFile(
            mode="wb", dir=store, prefix=".pao-", suffix=".tmp", delete=False
        ) as writer:
            temporary = writer.name
            while chunk := reader.read(1 << 20):
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ValueError(f"artifact exceeds max_artifact_bytes ({max_bytes}): {source}")
                digest.update(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        destination = store / digest.hexdigest()
        _replace_retry(temporary, destination)
        temporary = ""
        return digest.hexdigest(), total, destination
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


MAILBOX_DIRS = (
    "incoming",
    "claimed",
    "outgoing",
    "control",
    "control_claimed",
    "cancelled",
    "leases",
    "archive/tasks",
    "archive/results",
    "archive/control",
    "failed",
    "dead",
    "quarantine",
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
        _replace_retry(source, destination)
        return True
    except FileNotFoundError:
        return False


def _pid_alive(pid: int) -> bool | None:
    """Best-effort liveness probe. True/False on POSIX; None (unknown) where it
    cannot be determined without extra dependencies (e.g. Windows), so callers
    fall back to the age heuristic instead of a confident steal."""
    if pid <= 0:
        return False
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


class FileLock:
    """Small cross-platform lockfile with ownership-checked release.

    The lock content is `"<pid> <token> <utc>"`. A stale lock is stolen only
    when it is both older than `stale_s` AND its recorded PID is not alive
    (where liveness is knowable); on release only a lock still carrying THIS
    holder's token is removed, so a lock stolen out from under a slow holder is
    never deleted by that holder — preventing the double-grant cascade.
    """

    def __init__(self, path: Path, timeout_s: float = 5.0, stale_s: float = 30.0):
        self.path = path
        self.timeout_s = timeout_s
        self.stale_s = stale_s
        self.acquired = False
        self.token = uuid.uuid4().hex

    def _holder_pid(self) -> int:
        try:
            parts = self.path.read_text(encoding="utf-8").split()
            return int(parts[0]) if parts else -1
        except (OSError, ValueError):
            return -1

    def _owns(self) -> bool:
        try:
            parts = self.path.read_text(encoding="utf-8").split()
        except OSError:
            return False
        return len(parts) >= 2 and parts[1] == self.token

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(f"{os.getpid()} {self.token} {utc_now()}\n")
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > self.stale_s and _pid_alive(self._holder_pid()) is not True:
                        # Stale AND its owner is provably gone (or liveness is
                        # unknown and it has aged well past any real hold time).
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock timeout: {self.path}")
                time.sleep(0.05)

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.acquired:
            # Only remove the lock if we still own it: a lock stolen from a slow
            # holder must not be deleted by that holder (that would free a lock
            # a third party now legitimately holds).
            if self._owns():
                self.path.unlink(missing_ok=True)
            self.acquired = False
