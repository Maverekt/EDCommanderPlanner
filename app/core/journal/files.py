"""Journal discovery, Live-only filter and file ordering (DESIGN §5.2, §5.3)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

JOURNAL_GLOB = "Journal.*.log"
MIN_LIVE_MAJOR = 4


@dataclass(frozen=True)
class JournalFile:
    path: Path
    header_ts: str | None   # Fileheader.timestamp (UTC ISO-8601)
    part: int
    gameversion: str | None
    error: str | None = None

    @property
    def is_live(self) -> bool:
        major = _major_version(self.gameversion)
        return major is not None and major >= MIN_LIVE_MAJOR


def _major_version(gameversion: str | None) -> int | None:
    if not gameversion:
        return None
    head = gameversion.strip().split(".", 1)[0]
    return int(head) if head.isdigit() else None


def read_header(path: Path) -> JournalFile:
    """Read only the first line; every journal starts with `Fileheader`."""
    try:
        with path.open("rb") as f:
            first = f.readline()
    except OSError as exc:
        return JournalFile(path, None, 0, None, error=f"unreadable: {exc}")
    try:
        header = json.loads(first.decode("utf-8", errors="replace").rstrip("\r\n"))
    except json.JSONDecodeError:
        return JournalFile(path, None, 0, None, error="first line is not JSON")
    if header.get("event") != "Fileheader":
        return JournalFile(path, None, 0, None, error="first event is not Fileheader")
    return JournalFile(
        path=path,
        header_ts=header.get("timestamp"),
        part=int(header.get("part") or 1),
        gameversion=header.get("gameversion"),
    )


def discover(journal_dir: Path) -> list[JournalFile]:
    """All journal files, ordered by Fileheader UTC timestamp then part.

    Never by file name: the name carries local time and breaks across DST.
    Files whose header can't be read sort last.
    """
    files = [read_header(p) for p in journal_dir.glob(JOURNAL_GLOB) if p.is_file()]
    return sorted(files, key=lambda jf: (jf.header_ts is None, jf.header_ts or "", jf.part, jf.path.name))


def iter_lines(path: Path) -> Iterator[str]:
    """Complete lines with only the line ending stripped (lines end in CRLF).

    A final line with no newline is still being written by the game and is held back.
    """
    with path.open("rb") as f:
        data = f.read()
    chunks = data.split(b"\n")
    for chunk in chunks[:-1]:
        if chunk.endswith(b"\r"):
            chunk = chunk[:-1]
        yield chunk.decode("utf-8", errors="replace")
