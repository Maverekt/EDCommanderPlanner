"""Shared writer for projectors (DESIGN §8): snapshots keep the last value per day."""

from __future__ import annotations

import json
import sqlite3


def _json(detail: dict | None) -> str | None:
    return json.dumps(detail, sort_keys=True) if detail else None


class Writer:
    def __init__(self, conn: sqlite3.Connection, fid: str):
        self.conn = conn
        self.fid = fid
        self.event_id: int | None = None
        self.ts: str = ""
        self.snapshots = 0
        self.unlocks = 0

    def at(self, event_id: int, ts: str) -> None:
        self.event_id = event_id
        self.ts = ts

    def snapshot(self, metric: str, key: str, value: float) -> None:
        # Events arrive in timestamp order, so replacing keeps the day's last value.
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots (fid, date, metric, key, value, source_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.fid, self.ts[:10], metric, key, float(value), self.event_id),
        )
        self.snapshots += 1

    def unlock(self, kind: str, key: str, **detail) -> None:
        self.conn.execute(
            "INSERT INTO unlocks (fid, ts, kind, key, detail_json, source_event_id) VALUES (?, ?, ?, ?, ?, ?)",
            (self.fid, self.ts, kind, key, _json(detail), self.event_id),
        )
        self.unlocks += 1

    def ledger(self, currency: str, category: str, item: str | None, amount: int, detail: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO ledger (fid, ts, currency, category, item, amount_delta, detail_json, source_event_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self.fid, self.ts, currency, category, item, amount, _json(detail), self.event_id),
        )

    def ship(self, ship_id: int, ship_type: str | None, *, name: str | None = None, ident: str | None = None,
             acquired: bool = False, disposed_how: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO ships (fid, ship_id, ship_type, name, ident, acquired_ts, disposed_ts, disposed_how)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fid, ship_id) DO UPDATE SET
              ship_type = COALESCE(excluded.ship_type, ships.ship_type),
              name = COALESCE(excluded.name, ships.name),
              ident = COALESCE(excluded.ident, ships.ident),
              acquired_ts = COALESCE(ships.acquired_ts, excluded.acquired_ts),
              disposed_ts = COALESCE(excluded.disposed_ts, ships.disposed_ts),
              disposed_how = COALESCE(excluded.disposed_how, ships.disposed_how)
            """,
            (self.fid, ship_id, ship_type or "unknown", name, ident, self.ts if acquired else None,
             self.ts if disposed_how else None, disposed_how),
        )

    def replace_fit(self, ship_id: int, rows: list[dict]) -> None:
        self.conn.execute("DELETE FROM module_state WHERE fid = ? AND ship_id = ?", (self.fid, ship_id))
        self.conn.executemany(
            "INSERT INTO module_state (fid, ship_id, slot, item, blueprint, blueprint_id, grade, quality, "
            "experimental, engineer_id, modifiers_json, as_of_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(self.fid, ship_id, r["slot"], r["item"], r.get("blueprint"), r.get("blueprint_id"), r.get("grade"),
              r.get("quality"), r.get("experimental"), r.get("engineer_id"),
              json.dumps(r["modifiers"]) if r.get("modifiers") else None, self.ts) for r in rows],
        )

    def module_event(self, change: str, item: str | None, *, ship_id: int | None = None, slot: str | None = None,
                     blueprint: str | None = None, grade_from: int | None = None, grade_to: int | None = None,
                     quality: float | None = None, experimental: str | None = None, engineer_id: int | None = None,
                     credits_delta: int | None = None, detail: dict | None = None) -> None:
        if not item:
            return
        self.conn.execute(
            "INSERT INTO module_history (fid, ts, ship_id, slot, item, change, blueprint, grade_from, grade_to, "
            "experimental, engineer_id, credits_delta, merc_coins_delta, source_event_id, quality, detail_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
            (self.fid, self.ts, ship_id, slot, item, change, blueprint, grade_from, grade_to, experimental,
             engineer_id, credits_delta, self.event_id, quality, _json(detail)),
        )
