"""Mirror the canonical runtime master (pao-lwar) into the pao-oa bundle.

The skills live under `.agents/skills/`; `pao-lwar` is the runtime master.
Edit pao_runtime/, scripts/, or schemas/ ONLY under pao-lwar, then run:

    python tools/sync_bundles.py     # pao-lwar -> pao-oa

tests/test_standalone_skills.py (SkillsInternalSyncTests) fails on any drift
between the two bundles.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

MIRRORED = ("pao_runtime", "scripts", "schemas")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def replace(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise SystemExit(f"master source missing: {source}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=IGNORE)


def main() -> int:
    skills = Path(__file__).resolve().parents[1] / ".agents" / "skills"
    master = skills / "pao-lwar"
    mirror = skills / "pao-oa"
    if not (mirror / "SKILL.md").is_file():
        raise SystemExit(f"mirror skill not found: {mirror / 'SKILL.md'}")
    for name in MIRRORED:
        replace(master / name, mirror / name)
    print(json.dumps({"event": "bundles_synced", "master": "pao-lwar", "mirror": "pao-oa", "dirs": list(MIRRORED)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
