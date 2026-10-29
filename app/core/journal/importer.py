"""Journal backfill: discover → filter → attribute → dedup insert (DESIGN §5.4–§5.7).

Projectors (§9) don't exist yet, so step 4 of §5.5 is a no-op for now.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.core import db
from app.core.journal.files import JournalFile, discover, iter_lines
from app.core.journal.ingest_filter import IngestFilter

log = logging.getLogger(__name__)

COMMANDER_EVENTS = ("Commander", "LoadGame")
SKIP_STATUSES = ("imported", "skipped_legacy", "provisional_cmdr")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_hash(fid: str, raw_line: str) -> str:
    """`sha256(fid + "\\n" + raw_line_stripped)`; the line ending is already stripped."""
    return hashlib.sha256(f"{fid}\n{raw_line}".encode("utf-8")).hexdigest()


@dataclass
class ImportStatus:
    """Progress of the current or last import, read by the UI."""

    state: str = "idle"              # idle | running | done | error
    files_total: int = 0
    files_done: int = 0
    current_file: str | None = None
    events_added: int = 0
    events_dropped: Counter = field(default_factory=Counter)
    files_skipped_legacy: int = 0
    files_provisional: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "files_total": self.files_total,
                "files_done": self.files_done,
                "current_file": self.current_file,
                "events_added": self.events_added,
                "events_dropped": sum(self.events_dropped.values()),
                "files_skipped_legacy": self.files_skipped_legacy,
                "files_provisional": self.files_provisional,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
            }

    def update(self, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self, key, value)


@dataclass
class _Pending:
    ts: str
    event: str
    raw: str


@dataclass
class _FileResult:
    status: str
    added: int = 0
    dropped: Counter = field(default_factory=Counter)
    bad_lines: int = 0
    detail: str | None = None


class Importer:
    """One importer per run. `last_fid` carries attribution across files (§5.4)."""

    def __init__(self, conn: sqlite3.Connection, ingest_filter: IngestFilter):
        self.conn = conn
        self.filter = ingest_filter
        self.last_fid: str | None = db.get_setting(conn, "last_fid")
        # Events from files that never identified a commander, with no earlier
        # commander to fall back on: held until the first `Commander` (§5.4 rule 3).
        self._held: list[_Pending] = []
        self._held_log_ids: list[int] = []

    # -- database helpers -------------------------------------------------

    def _upsert_commander(self, fid: str, name: str, ts: str) -> None:
        self.conn.execute(
            """
            INSERT INTO commanders (fid, name, first_seen, last_seen) VALUES (?, ?, ?, ?)
            ON CONFLICT(fid) DO UPDATE SET
              name = CASE WHEN excluded.last_seen >= COALESCE(commanders.last_seen, '') THEN excluded.name ELSE commanders.name END,
              first_seen = MIN(COALESCE(commanders.first_seen, excluded.first_seen), excluded.first_seen),
              last_seen = MAX(COALESCE(commanders.last_seen, excluded.last_seen), excluded.last_seen)
            """,
            (fid, name, ts, ts),
        )

    def _insert(self, fid: str, fid_status: str, p: _Pending) -> int:
        digest = event_hash(fid, p.raw)
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO events (fid, fid_status, ts, event, raw_json, hash, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'journal')",
            (fid, fid_status, p.ts, p.event, p.raw, digest),
        )
        if cur.rowcount == 0 and fid_status == "confirmed":
            # Stored provisionally on an earlier read; this read confirms it.
            self.conn.execute(
                "UPDATE events SET fid_status = 'confirmed' WHERE hash = ? AND fid_status = 'provisional'",
                (digest,),
            )
        return cur.rowcount

    def insert_new(self, fid: str, fid_status: str, p: _Pending) -> int | None:
        """Insert one event; the new row id, or None if it was already stored."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO events (fid, fid_status, ts, event, raw_json, hash, source) "
            "VALUES (?, ?, ?, ?, ?, ?, 'journal')",
            (fid, fid_status, p.ts, p.event, p.raw, event_hash(fid, p.raw)),
        )
        return cur.lastrowid if cur.rowcount == 1 else None

    def already_imported(self, jf: JournalFile, size: int, mtime: str) -> bool:
        row = self.conn.execute(
            f"SELECT 1 FROM import_log WHERE kind = 'journal_file' AND ref = ? AND size = ? AND mtime = ? "
            f"AND status IN ({','.join('?' * len(SKIP_STATUSES))}) "
            f"AND (detail IS NULL OR detail NOT LIKE 'held:%') LIMIT 1",
            (jf.path.name, size, mtime, *SKIP_STATUSES),
        ).fetchone()
        return row is not None

    def _log(self, jf: JournalFile, size: int, mtime: str, sha: str | None, result: _FileResult) -> int:
        cur = self.conn.execute(
            "INSERT INTO import_log (kind, ref, size, mtime, sha256, status, events_added, "
            "events_dropped_json, at, detail) VALUES ('journal_file', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                jf.path.name, size, mtime, sha, result.status, result.added,
                json.dumps(dict(result.dropped), sort_keys=True), utc_now(), result.detail,
            ),
        )
        return cur.lastrowid

    # -- per-file import --------------------------------------------------

    def import_file(self, jf: JournalFile) -> _FileResult:
        if jf.error:
            return _FileResult(status="error", detail=jf.error)
        if not jf.is_live:
            return _FileResult(status="skipped_legacy", detail=f"gameversion {jf.gameversion}")

        result = _FileResult(status="imported")
        pending: list[_Pending] = []
        current_fid: str | None = None

        for raw in iter_lines(jf.path):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                result.bad_lines += 1
                continue
            name = event.get("event")
            ts = event.get("timestamp")
            if not name or not ts:
                result.bad_lines += 1
                continue
            if self.filter.drops(event):
                result.dropped[name] += 1
                continue

            p = _Pending(ts, name, raw)
            fid = event.get("FID") if name in COMMANDER_EVENTS else None
            if fid:
                self._upsert_commander(fid, event.get("Name") or event.get("Commander") or "", ts)
                if current_fid is None:
                    # First identification in this file confirms everything before it.
                    for held in self._held + pending:
                        result.added += self._insert(fid, "confirmed", held)
                    self._held.clear()
                    pending.clear()
                    self._resolve_held_logs()
                current_fid = fid
                self.last_fid = fid
                result.added += self._insert(fid, "confirmed", p)
            elif current_fid:
                result.added += self._insert(current_fid, "confirmed", p)
            else:
                pending.append(p)

        if pending:
            result.status = "provisional_cmdr"
            if self.last_fid:
                for p in pending:
                    result.added += self._insert(self.last_fid, "provisional", p)
                result.detail = f"{len(pending)} events attributed provisionally to the last commander"
            else:
                self._held.extend(pending)
                result.detail = f"held: {len(pending)} events waiting for the first commander"
        if result.bad_lines:
            note = f"{result.bad_lines} unparseable lines skipped"
            result.detail = f"{result.detail}; {note}" if result.detail else note
        return result

    def _resolve_held_logs(self) -> None:
        if self._held_log_ids:
            self.conn.executemany(
                "UPDATE import_log SET status = 'imported', detail = 'held events confirmed by a later file' WHERE id = ?",
                [(i,) for i in self._held_log_ids],
            )
            self._held_log_ids.clear()

    # -- run --------------------------------------------------------------

    def run(
        self,
        journal_dir: Path,
        status: ImportStatus,
        notify: Callable[[], None] = lambda: None,
        stop: threading.Event | None = None,
    ) -> None:
        files = discover(journal_dir) if journal_dir.is_dir() else []
        todo = []
        for jf in files:
            st = jf.path.stat()
            mtime = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            if not self.already_imported(jf, st.st_size, mtime):
                todo.append((jf, st.st_size, mtime))

        status.update(state="running", files_total=len(todo), files_done=0, events_added=0,
                      events_dropped=Counter(), files_skipped_legacy=0, files_provisional=0,
                      started_at=utc_now(), finished_at=None, error=None, current_file=None)
        notify()

        for jf, size, mtime in todo:
            if stop is not None and stop.is_set():
                break
            status.update(current_file=jf.path.name)
            sha = hashlib.sha256(jf.path.read_bytes()).hexdigest() if not jf.error else None
            with self.conn:
                result = self.import_file(jf)
                log_id = self._log(jf, size, mtime, sha, result)
                if result.detail and result.detail.startswith("held:"):
                    self._held_log_ids.append(log_id)
                if self.last_fid:
                    db.set_setting(self.conn, "last_fid", self.last_fid)
            with status._lock:
                status.files_done += 1
                status.events_added += result.added
                status.events_dropped.update(result.dropped)
                status.files_skipped_legacy += result.status == "skipped_legacy"
                status.files_provisional += result.status == "provisional_cmdr"
            log.info("journal %s: %s, +%d events, %d dropped", jf.path.name, result.status,
                     result.added, sum(result.dropped.values()))
            notify()

        with self.conn:
            select_latest_commander(self.conn)
        status.update(state="done", current_file=None, finished_at=utc_now())
        notify()


def select_latest_commander(conn: sqlite3.Connection) -> None:
    """Auto-select the commander from the most recent `LoadGame` (DESIGN §14)."""
    if conn.execute("SELECT 1 FROM commanders WHERE is_selected = 1").fetchone():
        return
    row = conn.execute("SELECT fid FROM events WHERE event = 'LoadGame' ORDER BY ts DESC LIMIT 1").fetchone()
    if row:
        conn.execute("UPDATE commanders SET is_selected = (fid = ?)", (row["fid"],))


def run_backfill(db_path: Path, journal_dir: Path, filter_csv: Path, status: ImportStatus,
                 notify: Callable[[], None] = lambda: None, stop: threading.Event | None = None) -> None:
    from app.core import projectors

    conn = db.connect(db_path)
    try:
        Importer(conn, IngestFilter.from_csv(filter_csv)).run(journal_dir, status, notify, stop)
        if projectors.is_stale(conn):
            projectors.rebuild(conn)
            notify()
    except Exception as exc:  # surfaced in the UI; never crash the app
        log.exception("journal import failed")
        status.update(state="error", error=str(exc), finished_at=utc_now(), current_file=None)
        notify()
    finally:
        conn.close()
