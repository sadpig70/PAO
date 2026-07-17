"""Mirror the runtime master bundle (pao-lwar) into pao-oa.

PAO_skills is the canonical source while the plugin is frozen; pao-lwar is the
runtime master. Edit pao_runtime/, scripts/, or schemas/ ONLY under pao-lwar,
then run:

    python PAO_skills/sync_bundles.py

tests/test_standalone_skills.py (SkillsInternalSyncTests) fails on any drift.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

MIRRORED = ("pao_runtime", "scripts", "schemas")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def main() -> int:
    skills = Path(__file__).resolve().parent
    master = skills / "pao-lwar"
    mirror = skills / "pao-oa"
    if not (mirror / "SKILL.md").is_file():
        raise SystemExit(f"mirror skill not found: {mirror / 'SKILL.md'}")
    synced = []
    for name in MIRRORED:
        source = master / name
        if not source.is_dir():
            raise SystemExit(f"master source missing: {source}")
        destination = mirror / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination, ignore=IGNORE)
        synced.append(name)
    print(json.dumps({"event": "bundles_synced", "master": "pao-lwar", "mirror": "pao-oa", "dirs": synced}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
