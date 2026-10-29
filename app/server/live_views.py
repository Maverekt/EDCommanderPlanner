"""View models for live values (DESIGN §10), game state, and the Materials tab."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache

from app.core import paths
from app.core.normalize import normalize_symbol
from app.core.projectors.materials import DELTA_EVENTS, MATERIAL_CAPS
from app.server.queries import display_name
from app.server.views import _latest

SHIP_EVENTS = ("LoadGame", "Loadout", "ShipyardSwap", "ShipyardNew")
MATERIAL_TYPES = ("Raw", "Manufactured", "Encoded")


def _event(conn: sqlite3.Connection, fid: str, names: tuple[str, ...]) -> tuple[str, str, dict] | None:
    placeholders = ",".join("?" * len(names))
    row = conn.execute(
        f"SELECT ts, event, raw_json FROM events WHERE fid = ? AND event IN ({placeholders}) "
        "ORDER BY ts DESC, id DESC LIMIT 1",
        (fid, *names),
    ).fetchone()
    return (row[0], row[1], json.loads(row[2])) if row else None


def game_state(conn: sqlite3.Connection, fid: str | None, status: dict | None) -> dict:
    """In game / main menu / offline, from the last journal event and Status.json flags."""
    last = conn.execute(
        "SELECT ts, event FROM events WHERE fid = ? ORDER BY ts DESC, id DESC LIMIT 1", (fid,)
    ).fetchone() if fid else None
    if last is None:
        return {"state": "unknown", "label": "No journal yet", "since": None}
    if last["event"] == "Shutdown":
        return {"state": "offline", "label": "Offline", "since": last["ts"]}
    if status is not None and ((status.get("Flags") or 0) or (status.get("Flags2") or 0)):
        return {"state": "ingame", "label": "In game", "since": status.get("timestamp")}
    if status is not None:
        return {"state": "menu", "label": "Main menu", "since": status.get("timestamp")}
    return {"state": "unknown", "label": "Game state unknown", "since": last["ts"]}


def _ship(conn: sqlite3.Connection, fid: str, login_ts: str) -> dict:
    ts, name, e = _event(conn, fid, SHIP_EVENTS)
    if name in ("ShipyardSwap", "ShipyardNew"):
        symbol, localised, ship_name, ident = e.get("ShipType"), e.get("ShipType_Localised"), None, None
    else:  # LoadGame and Loadout carry the ship's own name and ident
        symbol, localised = e.get("Ship"), e.get("Ship_Localised")
        ship_name = (e.get("ShipName") or "").strip() or None
        ident = (e.get("ShipIdent") or "").strip() or None
    return {"name": display_name(conn, symbol, localised), "ship_name": ship_name, "ident": ident,
            "ts": ts, "live": ts > login_ts}


def live_view(conn: sqlite3.Connection, fid: str, status: dict | None) -> dict | None:
    """Ship, balance, wealth and Merc Coins, each marked live or as of login."""
    login = _event(conn, fid, ("LoadGame",))
    if login is None:
        return None
    login_ts, _, login_ev = login
    login_credits = login_ev.get("Credits")

    status_ts = (status or {}).get("timestamp") or ""
    if status and isinstance(status.get("Balance"), (int, float)) and status_ts > login_ts:
        balance = {"value": status["Balance"], "live": True, "ts": status_ts}
    else:
        # Fallback until Status.json carries Balance (DESIGN §10, §16).
        balance = {"value": login_credits, "live": False, "ts": login_ts}
    session_delta = balance["value"] - login_credits if balance["live"] and login_credits is not None else None

    wealth_row = _latest(conn, fid, "wealth").get("total")
    wealth = None
    if wealth_row:
        wealth = {"value": wealth_row["value"] + (session_delta or 0), "live": balance["live"], "ts": wealth_row["ts"]}
    merc = _latest(conn, fid, "merc_coins").get("balance")

    return {
        "login_ts": login_ts,
        "game_mode": login_ev.get("GameMode"),
        "gameversion": login_ev.get("gameversion"),
        "ship": _ship(conn, fid, login_ts),
        "balance": balance,
        "session_delta": session_delta,
        "wealth": wealth,
        "merc_coins": {"value": merc["value"], "ts": merc["ts"]} if merc else None,
    }


# -- Materials -------------------------------------------------------------------

def _group_order(name: str):
    if name.startswith("Column "):
        return (0, int(name.split()[-1]), "")
    return (2 if name in ("Guardian", "Thargoid") else 1, 0, name)


def _deltas(name: str, e: dict) -> list[tuple[str | None, int]]:
    """(material name as the journal writes it, change) for one delta event."""
    if name == "MaterialCollected":
        return [(e.get("Name"), e.get("Count", 0))]
    if name == "MaterialDiscarded":
        return [(e.get("Name"), -e.get("Count", 0))]
    if name == "MaterialTrade":
        p, r = e.get("Paid") or {}, e.get("Received") or {}
        return [(p.get("Material"), -p.get("Quantity", 0)), (r.get("Material"), r.get("Quantity", 0))]
    if name == "EngineerCraft":
        return [(i.get("Name"), -i.get("Count", 0)) for i in e.get("Ingredients") or []]
    if name in ("Synthesis", "TechnologyBroker"):
        return [(i.get("Name"), -i.get("Count", 0)) for i in e.get("Materials") or []]
    if name == "MissionCompleted":
        return [(i.get("Name"), i.get("Count", 0)) for i in e.get("MaterialsReward") or []]
    return []


def _changes(conn: sqlite3.Connection, name: str, e: dict) -> list[tuple[str, int]]:
    return [(display_name(conn, symbol, None) or symbol or "?", n) for symbol, n in _deltas(name, e)]


def _local_midnight_utc(now: datetime | None = None) -> str:
    """Today's local midnight as a journal timestamp (UTC, `Z`)."""
    local = (now or datetime.now()).astimezone()
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


LOGIN_EVENTS = ("Commander", "LoadGame")   # the first events of a fresh login


def today_start(conn: sqlite3.Connection, fid: str, now: datetime | None = None) -> str:
    """Start of "today": local midnight, or the login of a session still being played at
    midnight, so an evening session that runs past 00:00 isn't split in two."""
    midnight = _local_midnight_utc(now)
    before = conn.execute("SELECT event FROM events WHERE fid = ? AND ts < ? ORDER BY ts DESC, id DESC LIMIT 1",
                          (fid, midnight)).fetchone()
    after = conn.execute("SELECT event FROM events WHERE fid = ? AND ts >= ? ORDER BY ts, id LIMIT 1",
                         (fid, midnight)).fetchone()
    if not before or not after or before[0] == "Shutdown" or after[0] in LOGIN_EVENTS:
        return midnight
    login = conn.execute("SELECT MAX(ts) FROM events WHERE fid = ? AND event = 'LoadGame' AND ts < ?",
                         (fid, midnight)).fetchone()[0]
    return login or midnight


def gained_since(conn: sqlite3.Connection, fid: str, since_ts: str) -> dict[str, int]:
    """Units gained per material since a timestamp: pickups, trade receipts and mission rewards.
    Spending isn't netted off; this is what was earned, not the change in the count."""
    placeholders = ",".join("?" * len(DELTA_EVENTS))
    gained: dict[str, int] = {}
    for name, raw in conn.execute(
        f"SELECT event, raw_json FROM events WHERE fid = ? AND event IN ({placeholders}) AND ts >= ?",
        (fid, *DELTA_EVENTS, since_ts),
    ):
        for material, n in _deltas(name, json.loads(raw)):
            symbol = normalize_symbol(material)
            if symbol and isinstance(n, int) and n > 0:
                gained[symbol] = gained.get(symbol, 0) + n
    return gained


@lru_cache(maxsize=1)
def material_sources() -> list[dict]:
    with (paths.refdata_dir() / "material_sources.csv").open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _targets(row: dict) -> list[str]:
    """A row's targets: `;`-separated symbols, `type:<Raw|Manufactured|Encoded>` or `family:<name>`."""
    return [t.strip() for t in row["target"].split(";") if t.strip()]


def sources_for(symbol: str, mtype: str | None, family: str | None) -> list[str]:
    """Notes for one material: its own rows first (in file order), then family, then type."""
    specific, by_family, by_type = [], [], []
    for row in material_sources():
        for target in _targets(row):
            kind, _, value = target.partition(":")
            if target == symbol:
                specific.append(row["where"].strip())
            elif kind == "family" and value == family:
                by_family.append(row["where"].strip())
            elif kind == "type" and value == mtype:
                by_type.append(row["where"].strip())
            else:
                continue
            break
    return specific + by_family + by_type


def has_specific_source(symbol: str) -> bool:
    return any(symbol in _targets(row) for row in material_sources())


def materials_view(conn: sqlite3.Connection, fid: str, now: datetime | None = None) -> dict:
    counts = _latest(conn, fid, "material")
    login = _event(conn, fid, ("Materials",))
    login_ts = login[0] if login else None
    since = today_start(conn, fid, now)
    today = gained_since(conn, fid, since)
    groups: dict[str, dict[str, list[dict]]] = {t: {} for t in MATERIAL_TYPES}
    known = set()
    for symbol, name, family, grade, attrs in conn.execute(
        "SELECT symbol, display_name, family, grade, attrs_json FROM ref_items WHERE kind = 'material'"
    ):
        known.add(symbol)
        a = json.loads(attrs or "{}")
        mtype = a.get("type", "Raw")
        group = f"Column {a['trader_column']}" if mtype == "Raw" and a.get("trader_column") else (family or "Other")
        c = counts.get(symbol)
        cap = MATERIAL_CAPS.get(grade or 0)
        value = int(c["value"]) if c else 0
        groups.setdefault(mtype, {}).setdefault(group, []).append({
            "symbol": symbol, "name": name, "grade": grade, "count": value, "cap": cap,
            "pct": min(100, round(100 * value / cap)) if cap else 0,
            "full": bool(cap and value >= cap), "near": bool(cap and value >= 0.9 * cap),
            "live": bool(c and login_ts and c["ts"] > login_ts),
            "today": today.get(symbol, 0),
            "sources": sources_for(symbol, mtype, family),
            "specific": has_specific_source(symbol),
        })
    for families in groups.values():
        for rows in families.values():
            rows.sort(key=lambda r: (r["grade"] or 0, r["name"]))

    return {
        "login_ts": login_ts,
        "groups": {t: sorted(f.items(), key=lambda kv: _group_order(kv[0])) for t, f in groups.items()},
        "unknown": [{"symbol": s, "count": int(c["value"])} for s, c in counts.items() if s not in known and c["value"]],
        "live_count": sum(1 for f in groups.values() for rows in f.values() for r in rows if r["live"]),
        "today": {t: sum(r["today"] for rows in f.values() for r in rows) for t, f in groups.items()},
        "today_since": since,
    }
