from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .common import (
    FileLock,
    atomic_write_json,
    ensure_mailbox,
    load_json,
    parse_utc,
    utc_now,
    validate_instance_id,
    validate_lwar_id,
)


ALLOWED_TRANSITIONS = {
    "on": {"draining", "off"},
    "draining": {"on", "off"},
    "off": {"on", "deregistered"},
}


class RegistryService:
    def __init__(self, root: Path, tombstone_retention_s: int = 300):
        self.root = root.resolve()
        self.registry_path = self.root / "var" / "registry" / "lwar_registry.json"
        self.tombstones_path = self.root / "var" / "registry" / "tombstones.json"
        self.lock_path = self.root / "var" / "registry" / ".registry.lock"
        self.tombstone_retention_s = tombstone_retention_s

    def load_registry(self) -> dict[str, Any]:
        if not self.registry_path.is_file():
            return {
                "schema_version": "pao.lwar-registry-state.v1",
                "registry_version": 0,
                "allocation_strategy": "lowest_available",
                "slots": {},
                "updated_at": utc_now(),
            }
        return load_json(self.registry_path)

    def load_tombstones(self) -> dict[str, Any]:
        if not self.tombstones_path.is_file():
            return {"schema_version": "pao.lwar-tombstones.v1", "entries": {}, "updated_at": utc_now()}
        return load_json(self.tombstones_path)

    def _tombstone_blocked(self, entry: dict[str, Any] | None) -> bool:
        if not entry:
            return False
        return parse_utc(entry["reusable_after"]) > datetime.now(timezone.utc)

    def _lowest_available(self, registry: dict[str, Any], tombstones: dict[str, Any]) -> str:
        index = 1
        while True:
            candidate = f"LWAR{index}"
            if candidate not in registry["slots"] and not self._tombstone_blocked(tombstones["entries"].get(candidate)):
                return candidate
            index += 1

    def _archive_request(self, request_path: Path, category: str) -> None:
        archive = self.root / "control" / category / "archive" / request_path.name
        archive.parent.mkdir(parents=True, exist_ok=True)
        if request_path.exists():
            os.replace(request_path, archive)

    def process_registration(self, request_path: Path) -> dict[str, Any]:
        request = load_json(request_path)
        request_id = request["request_id"]
        instance_id = validate_instance_id(request["instance_id"])
        response_path = self.root / "control" / "registration" / "responses" / f"{request_id}.json"
        if response_path.is_file():
            self._archive_request(request_path, "registration")
            return load_json(response_path)

        accepted = False
        reason = None
        lwar_id = None
        generation = None
        registry_version = None
        state = "unregistered"

        # Version handshake: a stamped, mismatched runtime is rejected before
        # any registry mutation; an absent stamp is a pre-0.5 legacy request
        # and stays accepted for the freeze window.
        request_version = request.get("runtime_version")
        version_mismatch = request_version is not None and request_version != __version__

        with FileLock(self.lock_path):
            registry = self.load_registry()
            tombstones = self.load_tombstones()
            # Idempotent replay: a prior reconcile committed the registry
            # mutation but crashed before writing the response. This instance is
            # already in a slot (one instance_id ⇒ one slot), so reconstruct its
            # response instead of allocating a SECOND slot for the same instance.
            existing_id = next(
                (lid for lid, s in registry["slots"].items() if s.get("instance_id") == instance_id),
                None,
            )
            if existing_id is not None:
                existing = registry["slots"][existing_id]
                accepted = True
                lwar_id = existing_id
                generation = existing["generation"]
                registry_version = registry["registry_version"]
                state = existing["state"]
                candidate = existing_id
            else:
                requested = request.get("requested_lwar_id")
                if requested is not None:
                    validate_lwar_id(requested)
                    candidate = requested
                else:
                    candidate = self._lowest_available(registry, tombstones)

                if version_mismatch:
                    reason = "runtime_version_mismatch"
                elif candidate in registry["slots"]:
                    reason = "lwar_id_in_use"
                elif self._tombstone_blocked(tombstones["entries"].get(candidate)):
                    reason = "lwar_id_tombstoned"
                else:
                    previous = tombstones["entries"].get(candidate, {})
                    generation = int(previous.get("last_generation", 0)) + 1
                    registry["registry_version"] = int(registry["registry_version"]) + 1
                    registry_version = registry["registry_version"]
                    registry["updated_at"] = utc_now()
                    registry["slots"][candidate] = {
                        "instance_id": instance_id,
                        "generation": generation,
                        "state": "on",
                        "profile": request["profile"],
                        "registered_at": utc_now(),
                        "last_seen": None,
                    }
                    tombstones["entries"].pop(candidate, None)
                    tombstones["updated_at"] = utc_now()
                    atomic_write_json(self.registry_path, registry)
                    atomic_write_json(self.tombstones_path, tombstones)
                    ensure_mailbox(self.root, candidate)
                    accepted = True
                    lwar_id = candidate
                    state = "on"

        response = {
            "schema_version": "pao.lwar-registration-response.v1",
            "request_id": request_id,
            "instance_id": instance_id,
            "accepted": accepted,
            "lwar_id": lwar_id,
            "generation": generation,
            "registry_version": registry_version,
            "state": state,
            "behavior_contract": "lwar-runtime.v2-adp",
            "reason": reason,
            "decided_at": utc_now(),
        }
        atomic_write_json(response_path, response)
        self._archive_request(request_path, "registration")
        return response

    def process_lifecycle(self, request_path: Path) -> dict[str, Any]:
        request = load_json(request_path)
        request_id = request["request_id"]
        lwar_id = validate_lwar_id(request["lwar_id"])
        instance_id = validate_instance_id(request["instance_id"])
        response_path = self.root / "control" / "lifecycle" / "responses" / f"{request_id}.json"
        if response_path.is_file():
            self._archive_request(request_path, "lifecycle")
            return load_json(response_path)

        accepted = False
        reason = None
        previous_state = "off"
        resulting_state = "off"
        registry_version = None

        with FileLock(self.lock_path):
            registry = self.load_registry()
            tombstones = self.load_tombstones()
            slot = registry["slots"].get(lwar_id)
            requested_state = request["requested_state"]
            tomb = tombstones["entries"].get(lwar_id)
            if slot is None and requested_state == "deregistered" and tomb is not None and (
                tomb.get("instance_id") == instance_id
                and tomb.get("last_generation") == request["generation"]
            ):
                # Idempotent replay of a committed deregister: the slot is gone
                # and a matching tombstone exists (crash before the response).
                accepted = True
                previous_state = "off"
                resulting_state = "deregistered"
                registry_version = registry["registry_version"]
            elif slot is None:
                reason = "lwar_not_registered"
            elif slot["instance_id"] != instance_id or slot["generation"] != request["generation"]:
                reason = "identity_mismatch"
            elif slot["state"] == requested_state:
                # Idempotent replay: the transition already applied (crash before
                # the response was written); re-affirm it rather than rejecting
                # it as an invalid same-state transition.
                accepted = True
                previous_state = requested_state
                resulting_state = requested_state
                registry_version = registry["registry_version"]
            else:
                previous_state = slot["state"]
                resulting_state = previous_state
                if requested_state not in ALLOWED_TRANSITIONS.get(previous_state, set()):
                    reason = "invalid_transition"
                else:
                    registry["registry_version"] = int(registry["registry_version"]) + 1
                    registry_version = registry["registry_version"]
                    registry["updated_at"] = utc_now()
                    resulting_state = requested_state
                    if requested_state == "deregistered":
                        del registry["slots"][lwar_id]
                        reusable_after = datetime.now(timezone.utc) + timedelta(seconds=self.tombstone_retention_s)
                        tombstones["entries"][lwar_id] = {
                            "last_generation": request["generation"],
                            "instance_id": instance_id,
                            "deregistered_at": utc_now(),
                            "reusable_after": reusable_after.isoformat().replace("+00:00", "Z"),
                        }
                        tombstones["updated_at"] = utc_now()
                        atomic_write_json(self.tombstones_path, tombstones)
                    else:
                        slot["state"] = requested_state
                    atomic_write_json(self.registry_path, registry)
                    accepted = True

        response = {
            "schema_version": "pao.lwar-lifecycle-response.v1",
            "request_id": request_id,
            "lwar_id": lwar_id,
            "instance_id": instance_id,
            "generation": request["generation"],
            "accepted": accepted,
            "previous_state": previous_state,
            "resulting_state": resulting_state,
            "registry_version": registry_version,
            "reason": reason,
            "decided_at": utc_now(),
        }
        atomic_write_json(response_path, response)
        self._archive_request(request_path, "lifecycle")
        return response

    def _quarantine_request(self, request_path: Path, category: str, error: object) -> None:
        """Move a request that could not be processed (corrupt JSON, missing
        required field, ...) into `control/<category>/failed/` so one poison
        request can never wedge every future reconcile at the same spot."""
        failed = self.root / "control" / category / "failed" / request_path.name
        failed.parent.mkdir(parents=True, exist_ok=True)
        if not request_path.exists():
            return
        try:
            os.replace(request_path, failed)
        except OSError:
            return
        try:
            atomic_write_json(
                failed.with_suffix(".error.json"),
                {"reason": f"reconcile_error:{error}", "failed_at": utc_now()},
            )
        except OSError:
            pass

    def reconcile(self) -> dict[str, int]:
        registration_dir = self.root / "control" / "registration" / "requests"
        lifecycle_dir = self.root / "control" / "lifecycle" / "requests"
        registration_dir.mkdir(parents=True, exist_ok=True)
        lifecycle_dir.mkdir(parents=True, exist_ok=True)
        registrations = 0
        lifecycles = 0
        quarantined = 0
        for path in sorted(registration_dir.glob("*.json")):
            try:
                self.process_registration(path)
                registrations += 1
            except Exception as error:  # noqa: BLE001 — one bad request must not wedge the sweep
                self._quarantine_request(path, "registration", error)
                quarantined += 1
        for path in sorted(lifecycle_dir.glob("*.json")):
            try:
                self.process_lifecycle(path)
                lifecycles += 1
            except Exception as error:  # noqa: BLE001
                self._quarantine_request(path, "lifecycle", error)
                quarantined += 1
        return {"registrations": registrations, "lifecycles": lifecycles, "quarantined": quarantined}
