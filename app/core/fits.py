"""Saved fits: named copies of an owned ship's fit, and the modules in storage.

A saved fit is the ship's latest journal `Loadout`, kept under a name, so the
commander can refit in game and still see (and share) the old fit. Saved fits
are never edited; a new one is saved instead.

Storage comes from the newest `StoredModules` (written whenever outfitting is
opened), brought forward by the store, retrieve, transfer and sell events after it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.core.builds import ship_loadout
from app.core.normalize import normalize_symbol
from app.core.projectors.modules import is_cosmetic


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def signature(loadout: dict) -> tuple:
    """What makes two fits the same: each slot's module and engineering, not health or value."""
    out = []
    for m in loadout.get("Modules") or []:
        if is_cosmetic(m.get("Slot")):
            continue
        eng = m.get("Engineering") or {}
        out.append((m.get("Slot"), normalize_symbol(m.get("Item")), normalize_symbol(eng.get("BlueprintName")),
                    eng.get("Level"), normalize_symbol(eng.get("ExperimentalEffect"))))
    return tuple(sorted(out, key=lambda x: (x[0] or "")))


def for_ship(conn: sqlite3.Connection, fid: str, ship_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM ship_fits WHERE fid = ? AND ship_id = ? ORDER BY id", (fid, ship_id)).fetchall()


def save(conn: sqlite3.Connection, fid: str, ship_id: int, name: str | None = None) -> int:
    """Save the ship's fit as it is now. A fit already saved under another name isn't saved twice."""
    return add(conn, fid, ship_id, ship_loadout(conn, fid, ship_id), name)


def add(conn: sqlite3.Connection, fid: str, ship_id: int, lo: dict, name: str | None = None,
        unique_name: bool = False) -> int:
    """Keep a Loadout as one of the ship's fits: its current one, or a build imported for it. With
    `unique_name`, a name already taken gets a number instead of being refused."""
    have = for_ship(conn, fid, ship_id)
    sig = signature(lo)
    same = next((r for r in have if signature(json.loads(r["loadout_json"])) == sig), None)
    if same:
        raise ValueError(f"this fit is already saved as “{same['name']}”")
    names = {r["name"].casefold() for r in have}
    name = (name or "").strip()[:40]
    if not name:
        n = 1
        while f"fit {n}" in names:
            n += 1
        name = f"Fit {n}"
    elif name.casefold() in names:
        if not unique_name:
            raise ValueError(f"this ship already has a fit called “{name}”")
        n = 2
        while f"{name[:36]} ({n})".casefold() in names:
            n += 1
        name = f"{name[:36]} ({n})"
    with conn:
        cur = conn.execute("INSERT INTO ship_fits (fid, ship_id, name, loadout_json, created_ts) VALUES (?, ?, ?, ?, ?)",
                           (fid, ship_id, name, json.dumps(lo), _now()))
    return cur.lastrowid


def get(conn: sqlite3.Connection, fid: str, fit_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM ship_fits WHERE id = ? AND fid = ?", (fit_id, fid)).fetchone()
    if not row:
        raise LookupError("no such saved fit")
    return row


def rename(conn: sqlite3.Connection, fid: str, fit_id: int, name: str) -> None:
    row = get(conn, fid, fit_id)
    name = (name or "").strip()[:40]
    if not name:
        raise ValueError("give the fit a name")
    if any(r["name"].casefold() == name.casefold() and r["id"] != fit_id for r in for_ship(conn, fid, row["ship_id"])):
        raise ValueError(f"this ship already has a fit called “{name}”")
    with conn:
        conn.execute("UPDATE ship_fits SET name = ? WHERE id = ?", (name, fit_id))


def delete(conn: sqlite3.Connection, fid: str, fit_id: int) -> None:
    get(conn, fid, fit_id)
    with conn:
        conn.execute("DELETE FROM ship_fits WHERE id = ?", (fit_id,))


# -- storage -------------------------------------------------------------------------------

def _stored(item, blueprint=None, grade=None, quality=None, system=None, slot=None, in_transit=False, hot=False) -> dict:
    return {"item": normalize_symbol(item), "blueprint": normalize_symbol(blueprint) or None,
            "grade": grade if blueprint else None, "quality": quality, "system": system, "storage_slot": slot,
            "in_transit": bool(in_transit), "hot": bool(hot)}


def _system_at(conn: sqlite3.Connection, fid: str, ts: str) -> str | None:
    row = conn.execute("SELECT json_extract(raw_json, '$.StarSystem') FROM events WHERE fid = ? AND ts <= ? "
                       "AND event IN ('Docked', 'Location') ORDER BY ts DESC, id DESC LIMIT 1", (fid, ts)).fetchone()
    return row[0] if row else None


def storage(conn: sqlite3.Connection, fid: str) -> dict:
    """Modules in storage now: {items: [...], as_of: ts of the StoredModules list, or None}."""
    snap = conn.execute("SELECT ts, id, raw_json FROM events WHERE fid = ? AND event = 'StoredModules' "
                        "ORDER BY ts DESC, id DESC LIMIT 1", (fid,)).fetchone()
    items: list[dict] = []
    since = ("", 0)
    if snap:
        since = (snap[0], snap[1])
        for i in json.loads(snap[2]).get("Items") or []:
            items.append(_stored(i.get("Name"), i.get("EngineerModifications"), i.get("Level"), i.get("Quality"),
                                 i.get("StarSystem"), i.get("StorageSlot"), i.get("InTransit"), i.get("Hot")))

    def take(item: str, blueprint=None, slot=None) -> None:
        for n, s in enumerate(items):
            if (slot is not None and s["storage_slot"] == slot) or (
                    slot is None and s["item"] == item and (blueprint is None or s["blueprint"] == blueprint)):
                del items[n]
                return

    for ts, raw in conn.execute(
            "SELECT ts, raw_json FROM events WHERE fid = ? AND (ts > ? OR (ts = ? AND id > ?)) AND event IN "
            "('ModuleStore', 'MassModuleStore', 'ModuleBuy', 'ModuleRetrieve', 'ModuleSellRemote', 'FetchRemoteModule') "
            "ORDER BY ts, id", (fid, since[0], since[0], since[1])):
        e = json.loads(raw)
        name = e.get("event")
        if name == "ModuleStore" or (name == "ModuleBuy" and e.get("StoredItem")):
            items.append(_stored(e.get("StoredItem"), e.get("EngineerModifications"), e.get("Level"), e.get("Quality"),
                                 _system_at(conn, fid, ts), hot=e.get("Hot")))
        elif name == "MassModuleStore":
            system = _system_at(conn, fid, ts)
            for i in e.get("Items") or []:
                items.append(_stored(i.get("Name"), i.get("EngineerModifications"), i.get("Level"), i.get("Quality"),
                                     system, hot=i.get("Hot")))
        elif name == "ModuleRetrieve":
            take(normalize_symbol(e.get("RetrievedItem")), normalize_symbol(e.get("EngineerModifications")) or None)
            if e.get("SwapOutItem"):
                items.append(_stored(e.get("SwapOutItem"), system=_system_at(conn, fid, ts)))
        elif name == "ModuleSellRemote":
            take(normalize_symbol(e.get("SellItem")), slot=e.get("StorageSlot"))
        elif name == "FetchRemoteModule":     # on its way here; there once the transfer time has passed
            arrives = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() + (e.get("TransferTime") or 0)
            for s in items:
                if s["storage_slot"] == e.get("StorageSlot"):
                    s["in_transit"], s["system"] = arrives > datetime.now(timezone.utc).timestamp(), _system_at(conn, fid, ts)
    return {"items": items, "as_of": snap[0] if snap else None}
