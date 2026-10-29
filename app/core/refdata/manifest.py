"""`refdata/manifest.json`: pinned upstream commits and a hash per file (DESIGN §7.5)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.core.refdata.build import REFDATA_SCHEMA

MANIFEST = "manifest.json"
UPSTREAM = {
    "fdevids": {"repo": "EDCD/FDevIDs", "files": {}},
    "coriolis": {"repo": "EDCD/coriolis-data", "files": {}},
}


def hashed_files(refdata_dir: Path) -> dict[str, str]:
    out = {}
    for path in sorted(refdata_dir.rglob("*")):
        if path.is_file() and path.name != MANIFEST:
            rel = path.relative_to(refdata_dir).as_posix()
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def read(refdata_dir: Path) -> dict | None:
    path = refdata_dir / MANIFEST
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write(refdata_dir: Path, version: str, sources: dict[str, dict]) -> dict:
    manifest = {
        "refdata_version": version,
        "refdata_schema": REFDATA_SCHEMA,
        "sources": sources,
        "files": hashed_files(refdata_dir),
    }
    (refdata_dir / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def verify(refdata_dir: Path) -> list[str]:
    """Every listed file present with its hash, and no unlisted files."""
    manifest = read(refdata_dir)
    if manifest is None:
        return ["manifest.json missing"]
    problems = []
    actual = hashed_files(refdata_dir)
    for rel, digest in manifest.get("files", {}).items():
        if rel not in actual:
            problems.append(f"{rel}: listed in manifest but missing")
        elif actual[rel] != digest:
            problems.append(f"{rel}: hash mismatch")
    for rel in actual.keys() - manifest.get("files", {}).keys():
        problems.append(f"{rel}: not listed in manifest")
    return problems


def commits(refdata_dir: Path) -> dict[str, str]:
    manifest = read(refdata_dir) or {}
    return {name: src.get("commit", "unknown") for name, src in manifest.get("sources", {}).items()}
