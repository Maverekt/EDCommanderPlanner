"""Activities (DESIGN §16 Phase 5e): sessions, trading, mining and exploration stats, mining favourites,
ship readiness and the engineer unlock items each activity can supply.

A session runs from one login (`LoadGame`) to the last event before the next one. Everything here is read
from stored events; nothing is projected, so new events show on the next refresh.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from app.core import db, paths
from app.core.normalize import normalize_symbol
from app.server.engineer_views import unlock_facts
from app.server.queries import display_name
from app.server.views import _latest, engineers_view

ACTIVITIES = ("trading", "mining", "exploration", "exobiology")
METHODS = {"laser": "Laser", "core": "Core (deep core)", "subsurface": "Sub-surface", "surface": "Surface (abrasion)"}

# Equipment groups, matched against fitted module symbols.
GEAR = {
    "laser_tool": ("Mining laser or Mining Volley Repeater", r"^hpt_mininglaser_|^hpt_miningtoolv2_"),
    "abrasion": ("Abrasion Blaster (or Mining Volley Repeater)", r"^hpt_mining_abrblstr_|^hpt_miningtoolv2_"),
    "seismic": ("Seismic Charge Launcher", r"^hpt_mining_seismchrgwarhd_"),
    "ssdm": ("Sub-Surface Displacement Missile", r"^hpt_mining_subsurfdispmisle_"),
    "pwa": ("Pulse Wave Analyser", r"^hpt_mrascanner_"),
    "refinery": ("Refinery", r"^int_refinery_"),
    "prospector": ("Prospector limpets", r"^int_dronecontrol_prospector_|^int_multidronecontrol_(mining|universal)"),
    "collector": ("Collector limpets", r"^int_dronecontrol_collection_|^int_multidronecontrol_(mining|universal)"),
    "dss": ("Detailed Surface Scanner", r"^int_detailedsurfacescanner_"),
    "fuelscoop": ("Fuel scoop", r"^int_fuelscoop_"),
    "afmu": ("Auto Field-Maintenance Unit", r"^int_repairer_"),
    "guardian_booster": ("Guardian FSD Booster", r"^int_guardianfsdbooster_"),
    "heatsink": ("Heat sink launcher", r"^hpt_heatsinklauncher_"),
    "srv": ("SRV or Nomad hangar", r"^int_buggybay_|^int_nomadbay_|^int_slvbay_"),
}
# Per mining method: (required, helps).
MINING_NEEDS = {
    "laser": (("laser_tool", "refinery"), ("prospector", "collector")),
    "core": (("seismic", "pwa"), ("prospector", "collector")),
    "subsurface": (("ssdm",), ("prospector", "pwa", "collector", "refinery")),
    "surface": (("abrasion",), ("pwa", "collector", "refinery")),
}
EXPLORATION_KIT = ("dss", "fuelscoop", "afmu", "guardian_booster", "heatsink", "srv")


@lru_cache(maxsize=1)
def mining_commodities() -> dict[str, dict]:
    with (paths.refdata_dir() / "mining_commodities.csv").open(newline="", encoding="utf-8") as f:
        return {r["commodity"]: {"methods": r["methods"].split(";"), "rings": r["rings"].replace(";", ", "),
                                 "note": r["note"]} for r in csv.DictReader(f)}


# -- sessions ------------------------------------------------------------------------------

def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def sessions(conn: sqlite3.Connection, fid: str) -> list[dict]:
    """Login sessions, newest first: {start, end, hours}."""
    logins = [r[0] for r in conn.execute("SELECT ts FROM events WHERE fid = ? AND event = 'LoadGame' ORDER BY ts, id", (fid,))]
    out = []
    for i, start in enumerate(logins):
        nxt = logins[i + 1] if i + 1 < len(logins) else "9999"
        end = conn.execute("SELECT MAX(ts) FROM events WHERE fid = ? AND ts >= ? AND ts < ?", (fid, start, nxt)).fetchone()[0]
        hours = max((_parse(end) - _parse(start)).total_seconds() / 3600, 0) if end else 0
        out.append({"start": start, "end": end or start, "hours": hours, "until": nxt})
    return out[::-1]


def _window(conn: sqlite3.Connection, fid: str, which: str) -> dict:
    """`session` (the latest), `all`, or a session start timestamp."""
    ss = sessions(conn, fid)
    if which == "all" or not ss:
        return {"key": "all", "label": "All time", "start": "0000", "until": "9999", "sessions": ss, "hours": None}
    s = next((x for x in ss if x["start"] == which), ss[0])
    return {"key": "session" if s is ss[0] else s["start"], "label": "This session" if s is ss[0] else "Session",
            "start": s["start"], "until": s["until"], "sessions": ss, "hours": s["hours"], "session": s}


def _events(conn: sqlite3.Connection, fid: str, w: dict, *names: str) -> list[tuple[str, str, dict]]:
    marks = ",".join("?" * len(names))
    return [(ts, ev, json.loads(raw)) for ts, ev, raw in conn.execute(
        f"SELECT ts, event, raw_json FROM events WHERE fid = ? AND event IN ({marks}) AND ts >= ? AND ts < ? "
        "ORDER BY ts, id", (fid, *names, w["start"], w["until"]))]


def _active_hours(w: dict, conn: sqlite3.Connection, fid: str, *names: str) -> float:
    """Hours for per-hour rates: the session's length, or (all time) the sessions where the activity happened."""
    if w["key"] != "all":
        return w["hours"] or 0
    total = 0.0
    marks = ",".join("?" * len(names))
    for s in w["sessions"]:
        if conn.execute(f"SELECT 1 FROM events WHERE fid = ? AND event IN ({marks}) AND ts >= ? AND ts < ? LIMIT 1",
                        (fid, *names, s["start"], s["until"])).fetchone():
            total += s["hours"]
    return total


MINING_EVENTS = ("MiningRefined", "ProspectedAsteroid", "AsteroidCracked")   # limpets fly for other jobs too


def _busy_hours(conn: sqlite3.Connection, fid: str, w: dict, names: tuple[str, ...]) -> float:
    """Time actually spent on an activity: first to last of its events, per session in the window."""
    marks = ",".join("?" * len(names))
    total = 0.0
    for s in w["sessions"]:
        if s["until"] <= w["start"] or s["start"] >= w["until"]:
            continue
        lo, hi = conn.execute(f"SELECT MIN(ts), MAX(ts) FROM events WHERE fid = ? AND event IN ({marks}) "
                              "AND ts >= ? AND ts < ?", (fid, *names, max(s["start"], w["start"]),
                                                         min(s["until"], w["until"]))).fetchone()
        if lo and hi:
            total += (_parse(hi) - _parse(lo)).total_seconds() / 3600
    return total


def _rate(value: float, hours: float) -> float | None:
    return value / hours if hours and hours >= 0.05 else None


# -- ships ---------------------------------------------------------------------------------

def activity_ship(conn: sqlite3.Connection, fid: str, activity: str) -> int | None:
    """The ship assigned to an activity while it's still owned; otherwise the current ship."""
    sid = db.get_setting(conn, f"activity_ship:{activity}:{fid}")
    if sid and sid.isdigit() and conn.execute("SELECT 1 FROM ships WHERE fid = ? AND ship_id = ? AND disposed_ts IS NULL",
                                              (fid, int(sid))).fetchone():
        return int(sid)
    row = conn.execute("SELECT json_extract(raw_json, '$.ShipID') FROM events WHERE fid = ? AND event IN "
                       "('Loadout', 'LoadGame', 'ShipyardSwap') ORDER BY ts DESC, id DESC LIMIT 1", (fid,)).fetchone()
    return row[0] if row else None


def ship_options(conn: sqlite3.Connection, fid: str) -> list[tuple[int, str]]:
    out = []
    for sid, stype, name, ident in conn.execute(
            "SELECT ship_id, ship_type, name, ident FROM ships WHERE fid = ? AND disposed_ts IS NULL "
            "AND ship_type IN (SELECT symbol FROM ref_items WHERE kind = 'ship') ORDER BY ship_id", (fid,)):
        out.append((sid, " · ".join(x for x in (display_name(conn, stype, None), f"“{name}”" if name else None, ident) if x)))
    return out


def gear(conn: sqlite3.Connection, fid: str, ship_id: int | None) -> dict[str, list[str]]:
    """Equipment group -> fitted module names on that ship."""
    out: dict[str, list[str]] = defaultdict(list)
    if ship_id is None:
        return out
    for item, in conn.execute("SELECT item FROM module_state WHERE fid = ? AND ship_id = ?", (fid, ship_id)):
        for key, (_label, pattern) in GEAR.items():
            if re.search(pattern, item):
                out[key].append(display_name(conn, item, None))
    return out


def _readiness(have: dict, required: tuple, helps: tuple) -> dict:
    rows = [{"key": k, "label": GEAR[k][0], "required": True, "fitted": have.get(k)} for k in required]
    rows += [{"key": k, "label": GEAR[k][0], "required": False, "fitted": have.get(k)} for k in helps]
    return {"rows": rows, "ready": all(r["fitted"] for r in rows if r["required"]),
            "missing": [r["label"] for r in rows if r["required"] and not r["fitted"]]}


# -- engineer unlock items -----------------------------------------------------------------

_UNLOCK = re.compile(r"^([\d,]+)\s+(mined\s+)?(.+)$")


@lru_cache(maxsize=1)
def _unlock_items_parsed() -> list[dict]:
    out = []
    for eid, f in unlock_facts().items():
        m = _UNLOCK.match((f.get("unlock") or "").strip())
        if not m or " Cr" in f["unlock"] or "Cr " in f["unlock"]:
            continue                                     # credits and vouchers aren't items
        out.append({"engineer_id": eid, "qty": int(m.group(1).replace(",", "")), "mined": bool(m.group(2)),
                    "name": m.group(3).strip(), "text": f["unlock"]})
    return out


def _match_item(name: str, items: list[tuple[str, str, str]]) -> tuple[str | None, str | None]:
    """Unlock text name -> (symbol, kind): exact, then singular, then every word present ("Xihe Companions")."""
    want = name.lower()
    for test in (lambda n: n == want, lambda n: n == want.rstrip("s"),
                 lambda n: set(want.split()) <= set(n.split())):
        hit = next(((s, "commodity" if k == "rare_commodity" else k) for s, k, n in items if test(n)), None)
        if hit:
            return hit
    return None, None


def unlock_items(conn: sqlite3.Connection, fid: str, cargo_file: Path | None = None) -> dict[str, list[dict]]:
    """Unlock items still needed, by the activity that supplies them, with what's in the hold or locker."""
    items = [(s, k, n.lower()) for s, k, n in conn.execute(
        "SELECT symbol, kind, display_name FROM ref_items WHERE kind IN ('commodity', 'rare_commodity', 'material')")]
    states = {int(e["id"]): e for group in engineers_view(conn, fid).values() for e in group}
    cargo = _cargo(conn, fid, cargo_file)
    mats = {s: int(c["value"]) for s, c in _latest(conn, fid, "material").items()}
    mined = mining_commodities()
    out: dict[str, list[dict]] = defaultdict(list)
    for it in _unlock_items_parsed():
        e = states.get(it["engineer_id"])
        if not e or e["state"] == "Unlocked":
            continue
        symbol, kind = _match_item(it["name"], items)
        have = cargo.get(symbol, 0) if kind == "commodity" else mats.get(symbol, 0) if kind == "material" else None
        activity = ("mining" if kind == "commodity" and (it["mined"] or symbol in mined) else
                    "trading" if kind == "commodity" else "exploration" if kind == "material" else "trading")
        out[activity].append(it | {"engineer": e["name"], "state": e["state"], "symbol": symbol, "kind": kind,
                                   "have": have, "enough": have is not None and have >= it["qty"],
                                   "where": "in your hold" if kind == "commodity" else "in your materials"})
    for rows in out.values():
        rows.sort(key=lambda r: (r["enough"], r["engineer"]))
    return out


def _cargo(conn: sqlite3.Connection, fid: str, cargo_file: Path | None = None) -> dict[str, int]:
    """The ship's hold: the game's Cargo.json when it's there (in-session Cargo events don't list the hold),
    else the newest Cargo event that does."""
    if cargo_file is not None and cargo_file.is_file():
        try:
            doc = json.loads(cargo_file.read_text(encoding="utf-8"))
            if doc.get("Vessel", "Ship") == "Ship" and isinstance(doc.get("Inventory"), list):
                out: dict[str, int] = defaultdict(int)
                for i in doc["Inventory"]:
                    out[normalize_symbol(i.get("Name"))] += int(i.get("Count") or 0)
                return out
        except (OSError, ValueError):
            pass
    row = conn.execute("SELECT raw_json FROM events WHERE fid = ? AND event = 'Cargo' "
                       "AND json_extract(raw_json, '$.Vessel') = 'Ship' AND json_extract(raw_json, '$.Inventory') IS NOT NULL "
                       "ORDER BY ts DESC, id DESC LIMIT 1", (fid,)).fetchone()
    out: dict[str, int] = defaultdict(int)
    for i in (json.loads(row[0]).get("Inventory") or []) if row else []:
        out[normalize_symbol(i.get("Name"))] += int(i.get("Count") or 0)
    return out


# -- mining --------------------------------------------------------------------------------

def mining_view(conn: sqlite3.Connection, fid: str, which: str = "session") -> dict:
    w = _window(conn, fid, which)
    refined: Counter = Counter()
    content: Counter = Counter()
    limpets: Counter = Counter()
    prospected = motherlodes = cracked = materials = 0
    for _ts, ev, e in _events(conn, fid, w, "MiningRefined", "ProspectedAsteroid", "AsteroidCracked", "LaunchDrone",
                              "MaterialCollected"):
        if ev == "MiningRefined":
            refined[normalize_symbol(e.get("Type"))] += 1
        elif ev == "ProspectedAsteroid":
            prospected += 1
            level = (normalize_symbol(e.get("Content")) or "").rsplit("_", 1)[-1]
            content[level.title() if level else "?"] += 1
            motherlodes += bool(e.get("MotherlodeMaterial"))
        elif ev == "AsteroidCracked":
            cracked += 1
        elif ev == "LaunchDrone":
            limpets[e.get("Type") or "?"] += 1
        elif ev == "MaterialCollected" and prospected:     # raw materials picked up while mining
            materials += int(e.get("Count") or 1)
    # Sales count as mining income only for what this commander has actually mined (refined, or a cracked core).
    mined_kinds = {normalize_symbol(r[0]) for r in conn.execute(
        "SELECT DISTINCT json_extract(raw_json, '$.Type') FROM events WHERE fid = ? AND event = 'MiningRefined' "
        "UNION SELECT DISTINCT json_extract(raw_json, '$.MotherlodeMaterial') FROM events WHERE fid = ? "
        "AND event = 'ProspectedAsteroid'", (fid, fid)) if r[0]}
    sold_t = sold_cr = 0
    sold: Counter = Counter()
    for _ts, _ev, e in _events(conn, fid, w, "MarketSell"):
        t = normalize_symbol(e.get("Type"))
        if t in mined_kinds:
            sold_t += int(e.get("Count") or 0)
            sold_cr += int(e.get("TotalSale") or 0)
            sold[t] += int(e.get("TotalSale") or 0)
    hours = _active_hours(w, conn, fid, "MiningRefined", "ProspectedAsteroid")
    busy = _busy_hours(conn, fid, w, MINING_EVENTS)
    tons = sum(refined.values())
    ship_id = activity_ship(conn, fid, "mining")
    have = gear(conn, fid, ship_id)
    names = dict(conn.execute("SELECT symbol, display_name FROM ref_items WHERE kind = 'commodity'"))
    spots = []
    for r in conn.execute("SELECT * FROM mining_spots WHERE fid = ? ORDER BY id", (fid,)):
        need, helps = MINING_NEEDS.get(r["method"], ((), ()))
        ref = mining_commodities().get(r["commodity"], {})
        spots.append(dict(r) | {"commodity_name": names.get(r["commodity"], r["commodity"]),
                                "method_label": METHODS.get(r["method"], r["method"]),
                                "usual": r["method"] in ref.get("methods", [r["method"]]),
                                "usual_methods": ", ".join(METHODS[m].split(" ")[0] for m in ref.get("methods", [])),
                                "readiness": _readiness(have, need, helps)})
    return {
        "window": w, "hours": hours, "tons": tons, "tons_per_hour": _rate(tons, hours),
        "busy_hours": busy, "tons_per_busy_hour": _rate(tons, busy),
        "refined": [{"name": names.get(k, k), "tons": v} for k, v in refined.most_common()],
        "prospected": prospected, "content": dict(content), "motherlodes": motherlodes, "cracked": cracked,
        "hit_rate": (content.get("High", 0) / prospected) if prospected else None,
        "limpets": dict(limpets), "materials": materials, "materials_per_hour": _rate(materials, hours),
        "sold_t": sold_t, "sold_cr": sold_cr, "cr_per_hour": _rate(sold_cr, hours),
        "sold": [{"name": names.get(k, k), "cr": v} for k, v in sold.most_common()],
        "ship_id": ship_id, "ship_choice": db.get_setting(conn, f"activity_ship:mining:{fid}"),
        "methods": {m: _readiness(have, *MINING_NEEDS[m]) | {"label": METHODS[m]} for m in METHODS},
        "spots": spots, "commodity_options": sorted(((c, names.get(c, c)) for c in mining_commodities()), key=lambda x: x[1]),
        "commodity_ref": mining_commodities(),
    }


def add_spot(conn: sqlite3.Connection, fid: str, cols: dict) -> int:
    commodity = normalize_symbol(str(cols.get("commodity") or ""))
    method = str(cols.get("method") or "")
    if not conn.execute("SELECT 1 FROM ref_items WHERE symbol = ? AND kind = 'commodity'", (commodity,)).fetchone():
        raise ValueError("pick a commodity")
    if method not in METHODS:
        raise ValueError("pick a mining method")
    clean = {k: (str(cols.get(k) or "").strip()[:200] or None) for k in ("system", "body", "notes")}
    with conn:
        cur = conn.execute("INSERT INTO mining_spots (fid, commodity, method, system, body, notes, created_ts) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)", (fid, commodity, method, clean["system"], clean["body"],
                                                            clean["notes"], datetime.now(timezone.utc).strftime(
                                                                "%Y-%m-%dT%H:%M:%SZ")))
    return cur.lastrowid


def delete_spot(conn: sqlite3.Connection, fid: str, spot_id: int) -> None:
    with conn:
        if not conn.execute("DELETE FROM mining_spots WHERE id = ? AND fid = ?", (spot_id, fid)).rowcount:
            raise LookupError("no such favourite")


def set_activity_ship(conn: sqlite3.Connection, fid: str, activity: str, ship_id: str) -> None:
    if activity not in ACTIVITIES:
        raise ValueError("unknown activity")
    key = f"activity_ship:{activity}:{fid}"
    with conn:
        if not str(ship_id or "").isdigit():
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            return
        if not conn.execute("SELECT 1 FROM ships WHERE fid = ? AND ship_id = ? AND disposed_ts IS NULL",
                            (fid, int(ship_id))).fetchone():
            raise ValueError("that isn't one of your ships")
        db.set_setting(conn, key, str(int(ship_id)))


def _stations(conn: sqlite3.Connection, fid: str) -> dict[int, str]:
    return {mid: f"{st} · {sy}" for mid, st, sy in conn.execute(
        "SELECT json_extract(raw_json, '$.MarketID'), json_extract(raw_json, '$.StationName'), "
        "json_extract(raw_json, '$.StarSystem') FROM events WHERE fid = ? AND event = 'Docked' ORDER BY ts", (fid,)) if mid}


def _travel(conn: sqlite3.Connection, fid: str, w: dict) -> tuple[int, float]:
    jumps = [e for _ts, _ev, e in _events(conn, fid, w, "FSDJump")]
    return len(jumps), sum(float(e.get("JumpDist") or 0) for e in jumps)


# The game's own lifetime counters per activity: (Statistics section, key, label, unit).
LIFETIME = {
    "trading": [("Trading", "Market_Profits", "Market profits", "cr"), ("Trading", "Resources_Traded", "Traded", "t"),
                ("Trading", "Markets_Traded_With", "Markets traded with", ""),
                ("Trading", "Highest_Single_Transaction", "Best single sale", "cr")],
    "mining": [("Mining", "Quantity_Mined", "Mined", "t"), ("Mining", "Mining_Profits", "Mining profits", "cr"),
               ("Mining", "Materials_Collected", "Materials collected", "")],
    "exploration": [("Exploration", "Exploration_Profits", "Exploration profits", "cr"),
                    ("Exploration", "Systems_Visited", "Systems visited", ""),
                    ("Exploration", "Total_Hyperspace_Distance", "Travelled", "ly"),
                    ("Exploration", "Planets_Scanned_To_Level_3", "Planets mapped", ""),
                    ("Exploration", "Greatest_Distance_From_Start", "Furthest from start", "ly")],
    "exobiology": [("Exobiology", "Organic_Data_Profits", "Organic data profits", "cr"),
                   ("Exobiology", "Organic_Species_Encountered", "Species encountered", ""),
                   ("Exobiology", "First_Logged", "First logged", ""),
                   ("Exobiology", "First_Logged_Profits", "First-logged bonus", "cr")],
}


def lifetime(conn: sqlite3.Connection, fid: str, activity: str) -> dict:
    """Lifetime figures from the newest `Statistics`: these count play from before the oldest journal too."""
    row = conn.execute("SELECT ts, raw_json FROM events WHERE fid = ? AND event = 'Statistics' ORDER BY ts DESC, id DESC "
                       "LIMIT 1", (fid,)).fetchone()
    if not row:
        return {"as_of": None, "rows": []}
    stats = json.loads(row[1])
    rows = [{"label": label, "value": (stats.get(section) or {}).get(key), "unit": unit}
            for section, key, label, unit in LIFETIME[activity]]
    return {"as_of": row[0], "rows": [r for r in rows if r["value"] is not None]}


def first_journal(conn: sqlite3.Connection, fid: str) -> str | None:
    return conn.execute("SELECT MIN(ts) FROM events WHERE fid = ?", (fid,)).fetchone()[0]


# -- trading -------------------------------------------------------------------------------

def trading_view(conn: sqlite3.Connection, fid: str, which: str = "session") -> dict:
    w = _window(conn, fid, which)
    names = dict(conn.execute("SELECT symbol, display_name FROM ref_items WHERE kind IN ('commodity', 'rare_commodity')"))
    mined = {normalize_symbol(r[0]) for r in conn.execute(
        "SELECT DISTINCT json_extract(raw_json, '$.Type') FROM events WHERE fid = ? AND event = 'MiningRefined'", (fid,)) if r[0]}
    stations = _stations(conn, fid)
    rows: dict[str, dict] = defaultdict(lambda: {"bought": 0, "spent": 0, "sold": 0, "revenue": 0, "profit": 0})
    routes: dict[tuple, dict] = defaultdict(lambda: {"tons": 0, "profit": 0, "runs": 0})
    last_buy: dict[str, int] = {}
    for _ts, ev, e in _events(conn, fid, w, "MarketBuy", "MarketSell"):
        t = normalize_symbol(e.get("Type"))
        if t in mined:
            continue                                       # mining income belongs to the Mining section
        n = int(e.get("Count") or 0)
        if ev == "MarketBuy":
            rows[t]["bought"] += n
            rows[t]["spent"] += int(e.get("TotalCost") or 0)
            last_buy[t] = e.get("MarketID")
        else:
            sale = int(e.get("TotalSale") or 0)
            profit = sale - int(e.get("AvgPricePaid") or 0) * n
            rows[t]["sold"] += n
            rows[t]["revenue"] += sale
            rows[t]["profit"] += profit
            key = (stations.get(last_buy.get(t), "?"), stations.get(e.get("MarketID"), "?"), names.get(t, t))
            routes[key]["tons"] += n
            routes[key]["profit"] += profit
            routes[key]["runs"] += 1
    jumps, ly = _travel(conn, fid, w)
    hours = _active_hours(w, conn, fid, "MarketBuy", "MarketSell")
    profit = sum(r["profit"] for r in rows.values())
    return {
        "window": w, "hours": hours, "jumps": jumps, "ly": ly, "ly_per_hour": _rate(ly, hours),
        "commodities": sorted(({"name": names.get(k, k)} | v for k, v in rows.items()), key=lambda r: -r["profit"]),
        "bought": sum(r["bought"] for r in rows.values()), "sold": sum(r["sold"] for r in rows.values()),
        "spent": sum(r["spent"] for r in rows.values()), "revenue": sum(r["revenue"] for r in rows.values()),
        "profit": profit, "cr_per_hour": _rate(profit, hours),
        "routes": sorted(({"from": a, "to": b, "commodity": c} | v for (a, b, c), v in routes.items()),
                         key=lambda r: -r["profit"])[:8],
        "ship_id": activity_ship(conn, fid, "trading"), "ship_choice": db.get_setting(conn, f"activity_ship:trading:{fid}"),
    }


# -- exploration ---------------------------------------------------------------------------

def exploration_view(conn: sqlite3.Connection, fid: str, which: str = "session") -> dict:
    w = _window(conn, fid, which)
    bodies, unmapped_before = set(), set()
    first_found = honks = mapped = first_mapped = codex = earned = 0
    for _ts, ev, e in _events(conn, fid, w, "Scan", "FSSDiscoveryScan", "SAAScanComplete", "CodexEntry",
                              "MultiSellExplorationData", "SellExplorationData"):
        key = (e.get("SystemAddress"), e.get("BodyID"))
        if ev == "Scan" and "Belt Cluster" not in (e.get("BodyName") or ""):
            if key not in bodies:
                bodies.add(key)
                first_found += e.get("WasDiscovered") is False
                if e.get("WasMapped") is False:
                    unmapped_before.add(key)
        elif ev == "FSSDiscoveryScan":
            honks += 1
        elif ev == "SAAScanComplete":
            mapped += 1
            first_mapped += key in unmapped_before
        elif ev == "CodexEntry":
            codex += 1
        elif ev == "MultiSellExplorationData":
            earned += int(e.get("TotalEarnings") or 0)
        elif ev == "SellExplorationData":
            earned += int(e.get("BaseValue") or 0) + int(e.get("Bonus") or 0)
    jumps, ly = _travel(conn, fid, w)
    hours = _active_hours(w, conn, fid, "FSDJump", "Scan")
    last_sale = conn.execute("SELECT MAX(ts) FROM events WHERE fid = ? AND event IN ('MultiSellExplorationData', "
                             "'SellExplorationData', 'Died')", (fid,)).fetchone()[0] or "0000"
    unsold = conn.execute("SELECT COUNT(DISTINCT json_extract(raw_json, '$.SystemAddress') || ':' || "
                          "json_extract(raw_json, '$.BodyID')) FROM events WHERE fid = ? AND event = 'Scan' AND ts > ? "
                          "AND coalesce(json_extract(raw_json, '$.BodyName'), '') NOT LIKE '%Belt Cluster%'",
                          (fid, last_sale)).fetchone()[0]
    ship_id = activity_ship(conn, fid, "exploration")
    have = gear(conn, fid, ship_id)
    jump = conn.execute("SELECT json_extract(raw_json, '$.MaxJumpRange') FROM events WHERE fid = ? AND event = 'Loadout' "
                        "AND json_extract(raw_json, '$.ShipID') = ? ORDER BY ts DESC LIMIT 1", (fid, ship_id)).fetchone()
    return {
        "window": w, "hours": hours, "jumps": jumps, "ly": ly, "ly_per_hour": _rate(ly, hours),
        "systems_honked": honks, "bodies": len(bodies), "first_discovered": first_found, "mapped": mapped,
        "first_mapped": first_mapped, "codex": codex, "earned": earned, "cr_per_hour": _rate(earned, hours),
        "bodies_per_hour": _rate(len(bodies), hours), "unsold_bodies": unsold,
        "unsold_since": None if last_sale == "0000" else last_sale,
        "ship_id": ship_id, "ship_choice": db.get_setting(conn, f"activity_ship:exploration:{fid}"),
        "jump_range": jump[0] if jump else None,
        "kit": _readiness(have, ("fuelscoop",), tuple(k for k in EXPLORATION_KIT if k != "fuelscoop")),
    }


# -- exobiology ----------------------------------------------------------------------------

_SUIT_GRADE = re.compile(r"class(\d)")


def _artemis(conn: sqlite3.Connection, fid: str) -> dict | None:
    """The newest Artemis suit seen (bought or loaded out), with its grade from the symbol (explorationsuit_class3)."""
    row = conn.execute("SELECT coalesce(json_extract(raw_json, '$.SuitName'), json_extract(raw_json, '$.Name')), "
                       "coalesce(json_extract(raw_json, '$.SuitName_Localised'), json_extract(raw_json, '$.Name_Localised')) "
                       "FROM events WHERE fid = ? AND event IN ('SuitLoadout', 'BuySuit', 'UpgradeSuit') AND "
                       "lower(coalesce(json_extract(raw_json, '$.SuitName'), json_extract(raw_json, '$.Name'))) "
                       "LIKE 'explorationsuit%' ORDER BY ts DESC LIMIT 1", (fid,)).fetchone()
    if not row:
        return None
    grade = _SUIT_GRADE.search(row[0] or "")
    return {"name": row[1] or row[0], "grade": int(grade.group(1)) if grade else 1}


def artemis_grade(conn: sqlite3.Connection, fid: str) -> int | None:
    suit = _artemis(conn, fid)
    return suit["grade"] if suit else None


def exobiology_view(conn: sqlite3.Connection, fid: str, which: str = "session") -> dict:
    w = _window(conn, fid, which)
    steps: Counter = Counter()
    analysed = []
    for ts, _ev, e in _events(conn, fid, w, "ScanOrganic"):
        steps[e.get("ScanType") or "?"] += 1
        if e.get("ScanType") == "Analyse":
            analysed.append({"ts": ts, "species": e.get("Species_Localised") or e.get("Species"),
                             "variant": e.get("Variant_Localised"), "genus": e.get("Genus_Localised") or e.get("Genus")})
    values: dict[str, int] = {}
    for _ts, _ev, e in _events(conn, fid, {"start": "0000", "until": "9999"}, "SellOrganicData"):
        for b in e.get("BioData") or []:
            values[normalize_symbol(b.get("Species"))] = int(b.get("Value") or 0)     # base value; newest wins
    sold_cr = bonus = 0
    sold_species: Counter = Counter()
    for _ts, _ev, e in _events(conn, fid, w, "SellOrganicData"):
        for b in e.get("BioData") or []:
            sold_cr += int(b.get("Value") or 0) + int(b.get("Bonus") or 0)
            bonus += int(b.get("Bonus") or 0)
            sold_species[b.get("Species_Localised") or b.get("Species")] += 1
    last = conn.execute("SELECT MAX(ts) FROM events WHERE fid = ? AND event IN ('SellOrganicData', 'Died')",
                        (fid,)).fetchone()[0] or "0000"
    unsold = []
    for ts, raw in conn.execute("SELECT ts, raw_json FROM events WHERE fid = ? AND event = 'ScanOrganic' AND ts > ? "
                                "AND json_extract(raw_json, '$.ScanType') = 'Analyse' ORDER BY ts", (fid, last)):
        e = json.loads(raw)
        unsold.append({"ts": ts, "species": e.get("Species_Localised") or e.get("Species"),
                       "variant": e.get("Variant_Localised"), "value": values.get(normalize_symbol(e.get("Species")))})
    hours = _active_hours(w, conn, fid, "ScanOrganic", "SellOrganicData")
    suit = _artemis(conn, fid)
    return {
        "window": w, "hours": hours, "steps": dict(steps), "analysed": analysed, "species_count": len(analysed),
        "sold_cr": sold_cr, "bonus": bonus, "sold_species": sold_species.most_common(), "cr_per_hour": _rate(sold_cr, hours),
        "unsold": unsold, "unsold_known": sum(u["value"] or 0 for u in unsold),
        "unsold_unknown": sum(1 for u in unsold if u["value"] is None),
        "artemis": suit,
    }


def default_tab(conn: sqlite3.Connection, fid: str) -> str:
    """The activity of the newest relevant event, so the tab opens where you've just been playing."""
    groups = {"mining": ("MiningRefined", "ProspectedAsteroid"), "trading": ("MarketBuy", "MarketSell"),
              "exploration": ("FSSDiscoveryScan", "SAAScanComplete", "MultiSellExplorationData"),
              "exobiology": ("ScanOrganic", "SellOrganicData")}
    best, best_ts = "trading", ""
    for key, names in groups.items():
        ts = conn.execute(f"SELECT MAX(ts) FROM events WHERE fid = ? AND event IN ({','.join('?' * len(names))})",
                          (fid, *names)).fetchone()[0] or ""
        if ts > best_ts:
            best, best_ts = key, ts
    return best
