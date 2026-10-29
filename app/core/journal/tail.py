"""Live tail of the newest journal (DESIGN §5.8), with live commander attribution (§5.4).

Each `tick()` reads only complete new lines. On start, and whenever the game
opens a new journal, the newest file is read from byte 0: dedup (§5.7) makes
re-reading lines the backfill already stored a no-op, so nothing is missed in
the gap between backfill and tail.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.core import db
from app.core.journal.files import JOURNAL_GLOB, JournalFile, read_header
from app.core.journal.importer import COMMANDER_EVENTS, Importer, _Pending, event_hash
from app.core.journal.ingest_filter import IngestFilter

log = logging.getLogger(__name__)

StoredEvent = tuple[int, str, str, str, dict]  # (id, fid, ts, name, event)


@dataclass
class TailStats:
    path: str | None = None
    offset: int = 0
    events_added: int = 0
    events_dropped: Counter = field(default_factory=Counter)
    last_event_ts: str | None = None
    reattributed: int = 0


class JournalTail:
    def __init__(self, conn: sqlite3.Connection, journal_dir: Path, ingest_filter: IngestFilter):
        self.conn = conn
        self.journal_dir = journal_dir
        self.importer = Importer(conn, ingest_filter)
        self.filter = ingest_filter
        self.stats = TailStats()
        self._headers: dict[str, JournalFile] = {}
        self._current: JournalFile | None = None
        self._current_fid: str | None = None
        self._provisional: list[tuple[int, _Pending, str]] = []  # (id, line, fid it was given)
        self._held: list[_Pending] = []

    # -- files ---------------------------------------------------------------

    def _newest(self) -> JournalFile | None:
        if not self.journal_dir.is_dir():
            return None
        for path in self.journal_dir.glob(JOURNAL_GLOB):
            known = self._headers.get(path.name)
            # A brand-new file may not have its Fileheader line yet; retry until it does.
            if known is None or known.error:
                self._headers[path.name] = read_header(path)
        live = [jf for jf in self._headers.values() if not jf.error and jf.is_live and jf.path.exists()]
        return max(live, key=lambda jf: (jf.header_ts or "", jf.part), default=None)

    def _read_new_lines(self, jf: JournalFile) -> list[str]:
        try:
            with jf.path.open("rb") as f:
                f.seek(self.stats.offset)
                data = f.read()
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []  # only a partial line so far
        self.stats.offset += end + 1
        out = []
        for chunk in data[: end + 1].split(b"\n")[:-1]:
            out.append((chunk[:-1] if chunk.endswith(b"\r") else chunk).decode("utf-8", errors="replace"))
        return out

    # -- attribution -----------------------------------------------------------

    def _resolve_provisional(self, fid: str) -> int:
        """Confirm or re-attribute events stored before this file named its commander (§5.4 rule 2)."""
        moved = 0
        for event_id, p, given in self._provisional:
            if given == fid:
                self.conn.execute("UPDATE events SET fid_status = 'confirmed' WHERE id = ?", (event_id,))
                continue
            new_hash = event_hash(fid, p.raw)
            if self.conn.execute("SELECT 1 FROM events WHERE hash = ?", (new_hash,)).fetchone():
                self.conn.execute("DELETE FROM events WHERE id = ?", (event_id,))  # already stored for fid
            else:
                self.conn.execute(
                    "UPDATE events SET fid = ?, fid_status = 'confirmed', hash = ? WHERE id = ?",
                    (fid, new_hash, event_id),
                )
            moved += 1
        self._provisional.clear()
        return moved

    def _ingest(self, raw: str, out: list[StoredEvent]) -> None:
        if not raw.strip():
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        name, ts = event.get("event"), event.get("timestamp")
        if not name or not ts:
            return
        if self.filter.drops(event):
            self.stats.events_dropped[name] += 1
            return
        p = _Pending(ts, name, raw)
        fid = event.get("FID") if name in COMMANDER_EVENTS else None
        if fid:
            self.importer._upsert_commander(fid, event.get("Name") or event.get("Commander") or "", ts)
            if self._current_fid is None:
                moved = self._resolve_provisional(fid)
                self.stats.reattributed += moved
                for held in self._held:
                    self._store(fid, "confirmed", held, out)
                self._held.clear()
            self._current_fid = fid
            self.importer.last_fid = fid
            db.set_setting(self.conn, "last_fid", fid)
            self._store(fid, "confirmed", p, out)
        elif self._current_fid:
            self._store(self._current_fid, "confirmed", p, out)
        elif self.importer.last_fid:
            event_id = self._store(self.importer.last_fid, "provisional", p, out)
            if event_id is not None:
                self._provisional.append((event_id, p, self.importer.last_fid))
        else:
            self._held.append(p)

    def _store(self, fid: str, status: str, p: _Pending, out: list[StoredEvent]) -> int | None:
        event_id = self.importer.insert_new(fid, status, p)
        if event_id is not None:
            out.append((event_id, fid, p.ts, p.event, json.loads(p.raw)))
            self.stats.events_added += 1
            self.stats.last_event_ts = p.ts
        return event_id

    # -- tick ------------------------------------------------------------------

    def tick(self) -> list[StoredEvent]:
        """Read whatever the game wrote since the last tick. Returns newly stored events."""
        out: list[StoredEvent] = []
        newest = self._newest()
        if newest is None:
            return out
        with self.conn:
            if self._current is None or newest.path != self._current.path:
                if self._current is not None:
                    for raw in self._read_new_lines(self._current):  # finish the old file first
                        self._ingest(raw, out)
                log.info("tailing %s", newest.path.name)
                self._current = newest
                self._current_fid = None
                self._provisional.clear()
                self.stats.path = newest.path.name
                self.stats.offset = 0
            for raw in self._read_new_lines(self._current):
                self._ingest(raw, out)
        return out
