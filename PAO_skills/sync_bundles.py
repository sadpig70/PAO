"""Mirror the canonical runtime master (pao-lwar) into its generated copies.

PAO_skills is the canonical source; pao-lwar is the runtime master. Edit
pao_runtime/, scripts/, or schemas/ ONLY under pao-lwar, then run:

    python PAO_skills/sync_bundles.py               # -> pao-oa mirror
    python PAO_skills/sync_bundles.py --to-plugin   # -> pao-oa AND PAO_plugin

The plugin mirror places pao_runtime/ and scripts/ at the plugin root and
schemas/ under skills/lwar-runtime/. tests/test_standalone_skills.py fails on
any drift in either direction.
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
    to_plugin = "--to-plugin" in sys.argv[1:]
    skills = Path(__file__).resolve().parent
    master = skills / "pao-lwar"
    mirror = skills / "pao-oa"
    if not (mirror / "SKILL.md").is_file():
        raise SystemExit(f"mirror skill not found: {mirror / 'SKILL.md'}")
    for name in MIRRORED:
        replace(master / name, mirror / name)
    mirrors = ["pao-oa"]
    if to_plugin:
        plugin = skills.parent / "PAO_plugin"
        if not (plugin / ".claude-plugin" / "plugin.json").is_file():
            raise SystemExit(f"plugin root not found: {plugin}")
        replace(master / "pao_runtime", plugin / "pao_runtime")
        replace(master / "scripts", plugin / "scripts")
        replace(master / "schemas", plugin / "skills" / "lwar-runtime" / "schemas")
        mirrors.append("PAO_plugin")
    print(json.dumps({"event": "bundles_synced", "master": "pao-lwar", "mirrors": mirrors, "dirs": list(MIRRORED)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
