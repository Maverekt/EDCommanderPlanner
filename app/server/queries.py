"""Read-side queries for the pages. Every commander-scoped query takes `fid`."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from app.core.backup.restore import summary as backup_summary
from app.core.normalize import display_text, normalize_symbol
from app.core.refdata.loader import display_name as ref_display_name


def display_name(conn: sqlite3.Connection, symbol: str | None, localised: str | None = None) -> str | None:
    """ref_items name, then the journal's localised name, then the raw symbol (DESIGN §6)."""
    normalized = normalize_symbol(symbol)
    name = ref_display_name(conn, normalized)
    if name:
        return name
    if localised and not localised.strip().startswith("$"):
        return localised.strip()
    return normalized


def commander_key(fid: str) -> str:
    """Opaque handle for a commander in page markup, so the full FID never reaches the UI."""
    return hashlib.sha256(f"edplanner-cmdr\n{fid}".encode("utf-8")).hexdigest()[:16]


def commanders(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.fid, c.name, c.first_seen, c.last_seen, c.is_selected,
               (SELECT COUNT(*) FROM events e WHERE e.fid = c.fid) AS event_count
        FROM commanders c ORDER BY c.last_seen DESC
        """
    ).fetchall()
    return [dict(r) | {"key": commander_key(r["fid"])} for r in rows]


def selected_fid(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT fid FROM commanders WHERE is_selected = 1").fetchone()
    if row:
        return row["fid"]
    row = conn.execute("SELECT fid FROM commanders ORDER BY last_seen DESC LIMIT 1").fetchone()
    return row["fid"] if row else None


def select_commander(conn: sqlite3.Connection, key: str) -> bool:
    fid = next((r["fid"] for r in conn.execute("SELECT fid FROM commanders") if commander_key(r["fid"]) == key), None)
    if fid is None:
        return False
    with conn:
        conn.execute("UPDATE commanders SET is_selected = (fid = ?)", (fid,))
    return True


def last_login(conn: sqlite3.Connection, fid: str) -> dict | None:
    """The latest `LoadGame`, read straight from the stored event until projectors exist."""
    row = conn.execute(
        "SELECT ts, raw_json FROM events WHERE fid = ? AND event = 'LoadGame' ORDER BY ts DESC LIMIT 1",
        (fid,),
    ).fetchone()
    if not row:
        return None
    e = json.loads(row["raw_json"])
    return {
        "ts": row["ts"],
        "ship": display_name(conn, e.get("Ship"), e.get("Ship_Localised")),
        "ship_name": display_text(e.get("ShipName")),
        "ship_ident": display_text(e.get("ShipIdent")),
        "credits": e.get("Credits"),
        "loan": e.get("Loan"),
        "game_mode": e.get("GameMode"),
        "gameversion": e.get("gameversion"),
        "fuel_level": e.get("FuelLevel"),
        "fuel_capacity": e.get("FuelCapacity"),
    }


def event_totals(conn: sqlite3.Connection, fid: str) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS events, COUNT(DISTINCT event) AS types,
               MIN(ts) AS first_ts, MAX(ts) AS last_ts,
               SUM(fid_status = 'provisional') AS provisional
        FROM events WHERE fid = ?
        """,
        (fid,),
    ).fetchone()
    return dict(row)


def activity_by_day(conn: sqlite3.Connection, fid: str, days: int = 21) -> list[dict]:
    """Stored events per UTC day for the last `days` days ending at the latest event."""
    last = conn.execute("SELECT MAX(ts) FROM events WHERE fid = ?", (fid,)).fetchone()[0]
    end = date.fromisoformat(last[:10]) if last else datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    counts = {
        r["day"]: r["n"]
        for r in conn.execute(
            "SELECT substr(ts, 1, 10) AS day, COUNT(*) AS n FROM events "
            "WHERE fid = ? AND ts >= ? GROUP BY day",
            (fid, start.isoformat()),
        )
    }
    peak = max(counts.values(), default=0)
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        n = counts.get(d.isoformat(), 0)
        out.append({"day": d.isoformat(), "n": n, "pct": round(100 * n / peak) if peak else 0})
    return out


def event_types(conn: sqlite3.Connection, fid: str | None, limit: int | None = None) -> list[dict]:
    sql = "SELECT event, COUNT(*) AS n FROM events"
    params: tuple = ()
    if fid:
        sql += " WHERE fid = ?"
        params = (fid,)
    sql += " GROUP BY event ORDER BY n DESC, event"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, params)]


def import_log(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM import_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    names = _names(conn)
    out = []
    for r in rows:
        d = dict(r)
        dropped = json.loads(d.get("events_dropped_json") or "{}")
        d["dropped_total"] = sum(dropped.values())
        if d["kind"] == "edplanner":           # its detail holds FIDs for a rollback; show the summary
            d["detail"] = backup_summary(d["detail"], names)
        out.append(d)
    return out


def _names(conn: sqlite3.Connection) -> dict[str, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT fid, name FROM commanders")}


def edplanner_imports(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    names = _names(conn)
    return [dict(r) | {"summary": backup_summary(r["detail"], names)} for r in conn.execute(
        "SELECT id, ref, at, status, events_added, detail FROM import_log WHERE kind = 'edplanner' "
        "ORDER BY id DESC LIMIT ?", (limit,))]


def dropped_by_type(conn: sqlite3.Connection) -> list[dict]:
    """Dropped-event counts per type, from the latest log row of each file (re-reads overlap)."""
    totals: Counter = Counter()
    rows = conn.execute(
        """
        SELECT events_dropped_json FROM import_log l
        WHERE kind = 'journal_file' AND id = (
          SELECT MAX(id) FROM import_log m WHERE m.kind = 'journal_file' AND m.ref = l.ref
        )
        """
    )
    for r in rows:
        totals.update(json.loads(r["events_dropped_json"] or "{}"))
    return [{"event": k, "n": v} for k, v in totals.most_common()]


def provisional(conn: sqlite3.Connection) -> dict:
    files = [
        dict(r)
        for r in conn.execute(
            "SELECT ref, detail, at FROM import_log WHERE status = 'provisional_cmdr' ORDER BY id DESC"
        )
    ]
    events = conn.execute("SELECT COUNT(*) FROM events WHERE fid_status = 'provisional'").fetchone()[0]
    return {"files": files, "events": events}


def last_completed_import(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT MAX(at) FROM import_log").fetchone()[0]


def unknown_symbol_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM unknown_symbols WHERE status = 'new'").fetchone()[0]
