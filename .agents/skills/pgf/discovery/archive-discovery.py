#!/usr/bin/env python3
"""Archive discovery artifacts by date."""
import shutil, sys
from pathlib import Path
from datetime import datetime

discovery_dir = Path(".pgf/discovery")
if not discovery_dir.exists():
    print("No discovery directory found.")
    sys.exit(0)

date_str = datetime.now().strftime("%Y%m%d")
archive_dir = discovery_dir / f"archive-{date_str}"
archive_dir.mkdir(exist_ok=True)

count = 0
for f in discovery_dir.glob("*.md"):
    if "archive" not in f.name:
        shutil.move(str(f), str(archive_dir / f.name))
        count += 1

print(f"Archived {count} files to {archive_dir}")
