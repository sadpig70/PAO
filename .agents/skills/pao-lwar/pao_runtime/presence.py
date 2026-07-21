from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .common import atomic_write_json, parse_utc, safe_load_json, utc_now
from .contracts import ContractError, validate_contract


OA_PRESENCE_TTL_S = 90.0
OA_PRESENCE_REFRESH_S = 30.0


def publish_oa_presence(root: Path, oa_id: str, ttl_s: float = OA_PRESENCE_TTL_S) -> dict[str, Any]:
    if ttl_s <= 0 or ttl_s > OA_PRESENCE_TTL_S:
        raise ValueError(f"OA presence TTL must be in (0, {OA_PRESENCE_TTL_S:g}]")
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": "pao.oa-presence.v1",
        "oa_id": oa_id,
        "status": "supervising",
        "last_seen": utc_now(),
        "expires_at": (now + timedelta(seconds=ttl_s)).isoformat().replace("+00:00", "Z"),
    }
    validate_contract(payload, "oa-presence.schema.json")
    atomic_write_json(root / "var" / "oa" / "presence.json", payload)
    return payload


def read_oa_presence(root: Path) -> dict[str, Any]:
    path = root / "var" / "oa" / "presence.json"
    if not path.is_file():
        return {"status": "missing", "live": False, "presence": None, "age_s": None}
    payload = safe_load_json(path)
    if payload is None:
        return {"status": "invalid", "live": False, "presence": None, "age_s": None}
    try:
        validate_contract(payload, "oa-presence.schema.json")
        now = datetime.now(timezone.utc)
        last_seen = parse_utc(payload["last_seen"])
        expires_at = parse_utc(payload["expires_at"])
    except (ContractError, KeyError, TypeError, ValueError):
        return {"status": "invalid", "live": False, "presence": None, "age_s": None}
    age_s = max(0.0, (now - last_seen).total_seconds())
    window_s = (expires_at - last_seen).total_seconds()
    if window_s <= 0 or window_s > OA_PRESENCE_TTL_S + 1.0:
        return {"status": "invalid", "live": False, "presence": None, "age_s": None}
    live = expires_at > now and age_s <= OA_PRESENCE_TTL_S
    return {
        "status": "live" if live else "stale",
        "live": live,
        "presence": payload,
        "age_s": round(age_s, 3),
    }
