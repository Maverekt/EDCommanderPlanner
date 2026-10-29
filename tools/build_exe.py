"""Build the onedir exe without touching its data folder (DESIGN §4: updates never touch data\\).

PyInstaller deletes its whole output folder before writing a build, which would
take `dist\\ED Commander Planner\\data\\` (database, logs, backups) with it. So
build into a staging folder, then copy every file except `data\\` into place.

  .venv\\Scripts\\python tools\\build_exe.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "ED Commander Planner"
STAGING = ROOT / "build" / "staging-dist"
TARGET = ROOT / "dist" / APP_NAME
KEEP = {"data"}


def main() -> int:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / "planner.spec"), "--noconfirm",
         "--log-level", "ERROR", "--distpath", str(STAGING), "--workpath", str(ROOT / "build" / "work")],
        check=True, cwd=ROOT,
    )
    built = STAGING / APP_NAME
    TARGET.mkdir(parents=True, exist_ok=True)
    for entry in TARGET.iterdir():
        if entry.name in KEEP:
            continue
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    for entry in built.iterdir():
        dest = TARGET / entry.name
        shutil.copytree(entry, dest) if entry.is_dir() else shutil.copy2(entry, dest)
    for notice in ("LICENSE", "THIRD_PARTY.md"):  # the zip carries the licences (pystray is LGPL)
        shutil.copy2(ROOT / notice, TARGET / notice)
    shutil.rmtree(STAGING)
    kept = [k for k in KEEP if (TARGET / k).exists()]
    print(f"built {TARGET}" + (f" (kept {', '.join(kept)})" if kept else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
