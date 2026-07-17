from __future__ import annotations

from datetime import datetime
from typing import Any

from .common import parse_utc
from .transport import Transport


STALE_AFTER_S_DEFAULT = 120
SCORE_RUNNING = 10
SCORE_STALE = 1000


def heartbeat_age_s(heartbeat: dict[str, Any] | None, now: datetime) -> float | None:
    if not heartbeat or not heartbeat.get("last_seen"):
        return None
    return max(0.0, (now - parse_utc(heartbeat["last_seen"])).total_seconds())


def heartbeat_stale(heartbeat: dict[str, Any] | None, now: datetime, stale_after_s: float) -> bool:
    age = heartbeat_age_s(heartbeat, now)
    return age is None or age > stale_after_s


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
        if slot.get("state") != "on":
            continue
        capabilities = set(slot.get("profile", {}).get("capabilities", []))
        if not require <= capabilities:
            continue
        score = load_score(transport, lwar_id, now, stale_after_s)
        candidates.append((score, lwar_number(lwar_id), lwar_id))
    if not candidates:
        return None
    return min(candidates)[2]
