"""Mirror the canonical runtime master (pao-lwar) into the pao-oa bundle.

The skills live under `.agents/skills/`; `pao-lwar` is the runtime master.
Edit pao_runtime/, scripts/, or schemas/ ONLY under pao-lwar, then run:

    python tools/sync_bundles.py     # pao-lwar -> pao-oa

tests/test_standalone_skills.py (SkillsInternalSyncTests) fails on any drift
between the two bundles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

MIRRORED = ("pao_runtime", "scripts", "schemas")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def replace(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise SystemExit(f"master source missing: {source}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=IGNORE)


def manifest(directory: Path) -> dict[str, str]:
    entries = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries[path.relative_to(directory).as_posix()] = digest
    return entries


def bundle_diff(master: Path, mirror: Path) -> dict[str, dict[str, list[str]]]:
    differences = {}
    for name in MIRRORED:
        expected = manifest(master / name)
        actual = manifest(mirror / name) if (mirror / name).is_dir() else {}
        if expected == actual:
            continue
        differences[name] = {
            "missing": sorted(set(expected) - set(actual)),
            "extra": sorted(set(actual) - set(expected)),
            "changed": sorted(key for key in set(expected) & set(actual) if expected[key] != actual[key]),
        }
    return differences


def inplace_sync_locked_mirror(master: Path, mirror: Path) -> None:
    """Converge generated trees when Windows blocks renaming the open skill root."""
    for name in MIRRORED:
        source_root = master / name
        destination_root = mirror / name
        if not source_root.is_dir():
            raise SystemExit(f"master source missing: {source_root}")
        destination_root.mkdir(parents=True, exist_ok=True)
        expected_manifest = manifest(source_root)
        actual_manifest = manifest(destination_root)
        changed = {
            relative
            for relative, digest in expected_manifest.items()
            if actual_manifest.get(relative) != digest
        }
        for relative in sorted(changed):
            source = source_root / relative
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.parent / f".{destination.name}.sync-{uuid.uuid4().hex}"
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        for relative in sorted(set(actual_manifest) - set(expected_manifest), reverse=True):
            (destination_root / relative).unlink(missing_ok=True)
        for directory in sorted(
            (path for path in destination_root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass


def transactional_sync(master: Path, mirror: Path) -> None:
    parent = mirror.parent
    token = uuid.uuid4().hex
    staging = parent / f".pao-oa-sync-{token}"
    backup = parent / f".pao-oa-backup-{token}"
    try:
        shutil.copytree(mirror, staging, ignore=IGNORE)
        for name in MIRRORED:
            replace(master / name, staging / name)
        try:
            os.replace(mirror, backup)
        except PermissionError:
            inplace_sync_locked_mirror(master, mirror)
            return
        try:
            os.replace(staging, mirror)
        except BaseException:
            os.replace(backup, mirror)
            raise
        shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if backup.exists() and mirror.exists():
            shutil.rmtree(backup)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync the pao-lwar runtime master into pao-oa")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report bundle drift without changing either bundle",
    )
    args = parser.parse_args()
    skills = Path(__file__).resolve().parents[1] / ".agents" / "skills"
    master = skills / "pao-lwar"
    mirror = skills / "pao-oa"
    if not (mirror / "SKILL.md").is_file():
        raise SystemExit(f"mirror skill not found: {mirror / 'SKILL.md'}")
    differences = bundle_diff(master, mirror)
    if args.check:
        print(json.dumps({"event": "bundle_check", "in_sync": not differences, "differences": differences}))
        return 0 if not differences else 1
    transactional_sync(master, mirror)
    print(json.dumps({"event": "bundles_synced", "master": "pao-lwar", "mirror": "pao-oa", "dirs": list(MIRRORED)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
