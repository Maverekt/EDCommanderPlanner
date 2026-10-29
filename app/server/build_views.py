"""Builds vs real fits (DESIGN §16 Phase 5d): credits to buy, engineering left, engineers, pins and materials."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from functools import lru_cache

from app.core import builds, engplan, paths
from app.core.normalize import normalize_symbol
from app.core.projectors.modules import is_cosmetic
from app.server.engineer_views import chain_view, pin_label
from app.server.engplan_views import rolls
from app.server.queries import display_name
from app.server.views import _latest

EXAMPLE_KEY = "type11_mining"


@lru_cache(maxsize=1)
def example_text() -> str:
    return (paths.refdata_dir() / "templates" / f"{EXAMPLE_KEY}.slef.json").read_text(encoding="utf-8")


def _module_types(conn: sqlite3.Connection) -> dict[str, str]:
    return {s: json.loads(a or "{}").get("module_type") for s, a in
            conn.execute("SELECT symbol, attrs_json FROM ref_items WHERE kind = 'module'")}


def _ship_label(conn: sqlite3.Connection, fid: str, ship_id: int | None) -> str | None:
    if ship_id is None:
        return None
    r = conn.execute("SELECT ship_type, name, ident FROM ships WHERE fid = ? AND ship_id = ?", (fid, ship_id)).fetchone()
    if not r:
        return None
    return " · ".join(x for x in (display_name(conn, r["ship_type"], None), f"“{r['name']}”" if r["name"] else None,
                                  r["ident"]) if x)


def compare_ship(conn: sqlite3.Connection, fid: str, row: sqlite3.Row) -> int | None:
    """The owned ship a build is compared with: the one picked, while it's still owned; otherwise any of its type."""
    if row["ship_id"] and conn.execute("SELECT 1 FROM ships WHERE fid = ? AND ship_id = ? AND ship_type = ? "
                                       "AND disposed_ts IS NULL", (fid, row["ship_id"], row["ship_type"])).fetchone():
        return row["ship_id"]
    return builds.default_ship(conn, fid, row["ship_type"])


def build_view(conn: sqlite3.Connection, fid: str, row: sqlite3.Row, ctx: dict | None = None) -> dict:
    """Compare one build with its owned ship. `ctx` caches shared lookups across builds."""
    ctx = ctx if ctx is not None else _context(conn, fid)
    lo = json.loads(row["loadout_json"])
    ship_id = compare_ship(conn, fid, row)
    actual = {r["slot"]: dict(r) for r in conn.execute(
        "SELECT * FROM module_state WHERE fid = ? AND ship_id = ?", (fid, ship_id))} if ship_id else {}

    targets = {m.get("Slot"): normalize_symbol(m.get("Item")) for m in lo.get("Modules") or []}
    spare: dict[str, list[dict]] = defaultdict(list)   # fitted modules their own slot doesn't need: can be moved
    for slot, r in sorted(actual.items()):
        if targets.get(slot) != r["item"]:
            spare[r["item"]].append(r)

    modules, needs_by_bp, mats = [], {}, defaultdict(int)
    to_buy = unknown_prices = 0
    for m in lo.get("Modules") or []:
        slot = m.get("Slot")
        if is_cosmetic(slot):
            continue
        item = normalize_symbol(m.get("Item")) or ""
        eng = m.get("Engineering") or {}
        bp = normalize_symbol(eng.get("BlueprintName")) or None
        grade = eng.get("Level") if bp else None
        exp = normalize_symbol(eng.get("ExperimentalEffect")) or None
        have = actual.get(slot)
        item_ok = bool(have) and have["item"] == item
        moved = spare[item].pop(0) if not item_ok and spare.get(item) else None
        src = have if item_ok else moved                  # the module that will sit in this slot, if owned
        done_grade = (src["grade"] or 0) if src and bp and src["blueprint"] == bp else 0
        exp_ok = not exp or (bool(src) and src["experimental"] == exp)
        eng_ok = not bp or (done_grade >= grade and exp_ok)
        status = ("ok" if eng_ok else "engineer") if item_ok else "move" if moved else "buy"
        if status == "buy":
            price = next((x for x in (m.get("Value"), ctx["prices"].get(item), ctx["list_prices"].get(item))
                          if x is not None), None)
            if price is None:
                unknown_prices += 1
            to_buy += price or 0
        mt = ctx["module_types"].get(item)
        if bp and not eng_ok:
            need = engplan.entry_needs(ctx["recipes"], {"blueprint": bp, "modules": 1, "target_grade": grade,
                                                        "done_grade": done_grade, "experimental": None if exp_ok else exp},
                                       ctx["rolls"])
            for mat, q in need.items():
                mats[mat] += q
            key = (mt, bp, grade)
            n = needs_by_bp.setdefault(key, {"module_type": mt, "blueprint": bp, "grade": grade, "slots": []})
            n["slots"].append({"slot": slot, "done_grade": done_grade, "experimental": None if exp_ok else exp})
        modules.append({
            "slot": slot, "item": item, "name": display_name(conn, item, None),
            "blueprint": ctx["bp_names"].get(bp, bp) if bp else None, "grade": grade,
            "experimental": ctx["exp_names"].get(exp, exp) if exp else None,
            "actual": display_name(conn, have["item"], None) if have else None,
            "actual_eng": (f"{ctx['bp_names'].get(have['blueprint'], have['blueprint'])} G{have['grade']}"
                           if have and have["blueprint"] else None),
            "status": status, "value": m.get("Value"), "move_from": moved["slot"] if moved else None,
            "eng_left": bool(bp and not eng_ok),
        })

    blueprints = [_engineer_status(conn, ctx, n) for n in needs_by_bp.values()]
    have_mats = ctx["have"]
    mat_rows = sorted(({"symbol": s, "name": ctx["mat_names"].get(s, s), "need": q, "have": have_mats.get(s, 0),
                        "short": max(0, q - have_mats.get(s, 0))} for s, q in mats.items()),
                      key=lambda x: (-x["short"], x["name"]))
    hull = 0 if ship_id else (lo.get("HullValue") or ctx["hull_prices"].get(row["ship_type"]) or 0)
    if not ship_id and lo.get("ModulesValue"):
        to_buy, unknown_prices = lo["ModulesValue"], 0     # buying every module: the export's own total is exact
    total_value = (lo.get("HullValue") or 0) + (lo.get("ModulesValue") or sum(m.get("Value") or 0 for m in lo["Modules"]))
    goal = conn.execute("SELECT title FROM goals WHERE id = ?", (row["goal_id"],)).fetchone() if row["goal_id"] else None
    return {
        "id": row["id"], "name": row["name"], "ship_type": row["ship_type"],
        "ship_name": display_name(conn, row["ship_type"], None), "source": row["source"],
        "source_url": row["source_url"], "imported": row["created_ts"], "goal_id": row["goal_id"],
        "builder": {"edsy": "EDSY", "coriolis": "Coriolis"}.get(row["source"]), "goal_title": goal[0] if goal else None,
        "ship_id": ship_id, "ship_choice": row["ship_id"], "compare_label": _ship_label(conn, fid, ship_id),
        "total_value": total_value, "hull_cost": hull, "modules_cost": to_buy, "to_buy": hull + to_buy,
        "unknown_prices": unknown_prices, "rebuy": lo.get("Rebuy"),
        "modules": modules, "blueprints": blueprints, "materials": mat_rows,
        "need_units": sum(x["need"] for x in mat_rows), "short_units": sum(x["short"] for x in mat_rows),
        "short_count": sum(1 for x in mat_rows if x["short"]),
        "eng_left": sum(len(b["slots"]) for b in blueprints),
        "engineers_ready": sum(1 for b in blueprints if b["state"] == "ready"),
        "done": all(m["status"] == "ok" for m in modules) and bool(ship_id),
    }


def _context(conn: sqlite3.Connection, fid: str) -> dict:
    chain = {e["engineer_id"]: e for e in chain_view(conn, fid)["all"]}
    return {
        "recipes": engplan.load_recipes(conn), "rolls": rolls(conn), "module_types": _module_types(conn),
        "bp_names": {b: n for b, n in conn.execute("SELECT DISTINCT blueprint, display_name FROM ref_blueprints") if n},
        "exp_names": dict(conn.execute("SELECT effect, display_name FROM ref_experimentals")),
        "mat_names": dict(conn.execute("SELECT symbol, display_name FROM ref_items WHERE kind = 'material'")),
        "have": {s: int(c["value"]) for s, c in _latest(conn, fid, "material").items()},
        "prices": _known_prices(conn, fid),
        **_list_prices(conn),
        "chain": chain,
    }


def _list_prices(conn: sqlite3.Connection) -> dict:
    """List prices from reference data (coriolis-data): module cost and ship hull cost."""
    modules, hulls = {}, {}
    for symbol, kind, attrs in conn.execute("SELECT symbol, kind, attrs_json FROM ref_items WHERE kind IN ('module', 'ship')"):
        a = json.loads(attrs or "{}")
        if kind == "module" and a.get("cost") is not None:
            modules[symbol] = a["cost"]
        elif kind == "ship" and a.get("hull_cost"):
            hulls[symbol] = a["hull_cost"]
    return {"list_prices": modules, "hull_prices": hulls}


def _known_prices(conn: sqlite3.Connection, fid: str) -> dict[str, int]:
    """Module prices from the commander's own journal (Loadout values, then purchases), newest wins."""
    prices: dict[str, int] = {}
    for raw, in conn.execute("SELECT raw_json FROM events WHERE fid = ? AND event = 'Loadout' ORDER BY ts, id", (fid,)):
        for m in json.loads(raw).get("Modules") or []:
            if m.get("Value"):
                prices[normalize_symbol(m.get("Item"))] = int(m["Value"])
    for item, price in conn.execute("SELECT json_extract(raw_json, '$.BuyItem'), json_extract(raw_json, '$.BuyPrice') "
                                    "FROM events WHERE fid = ? AND event = 'ModuleBuy' ORDER BY ts, id", (fid,)):
        if item and price:
            prices[normalize_symbol(item)] = int(price)
    return prices


def _engineer_status(conn: sqlite3.Connection, ctx: dict, n: dict) -> dict:
    """Who can engineer this blueprint to the target grade, and which pin to use for remote rolls."""
    offers = []
    for eid, max_grade in conn.execute("SELECT engineer_id, max_grade FROM ref_engineer_blueprints "
                                       "WHERE module_type = ? AND blueprint = ?", (n["module_type"], n["blueprint"])):
        e = ctx["chain"].get(eid)
        if not e:
            continue
        unlocked = e["state"] == "Unlocked"
        can = min(max_grade, e["grade"] or 0) if unlocked else 0
        pinned = bool(e.get("pin")) and e["pin"]["module_type"] == n["module_type"] and e["pin"]["blueprint"] == n["blueprint"]
        offers.append({"id": eid, "name": e["name"], "max_grade": max_grade, "can": can, "unlocked": unlocked,
                       "pinned": pinned, "next_step": e["next_step"], "priority": e.get("priority") or 99})
    offers.sort(key=lambda o: (-(o["can"] >= n["grade"]), -o["can"], -(o["max_grade"] >= n["grade"]), o["priority"]))
    best = offers[0] if offers else None
    path = next((o for o in offers if o["max_grade"] >= n["grade"]), None)   # someone who can reach the target
    now = f"{best['name']} can do up to G{best['can']} now. " if best and best["can"] else ""
    if best and best["can"] >= n["grade"]:
        state, advice = "ready", f"{best['name']} can do G{n['grade']}"
    elif path:
        state = "partial" if now else "locked"
        advice = f"{now}For G{n['grade']}: {path['name']}: {path['next_step']}"
    elif offers:
        state, advice = "partial" if now else "locked", f"{now}No engineer goes to G{n['grade']} for this blueprint"
    else:
        state, advice = "none", "No engineer offers this blueprint"
    pinned_at = next((o["name"] for o in offers if o["pinned"]), None)
    return n | {
        "label": pin_label(conn, n["module_type"], n["blueprint"], n["grade"]) if n["module_type"] else n["blueprint"],
        "state": state, "advice": advice, "offers": offers[:4],
        "pin_advice": (f"Your planned pin: {pinned_at}" if pinned_at else
                       f"Pin it at {path['name']} to roll remotely" if path else None),
    }


def builds_view(conn: sqlite3.Connection, fid: str) -> dict:
    rows = conn.execute("SELECT * FROM builds WHERE fid = ? ORDER BY id", (fid,)).fetchall()
    ctx = _context(conn, fid) if rows else None
    builds = [build_view(conn, fid, r, ctx) for r in rows]
    goals = [(r[0], r[1], r[2]) for r in conn.execute(
        "SELECT id, title, target_key FROM goals WHERE fid = ? AND kind = 'ship' ORDER BY priority", (fid,))]
    ships = [(r[0], r[1], _ship_label(conn, fid, r[0])) for r in conn.execute(
        "SELECT ship_id, ship_type FROM ships WHERE fid = ? AND disposed_ts IS NULL ORDER BY ship_id", (fid,))]
    return {"builds": builds, "goal_options": goals, "ship_options": ships,
            "has_example": any(r["template_key"] == EXAMPLE_KEY for r in rows)}


def by_goal(conn: sqlite3.Connection, fid: str) -> dict[int, dict]:
    """Build summaries keyed by the goal they're attached to (for the Goals tab)."""
    rows = conn.execute("SELECT * FROM builds WHERE fid = ? AND goal_id IS NOT NULL", (fid,)).fetchall()
    if not rows:
        return {}
    ctx = _context(conn, fid)
    return {r["goal_id"]: build_view(conn, fid, r, ctx) for r in rows}


def plan_entries(view: dict) -> list[dict]:
    """The build's remaining engineering as engineering-plan entries (grouped by blueprint, grade and floor)."""
    groups: dict[tuple, int] = defaultdict(int)
    for b in view["blueprints"]:
        for s in b["slots"]:
            groups[(b["module_type"], b["blueprint"], b["grade"], s["done_grade"], s["experimental"])] += 1
    return [{"module_type": mt, "blueprint": bp, "target_grade": g, "done_grade": done, "experimental": exp,
             "modules": n, "include": 1, "note": view["name"]}
            for (mt, bp, g, done, exp), n in groups.items() if mt and (done < g or exp)]
