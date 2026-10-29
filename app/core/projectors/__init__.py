"""Projectors: stored events → derived tables (DESIGN §9).

Derived tables are disposable (§8): `rebuild` wipes a commander's derived rows
and replays every relevant event in timestamp order. `LiveProjection` keeps
the replayed state in memory so the live tail (§5.8) can apply new events
through the same handlers without a full rebuild.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading

from app.core import db
from app.core.projectors import engineers, ledger, materials, modules, progress, ships, statistics
from app.core.projectors.base import Writer

log = logging.getLogger(__name__)

PROJECTORS = (progress, engineers, statistics, materials, ledger, ships, modules)
EVENTS = sorted({e for p in PROJECTORS for e in p.EVENTS})
SETTING_KEY = "derived_built_through"
# Bump whenever projector logic changes, so existing databases replay once.
PROJECTOR_VERSION = 4
DERIVED_TABLES = ("snapshots", "unlocks", "ledger", "ships", "module_state", "module_history")

States = dict  # projector module -> its State


def _max_event_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


def _marker(conn: sqlite3.Connection) -> str:
    return f"v{PROJECTOR_VERSION}:{_max_event_id(conn)}"


def _built_through(conn: sqlite3.Connection) -> int | None:
    """Last projected event id, or None if never built or built by other projector logic."""
    marker = db.get_setting(conn, SETTING_KEY) or ""
    version, _, top = marker.partition(":")
    return int(top) if version == f"v{PROJECTOR_VERSION}" and top.isdigit() else None


def _replay(conn: sqlite3.Connection, fid: str, counts: dict[str, int]) -> States:
    for table in DERIVED_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE fid = ?", (fid,))
    writer = Writer(conn, fid)
    states = {p: p.State() for p in PROJECTORS}
    placeholders = ",".join("?" * len(EVENTS))
    rows = conn.execute(
        f"SELECT id, ts, event, raw_json FROM events WHERE fid = ? AND event IN ({placeholders}) ORDER BY ts, id",
        (fid, *EVENTS),
    )
    for event_id, ts, name, raw in rows:
        writer.at(event_id, ts)
        event = json.loads(raw)
        for p in PROJECTORS:
            if name in p.EVENTS:
                p.handle(states[p], writer, name, event)
        counts["events"] += 1
    counts["snapshots"] += writer.snapshots
    counts["unlocks"] += writer.unlocks
    return states


def rebuild_with_states(conn: sqlite3.Connection, fid: str | None = None) -> tuple[dict[str, int], dict[str, States]]:
    fids = [fid] if fid else [r[0] for r in conn.execute("SELECT fid FROM commanders")]
    counts = {"events": 0, "snapshots": 0, "unlocks": 0}
    states: dict[str, States] = {}
    with conn:
        for f in fids:
            states[f] = _replay(conn, f, counts)
        db.set_setting(conn, SETTING_KEY, _marker(conn))
    log.info("derived data rebuilt: %s", counts)
    return counts, states


def rebuild(conn: sqlite3.Connection, fid: str | None = None) -> dict[str, int]:
    """Replay events into snapshots and unlocks. Returns rows written per table."""
    return rebuild_with_states(conn, fid)[0]


def is_stale(conn: sqlite3.Connection) -> bool:
    return db.get_setting(conn, SETTING_KEY) != _marker(conn)


class LiveProjection:
    """Projector state per commander, kept in memory between live ticks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, States] = {}

    def invalidate(self) -> None:
        """Drop cached state after a rebuild elsewhere; the next apply replays from scratch."""
        with self._lock:
            self._states.clear()

    def rebuild(self, conn: sqlite3.Connection, fid: str | None = None) -> dict[str, int]:
        with self._lock:
            counts, states = rebuild_with_states(conn, fid)
            if fid is None:
                self._states = states
            else:
                self._states.update(states)
            return counts

    def apply(self, conn: sqlite3.Connection, events: list[tuple[int, str, str, str, dict]]) -> set[str]:
        """Apply newly stored `(id, fid, ts, name, event)` rows; returns the event names projected."""
        relevant = [e for e in events if e[3] in EVENTS]
        if not relevant:
            return set()
        with self._lock:
            stale = is_stale_before(conn, min(e[0] for e in relevant))
            missing = {e[1] for e in relevant if e[1] not in self._states}
            if stale or missing:
                # Something landed that we never projected (a rescan, a gap): replay, which
                # already includes these events.
                counts, states = rebuild_with_states(conn)
                self._states = states
                return {e[3] for e in relevant}
            with conn:
                writers: dict[str, Writer] = {}
                for event_id, fid, ts, name, event in sorted(relevant, key=lambda e: (e[2], e[0])):
                    w = writers.setdefault(fid, Writer(conn, fid))
                    w.at(event_id, ts)
                    for p in PROJECTORS:
                        if name in p.EVENTS:
                            p.handle(self._states[fid][p], w, name, event)
                db.set_setting(conn, SETTING_KEY, _marker(conn))
            return {e[3] for e in relevant}


def is_stale_before(conn: sqlite3.Connection, first_new_id: int) -> bool:
    """True if events older than `first_new_id` were stored but never projected."""
    built = _built_through(conn)
    if built is None:
        return True
    unprojected = conn.execute(
        f"SELECT 1 FROM events WHERE id > ? AND id < ? AND event IN ({','.join('?' * len(EVENTS))}) LIMIT 1",
        (built, first_new_id, *EVENTS),
    ).fetchone()
    return unprojected is not None
