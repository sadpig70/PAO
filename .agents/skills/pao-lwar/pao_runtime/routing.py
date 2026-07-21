from __future__ import annotations

from datetime import datetime
from typing import Any

from .common import LWAR_ID_RE, parse_utc
from .transport import Transport


STALE_AFTER_S_DEFAULT = 120
STARTUP_DEADLINE_S_DEFAULT = 30
SCORE_RUNNING = 10
SCORE_STALE = 1000
ROUTABLE_HEARTBEAT_STATUSES = frozenset({"watching", "idle", "running"})


def heartbeat_age_s(heartbeat: dict[str, Any] | None, now: datetime) -> float | None:
    if not heartbeat or not heartbeat.get("last_seen"):
        return None
    return max(0.0, (now - parse_utc(heartbeat["last_seen"])).total_seconds())


def heartbeat_stale(heartbeat: dict[str, Any] | None, now: datetime, stale_after_s: float) -> bool:
    age = heartbeat_age_s(heartbeat, now)
    return age is None or age > stale_after_s


def heartbeat_matches_slot(heartbeat: dict[str, Any] | None, slot: dict[str, Any]) -> bool:
    """Return whether a heartbeat belongs to the registry's current identity."""
    return bool(
        heartbeat
        and heartbeat.get("instance_id") == slot.get("instance_id")
        and heartbeat.get("generation") == slot.get("generation")
    )


def classify_lwar_runtime(
    slot: dict[str, Any],
    heartbeat: dict[str, Any] | None,
    now: datetime,
    stale_after_s: float,
    startup_deadline_s: float = STARTUP_DEADLINE_S_DEFAULT,
) -> dict[str, Any]:
    """Separate never-started identities from runtimes that became stale.

    Identity adoption publishes a matching ``starting`` heartbeat. Until the
    resident watcher replaces it with an operational status, the LWAR is not
    routable and is governed by the shorter startup deadline.
    """
    identity_match = heartbeat_matches_slot(heartbeat, slot)
    age = heartbeat_age_s(heartbeat, now) if identity_match else None
    heartbeat_status = heartbeat.get("status") if identity_match and heartbeat else None

    if not identity_match:
        runtime_status = "registered_not_started"
        registered_not_started = True
        startup_deadline_missed = False
        stale = True
    elif heartbeat_status == "starting":
        startup_deadline_missed = age is None or age > startup_deadline_s
        runtime_status = "registered_not_started" if startup_deadline_missed else "starting"
        registered_not_started = True
        stale = startup_deadline_missed
    else:
        startup_deadline_missed = False
        registered_not_started = False
        stale = heartbeat_stale(heartbeat, now, stale_after_s)
        if stale:
            runtime_status = "stale"
        elif heartbeat_status in ROUTABLE_HEARTBEAT_STATUSES:
            runtime_status = "active"
        else:
            runtime_status = "inactive"

    return {
        "runtime_status": runtime_status,
        "registered_not_started": registered_not_started,
        "heartbeat_identity_match": identity_match,
        "heartbeat_stale": stale,
        "startup_age_s": age if registered_not_started else None,
        "startup_deadline_s": startup_deadline_s,
        "startup_deadline_missed": startup_deadline_missed,
    }


def load_score(transport: Transport, lwar_id: str, now: datetime, stale_after_s: float) -> int:
    """Lower is better: incoming backlog, busy penalty, stale near-exclusion."""
    heartbeat = transport.read_heartbeat(lwar_id)
    score = transport.incoming_backlog(lwar_id)
    if heartbeat and heartbeat.get("status") == "running":
        score += SCORE_RUNNING
    if heartbeat_stale(heartbeat, now, stale_after_s):
        score += SCORE_STALE
    return score


def lwar_number(lwar_id: str) -> int:
    return int(lwar_id[len("LWAR"):])


def auto_route(
    registry: dict[str, Any],
    transport: Transport,
    require: set[str],
    now: datetime,
    stale_after_s: float = STALE_AFTER_S_DEFAULT,
) -> str | None:
    """Pick the best `on` LWAR holding every required capability.

    Deterministic: ties break toward the lowest LWAR number. Returns None when
    no eligible candidate exists — callers must not fall back to an arbitrary
    LWAR.
    """
    candidates = []
    for lwar_id, slot in registry.get("slots", {}).items():
        # Skip any hand-corrupted / foreign slot key: lwar_number would raise on
        # a non-LWARn key and abort routing for every LWAR.
        if not LWAR_ID_RE.fullmatch(lwar_id):
            continue
        if slot.get("state") != "on":
            continue
        capabilities = set(slot.get("profile", {}).get("capabilities", []))
        if not require <= capabilities:
            continue
        heartbeat = transport.read_heartbeat(lwar_id)
        if not heartbeat_matches_slot(heartbeat, slot):
            continue
        if heartbeat.get("status") not in ROUTABLE_HEARTBEAT_STATUSES:
            continue
        if heartbeat_stale(heartbeat, now, stale_after_s):
            continue
        score = load_score(transport, lwar_id, now, stale_after_s)
        candidates.append((score, lwar_number(lwar_id), lwar_id))
    if not candidates:
        return None
    return min(candidates)[2]
