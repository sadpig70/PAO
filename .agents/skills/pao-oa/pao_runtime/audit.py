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

from .common import FileLock, _replace_retry, atomic_write_json, parse_utc, utc_now


AUDIT_SEGMENT_MAX_BYTES = 10 * 1024 * 1024
AUDIT_REPAIR_SEGMENT_RE = re.compile(r"(?:events(?:\.\d+)?|degraded)\.jsonl")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
AUDIT_REPAIR_RECEIPT_SCHEMA = "pao.audit-repair-receipt.v1"
AUDIT_REPAIR_PHASES = {"prepared", "replaced", "committed"}
AUDIT_REPAIR_RETENTION_SCHEMA = "pao.audit-repair-retention.v1"
AUDIT_REPAIR_RETENTION_PHASES = {"authorized", "backup_staged"}
AUDIT_ROTATED_PRUNE_SCHEMA = "pao.rotated-prune-receipt.v1"
AUDIT_ROTATED_PRUNE_PHASES = {"prepared", "applied"}
AUDIT_ROTATED_PRESERVATION_SCHEMA = "pao.rotated-prune-preservation.v1"
AUDIT_ROTATED_PRUNE_REASON_CODES = {
    "valid_expired",
    "retention_target",
    "retention_audit_key",
    "degraded_replay_key",
    "retention_snapshot_invalid",
    "degraded_snapshot_invalid",
    "segment_unreadable",
    "invalid_utf8",
    "malformed_jsonl",
    "non_object_jsonl",
    "metadata_unreadable",
    "segment_disappeared",
    "segment_drifted",
    "unlink_failed",
    "operator_preserved_recreated_segment",
    "operator_preserved_target",
    "preservation_snapshot_invalid",
}
AUDIT_ROTATED_PRUNE_PATH_RE = re.compile(r"var/audit/events\.\d+\.jsonl")
AUDIT_ROTATED_SEGMENT_RE = re.compile(r"events\.\d+\.jsonl")
AUDIT_PRESERVATION_RELEASE_KEY_RE = re.compile(
    r"rotated-preserve-release:"
    r"([0-9a-f]{64}):(events\.\d+\.jsonl):"
    r"([0-9a-f]{64}):([0-9a-f]{64})"
)


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


def _repair_candidate(original: bytes, selected: list[int]) -> bytes:
    malformed = _malformed_line_numbers(original)
    if selected != malformed:
        raise ValueError(
            "drop_lines must exactly match every malformed line; "
            f"selected={selected}, malformed={malformed}"
        )
    selected_set = set(selected)
    candidate = b"".join(
        raw
        for index, raw in enumerate(original.splitlines(keepends=True), start=1)
        if index not in selected_set
    )
    if candidate and not candidate.endswith(b"\n"):
        candidate += b"\n"
    remaining_malformed = _malformed_line_numbers(candidate)
    if remaining_malformed:
        raise ValueError(f"repair candidate still contains malformed lines: {remaining_malformed}")
    return candidate


def _repair_receipt_path(directory: Path, segment: str, original_sha256: str) -> Path:
    return directory / ".repairs" / f"{segment}.{original_sha256}.json"


def _repair_retention_path(directory: Path, segment: str, original_sha256: str) -> Path:
    return directory / ".repair-prune" / f"{segment}.{original_sha256}.json"


def _repair_retention_stage_path(
    directory: Path, segment: str, original_sha256: str
) -> Path:
    return (
        directory
        / ".repair-prune"
        / f"{segment}.{original_sha256}.repair-original"
    )


def _load_repair_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OSError(f"unreadable audit repair receipt: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"audit repair receipt must be an object: {path}")
    segment = value.get("segment")
    original = value.get("original_sha256")
    repaired = value.get("repaired_sha256")
    dropped = value.get("dropped_lines")
    phase = value.get("phase")
    if value.get("schema_version") != AUDIT_REPAIR_RECEIPT_SCHEMA:
        raise ValueError(f"unsupported audit repair receipt schema: {path}")
    if not isinstance(segment, str) or not AUDIT_REPAIR_SEGMENT_RE.fullmatch(segment):
        raise ValueError(f"invalid audit repair receipt segment: {path}")
    if not isinstance(original, str) or not SHA256_RE.fullmatch(original):
        raise ValueError(f"invalid original hash in audit repair receipt: {path}")
    if not isinstance(repaired, str) or not SHA256_RE.fullmatch(repaired):
        raise ValueError(f"invalid repaired hash in audit repair receipt: {path}")
    if (
        not isinstance(dropped, list)
        or not dropped
        or any(not isinstance(line, int) or line <= 0 for line in dropped)
        or dropped != sorted(set(dropped))
    ):
        raise ValueError(f"invalid dropped lines in audit repair receipt: {path}")
    if phase not in AUDIT_REPAIR_PHASES:
        raise ValueError(f"invalid phase in audit repair receipt: {path}")
    if path.name != f"{segment}.{original}.json":
        raise ValueError(f"audit repair receipt filename does not match its identity: {path}")
    expected_backup = f".corrupt/{segment}.{original}.repair-original"
    if value.get("backup") != expected_backup:
        raise ValueError(f"audit repair receipt backup path mismatch: {path}")
    for field in ("original_bytes", "repaired_bytes"):
        if not isinstance(value.get(field), int) or value[field] < 0:
            raise ValueError(f"invalid {field} in audit repair receipt: {path}")
    if not isinstance(value.get("prepared_at"), str) or not value["prepared_at"]:
        raise ValueError(f"missing prepared_at in audit repair receipt: {path}")
    if not isinstance(value.get("audit_event_committed"), bool):
        raise ValueError(f"invalid audit completion flag in repair receipt: {path}")
    if phase in {"replaced", "committed"} and not isinstance(value.get("replaced_at"), str):
        raise ValueError(f"missing replaced_at in audit repair receipt: {path}")
    if phase == "committed" and (
        value.get("audit_event_committed") is not True
        or not isinstance(value.get("committed_at"), str)
    ):
        raise ValueError(f"incomplete committed audit repair receipt: {path}")
    return value


def _load_repair_retention(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OSError(f"unreadable audit repair retention tombstone: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"audit repair retention tombstone must be an object: {path}")
    segment = value.get("segment")
    original = value.get("original_sha256")
    repaired = value.get("repaired_sha256")
    if value.get("schema_version") != AUDIT_REPAIR_RETENTION_SCHEMA:
        raise ValueError(f"unsupported audit repair retention schema: {path}")
    if value.get("phase") not in AUDIT_REPAIR_RETENTION_PHASES:
        raise ValueError(f"invalid audit repair retention phase: {path}")
    if not isinstance(segment, str) or not AUDIT_REPAIR_SEGMENT_RE.fullmatch(segment):
        raise ValueError(f"invalid audit repair retention segment: {path}")
    if not isinstance(original, str) or not SHA256_RE.fullmatch(original):
        raise ValueError(f"invalid original hash in audit repair retention: {path}")
    if not isinstance(repaired, str) or not SHA256_RE.fullmatch(repaired):
        raise ValueError(f"invalid repaired hash in audit repair retention: {path}")
    if path.name != f"{segment}.{original}.json":
        raise ValueError(f"audit repair retention filename does not match identity: {path}")
    expected_receipt = f".repairs/{segment}.{original}.json"
    expected_backup = f".corrupt/{segment}.{original}.repair-original"
    expected_staged = f".repair-prune/{segment}.{original}.repair-original"
    expected_key = f"audit-repair:{segment}:{original}"
    if value.get("receipt") != expected_receipt:
        raise ValueError(f"audit repair retention receipt path mismatch: {path}")
    if value.get("backup") != expected_backup:
        raise ValueError(f"audit repair retention backup path mismatch: {path}")
    if value.get("staged_backup") != expected_staged:
        raise ValueError(f"audit repair retention staged path mismatch: {path}")
    if value.get("audit_key") != expected_key:
        raise ValueError(f"audit repair retention audit key mismatch: {path}")
    if not isinstance(value.get("receipt_sha256"), str) or not SHA256_RE.fullmatch(
        value["receipt_sha256"]
    ):
        raise ValueError(f"invalid receipt hash in audit repair retention: {path}")
    for field in ("original_bytes", "repaired_bytes"):
        if not isinstance(value.get(field), int) or value[field] < 0:
            raise ValueError(f"invalid {field} in audit repair retention: {path}")
    for field in ("committed_at", "authorized_at"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f"missing {field} in audit repair retention: {path}")
    if value["phase"] == "backup_staged" and (
        not isinstance(value.get("staged_at"), str) or not value["staged_at"]
    ):
        raise ValueError(f"missing staged_at in audit repair retention: {path}")
    return value


def _rotated_prune_receipt_path(directory: Path, run_id: str) -> Path:
    return directory / ".rotated-prune" / f"{run_id}.json"


def _rotated_preservation_path(directory: Path, run_id: str, segment: str) -> Path:
    return directory / ".rotated-preserve" / f"{run_id}.{segment}.json"


def _load_rotated_prune_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OSError(f"unreadable rotated prune receipt: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"rotated prune receipt must be an object: {path}")
    run_id = value.get("run_id")
    phase = value.get("phase")
    if value.get("schema_version") != AUDIT_ROTATED_PRUNE_SCHEMA:
        raise ValueError(f"unsupported rotated prune receipt schema: {path}")
    if not isinstance(run_id, str) or not SHA256_RE.fullmatch(run_id):
        raise ValueError(f"invalid rotated prune run id: {path}")
    if path.name != f"{run_id}.json":
        raise ValueError(f"rotated prune receipt filename does not match identity: {path}")
    if phase not in AUDIT_ROTATED_PRUNE_PHASES:
        raise ValueError(f"invalid rotated prune receipt phase: {path}")
    if value.get("audit_key") != f"rotated-prune:{run_id}":
        raise ValueError(f"rotated prune receipt audit key mismatch: {path}")
    for field in ("cutoff", "created_at"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f"missing {field} in rotated prune receipt: {path}")
        parse_utc(value[field])
    if phase == "applied":
        if not isinstance(value.get("applied_at"), str) or not value["applied_at"]:
            raise ValueError(f"missing applied_at in rotated prune receipt: {path}")
        parse_utc(value["applied_at"])

    outcomes = value.get("outcomes")
    if not isinstance(outcomes, list):
        raise ValueError(f"invalid outcomes in rotated prune receipt: {path}")
    seen_paths: set[str] = set()
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            raise ValueError(f"rotated prune outcome must be an object: {path}")
        relative = outcome.get("path")
        status = outcome.get("status")
        reasons = outcome.get("reason_codes")
        action = outcome.get("action")
        if (
            not isinstance(relative, str)
            or not AUDIT_ROTATED_PRUNE_PATH_RE.fullmatch(relative)
            or relative in seen_paths
        ):
            raise ValueError(f"invalid or duplicate rotated prune path: {path}")
        seen_paths.add(relative)
        if status not in {"removed", "protected", "blocked"}:
            raise ValueError(f"invalid rotated prune status: {path}")
        if (
            not isinstance(reasons, list)
            or not reasons
            or len(reasons) != len(set(reasons))
            or any(reason not in AUDIT_ROTATED_PRUNE_REASON_CODES for reason in reasons)
        ):
            raise ValueError(f"invalid rotated prune reason codes: {path}")
        if action not in {"remove", "retain"}:
            raise ValueError(f"invalid rotated prune action: {path}")
        if action == "remove":
            if status != "removed" or reasons != ["valid_expired"]:
                raise ValueError(f"invalid rotated prune removal decision: {path}")
            digest = outcome.get("expected_sha256")
            size = outcome.get("expected_bytes")
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                raise ValueError(f"invalid rotated prune fingerprint: {path}")
            if not isinstance(size, int) or size < 0:
                raise ValueError(f"invalid rotated prune byte count: {path}")
        else:
            if status == "removed":
                raise ValueError(f"retained rotated prune outcome is removed: {path}")
            if "expected_sha256" in outcome or "expected_bytes" in outcome:
                raise ValueError(f"retained rotated prune outcome has a witness: {path}")
            allowed_reasons = (
                {
                    "retention_target",
                    "retention_audit_key",
                    "degraded_replay_key",
                    "operator_preserved_target",
                }
                if status == "protected"
                else AUDIT_ROTATED_PRUNE_REASON_CODES
                - {
                    "valid_expired",
                    "retention_target",
                    "retention_audit_key",
                    "degraded_replay_key",
                    "operator_preserved_target",
                }
            )
            if any(reason not in allowed_reasons for reason in reasons):
                raise ValueError(f"rotated prune status/reason mismatch: {path}")
        resolution = outcome.get("resolution")
        if resolution is None:
            if "operator_preserved_recreated_segment" in reasons:
                raise ValueError(f"rotated prune resolution metadata missing: {path}")
        else:
            if (
                status != "blocked"
                or action != "retain"
                or reasons != ["operator_preserved_recreated_segment"]
                or not isinstance(resolution, dict)
            ):
                raise ValueError(f"invalid rotated prune resolution outcome: {path}")
            segment = Path(relative).name
            expected_marker = _rotated_preservation_path(
                path.parents[1], run_id, segment
            ).relative_to(path.parents[1]).as_posix()
            expected_resolution_key = (
                f"rotated-prune-resolve:{run_id}:{segment}:"
                f"{resolution.get('preserved_sha256')}"
            )
            if resolution.get("decision") != "preserve-recreated":
                raise ValueError(f"invalid rotated prune resolution decision: {path}")
            if resolution.get("preservation") != expected_marker:
                raise ValueError(f"rotated prune preservation path mismatch: {path}")
            if resolution.get("audit_key") != expected_resolution_key:
                raise ValueError(f"rotated prune resolution audit key mismatch: {path}")
            for field in (
                "receipt_sha256_before",
                "original_expected_sha256",
                "preserved_sha256",
            ):
                if not isinstance(resolution.get(field), str) or not SHA256_RE.fullmatch(
                    resolution[field]
                ):
                    raise ValueError(f"invalid {field} in rotated prune resolution: {path}")
            for field in ("original_expected_bytes", "preserved_bytes"):
                if not isinstance(resolution.get(field), int) or resolution[field] < 0:
                    raise ValueError(f"invalid {field} in rotated prune resolution: {path}")
            if (
                not isinstance(resolution.get("resolved_at"), str)
                or not resolution["resolved_at"]
            ):
                raise ValueError(f"missing resolved_at in rotated prune resolution: {path}")
            parse_utc(resolution["resolved_at"])
        if "error" in outcome and (
            not isinstance(outcome["error"], str) or not outcome["error"]
        ):
            raise ValueError(f"invalid rotated prune error detail: {path}")
    if [item["path"] for item in outcomes] != sorted(seen_paths):
        raise ValueError(f"rotated prune outcomes are not path-sorted: {path}")
    return value


def _load_rotated_preservation(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OSError(f"unreadable rotated preservation marker: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"rotated preservation marker must be an object: {path}")
    run_id = value.get("run_id")
    segment = value.get("segment")
    preserved = value.get("preserved_sha256")
    if value.get("schema_version") != AUDIT_ROTATED_PRESERVATION_SCHEMA:
        raise ValueError(f"unsupported rotated preservation schema: {path}")
    if not isinstance(run_id, str) or not SHA256_RE.fullmatch(run_id):
        raise ValueError(f"invalid rotated preservation run id: {path}")
    if not isinstance(segment, str) or not AUDIT_ROTATED_SEGMENT_RE.fullmatch(segment):
        raise ValueError(f"invalid rotated preservation segment: {path}")
    if path.name != f"{run_id}.{segment}.json":
        raise ValueError(f"rotated preservation filename mismatch: {path}")
    if not isinstance(preserved, str) or not SHA256_RE.fullmatch(preserved):
        raise ValueError(f"invalid rotated preservation fingerprint: {path}")
    if not isinstance(value.get("preserved_bytes"), int) or value["preserved_bytes"] < 0:
        raise ValueError(f"invalid rotated preservation byte count: {path}")
    if (
        not isinstance(value.get("receipt_sha256_before"), str)
        or not SHA256_RE.fullmatch(value["receipt_sha256_before"])
    ):
        raise ValueError(f"invalid rotated preservation receipt fingerprint: {path}")
    if (
        not isinstance(value.get("original_expected_sha256"), str)
        or not SHA256_RE.fullmatch(value["original_expected_sha256"])
    ):
        raise ValueError(f"invalid rotated preservation original fingerprint: {path}")
    if (
        not isinstance(value.get("original_expected_bytes"), int)
        or value["original_expected_bytes"] < 0
    ):
        raise ValueError(f"invalid rotated preservation original byte count: {path}")
    if value.get("decision") != "preserve-recreated":
        raise ValueError(f"invalid rotated preservation decision: {path}")
    expected_key = f"rotated-prune-resolve:{run_id}:{segment}:{preserved}"
    if value.get("audit_key") != expected_key:
        raise ValueError(f"rotated preservation audit key mismatch: {path}")
    if not isinstance(value.get("created_at"), str) or not value["created_at"]:
        raise ValueError(f"missing created_at in rotated preservation marker: {path}")
    parse_utc(value["created_at"])
    return value


def _validate_preserved_target(
    directory: Path, marker: dict[str, Any]
) -> Path:
    target = directory / marker["segment"]
    try:
        target.lstat()
    except OSError as error:
        raise OSError(f"preserved rotated target is unavailable: {target}") from error
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"preserved rotated target is not a regular file: {target}")
    try:
        data = target.read_bytes()
    except OSError as error:
        raise OSError(f"cannot read preserved rotated target: {target}") from error
    if (
        len(data) != marker["preserved_bytes"]
        or hashlib.sha256(data).hexdigest() != marker["preserved_sha256"]
    ):
        raise ValueError(f"preserved rotated target fingerprint drift: {target}")
    return target


def _validate_resolution_preservation(
    directory: Path,
    receipt: dict[str, Any],
    outcome: dict[str, Any],
) -> dict[str, Any]:
    resolution = outcome["resolution"]
    marker_path = directory / resolution["preservation"]
    marker = _load_rotated_preservation(marker_path)
    segment = Path(outcome["path"]).name
    expected = {
        "run_id": receipt["run_id"],
        "segment": segment,
        "preserved_sha256": resolution["preserved_sha256"],
        "preserved_bytes": resolution["preserved_bytes"],
        "receipt_sha256_before": resolution["receipt_sha256_before"],
        "original_expected_sha256": resolution["original_expected_sha256"],
        "original_expected_bytes": resolution["original_expected_bytes"],
        "decision": resolution["decision"],
        "audit_key": resolution["audit_key"],
    }
    for field, expected_value in expected.items():
        if marker.get(field) != expected_value:
            raise ValueError(f"rotated preservation marker field drift: {field}")
    _validate_preserved_target(directory, marker)
    return marker


def _pending_rotated_prune_receipt(directory: Path) -> Path | None:
    receipt_dir = directory / ".rotated-prune"
    try:
        if not receipt_dir.exists():
            return None
        if not receipt_dir.is_dir():
            raise OSError(f"rotated prune receipt path is not a directory: {receipt_dir}")
        entries = sorted(receipt_dir.iterdir())
    except OSError:
        raise
    if any(not path.is_file() or path.suffix != ".json" for path in entries):
        raise OSError(f"unexpected entry in rotated prune receipt directory: {receipt_dir}")
    if len(entries) > 1:
        raise OSError(f"multiple pending rotated prune receipts: {receipt_dir}")
    return entries[0] if entries else None


def _public_rotated_prune_outcomes(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    public = []
    for outcome in receipt["outcomes"]:
        item = {
            "path": outcome["path"],
            "status": outcome["status"],
            "reason_codes": outcome["reason_codes"],
        }
        if outcome.get("error"):
            item["error"] = outcome["error"]
        if outcome.get("resolution"):
            item["resolution"] = outcome["resolution"]
        public.append(item)
    return public


def _rotated_prune_report(
    directory: Path, receipt: dict[str, Any], resumed: bool
) -> dict[str, Any]:
    outcomes = _public_rotated_prune_outcomes(receipt)
    return {
        "audit_segments_removed": sum(
            item["status"] == "removed" for item in outcomes
        ),
        "audit_segments_protected": sum(
            item["status"] == "protected" for item in outcomes
        ),
        "audit_segments_blocked": sum(
            item["status"] == "blocked" for item in outcomes
        ),
        "audit_segment_outcomes": outcomes,
        "audit_prune_run_id": receipt["run_id"],
        "audit_prune_audit_key": receipt["audit_key"],
        "audit_prune_cutoff": receipt["cutoff"],
        "audit_prune_receipt": _rotated_prune_receipt_path(
            directory, receipt["run_id"]
        )
        .relative_to(directory.parents[1])
        .as_posix(),
        "audit_prune_receipt_phase": receipt["phase"],
        "audit_prune_resumed": resumed,
    }


def _require_applied_rotated_prune_topology(
    directory: Path, receipt: dict[str, Any]
) -> None:
    """Require every applied, authorized removal target to remain absent."""
    if receipt["phase"] != "applied":
        return
    for outcome in receipt["outcomes"]:
        if outcome["action"] != "remove":
            continue
        target = directory.parents[1] / outcome["path"]
        try:
            target.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise OSError(f"cannot inspect applied rotated prune target: {target}") from error
        raise ValueError(f"applied rotated prune target is present: {target}")
    for outcome in receipt["outcomes"]:
        if outcome.get("resolution"):
            _validate_resolution_preservation(directory, receipt, outcome)


def _apply_rotated_prune_receipt(
    directory: Path, receipt_path: Path, receipt: dict[str, Any]
) -> dict[str, Any]:
    """Converge exact pre-authorized deletions and persist their final outcomes."""
    if receipt["phase"] == "applied":
        _require_applied_rotated_prune_topology(directory, receipt)
        return receipt
    outcomes = [dict(item) for item in receipt["outcomes"]]
    for outcome in outcomes:
        if outcome["action"] != "remove":
            continue
        target = directory.parents[1] / outcome["path"]
        try:
            data = target.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as error:
            outcome.clear()
            outcome.update(
                {
                    "path": target.relative_to(directory.parents[1]).as_posix(),
                    "status": "blocked",
                    "reason_codes": ["segment_unreadable"],
                    "action": "retain",
                    "error": str(error),
                }
            )
            continue
        if (
            len(data) != outcome["expected_bytes"]
            or hashlib.sha256(data).hexdigest() != outcome["expected_sha256"]
        ):
            outcome.clear()
            outcome.update(
                {
                    "path": target.relative_to(directory.parents[1]).as_posix(),
                    "status": "blocked",
                    "reason_codes": ["segment_drifted"],
                    "action": "retain",
                }
            )
            continue
        try:
            target.unlink()
        except FileNotFoundError:
            continue
        except OSError as error:
            outcome.clear()
            outcome.update(
                {
                    "path": target.relative_to(directory.parents[1]).as_posix(),
                    "status": "blocked",
                    "reason_codes": ["unlink_failed"],
                    "action": "retain",
                    "error": str(error),
                }
            )
    receipt = {
        **receipt,
        "phase": "applied",
        "outcomes": outcomes,
        "applied_at": utc_now(),
    }
    atomic_write_json(receipt_path, receipt)
    return receipt


def _validate_repair_backup(directory: Path, receipt: dict[str, Any]) -> Path:
    backup = directory / receipt["backup"]
    try:
        data = backup.read_bytes()
    except OSError as error:
        raise OSError(f"cannot read audit repair backup: {backup}") from error
    if len(data) != receipt["original_bytes"]:
        raise ValueError(f"audit repair backup size drift: {backup}")
    if hashlib.sha256(data).hexdigest() != receipt["original_sha256"]:
        raise ValueError(f"audit repair backup fingerprint drift: {backup}")
    return backup


def _validate_retention_backup(path: Path, transaction: dict[str, Any]) -> None:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise OSError(f"cannot read staged audit repair backup: {path}") from error
    if len(data) != transaction["original_bytes"]:
        raise ValueError(f"audit repair retention backup size drift: {path}")
    if hashlib.sha256(data).hexdigest() != transaction["original_sha256"]:
        raise ValueError(f"audit repair retention backup fingerprint drift: {path}")


def _retention_transaction(
    directory: Path, receipt_path: Path, receipt: dict[str, Any]
) -> dict[str, Any]:
    receipt_bytes = receipt_path.read_bytes()
    return {
        "schema_version": AUDIT_REPAIR_RETENTION_SCHEMA,
        "phase": "authorized",
        "segment": receipt["segment"],
        "original_sha256": receipt["original_sha256"],
        "repaired_sha256": receipt["repaired_sha256"],
        "original_bytes": receipt["original_bytes"],
        "repaired_bytes": receipt["repaired_bytes"],
        "receipt": receipt_path.relative_to(directory).as_posix(),
        "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "backup": receipt["backup"],
        "staged_backup": _repair_retention_stage_path(
            directory, receipt["segment"], receipt["original_sha256"]
        )
        .relative_to(directory)
        .as_posix(),
        "audit_key": (
            f"audit-repair:{receipt['segment']}:{receipt['original_sha256']}"
        ),
        "committed_at": receipt["committed_at"],
        "authorized_at": utc_now(),
    }


def _validate_retention_receipt(
    receipt_path: Path, transaction: dict[str, Any]
) -> None:
    try:
        receipt_bytes = receipt_path.read_bytes()
    except OSError as error:
        raise OSError(f"cannot read retained audit repair receipt: {receipt_path}") from error
    if hashlib.sha256(receipt_bytes).hexdigest() != transaction["receipt_sha256"]:
        raise ValueError(f"audit repair retention receipt fingerprint drift: {receipt_path}")
    receipt = _load_repair_receipt(receipt_path)
    if receipt["phase"] != "committed":
        raise ValueError(f"audit repair retention receipt is not committed: {receipt_path}")
    for field in (
        "segment",
        "original_sha256",
        "repaired_sha256",
        "original_bytes",
        "repaired_bytes",
        "backup",
        "committed_at",
    ):
        if receipt[field] != transaction[field]:
            raise ValueError(
                f"audit repair retention receipt field {field} drift: {receipt_path}"
            )


def _repair_target_state(
    directory: Path, receipt: dict[str, Any]
) -> tuple[str, bytes | None]:
    target = directory / receipt["segment"]
    try:
        data = target.read_bytes()
    except FileNotFoundError:
        if receipt["segment"] == "degraded.jsonl":
            return "consumed", None
        return "missing", None
    except OSError:
        return "unreadable", None
    digest = hashlib.sha256(data).hexdigest()
    if digest == receipt["original_sha256"]:
        return "original", data
    if digest == receipt["repaired_sha256"]:
        return "repaired", data
    if receipt["segment"] == "events.jsonl":
        repaired_prefix = data[: receipt["repaired_bytes"]]
        if (
            len(repaired_prefix) == receipt["repaired_bytes"]
            and hashlib.sha256(repaired_prefix).hexdigest() == receipt["repaired_sha256"]
            and not _malformed_line_numbers(data)
        ):
            return "repaired", repaired_prefix
    return "drifted", data


def _resume_repair_retention(
    directory: Path,
    tombstone_path: Path,
    committed_keys: set[str],
) -> tuple[int, int]:
    """Resume one authorized repair-evidence cleanup without guessing."""
    receipt_removed = 0
    backup_removed = 0
    try:
        transaction = _load_repair_retention(tombstone_path)
        if transaction["audit_key"] not in committed_keys:
            return receipt_removed, backup_removed
        target_state, _ = _repair_target_state(directory, transaction)
        if target_state not in {"repaired", "consumed"}:
            return receipt_removed, backup_removed

        receipt_path = directory / transaction["receipt"]
        backup = directory / transaction["backup"]
        staged = directory / transaction["staged_backup"]
        for path in (receipt_path, backup, staged):
            if path.exists() and not path.is_file():
                return receipt_removed, backup_removed

        receipt_exists = receipt_path.is_file()
        backup_exists = backup.is_file()
        staged_exists = staged.is_file()
        if backup_exists and staged_exists:
            return receipt_removed, backup_removed

        if receipt_exists:
            if transaction["phase"] != "authorized" or staged_exists or not backup_exists:
                return receipt_removed, backup_removed
            _validate_retention_receipt(receipt_path, transaction)
            _validate_retention_backup(backup, transaction)
            try:
                receipt_path.unlink()
            except OSError:
                return receipt_removed, backup_removed
            receipt_removed = 1

        if backup_exists:
            if transaction["phase"] != "authorized" or staged_exists:
                return receipt_removed, backup_removed
            _validate_retention_backup(backup, transaction)
            try:
                _replace_retry(backup, staged)
            except OSError:
                return receipt_removed, backup_removed
            staged_exists = True
            backup_exists = False

        if staged_exists:
            _validate_retention_backup(staged, transaction)
            if transaction["phase"] == "authorized":
                transaction = {
                    **transaction,
                    "phase": "backup_staged",
                    "staged_at": utc_now(),
                }
                try:
                    atomic_write_json(tombstone_path, transaction)
                except OSError:
                    return receipt_removed, backup_removed
            try:
                staged.unlink()
            except OSError:
                return receipt_removed, backup_removed
            backup_removed = 1
        elif transaction["phase"] != "backup_staged" or receipt_exists:
            return receipt_removed, backup_removed

        try:
            tombstone_path.unlink()
        except OSError:
            pass
    except (OSError, TypeError, ValueError):
        return receipt_removed, backup_removed
    return receipt_removed, backup_removed


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


def _rotated_segment_keys(
    path: Path,
) -> tuple[set[str] | None, str | None, str | None, str | None, int | None]:
    """Return keys and a deletion witness, or one stable failure reason."""
    try:
        data = path.read_bytes()
    except OSError as error:
        return None, "segment_unreadable", str(error), None, None
    keys: set[str] = set()
    for raw in data.splitlines():
        try:
            line = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            return None, "invalid_utf8", str(error), None, None
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            return None, "malformed_jsonl", str(error), None, None
        if not isinstance(value, dict):
            return None, "non_object_jsonl", None, None, None
        key = value.get("idempotency_key")
        if isinstance(key, str) and key:
            keys.add(key)
    return keys, None, None, hashlib.sha256(data).hexdigest(), len(data)


def _logged_keys(directory: Path) -> set[str]:
    """Return a complete key snapshot or fail closed on an unreadable segment."""
    committed_keys: set[str] = set()
    for candidate in sorted(directory.glob("events*.jsonl")):
        keys = _file_keys(candidate)
        if keys is None:
            raise OSError(f"unreadable audit segment: {candidate}")
        committed_keys.update(keys)
    return committed_keys


def _logged_events_for_key(directory: Path, idempotency_key: str) -> list[dict[str, Any]]:
    """Return every committed event for one key or fail on an invalid segment."""
    events: list[dict[str, Any]] = []
    for candidate in sorted(directory.glob("events*.jsonl")):
        lines = _validated_lines(
            candidate,
            repair_truncated_tail=candidate.name == "events.jsonl",
        )
        if lines is None:
            raise OSError(f"unreadable audit segment: {candidate}")
        for line in lines:
            value = json.loads(line)
            if value.get("idempotency_key") == idempotency_key:
                events.append(value)
    return events


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


def _read_only_logged_keys(directory: Path) -> set[str] | None:
    """Read a complete audit key snapshot without tail repair or other writes."""
    committed_keys: set[str] = set()
    for path in sorted(directory.glob("events*.jsonl")):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            return None
        for raw in data.splitlines():
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if not isinstance(value, dict):
                return None
            key = value.get("idempotency_key")
            if isinstance(key, str) and key:
                committed_keys.add(key)
    return committed_keys


def _read_only_preservation_release_events(
    directory: Path,
) -> list[dict[str, Any]] | None:
    """Extract committed release evidence without locks, repair, or writes."""
    releases: list[dict[str, Any]] = []
    for path in sorted(directory.glob("events*.jsonl")):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            return None
        for line_number, raw in enumerate(data.splitlines(), start=1):
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            if not isinstance(value, dict):
                return None
            key = value.get("idempotency_key")
            if value.get("event") == "audit_preservation_released" or (
                isinstance(key, str)
                and key.startswith("rotated-preserve-release:")
            ):
                releases.append(
                    {
                        "path": path.relative_to(directory).as_posix(),
                        "line": line_number,
                        "event": value,
                    }
                )
    return releases


def _inspect_preservation_release_group(
    directory: Path,
    release_audit_key: str | None,
    entries: list[dict[str, Any]],
    preservation_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify one deterministic release-event group and its marker topology."""
    report: dict[str, Any] = {
        "release_audit_key": release_audit_key,
        "status": "blocked",
        "reason_codes": [],
        "event_count": len(entries),
        "locations": [
            {"path": item["path"], "line": item["line"]}
            for item in entries
        ],
    }
    reasons: list[str] = []
    match = (
        AUDIT_PRESERVATION_RELEASE_KEY_RE.fullmatch(release_audit_key)
        if isinstance(release_audit_key, str)
        else None
    )
    if match is None:
        report["reason_codes"] = ["invalid_release_key"]
        return report

    run_id, segment, marker_sha256, preserved_sha256 = match.groups()
    preservation = _rotated_preservation_path(
        directory, run_id, segment
    ).relative_to(directory).as_posix()
    pruned_audit_key = f"rotated-prune:{run_id}"
    resolution_audit_key = (
        f"rotated-prune-resolve:{run_id}:{segment}:{preserved_sha256}"
    )
    expected = {
        "schema_version": "pao.audit-event.v1",
        "actor": "oa",
        "event": "audit_preservation_released",
        "idempotency_key": release_audit_key,
        "decision": "release-protection",
        "run_id": run_id,
        "segment": segment,
        "preservation": preservation,
        "marker_sha256": marker_sha256,
        "preserved_sha256": preserved_sha256,
        "pruned_audit_key": pruned_audit_key,
        "resolution_audit_key": resolution_audit_key,
    }
    payloads_valid = True
    preserved_byte_counts: set[int] = set()
    payload_signatures: set[str] = set()
    signature_fields = (*expected.keys(), "preserved_bytes")
    for entry in entries:
        event = entry["event"]
        byte_count = event.get("preserved_bytes")
        if (
            any(event.get(field) != value for field, value in expected.items())
            or not isinstance(byte_count, int)
            or byte_count < 0
        ):
            payloads_valid = False
        else:
            preserved_byte_counts.add(byte_count)
        payload_signatures.add(
            json.dumps(
                {field: event.get(field) for field in signature_fields},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    if len(entries) > 1:
        reasons.append("duplicate_release_event")
    if (
        not payloads_valid
        or len(preserved_byte_counts) != 1
        or len(payload_signatures) > 1
    ):
        reasons.append("release_event_payload_conflict")

    preserved_bytes = (
        next(iter(preserved_byte_counts))
        if len(preserved_byte_counts) == 1
        else None
    )
    marker_path = directory / preservation
    marker_state = "absent"
    try:
        marker_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        marker_state = "unreadable"
        reasons.append("release_marker_unreadable")
        report["marker_error"] = str(error)
    else:
        marker_state = "present"
        if marker_path.is_symlink() or not marker_path.is_file():
            marker_state = "not_file"
            reasons.append("release_marker_not_file")
        else:
            try:
                marker_bytes = marker_path.read_bytes()
            except OSError as error:
                marker_state = "unreadable"
                reasons.append("release_marker_unreadable")
                report["marker_error"] = str(error)
            else:
                if hashlib.sha256(marker_bytes).hexdigest() != marker_sha256:
                    marker_state = "drifted"
                    reasons.append("release_marker_fingerprint_drift")

        matching_bindings = [
            item
            for item in preservation_reports
            if item.get("path") == preservation
        ]
        if len(matching_bindings) != 1:
            reasons.append("release_marker_binding_missing")
        elif matching_bindings[0]["status"] != "protected":
            reasons.append("release_marker_binding_blocked")
            report["marker_reason_codes"] = matching_bindings[0][
                "reason_codes"
            ]

    report.update(
        {
            "run_id": run_id,
            "segment": segment,
            "decision": "release-protection",
            "preservation": preservation,
            "marker_sha256": marker_sha256,
            "preserved_sha256": preserved_sha256,
            "preserved_bytes": preserved_bytes,
            "pruned_audit_key": pruned_audit_key,
            "resolution_audit_key": resolution_audit_key,
            "marker_state": marker_state,
            "reason_codes": list(dict.fromkeys(reasons)),
        }
    )
    if not reasons:
        if marker_state == "absent":
            report["status"] = "completed"
        else:
            report["status"] = "resumable"
            report["reason_codes"] = [
                "release_event_committed_marker_present"
            ]
    return report


def _inspect_preservation_releases(
    directory: Path,
    committed_keys: set[str] | None,
    preservation_reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Classify committed preservation-release evidence strictly read-only."""
    if committed_keys is None:
        return [
            {
                "release_audit_key": None,
                "status": "blocked",
                "reason_codes": ["audit_snapshot_incomplete"],
                "event_count": None,
                "locations": [],
            }
        ]
    entries = _read_only_preservation_release_events(directory)
    if entries is None:
        return [
            {
                "release_audit_key": None,
                "status": "blocked",
                "reason_codes": ["audit_snapshot_incomplete"],
                "event_count": None,
                "locations": [],
            }
        ]
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        key = entry["event"].get("idempotency_key")
        group_key = (
            key
            if isinstance(key, str) and key
            else f"<invalid>:{entry['path']}:{entry['line']}"
        )
        groups.setdefault(group_key, []).append(entry)
    return [
        _inspect_preservation_release_group(
            directory,
            key if not key.startswith("<invalid>:") else None,
            groups[key],
            preservation_reports,
        )
        for key in sorted(groups)
    ]


def _inspect_repair_retention(
    directory: Path,
    tombstone_path: Path,
    committed_keys: set[str] | None,
) -> dict[str, Any]:
    """Classify one retention tombstone without changing transaction state."""
    report: dict[str, Any] = {
        "path": tombstone_path.relative_to(directory).as_posix(),
        "status": "blocked",
        "reason_codes": [],
    }
    try:
        transaction = _load_repair_retention(tombstone_path)
    except (OSError, ValueError) as error:
        report.update(
            {
                "phase": None,
                "reason_codes": ["invalid_tombstone"],
                "error": str(error),
            }
        )
        return report

    receipt_path = directory / transaction["receipt"]
    backup = directory / transaction["backup"]
    staged = directory / transaction["staged_backup"]
    receipt_present = receipt_path.is_file()
    backup_present = backup.is_file()
    staged_present = staged.is_file()
    target_state, _ = _repair_target_state(directory, transaction)
    audit_key_present = (
        None if committed_keys is None else transaction["audit_key"] in committed_keys
    )
    report.update(
        {
            "phase": transaction["phase"],
            "segment": transaction["segment"],
            "original_sha256": transaction["original_sha256"],
            "audit_key_present": audit_key_present,
            "target_state": target_state,
            "receipt_present": receipt_present,
            "backup_present": backup_present,
            "staged_backup_present": staged_present,
        }
    )
    reasons: list[str] = []
    errors: list[str] = []
    if committed_keys is None:
        reasons.append("audit_snapshot_incomplete")
    elif not audit_key_present:
        reasons.append("audit_key_missing")
    if target_state not in {"repaired", "consumed"}:
        reasons.append("target_not_repaired")
    for candidate, reason in (
        (receipt_path, "receipt_not_file"),
        (backup, "backup_not_file"),
        (staged, "staged_backup_not_file"),
    ):
        if candidate.exists() and not candidate.is_file():
            reasons.append(reason)
    if receipt_present:
        try:
            _validate_retention_receipt(receipt_path, transaction)
        except (OSError, ValueError) as error:
            reasons.append("receipt_invalid")
            errors.append(str(error))
    if backup_present:
        try:
            _validate_retention_backup(backup, transaction)
        except (OSError, ValueError) as error:
            reasons.append("backup_invalid")
            errors.append(str(error))
    if staged_present:
        try:
            _validate_retention_backup(staged, transaction)
        except (OSError, ValueError) as error:
            reasons.append("staged_backup_invalid")
            errors.append(str(error))

    if transaction["phase"] == "authorized":
        valid_topology = (
            (receipt_present and backup_present and not staged_present)
            or (not receipt_present and backup_present and not staged_present)
            or (not receipt_present and not backup_present and staged_present)
        )
    else:
        valid_topology = (
            not receipt_present and not backup_present
        )
    if not valid_topology:
        reasons.append("inconsistent_file_state")
    report["reason_codes"] = list(dict.fromkeys(reasons))
    if errors:
        report["errors"] = errors
    if not reasons:
        report["status"] = "resumable"
    return report


def _inspect_rotated_prune_receipt(
    directory: Path,
    receipt_path: Path,
    committed_keys: set[str] | None,
    multiple_pending: bool = False,
) -> dict[str, Any]:
    """Classify one rotated-prune receipt without locks or filesystem changes."""
    report: dict[str, Any] = {
        "path": receipt_path.relative_to(directory).as_posix(),
        "status": "blocked",
        "reason_codes": [],
    }
    reasons: list[str] = []
    errors: list[str] = []
    try:
        receipt = _load_rotated_prune_receipt(receipt_path)
    except (OSError, ValueError) as error:
        report.update({"phase": None, "reason_codes": ["invalid_receipt"]})
        report["error"] = str(error)
        if multiple_pending:
            report["reason_codes"].append("multiple_pending_receipts")
        return report

    audit_key_present = (
        None if committed_keys is None else receipt["audit_key"] in committed_keys
    )
    target_states = []
    resolution_states = []
    for outcome in receipt["outcomes"]:
        if outcome["action"] != "remove":
            continue
        target = directory.parents[1] / outcome["path"]
        try:
            target.lstat()
        except FileNotFoundError:
            state = "authorized_absent"
        except OSError as error:
            state = "unreadable"
            reasons.append("segment_unreadable")
            errors.append(str(error))
        else:
            if target.is_symlink() or not target.is_file():
                state = "not_file"
                reasons.append("target_not_file")
            else:
                state = "matching"
                try:
                    data = target.read_bytes()
                except OSError as error:
                    state = "unreadable"
                    reasons.append("segment_unreadable")
                    errors.append(str(error))
                else:
                    if (
                        len(data) != outcome["expected_bytes"]
                        or hashlib.sha256(data).hexdigest()
                        != outcome["expected_sha256"]
                    ):
                        state = "drifted"
                        reasons.append("segment_drifted")
            if receipt["phase"] == "applied":
                reasons.append("applied_target_present")
        target_states.append({"path": outcome["path"], "state": state})
        continue

    for outcome in receipt["outcomes"]:
        resolution = outcome.get("resolution")
        if not resolution:
            continue
        state = "matching"
        try:
            marker = _validate_resolution_preservation(
                directory, receipt, outcome
            )
        except FileNotFoundError as error:
            state = "missing"
            reasons.append("preservation_marker_missing")
            errors.append(str(error))
        except (OSError, ValueError) as error:
            state = "invalid"
            reasons.append("preservation_marker_invalid")
            errors.append(str(error))
        else:
            if committed_keys is not None and marker["audit_key"] not in committed_keys:
                reasons.append("resolution_audit_missing")
        resolution_states.append(
            {
                "path": outcome["path"],
                "state": state,
                "preservation": resolution["preservation"],
                "audit_key": resolution["audit_key"],
                "audit_key_present": (
                    None
                    if committed_keys is None
                    else resolution["audit_key"] in committed_keys
                ),
            }
        )

    if committed_keys is None:
        reasons.append("audit_snapshot_incomplete")
    if multiple_pending:
        reasons.append("multiple_pending_receipts")
    report.update(
        {
            "phase": receipt["phase"],
            "run_id": receipt["run_id"],
            "cutoff": receipt["cutoff"],
            "audit_key": receipt["audit_key"],
            "audit_key_present": audit_key_present,
            "outcome_count": len(receipt["outcomes"]),
            "removal_target_states": target_states,
            "resolution_states": resolution_states,
            "reason_codes": list(dict.fromkeys(reasons)),
        }
    )
    if errors:
        report["errors"] = errors
    if not reasons:
        report["status"] = "resumable"
    return report


def _inspect_rotated_prune_receipts(
    directory: Path, committed_keys: set[str] | None
) -> list[dict[str, Any]]:
    """Return a strict read-only snapshot of the rotated-prune receipt directory."""
    receipt_dir = directory / ".rotated-prune"
    try:
        if not receipt_dir.exists():
            return []
        if not receipt_dir.is_dir():
            return [
                {
                    "path": ".rotated-prune",
                    "status": "blocked",
                    "phase": None,
                    "reason_codes": ["receipt_directory_not_directory"],
                }
            ]
        entries = sorted(receipt_dir.iterdir())
    except OSError as error:
        return [
            {
                "path": ".rotated-prune",
                "status": "blocked",
                "phase": None,
                "reason_codes": ["receipt_directory_unreadable"],
                "error": str(error),
            }
        ]
    multiple_pending = len(entries) > 1
    reports = []
    for path in entries:
        if path.suffix != ".json" or not path.is_file():
            reasons = ["unexpected_entry"]
            if multiple_pending:
                reasons.append("multiple_pending_receipts")
            reports.append(
                {
                    "path": path.relative_to(directory).as_posix(),
                    "status": "blocked",
                    "phase": None,
                    "reason_codes": reasons,
                }
            )
            continue
        reports.append(
            _inspect_rotated_prune_receipt(
                directory,
                path,
                committed_keys,
                multiple_pending=multiple_pending,
            )
        )
    return reports


def _inspect_rotated_preservation(
    directory: Path,
    marker_path: Path,
    committed_keys: set[str] | None,
) -> dict[str, Any]:
    """Classify one permanent preservation marker without changing state."""
    report: dict[str, Any] = {
        "path": marker_path.relative_to(directory).as_posix(),
        "status": "blocked",
        "reason_codes": [],
    }
    try:
        marker = _load_rotated_preservation(marker_path)
    except (OSError, ValueError) as error:
        report.update(
            {
                "reason_codes": ["invalid_marker"],
                "error": str(error),
            }
        )
        return report

    reasons: list[str] = []
    errors: list[str] = []
    target = directory / marker["segment"]
    target_state = "matching"
    try:
        target.lstat()
    except FileNotFoundError:
        target_state = "missing"
        reasons.append("orphaned_marker")
    except OSError as error:
        target_state = "unreadable"
        reasons.append("target_unreadable")
        errors.append(str(error))
    else:
        if target.is_symlink() or not target.is_file():
            target_state = "not_file"
            reasons.append("target_not_file")
        else:
            try:
                data = target.read_bytes()
            except OSError as error:
                target_state = "unreadable"
                reasons.append("target_unreadable")
                errors.append(str(error))
            else:
                if (
                    len(data) != marker["preserved_bytes"]
                    or hashlib.sha256(data).hexdigest()
                    != marker["preserved_sha256"]
                ):
                    target_state = "drifted"
                    reasons.append("target_fingerprint_drift")

    pruned_audit_key = f"rotated-prune:{marker['run_id']}"
    resolution_audit_key = marker["audit_key"]
    pruned_audit_key_present = (
        None if committed_keys is None else pruned_audit_key in committed_keys
    )
    resolution_audit_key_present = (
        None
        if committed_keys is None
        else resolution_audit_key in committed_keys
    )
    if committed_keys is None:
        reasons.append("audit_snapshot_incomplete")
    else:
        if not pruned_audit_key_present:
            reasons.append("pruned_audit_missing")
        if not resolution_audit_key_present:
            reasons.append("resolution_audit_missing")
    report.update(
        {
            "run_id": marker["run_id"],
            "segment": marker["segment"],
            "decision": marker["decision"],
            "preserved_sha256": marker["preserved_sha256"],
            "preserved_bytes": marker["preserved_bytes"],
            "target_state": target_state,
            "pruned_audit_key": pruned_audit_key,
            "pruned_audit_key_present": pruned_audit_key_present,
            "resolution_audit_key": resolution_audit_key,
            "resolution_audit_key_present": resolution_audit_key_present,
            "created_at": marker["created_at"],
            "reason_codes": list(dict.fromkeys(reasons)),
        }
    )
    if errors:
        report["errors"] = errors
    if not reasons:
        report["status"] = "protected"
    return report


def _inspect_rotated_preservations(
    directory: Path, committed_keys: set[str] | None
) -> list[dict[str, Any]]:
    """Return a strict read-only snapshot of permanent preservation markers."""
    preservation = directory / ".rotated-preserve"
    try:
        if not preservation.exists():
            return []
        if not preservation.is_dir():
            return [
                {
                    "path": ".rotated-preserve",
                    "status": "blocked",
                    "reason_codes": ["preservation_directory_not_directory"],
                }
            ]
        entries = sorted(preservation.iterdir())
    except OSError as error:
        return [
            {
                "path": ".rotated-preserve",
                "status": "blocked",
                "reason_codes": ["preservation_directory_unreadable"],
                "error": str(error),
            }
        ]

    reports = []
    for path in entries:
        if path.suffix != ".json" or not path.is_file():
            reports.append(
                {
                    "path": path.relative_to(directory).as_posix(),
                    "status": "blocked",
                    "reason_codes": ["unexpected_entry"],
                }
            )
            continue
        reports.append(
            _inspect_rotated_preservation(directory, path, committed_keys)
        )

    target_claims: dict[str, int] = {}
    for report in reports:
        segment = report.get("segment")
        if isinstance(segment, str):
            target_claims[segment] = target_claims.get(segment, 0) + 1
    for report in reports:
        segment = report.get("segment")
        if isinstance(segment, str) and target_claims.get(segment, 0) > 1:
            report["status"] = "blocked"
            report["reason_codes"] = list(
                dict.fromkeys(
                    [*report["reason_codes"], "duplicate_target_claim"]
                )
            )
    return reports


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

    repair_receipts = []
    repairs = directory / ".repairs"
    if repairs.is_dir():
        for path in sorted(repairs.glob("*.json")):
            try:
                receipt = _load_repair_receipt(path)
                backup = directory / receipt["backup"]
                repair_receipts.append(
                    {
                        "path": path.relative_to(directory).as_posix(),
                        "status": "valid",
                        "phase": receipt["phase"],
                        "segment": receipt["segment"],
                        "original_sha256": receipt["original_sha256"],
                        "repaired_sha256": receipt["repaired_sha256"],
                        "dropped_lines": receipt["dropped_lines"],
                        "backup_present": backup.is_file(),
                        "audit_event_committed": receipt["audit_event_committed"],
                    }
                )
            except (OSError, ValueError) as error:
                repair_receipts.append(
                    {
                        "path": path.relative_to(directory).as_posix(),
                        "status": "invalid",
                        "error": str(error),
                    }
                )

    retention_tombstones = []
    retention = directory / ".repair-prune"
    committed_keys = (
        _read_only_logged_keys(directory) if directory.is_dir() else set()
    )
    if retention.is_dir():
        retention_tombstones = [
            _inspect_repair_retention(directory, path, committed_keys)
            for path in sorted(retention.glob("*.json"))
            if path.is_file()
        ]
    rotated_prune_receipts = (
        _inspect_rotated_prune_receipts(directory, committed_keys)
        if directory.exists()
        else []
    )
    rotated_preservations = (
        _inspect_rotated_preservations(directory, committed_keys)
        if directory.exists()
        else []
    )
    preservation_releases = (
        _inspect_preservation_releases(
            directory,
            committed_keys,
            rotated_preservations,
        )
        if directory.exists()
        else []
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
    pending_repair_count = sum(
        item.get("status") != "valid" or item.get("phase") != "committed"
        for item in repair_receipts
    )
    resumable_retention_count = sum(
        item["status"] == "resumable" for item in retention_tombstones
    )
    blocked_retention_count = sum(
        item["status"] == "blocked" for item in retention_tombstones
    )
    resumable_rotated_prune_count = sum(
        item["status"] == "resumable" for item in rotated_prune_receipts
    )
    blocked_rotated_prune_count = sum(
        item["status"] == "blocked" for item in rotated_prune_receipts
    )
    protected_rotated_preservation_count = sum(
        item["status"] == "protected" for item in rotated_preservations
    )
    blocked_rotated_preservation_count = sum(
        item["status"] == "blocked" for item in rotated_preservations
    )
    completed_preservation_release_count = sum(
        item["status"] == "completed" for item in preservation_releases
    )
    resumable_preservation_release_count = sum(
        item["status"] == "resumable" for item in preservation_releases
    )
    blocked_preservation_release_count = sum(
        item["status"] == "blocked" for item in preservation_releases
    )
    if keyed_append_blocked:
        status = "blocked"
    elif (
        pending_count
        or quarantined
        or pending_repair_count
        or resumable_retention_count
        or blocked_retention_count
        or resumable_rotated_prune_count
        or blocked_rotated_prune_count
        or protected_rotated_preservation_count
        or blocked_rotated_preservation_count
        or resumable_preservation_release_count
        or blocked_preservation_release_count
    ):
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
    if pending_repair_count:
        guidance.append(
            "An audit repair receipt is incomplete or invalid; retry the exact audit-repair intent or investigate receipt drift."
        )
    if resumable_retention_count:
        guidance.append(
            "Run prune to resume the authorized repair-retention transactions."
        )
    if blocked_retention_count:
        guidance.append(
            "Do not delete blocked repair-retention evidence manually; inspect each tombstone reason_codes and restore the bound state before retrying prune."
        )
    if resumable_rotated_prune_count:
        guidance.append(
            "Run prune to resume the pending rotated-prune receipt and commit its deterministic audit event."
        )
    if blocked_rotated_prune_count:
        guidance.append(
            "Do not delete blocked rotated-prune receipts manually; inspect reason_codes and restore the exact receipt or target state before retrying prune."
        )
    if protected_rotated_preservation_count:
        guidance.append(
            "Retain protected rotated-preservation markers with their bound targets until a guarded release is explicitly authorized."
        )
    if blocked_rotated_preservation_count:
        guidance.append(
            "Do not delete blocked rotated-preservation markers manually; inspect reason_codes and restore the marker, target, or audit binding."
        )
    if resumable_preservation_release_count:
        guidance.append(
            "Retry audit-preserve-release with the exact run, segment, marker, and target fingerprints reported by the resumable release entry."
        )
    if blocked_preservation_release_count:
        guidance.append(
            "Do not remove release markers or rewrite release events manually; inspect preservation_releases reason_codes and restore unambiguous event/marker evidence."
        )
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
        "repair_receipts": repair_receipts,
        "pending_repair_count": pending_repair_count,
        "retention_tombstones": retention_tombstones,
        "resumable_retention_count": resumable_retention_count,
        "blocked_retention_count": blocked_retention_count,
        "rotated_prune_receipts": rotated_prune_receipts,
        "resumable_rotated_prune_count": resumable_rotated_prune_count,
        "blocked_rotated_prune_count": blocked_rotated_prune_count,
        "rotated_preservations": rotated_preservations,
        "protected_rotated_preservation_count": protected_rotated_preservation_count,
        "blocked_rotated_preservation_count": blocked_rotated_preservation_count,
        "preservation_releases": preservation_releases,
        "completed_preservation_release_count": completed_preservation_release_count,
        "resumable_preservation_release_count": resumable_preservation_release_count,
        "blocked_preservation_release_count": blocked_preservation_release_count,
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

    directory = root.resolve() / "var" / "audit"
    target = directory / segment
    receipt_path = _repair_receipt_path(directory, segment, expected)
    resumed = False
    already_repaired = False
    with FileLock(directory / ".audit.lock"):
        with FileLock(directory / ".degraded.lock"):
            if receipt_path.is_file():
                receipt = _load_repair_receipt(receipt_path)
                if (
                    receipt["segment"] != segment
                    or receipt["original_sha256"] != expected
                    or receipt["dropped_lines"] != selected
                ):
                    raise ValueError("audit repair receipt does not match the requested intent")
                backup = _validate_repair_backup(directory, receipt)
                resumed = True
                target_state, current = _repair_target_state(directory, receipt)
                if target_state == "original":
                    if receipt["phase"] != "prepared":
                        raise ValueError(
                            "audit repair target rolled back after replacement; refusing implicit replay"
                        )
                    assert current is not None
                    candidate = _repair_candidate(current, selected)
                    if len(current) != receipt["original_bytes"]:
                        raise ValueError("audit repair original size does not match receipt")
                    if len(candidate) != receipt["repaired_bytes"]:
                        raise ValueError("audit repair candidate size does not match receipt")
                    if hashlib.sha256(candidate).hexdigest() != receipt["repaired_sha256"]:
                        raise ValueError("audit repair candidate fingerprint does not match receipt")
                elif target_state == "repaired":
                    assert current is not None
                    candidate = current
                    already_repaired = True
                elif target_state == "consumed" and receipt["phase"] in {
                    "replaced",
                    "committed",
                }:
                    candidate = b""
                    already_repaired = True
                else:
                    raise ValueError(
                        f"audit repair target drift or unsafe state {target_state!r}; cannot resume "
                        f"receipt phase {receipt['phase']!r}"
                    )
            else:
                try:
                    current = target.read_bytes()
                except OSError as error:
                    raise OSError(f"cannot read audit segment {segment}: {error}") from error
                current_digest = hashlib.sha256(current).hexdigest()
                if current_digest != expected:
                    raise ValueError(
                        f"audit segment fingerprint changed: expected {expected}, found {current_digest}"
                    )
                candidate = _repair_candidate(current, selected)
                repaired_digest = hashlib.sha256(candidate).hexdigest()
                corrupt = directory / ".corrupt"
                corrupt.mkdir(parents=True, exist_ok=True)
                backup = corrupt / f"{segment}.{expected}.repair-original"
                try:
                    with open(backup, "xb") as handle:
                        handle.write(current)
                        _durable_flush(handle)
                except FileExistsError:
                    if backup.read_bytes() != current:
                        raise OSError(f"repair backup collision: {backup}")
                receipt = {
                    "schema_version": AUDIT_REPAIR_RECEIPT_SCHEMA,
                    "phase": "prepared",
                    "segment": segment,
                    "original_sha256": expected,
                    "repaired_sha256": repaired_digest,
                    "dropped_lines": selected,
                    "original_bytes": len(current),
                    "repaired_bytes": len(candidate),
                    "backup": backup.relative_to(directory).as_posix(),
                    "prepared_at": utc_now(),
                    "audit_event_committed": False,
                }
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(receipt_path, receipt)

            if not already_repaired:
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

            target_state, _ = _repair_target_state(directory, receipt)
            if target_state not in {"repaired", "consumed"}:
                raise OSError(
                    f"audit segment verification failed after repair: "
                    f"{segment} is {target_state}"
                )
            repaired_digest = receipt["repaired_sha256"]
            if receipt["phase"] != "committed":
                receipt = {
                    **receipt,
                    "phase": "replaced",
                    "replaced_at": receipt.get("replaced_at", utc_now()),
                    "audit_event_committed": False,
                }
                atomic_write_json(receipt_path, receipt)

    return {
        "event": "audit_segment_repaired",
        "segment": segment,
        "original_sha256": expected,
        "repaired_sha256": repaired_digest,
        "dropped_lines": selected,
        "original_bytes": receipt["original_bytes"],
        "repaired_bytes": receipt["repaired_bytes"],
        "backup": backup.relative_to(directory).as_posix(),
        "receipt": receipt_path.relative_to(directory).as_posix(),
        "receipt_phase": receipt["phase"],
        "resumed": resumed,
        "already_repaired": already_repaired,
    }


def commit_repair_receipt(root: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Mark a verified repair receipt committed after its keyed audit append."""
    directory = root.resolve() / "var" / "audit"
    expected_path = _repair_receipt_path(
        directory, report["segment"], report["original_sha256"]
    )
    if report.get("receipt") != expected_path.relative_to(directory).as_posix():
        raise ValueError("audit repair report receipt path mismatch")
    with FileLock(directory / ".audit.lock"):
        with FileLock(directory / ".degraded.lock"):
            receipt = _load_repair_receipt(expected_path)
            if (
                receipt["segment"] != report["segment"]
                or receipt["original_sha256"] != report["original_sha256"]
                or receipt["repaired_sha256"] != report["repaired_sha256"]
                or receipt["dropped_lines"] != report["dropped_lines"]
            ):
                raise ValueError("audit repair receipt changed before commit")
            _validate_repair_backup(directory, receipt)
            target_state, _ = _repair_target_state(directory, receipt)
            if target_state not in {"repaired", "consumed"}:
                raise ValueError(
                    f"repaired audit target state changed before receipt commit: {target_state}"
                )
            if receipt["phase"] == "committed":
                return receipt
            if receipt["phase"] != "replaced":
                raise ValueError("audit repair receipt is not ready to commit")
            receipt = {
                **receipt,
                "phase": "committed",
                "audit_event_committed": True,
                "committed_at": utc_now(),
            }
            atomic_write_json(expected_path, receipt)
            return receipt


def prune_committed_repairs(root: Path, older_than: datetime) -> dict[str, int]:
    """Crash-convergently prune old, fully bound committed repair evidence."""
    directory = root.resolve() / "var" / "audit"
    counts = {"repair_receipts_removed": 0, "repair_backups_removed": 0}
    repairs = directory / ".repairs"
    retention = directory / ".repair-prune"
    if not repairs.is_dir() and not retention.is_dir():
        return counts
    with FileLock(directory / ".audit.lock"):
        with FileLock(directory / ".degraded.lock"):
            try:
                committed_keys = _logged_keys(directory)
            except OSError:
                return counts
            if retention.is_dir():
                for tombstone_path in sorted(retention.glob("*.json")):
                    receipt_removed, backup_removed = _resume_repair_retention(
                        directory, tombstone_path, committed_keys
                    )
                    counts["repair_receipts_removed"] += receipt_removed
                    counts["repair_backups_removed"] += backup_removed
            if not repairs.is_dir():
                return counts
            for receipt_path in sorted(repairs.glob("*.json")):
                try:
                    receipt = _load_repair_receipt(receipt_path)
                    if receipt["phase"] != "committed":
                        continue
                    committed_at = parse_utc(receipt["committed_at"])
                    if committed_at > older_than:
                        continue
                    _validate_repair_backup(directory, receipt)
                    audit_key = (
                        f"audit-repair:{receipt['segment']}:{receipt['original_sha256']}"
                    )
                    if audit_key not in committed_keys:
                        continue
                    target_state, _ = _repair_target_state(directory, receipt)
                    if target_state not in {"repaired", "consumed"}:
                        continue
                    tombstone_path = _repair_retention_path(
                        directory, receipt["segment"], receipt["original_sha256"]
                    )
                    if tombstone_path.exists():
                        continue
                    transaction = _retention_transaction(
                        directory, receipt_path, receipt
                    )
                    atomic_write_json(tombstone_path, transaction)
                    receipt_removed, backup_removed = _resume_repair_retention(
                        directory, tombstone_path, committed_keys
                    )
                    counts["repair_receipts_removed"] += receipt_removed
                    counts["repair_backups_removed"] += backup_removed
                except (OSError, TypeError, ValueError):
                    continue
    return counts


def _retention_rotation_fences(
    directory: Path,
) -> tuple[set[str], set[str]] | None:
    """Return protected audit keys and rotated targets, or fail closed."""
    retention = directory / ".repair-prune"
    try:
        if not retention.exists():
            return set(), set()
        if not retention.is_dir():
            return None
        entries = sorted(retention.iterdir())
    except OSError:
        return None
    protected_keys: set[str] = set()
    protected_targets: set[str] = set()
    for path in entries:
        if path.suffix != ".json":
            continue
        if not path.is_file():
            return None
        try:
            transaction = _load_repair_retention(path)
        except (OSError, ValueError):
            return None
        protected_keys.add(transaction["audit_key"])
        segment = transaction["segment"]
        if re.fullmatch(r"events\.\d+\.jsonl", segment):
            protected_targets.add(segment)
    return protected_keys, protected_targets


def _rotated_preservation_targets(directory: Path) -> set[str] | None:
    """Return strictly validated operator-preserved targets, or fail closed."""
    preservation = directory / ".rotated-preserve"
    try:
        if not preservation.exists():
            return set()
        if not preservation.is_dir():
            return None
        entries = sorted(preservation.iterdir())
    except OSError:
        return None
    targets: set[str] = set()
    for path in entries:
        if path.suffix != ".json" or not path.is_file():
            return None
        try:
            marker = _load_rotated_preservation(path)
            _validate_preserved_target(directory, marker)
        except (OSError, ValueError):
            return None
        targets.add(marker["segment"])
    return targets


def prune_rotated(root: Path, older_than: datetime) -> dict[str, Any]:
    """Prepare, apply, and expose one crash-convergent rotated prune run."""
    resolved_root = root.resolve()
    directory = resolved_root / "var" / "audit"
    directory.mkdir(parents=True, exist_ok=True)
    outcomes: list[dict[str, Any]] = []

    def add_outcome(
        path: Path,
        status: str,
        reason_codes: list[str],
        error: str | None = None,
        expected_sha256: str | None = None,
        expected_bytes: int | None = None,
    ) -> None:
        outcome: dict[str, Any] = {
            "path": path.relative_to(resolved_root).as_posix(),
            "status": status,
            "reason_codes": reason_codes,
            "action": "remove" if status == "removed" else "retain",
        }
        if error:
            outcome["error"] = error
        if status == "removed":
            if expected_sha256 is None or expected_bytes is None:
                raise ValueError("rotated prune removal requires a deletion witness")
            outcome["expected_sha256"] = expected_sha256
            outcome["expected_bytes"] = expected_bytes
        outcomes.append(outcome)

    with FileLock(directory / ".audit.lock"):
        with FileLock(directory / ".degraded.lock"):
            pending = _pending_rotated_prune_receipt(directory)
            if pending is not None:
                receipt = _load_rotated_prune_receipt(pending)
                receipt = _apply_rotated_prune_receipt(directory, pending, receipt)
                return _rotated_prune_report(directory, receipt, resumed=True)

            candidates: list[Path] = []
            for path in sorted(directory.glob("events.*.jsonl")):
                try:
                    modified = datetime.fromtimestamp(
                        path.stat().st_mtime, tz=timezone.utc
                    )
                except FileNotFoundError as error:
                    add_outcome(
                        path,
                        "blocked",
                        ["segment_disappeared"],
                        str(error),
                    )
                    continue
                except OSError as error:
                    add_outcome(
                        path,
                        "blocked",
                        ["metadata_unreadable"],
                        str(error),
                    )
                    continue
                if modified <= older_than:
                    candidates.append(path)
            retention_fences = _retention_rotation_fences(directory)
            if retention_fences is None:
                for path in candidates:
                    add_outcome(path, "blocked", ["retention_snapshot_invalid"])
            else:
                retention_keys, protected_targets = retention_fences
                preservation_targets = _rotated_preservation_targets(directory)
                if preservation_targets is None:
                    for path in candidates:
                        add_outcome(
                            path, "blocked", ["preservation_snapshot_invalid"]
                        )
                else:
                    degraded = directory / "degraded.jsonl"
                    pending_keys: set[str] = set()
                    degraded_valid = True
                    if degraded.is_file():
                        keys = _file_keys(degraded)
                        if keys is None:
                            degraded_valid = False
                        else:
                            pending_keys = keys
                    if not degraded_valid:
                        for path in candidates:
                            add_outcome(
                                path, "blocked", ["degraded_snapshot_invalid"]
                            )
                    else:
                        for path in candidates:
                            if path.name in preservation_targets:
                                add_outcome(
                                    path,
                                    "protected",
                                    ["operator_preserved_target"],
                                )
                                continue
                            if path.name in protected_targets:
                                add_outcome(path, "protected", ["retention_target"])
                                continue
                            (
                                segment_keys,
                                reason_code,
                                error,
                                digest,
                                size,
                            ) = _rotated_segment_keys(path)
                            if segment_keys is None:
                                add_outcome(
                                    path,
                                    "blocked",
                                    [reason_code or "segment_unreadable"],
                                    error,
                                )
                                continue
                            protection_reasons: list[str] = []
                            if segment_keys & pending_keys:
                                protection_reasons.append("degraded_replay_key")
                            if segment_keys & retention_keys:
                                protection_reasons.append("retention_audit_key")
                            if protection_reasons:
                                add_outcome(path, "protected", protection_reasons)
                                continue
                            add_outcome(
                                path,
                                "removed",
                                ["valid_expired"],
                                expected_sha256=digest,
                                expected_bytes=size,
                            )

            outcomes.sort(key=lambda item: item["path"])
            cutoff = older_than.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            created_at = utc_now()
            run_seed = json.dumps(
                {
                    "cutoff": cutoff,
                    "created_at": created_at,
                    "pid": os.getpid(),
                    "nonce": time.time_ns(),
                    "outcomes": outcomes,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            run_id = hashlib.sha256(run_seed).hexdigest()
            receipt = {
                "schema_version": AUDIT_ROTATED_PRUNE_SCHEMA,
                "phase": "prepared",
                "run_id": run_id,
                "audit_key": f"rotated-prune:{run_id}",
                "cutoff": cutoff,
                "created_at": created_at,
                "outcomes": outcomes,
            }
            receipt_path = _rotated_prune_receipt_path(directory, run_id)
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(receipt_path, receipt)
            receipt = _apply_rotated_prune_receipt(
                directory, receipt_path, receipt
            )
            return _rotated_prune_report(directory, receipt, resumed=False)


def resolve_rotated_prune(
    root: Path,
    run_id: str,
    expected_receipt_sha256: str,
    segment: str,
    expected_segment_sha256: str,
    decision: str,
) -> dict[str, Any]:
    """Preserve one recreated applied target under exact operator fingerprints."""
    if not isinstance(run_id, str) or not SHA256_RE.fullmatch(run_id):
        raise ValueError("run_id must be exactly 64 hexadecimal characters")
    receipt_fingerprint = (
        expected_receipt_sha256.casefold()
        if isinstance(expected_receipt_sha256, str)
        else ""
    )
    segment_fingerprint = (
        expected_segment_sha256.casefold()
        if isinstance(expected_segment_sha256, str)
        else ""
    )
    if not SHA256_RE.fullmatch(receipt_fingerprint):
        raise ValueError(
            "expected_receipt_sha256 must be exactly 64 hexadecimal characters"
        )
    if not isinstance(segment, str) or not AUDIT_ROTATED_SEGMENT_RE.fullmatch(segment):
        raise ValueError("segment must be events.<digits>.jsonl")
    if not SHA256_RE.fullmatch(segment_fingerprint):
        raise ValueError(
            "expected_segment_sha256 must be exactly 64 hexadecimal characters"
        )
    if decision != "preserve-recreated":
        raise ValueError("decision must be preserve-recreated")

    resolved_root = root.resolve()
    directory = resolved_root / "var" / "audit"
    target_relative = f"var/audit/{segment}"
    with FileLock(directory / ".audit.lock"):
        with FileLock(directory / ".degraded.lock"):
            pending = _pending_rotated_prune_receipt(directory)
            if pending is None:
                raise ValueError("no pending rotated prune receipt")
            expected_path = _rotated_prune_receipt_path(directory, run_id)
            if pending != expected_path:
                raise ValueError("pending rotated prune receipt does not match run_id")
            try:
                receipt_bytes = pending.read_bytes()
            except OSError as error:
                raise OSError(f"cannot read rotated prune receipt: {pending}") from error
            current_receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
            receipt = _load_rotated_prune_receipt(pending)
            if receipt["phase"] != "applied":
                raise ValueError("rotated prune receipt must be applied")
            matches = [
                outcome
                for outcome in receipt["outcomes"]
                if outcome["path"] == target_relative
            ]
            if len(matches) != 1:
                raise ValueError("segment is not uniquely present in rotated prune receipt")
            outcome = matches[0]

            target = directory / segment
            try:
                target.lstat()
            except OSError as error:
                raise OSError(f"recreated rotated segment is unavailable: {target}") from error
            if target.is_symlink() or not target.is_file():
                raise ValueError("recreated rotated segment must be a regular file")
            try:
                target_bytes = target.read_bytes()
            except OSError as error:
                raise OSError(f"cannot read recreated rotated segment: {target}") from error
            current_segment_sha256 = hashlib.sha256(target_bytes).hexdigest()
            if current_segment_sha256 != segment_fingerprint:
                raise ValueError(
                    "recreated rotated segment fingerprint changed: "
                    f"expected {segment_fingerprint}, found {current_segment_sha256}"
                )

            existing_resolution = outcome.get("resolution")
            already_resolved = existing_resolution is not None
            if already_resolved:
                if (
                    existing_resolution["decision"] != decision
                    or existing_resolution["receipt_sha256_before"]
                    != receipt_fingerprint
                    or existing_resolution["preserved_sha256"]
                    != segment_fingerprint
                    or existing_resolution["preserved_bytes"] != len(target_bytes)
                ):
                    raise ValueError(
                        "rotated prune resolution does not match the requested intent"
                    )
                marker = _validate_resolution_preservation(
                    directory, receipt, outcome
                )
            else:
                if current_receipt_sha256 != receipt_fingerprint:
                    raise ValueError(
                        "rotated prune receipt fingerprint changed: "
                        f"expected {receipt_fingerprint}, "
                        f"found {current_receipt_sha256}"
                    )
                if (
                    outcome["action"] != "remove"
                    or outcome["status"] != "removed"
                    or outcome["reason_codes"] != ["valid_expired"]
                ):
                    raise ValueError(
                        "segment is not an unresolved authorized removal"
                    )
                resolved_at = utc_now()
                resolution_audit_key = (
                    f"rotated-prune-resolve:{run_id}:{segment}:"
                    f"{segment_fingerprint}"
                )
                marker_path = _rotated_preservation_path(
                    directory, run_id, segment
                )
                marker = {
                    "schema_version": AUDIT_ROTATED_PRESERVATION_SCHEMA,
                    "run_id": run_id,
                    "segment": segment,
                    "preserved_sha256": segment_fingerprint,
                    "preserved_bytes": len(target_bytes),
                    "receipt_sha256_before": receipt_fingerprint,
                    "original_expected_sha256": outcome["expected_sha256"],
                    "original_expected_bytes": outcome["expected_bytes"],
                    "decision": decision,
                    "audit_key": resolution_audit_key,
                    "created_at": resolved_at,
                }
                marker_path.parent.mkdir(parents=True, exist_ok=True)
                if marker_path.exists():
                    existing_marker = _load_rotated_preservation(marker_path)
                    for field, expected_value in marker.items():
                        if field == "created_at":
                            continue
                        if existing_marker.get(field) != expected_value:
                            raise ValueError(
                                f"rotated preservation marker collision: {field}"
                            )
                    marker = existing_marker
                    resolved_at = marker["created_at"]
                else:
                    atomic_write_json(marker_path, marker)
                resolution = {
                    "decision": decision,
                    "preservation": marker_path.relative_to(directory).as_posix(),
                    "receipt_sha256_before": receipt_fingerprint,
                    "original_expected_sha256": outcome["expected_sha256"],
                    "original_expected_bytes": outcome["expected_bytes"],
                    "preserved_sha256": segment_fingerprint,
                    "preserved_bytes": len(target_bytes),
                    "audit_key": resolution_audit_key,
                    "resolved_at": resolved_at,
                }
                resolved_outcome = {
                    "path": outcome["path"],
                    "status": "blocked",
                    "reason_codes": ["operator_preserved_recreated_segment"],
                    "action": "retain",
                    "resolution": resolution,
                }
                receipt = {
                    **receipt,
                    "outcomes": [
                        resolved_outcome if item is outcome else item
                        for item in receipt["outcomes"]
                    ],
                }
                atomic_write_json(pending, receipt)
                receipt = _load_rotated_prune_receipt(pending)
                outcome = next(
                    item
                    for item in receipt["outcomes"]
                    if item["path"] == target_relative
                )
                marker = _validate_resolution_preservation(
                    directory, receipt, outcome
                )

            report = _rotated_prune_report(directory, receipt, resumed=True)
            resolution = outcome["resolution"]
            return {
                "event": "audit_prune_resolution_prepared",
                "decision": decision,
                "segment": segment,
                "preserved_sha256": marker["preserved_sha256"],
                "preserved_bytes": marker["preserved_bytes"],
                "receipt_sha256_before": resolution["receipt_sha256_before"],
                "preservation": resolution["preservation"],
                "resolution_audit_key": resolution["audit_key"],
                "already_resolved": already_resolved,
                **report,
            }


def commit_rotated_prune_receipt(
    root: Path, report: dict[str, Any]
) -> bool:
    """Remove an applied receipt only after its deterministic event is logged."""
    resolved_root = root.resolve()
    directory = resolved_root / "var" / "audit"
    run_id = report.get("audit_prune_run_id")
    audit_key = report.get("audit_prune_audit_key")
    if not isinstance(run_id, str) or not SHA256_RE.fullmatch(run_id):
        raise ValueError("invalid rotated prune report run id")
    receipt_path = _rotated_prune_receipt_path(directory, run_id)
    expected_relative = receipt_path.relative_to(resolved_root).as_posix()
    if report.get("audit_prune_receipt") != expected_relative:
        raise ValueError("rotated prune report receipt path mismatch")
    if audit_key != f"rotated-prune:{run_id}":
        raise ValueError("rotated prune report audit key mismatch")
    with FileLock(directory / ".audit.lock"):
        with FileLock(directory / ".degraded.lock"):
            receipt = _load_rotated_prune_receipt(receipt_path)
            if receipt["phase"] != "applied":
                raise ValueError("rotated prune receipt is not applied")
            _require_applied_rotated_prune_topology(directory, receipt)
            expected = _rotated_prune_report(
                directory,
                receipt,
                resumed=bool(report.get("audit_prune_resumed")),
            )
            for field in (
                "audit_segments_removed",
                "audit_segments_protected",
                "audit_segments_blocked",
                "audit_segment_outcomes",
                "audit_prune_run_id",
                "audit_prune_audit_key",
                "audit_prune_cutoff",
                "audit_prune_receipt",
                "audit_prune_receipt_phase",
            ):
                if report.get(field) != expected[field]:
                    raise ValueError(f"rotated prune report field drift: {field}")
            committed_keys = _logged_keys(directory)
            if audit_key not in committed_keys:
                raise OSError("rotated prune audit event is not committed")
            resolution_keys = {
                item["resolution"]["audit_key"]
                for item in receipt["outcomes"]
                if item.get("resolution")
            }
            missing_resolution_keys = sorted(resolution_keys - committed_keys)
            if missing_resolution_keys:
                raise OSError(
                    "rotated prune resolution audit event is not committed: "
                    + ", ".join(missing_resolution_keys)
                )
            receipt_path.unlink()
            return True


def _preservation_release_audit_key(
    run_id: str,
    segment: str,
    marker_sha256: str,
    preserved_sha256: str,
) -> str:
    return (
        f"rotated-preserve-release:{run_id}:{segment}:"
        f"{marker_sha256}:{preserved_sha256}"
    )


def _require_preservation_release_event(
    directory: Path,
    *,
    release_audit_key: str,
    run_id: str,
    segment: str,
    preservation: str,
    marker_sha256: str,
    preserved_sha256: str,
    decision: str,
) -> dict[str, Any]:
    events = _logged_events_for_key(directory, release_audit_key)
    if len(events) != 1:
        raise OSError(
            "preservation release audit event must be committed exactly once"
        )
    event = events[0]
    expected = {
        "schema_version": "pao.audit-event.v1",
        "actor": "oa",
        "event": "audit_preservation_released",
        "idempotency_key": release_audit_key,
        "decision": decision,
        "run_id": run_id,
        "segment": segment,
        "preservation": preservation,
        "marker_sha256": marker_sha256,
        "preserved_sha256": preserved_sha256,
        "pruned_audit_key": f"rotated-prune:{run_id}",
        "resolution_audit_key": (
            f"rotated-prune-resolve:{run_id}:{segment}:{preserved_sha256}"
        ),
    }
    for field, expected_value in expected.items():
        if event.get(field) != expected_value:
            raise ValueError(
                f"preservation release audit event field drift: {field}"
            )
    if (
        not isinstance(event.get("preserved_bytes"), int)
        or event["preserved_bytes"] < 0
    ):
        raise ValueError("invalid preserved_bytes in preservation release event")
    return event


def prepare_rotated_preservation_release(
    root: Path,
    run_id: str,
    segment: str,
    expected_marker_sha256: str,
    expected_segment_sha256: str,
    decision: str,
) -> dict[str, Any]:
    """Validate one permanent protection binding under exact operator fences."""
    if not isinstance(run_id, str) or not SHA256_RE.fullmatch(run_id):
        raise ValueError("run_id must be exactly 64 hexadecimal characters")
    if not isinstance(segment, str) or not AUDIT_ROTATED_SEGMENT_RE.fullmatch(segment):
        raise ValueError("segment must be events.<digits>.jsonl")
    marker_fingerprint = (
        expected_marker_sha256.casefold()
        if isinstance(expected_marker_sha256, str)
        else ""
    )
    segment_fingerprint = (
        expected_segment_sha256.casefold()
        if isinstance(expected_segment_sha256, str)
        else ""
    )
    if not SHA256_RE.fullmatch(marker_fingerprint):
        raise ValueError(
            "expected_marker_sha256 must be exactly 64 hexadecimal characters"
        )
    if not SHA256_RE.fullmatch(segment_fingerprint):
        raise ValueError(
            "expected_segment_sha256 must be exactly 64 hexadecimal characters"
        )
    if decision != "release-protection":
        raise ValueError("decision must be release-protection")

    resolved_root = root.resolve()
    directory = resolved_root / "var" / "audit"
    marker_path = _rotated_preservation_path(directory, run_id, segment)
    preservation = marker_path.relative_to(directory).as_posix()
    release_audit_key = _preservation_release_audit_key(
        run_id,
        segment,
        marker_fingerprint,
        segment_fingerprint,
    )
    pruned_audit_key = f"rotated-prune:{run_id}"
    resolution_audit_key = (
        f"rotated-prune-resolve:{run_id}:{segment}:{segment_fingerprint}"
    )

    with FileLock(directory / ".audit.lock"):
        with FileLock(directory / ".degraded.lock"):
            try:
                marker_path.lstat()
            except FileNotFoundError:
                event = _require_preservation_release_event(
                    directory,
                    release_audit_key=release_audit_key,
                    run_id=run_id,
                    segment=segment,
                    preservation=preservation,
                    marker_sha256=marker_fingerprint,
                    preserved_sha256=segment_fingerprint,
                    decision=decision,
                )
                return {
                    "event": "audit_preservation_release_prepared",
                    "decision": decision,
                    "run_id": run_id,
                    "segment": segment,
                    "preservation": preservation,
                    "marker_sha256": marker_fingerprint,
                    "preserved_sha256": segment_fingerprint,
                    "preserved_bytes": event["preserved_bytes"],
                    "pruned_audit_key": pruned_audit_key,
                    "resolution_audit_key": resolution_audit_key,
                    "release_audit_key": release_audit_key,
                    "already_released": True,
                }
            except OSError as error:
                raise OSError(
                    f"preservation marker is unavailable: {marker_path}"
                ) from error
            if marker_path.is_symlink() or not marker_path.is_file():
                raise ValueError("preservation marker must be a regular file")
            try:
                marker_bytes = marker_path.read_bytes()
            except OSError as error:
                raise OSError(
                    f"cannot read preservation marker: {marker_path}"
                ) from error
            current_marker_sha256 = hashlib.sha256(marker_bytes).hexdigest()
            if current_marker_sha256 != marker_fingerprint:
                raise ValueError(
                    "preservation marker fingerprint changed: "
                    f"expected {marker_fingerprint}, found {current_marker_sha256}"
                )
            marker = _load_rotated_preservation(marker_path)
            if marker["preserved_sha256"] != segment_fingerprint:
                raise ValueError(
                    "preservation marker target fingerprint does not match "
                    "expected_segment_sha256"
                )

            target = directory / segment
            try:
                target.lstat()
            except OSError as error:
                raise OSError(f"preserved target is unavailable: {target}") from error
            if target.is_symlink() or not target.is_file():
                raise ValueError("preserved target must be a regular file")
            try:
                target_bytes = target.read_bytes()
            except OSError as error:
                raise OSError(f"cannot read preserved target: {target}") from error
            current_segment_sha256 = hashlib.sha256(target_bytes).hexdigest()
            if current_segment_sha256 != segment_fingerprint:
                raise ValueError(
                    "preserved target fingerprint changed: "
                    f"expected {segment_fingerprint}, found {current_segment_sha256}"
                )
            if len(target_bytes) != marker["preserved_bytes"]:
                raise ValueError("preserved target byte count changed")

            committed_keys = _logged_keys(directory)
            preservation_reports = _inspect_rotated_preservations(
                directory, committed_keys
            )
            matching_reports = [
                item
                for item in preservation_reports
                if item.get("path") == preservation
            ]
            if len(matching_reports) != 1:
                raise ValueError("preservation marker is not uniquely inspectable")
            binding = matching_reports[0]
            if binding["status"] != "protected":
                raise ValueError(
                    "preservation marker is not releasable: "
                    + ",".join(binding["reason_codes"])
                )
            return {
                "event": "audit_preservation_release_prepared",
                "decision": decision,
                "run_id": run_id,
                "segment": segment,
                "preservation": preservation,
                "marker_sha256": marker_fingerprint,
                "preserved_sha256": segment_fingerprint,
                "preserved_bytes": len(target_bytes),
                "pruned_audit_key": pruned_audit_key,
                "resolution_audit_key": resolution_audit_key,
                "release_audit_key": release_audit_key,
                "already_released": False,
            }


def commit_rotated_preservation_release(
    root: Path, report: dict[str, Any]
) -> bool:
    """Remove one exact marker only after its strict release event is committed."""
    run_id = report.get("run_id")
    segment = report.get("segment")
    marker_sha256 = report.get("marker_sha256")
    preserved_sha256 = report.get("preserved_sha256")
    decision = report.get("decision")
    if not isinstance(run_id, str) or not SHA256_RE.fullmatch(run_id):
        raise ValueError("invalid preservation release run id")
    if not isinstance(segment, str) or not AUDIT_ROTATED_SEGMENT_RE.fullmatch(segment):
        raise ValueError("invalid preservation release segment")
    if not isinstance(marker_sha256, str) or not SHA256_RE.fullmatch(marker_sha256):
        raise ValueError("invalid preservation release marker fingerprint")
    if not isinstance(preserved_sha256, str) or not SHA256_RE.fullmatch(
        preserved_sha256
    ):
        raise ValueError("invalid preservation release target fingerprint")
    if decision != "release-protection":
        raise ValueError("invalid preservation release decision")
    if (
        not isinstance(report.get("preserved_bytes"), int)
        or report["preserved_bytes"] < 0
    ):
        raise ValueError("invalid preservation release target byte count")

    resolved_root = root.resolve()
    directory = resolved_root / "var" / "audit"
    marker_path = _rotated_preservation_path(directory, run_id, segment)
    preservation = marker_path.relative_to(directory).as_posix()
    release_audit_key = _preservation_release_audit_key(
        run_id,
        segment,
        marker_sha256,
        preserved_sha256,
    )
    expected_fields = {
        "preservation": preservation,
        "pruned_audit_key": f"rotated-prune:{run_id}",
        "resolution_audit_key": (
            f"rotated-prune-resolve:{run_id}:{segment}:{preserved_sha256}"
        ),
        "release_audit_key": release_audit_key,
    }
    for field, expected_value in expected_fields.items():
        if report.get(field) != expected_value:
            raise ValueError(f"preservation release report field drift: {field}")

    with FileLock(directory / ".audit.lock"):
        with FileLock(directory / ".degraded.lock"):
            event = _require_preservation_release_event(
                directory,
                release_audit_key=release_audit_key,
                run_id=run_id,
                segment=segment,
                preservation=preservation,
                marker_sha256=marker_sha256,
                preserved_sha256=preserved_sha256,
                decision=decision,
            )
            if event["preserved_bytes"] != report["preserved_bytes"]:
                raise ValueError(
                    "preservation release audit event byte count drift"
                )
            try:
                marker_path.lstat()
            except FileNotFoundError:
                return False
            except OSError as error:
                raise OSError(
                    f"preservation marker is unavailable: {marker_path}"
                ) from error
            if marker_path.is_symlink() or not marker_path.is_file():
                raise ValueError("preservation marker must be a regular file")
            try:
                marker_bytes = marker_path.read_bytes()
            except OSError as error:
                raise OSError(
                    f"cannot read preservation marker: {marker_path}"
                ) from error
            if hashlib.sha256(marker_bytes).hexdigest() != marker_sha256:
                raise ValueError("preservation marker fingerprint changed")
            marker = _load_rotated_preservation(marker_path)
            if (
                marker["run_id"] != run_id
                or marker["segment"] != segment
                or marker["preserved_sha256"] != preserved_sha256
                or marker["preserved_bytes"] != report["preserved_bytes"]
            ):
                raise ValueError("preservation marker binding changed")

            target = directory / segment
            try:
                target.lstat()
            except OSError as error:
                raise OSError(f"preserved target is unavailable: {target}") from error
            if target.is_symlink() or not target.is_file():
                raise ValueError("preserved target must be a regular file")
            try:
                target_bytes = target.read_bytes()
            except OSError as error:
                raise OSError(f"cannot read preserved target: {target}") from error
            if (
                hashlib.sha256(target_bytes).hexdigest() != preserved_sha256
                or len(target_bytes) != report["preserved_bytes"]
            ):
                raise ValueError("preserved target binding changed")

            committed_keys = _logged_keys(directory)
            preservation_reports = _inspect_rotated_preservations(
                directory, committed_keys
            )
            matching_reports = [
                item
                for item in preservation_reports
                if item.get("path") == preservation
            ]
            if len(matching_reports) != 1:
                raise ValueError("preservation marker is not uniquely inspectable")
            binding = matching_reports[0]
            if binding["status"] != "protected":
                raise ValueError(
                    "preservation marker is not releasable: "
                    + ",".join(binding["reason_codes"])
                )
            marker_path.unlink()
            return True
