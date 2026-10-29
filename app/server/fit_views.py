"""Saved fits on the Ships tab: each one beside the ship's current fit, slot by slot.

For every module the saved fit needs that isn't in its slot now, the view says
where one is: elsewhere on this ship, in storage (and where), on another ship,
or not owned (buy, at the price the saved Loadout recorded).
"""

from __future__ import annotations

import json
import re
import sqlite3

from app.core import builds, fits
from app.core.normalize import normalize_symbol
from app.core.projectors.modules import is_cosmetic
from app.server.build_views import _ship_label
from app.server.history_views import SLOT_GROUPS
from app.server.queries import display_name

CORE = {"Armour": "Armour", "PowerPlant": "Power plant", "MainEngines": "Thrusters", "FrameShiftDrive": "Frame shift drive",
        "LifeSupport": "Life support", "PowerDistributor": "Power distributor", "Radar": "Sensors", "FuelTank": "Fuel tank"}


def slot_label(slot: str) -> str:
    if slot in CORE:
        return CORE[slot]
    if m := re.fullmatch(r"(Huge|Large|Medium|Small)Hardpoint(\d+)", slot):
        return f"{m[1]} hardpoint {m[2]}"
    if m := re.fullmatch(r"TinyHardpoint(\d+)", slot):
        return f"Utility {m[1]}"
    if m := re.fullmatch(r"Slot(\d+)_Size(\d+)", slot):
        return f"Optional {int(m[1])} (size {m[2]})"
    if m := re.fullmatch(r"Military(\d+)", slot):
        return f"Military {int(m[1])}"
    return re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", slot).capitalize()


def _slot_order(slot: str) -> tuple:
    group = next((n for n, (_, test) in enumerate(SLOT_GROUPS) if test(slot)), len(SLOT_GROUPS))
    return group, slot


def context(conn: sqlite3.Connection, fid: str) -> dict:
    """Lookups shared by every ship on the page."""
    others: dict[int, list[dict]] = {}
    for r in conn.execute("SELECT m.* FROM module_state m JOIN ships s ON s.fid = m.fid AND s.ship_id = m.ship_id "
                          "WHERE m.fid = ? AND s.disposed_ts IS NULL ORDER BY m.ship_id, m.slot", (fid,)):
        others.setdefault(r["ship_id"], []).append(dict(r))
    return {
        "storage": fits.storage(conn, fid), "fitted": others,
        "bp_names": {b: n for b, n in conn.execute("SELECT DISTINCT blueprint, display_name FROM ref_blueprints") if n},
        "exp_names": dict(conn.execute("SELECT effect, display_name FROM ref_experimentals")),
    }


def _eng(ctx: dict, bp: str | None, grade, exp: str | None = None) -> str | None:
    if not bp:
        return None
    text = f"{ctx['bp_names'].get(bp, bp)} G{grade}"
    return f"{text} · {ctx['exp_names'].get(exp, exp)}" if exp else text


def _pick(pool: list[dict], item: str, bp: str | None, grade) -> dict | None:
    """Take the best match for a module from a pool: same engineering first, then any of the item."""
    same = [x for x in pool if x["item"] == item]
    if not same:
        return None
    best = next((x for x in same if x.get("blueprint") == bp and (not bp or x.get("grade") == grade)), None)
    best = best or same[0]
    pool.remove(best)
    return best


def compare(conn: sqlite3.Connection, fid: str, ship_id: int, saved: dict, ctx: dict) -> dict:
    """A saved fit beside the ship's current fit: per slot what's needed and where to find it."""
    now = {r["slot"]: r for r in ctx["fitted"].get(ship_id, [])}
    want = {}
    for m in saved.get("Modules") or []:
        if is_cosmetic(m.get("Slot")):
            continue
        eng = m.get("Engineering") or {}
        bp = normalize_symbol(eng.get("BlueprintName")) or None
        want[m["Slot"]] = {"item": normalize_symbol(m.get("Item")), "blueprint": bp, "grade": eng.get("Level") if bp else None,
                           "experimental": normalize_symbol(eng.get("ExperimentalEffect")) or None, "value": m.get("Value")}
    spare = [r | {"from_slot": s} for s, r in now.items() if not want.get(s) or want[s]["item"] != r["item"]]
    stored = [dict(x) for x in ctx["storage"]["items"]]
    elsewhere = [r | {"ship_id": sid} for sid, rows in ctx["fitted"].items() if sid != ship_id for r in rows]

    rows, moved_out = [], set()
    for slot in sorted(set(want) | set(now), key=_slot_order):
        w, c = want.get(slot), now.get(slot)
        row = {"slot": slot, "label": slot_label(slot),
               "saved": display_name(conn, w["item"], None) if w else None,
               "saved_eng": _eng(ctx, w["blueprint"], w["grade"], w["experimental"]) if w else None,
               "now": display_name(conn, c["item"], None) if c else None,
               "now_eng": _eng(ctx, c["blueprint"], c["grade"], c["experimental"]) if c else None}
        if w and c and w["item"] == c["item"]:
            same = (w["blueprint"], w["grade"], w["experimental"]) == (c["blueprint"], c["grade"], c["experimental"])
            row["status"], row["note"] = ("fitted", None) if same else ("differs", "Engineering differs")
        elif w:
            if src := _pick(spare, w["item"], w["blueprint"], w["grade"]):
                moved_out.add(src["from_slot"])
                row["status"], row["note"] = "move", f"Move from {slot_label(src['from_slot'])}"
            elif src := _pick(stored, w["item"], w["blueprint"], w["grade"]):
                where = src["system"] or "a station"
                row["status"] = "stored"
                row["note"] = f"In transit to {where}" if src["in_transit"] else f"Stored at {where}"
                if w["blueprint"] and (src["blueprint"], src["grade"]) != (w["blueprint"], w["grade"]):
                    row["note"] += f" ({_eng(ctx, src['blueprint'], src['grade']) or 'not engineered'})"
            elif src := _pick(elsewhere, w["item"], w["blueprint"], w["grade"]):
                row["status"], row["note"] = "other", f"On your {_ship_label(conn, fid, src['ship_id'])}"
            else:
                row["status"] = "buy"
                row["note"] = f"Buy · {w['value']:,} cr" if w.get("value") else "Buy"
        else:
            row["status"], row["note"] = "remove", None
        rows.append(row)
    for row in rows:
        if row["status"] == "remove" and row["slot"] in moved_out:
            row["status"], row["note"] = "move", "Moves to another slot"
        elif row["status"] == "remove":
            row["note"] = "Not in this fit"

    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("fitted", "differs", "move", "stored", "other", "buy", "remove")}
    parts = [f"{counts['stored']} in storage" if counts["stored"] else None,
             f"{counts['move']} to move" if counts["move"] else None,
             f"{counts['other']} on other ships" if counts["other"] else None,
             f"{counts['buy']} to buy" if counts["buy"] else None,
             f"{counts['differs']} engineered differently" if counts["differs"] else None,
             f"{counts['remove']} to take out" if counts["remove"] else None]
    parts = [p for p in parts if p]
    return {"rows": rows, "counts": counts, "matches": not parts,
            "summary": "Matches the current fit" if not parts else "To refit: " + ", ".join(parts)}


def ship_fits(conn: sqlite3.Connection, fid: str, ship_id: int, ctx: dict, version: str) -> list[dict]:
    out = []
    for r in fits.for_ship(conn, fid, ship_id):
        lo = json.loads(r["loadout_json"])
        out.append({"id": r["id"], "name": r["name"], "saved": r["created_ts"],
                    "share": builds.share(lo, version), **compare(conn, fid, ship_id, lo, ctx)})
    return out
