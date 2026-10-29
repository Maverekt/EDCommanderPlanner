"""View models for History, Ledger and Ships (DESIGN §9.3, §13)."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from app.core import paths
from app.core.projectors.progress import NAVY, PILOTS
from app.core.projectors.statistics import counters
from app.server import charts
from app.server.queries import display_name
from app.server.views import NAVY_TRACKS, PILOT_TRACKS, rank_names

RANGES = {"30d": "30 days", "90d": "90 days", "1y": "1 year", "all": "All time"}
LEDGER_RANGES = {"session": "Last session", "7d": "7 days", "30d": "30 days", "all": "All time"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def range_start(period: str, latest: str | None) -> str | None:
    if period == "all" or not latest:
        return None
    days = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}.get(period, 30)
    end = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    return _iso(end - timedelta(days=days))


def _events(conn, fid, name, since=None):
    sql = "SELECT ts, raw_json FROM events WHERE fid = ? AND event = ?"
    params = [fid, name]
    if since:
        sql += " AND ts >= ?"
        params.append(since)
    return [(ts, json.loads(raw)) for ts, raw in conn.execute(sql + " ORDER BY ts, id", params)]


# -- History ----------------------------------------------------------------------

def history_view(conn: sqlite3.Connection, fid: str, period: str, track: str, material: str | None) -> dict:
    period = period if period in RANGES else "90d"
    latest = conn.execute("SELECT MAX(ts) FROM events WHERE fid = ?", (fid,)).fetchone()[0]
    since = range_start(period, latest)

    day_counts = {d: n for d, n in conn.execute(
        "SELECT substr(ts, 1, 10), COUNT(*) FROM events WHERE fid = ? GROUP BY 1", (fid,))}
    end = date.fromisoformat(latest[:10]) if latest else _now().date()
    # The calendar covers the selected range; all time starts at the first journal day.
    first_day = date.fromisoformat(since[:10]) if since else date.fromisoformat(min(day_counts)) if day_counts else end

    logins = [(ts, e.get("Credits")) for ts, e in _events(conn, fid, "LoadGame", since)]
    stats = [(ts, counters(e)) for ts, e in _events(conn, fid, "Statistics", since)]

    # Rank progress per login: rank + progress/100, so a promotion is a step up.
    tracks = dict(PILOT_TRACKS + NAVY_TRACKS)
    track = track if track in tracks else "combat"
    jkey = next((k for k, v in {**PILOTS, **NAVY}.items() if v == track), None)
    rank_pts, rank = [], None
    for ts, name, raw in conn.execute(
        "SELECT ts, event, raw_json FROM events WHERE fid = ? AND event IN ('Rank', 'Progress', 'Promotion') "
        "ORDER BY ts, id", (fid,)):
        e = json.loads(raw)
        if jkey not in e:
            continue
        if name in ("Rank", "Promotion"):
            rank = e[jkey]
            if name == "Promotion" and (not since or ts >= since):
                rank_pts.append((ts, float(rank)))
        elif rank is not None and (not since or ts >= since):
            rank_pts.append((ts, rank + min(e[jkey], 100) / 100))
    names = rank_names().get(track, [])

    mats = [r[0] for r in conn.execute(
        "SELECT key FROM snapshots WHERE fid = ? AND metric = 'material' GROUP BY key ORDER BY MAX(date) DESC, key",
        (fid,))]
    material = material if material in mats else (mats[0] if mats else None)
    mat_pts = []
    if material:
        mat_pts = [(f"{d}T12:00:00Z", v) for d, v in conn.execute(
            "SELECT date, value FROM snapshots WHERE fid = ? AND metric = 'material' AND key = ? "
            + ("AND date >= ? " if since else "") + "ORDER BY date",
            (fid, material, since[:10]) if since else (fid, material))]

    def rank_label(v):
        r = int(v)
        return f"{names[r] if r < len(names) else r} {round((v - r) * 100)}%"

    return {
        "period": period, "ranges": RANGES, "since": since,
        "calendar": charts.calendar(day_counts, end, first_day=first_day),
        "period_label": RANGES[period].lower(),
        "balance": charts.line_chart(logins, step=True),
        "balance_points": logins,
        "wealth": charts.line_chart([(ts, c.get("Bank_Account.Current_Wealth")) for ts, c in stats]),
        "merc": charts.line_chart([(ts, c.get("Bank_Account.MercCoins_Current")) for ts, c in stats], step=True,
                                  width=340, height=190),
        "track": track, "tracks": tracks,
        "rank": charts.line_chart(rank_pts, label_fmt=rank_label, step=True, width=340, height=190),
        "material": material,
        "material_name": display_name(conn, material, None) if material else None,
        "materials": [(m, display_name(conn, m, None) or m) for m in mats],
        "material_chart": charts.line_chart(mat_pts, step=True),
    }


# -- Ledger --------------------------------------------------------------------

@lru_cache(maxsize=1)
def ledger_categories() -> dict[str, dict]:
    with (paths.refdata_dir() / "ledger_categories.csv").open(newline="", encoding="utf-8") as f:
        return {r["category"]: r | {"counters": [c.strip() for c in r["stat_counters"].split(";") if c.strip()]}
                for r in csv.DictReader(f)}


def _stat_window(conn, fid, period):
    """(baseline Statistics ts, baseline counters, latest ts, latest counters) for the period."""
    stats = [(ts, counters(e)) for ts, e in _events(conn, fid, "Statistics")]
    if not stats:
        return None, {}, None, {}
    latest_ts, latest = stats[-1]
    if len(stats) < 2:
        return None, {}, latest_ts, latest
    if period == "all":
        # Since the first login in the journals: lifetime counters predate the ledger.
        return stats[0][0], stats[0][1], latest_ts, latest
    if period == "session":
        return stats[-2][0], stats[-2][1], latest_ts, latest
    start = range_start(period, latest_ts)
    before = [s for s in stats if s[0] < start]
    base = before[-1] if before else stats[0]
    return base[0], base[1], latest_ts, latest


def ledger_credits_view(conn: sqlite3.Connection, fid: str, period: str) -> dict:
    period = period if period in LEDGER_RANGES else "session"
    base_ts, base, latest_ts, latest = _stat_window(conn, fid, period)
    cats = ledger_categories()

    sql = "SELECT id, ts, category, item, amount_delta, detail_json FROM ledger WHERE fid = ? AND currency = 'cr'"
    params: list = [fid]
    if base_ts:
        sql += " AND ts > ?"
        params.append(base_ts)
    if latest_ts:
        sql += " AND ts <= ?"
        params.append(latest_ts)
    rows = conn.execute(sql + " ORDER BY ts DESC, id DESC", params).fetchall()

    by_cat: dict[str, dict] = {}
    for r in rows:
        c = by_cat.setdefault(r["category"], {"income": 0, "spend": 0, "rows": []})
        c["income" if r["amount_delta"] > 0 else "spend"] += r["amount_delta"]
        if len(c["rows"]) < 40:
            detail = json.loads(r["detail_json"] or "{}")
            c["rows"].append({"ts": r["ts"], "amount": r["amount_delta"], "event": detail.get("event"),
                              "item": display_name(conn, r["item"], None) if r["item"] else None})
    out = []
    for key, meta in cats.items():
        c = by_cat.get(key)
        stat_delta = None
        if meta["counters"] and latest:
            stat_delta = sum(latest.get(k, 0) - (base.get(k, 0) if base_ts else 0) for k in meta["counters"])
            if "Spent_On" in meta["stat_counters"] or "TotalWages" in meta["stat_counters"]:
                stat_delta = -stat_delta
        if not c and not stat_delta:
            continue
        net = (c["income"] + c["spend"]) if c else 0
        spend_counter = "Spent_On" in meta["stat_counters"] or "TotalWages" in meta["stat_counters"]
        # Spending counters only count money going out; compare like with like.
        itemized = (c["spend"] if c else 0) if spend_counter else net
        out.append({
            "key": key, "label": meta["label"], "note": meta["note"],
            "income": c["income"] if c else 0, "spend": c["spend"] if c else 0, "net": net,
            "itemized": itemized, "compares": "spend" if spend_counter else "net",
            "diff": (itemized - stat_delta) if stat_delta is not None else None,
            "stat_delta": stat_delta, "stat_counters": meta["counters"],
            "rows": c["rows"] if c else [], "count": sum(1 for r in rows if r["category"] == key),
        })
    out.sort(key=lambda r: -abs(r["net"]))
    flows = sum(abs(r["amount_delta"]) for r in rows)
    unattributed = sum(abs(r["amount_delta"]) for r in rows if r["category"] == "unattributed")

    session = conn.execute(
        "SELECT category, SUM(amount_delta) FROM ledger WHERE fid = ? AND currency = 'cr' AND ts > COALESCE("
        "(SELECT MAX(ts) FROM events WHERE fid = ? AND event = 'LoadGame'), '') GROUP BY category ORDER BY 2",
        (fid, fid)).fetchall()
    return {
        "period": period, "ranges": LEDGER_RANGES, "base_ts": base_ts, "latest_ts": latest_ts,
        "categories": out, "net": sum(r["amount_delta"] for r in rows),
        "unattributed_share": (unattributed / flows) if flows else 0,
        "session": [{"label": cats.get(k, {}).get("label", k), "amount": v} for k, v in session],
    }


def ledger_merc_view(conn: sqlite3.Connection, fid: str) -> dict:
    rows = conn.execute("SELECT ts, category, item, amount_delta FROM ledger WHERE fid = ? AND currency = 'mc' "
                        "ORDER BY ts DESC", (fid,)).fetchall()
    stats = _events(conn, fid, "Statistics")
    latest = counters(stats[-1][1]) if stats else {}
    return {"rows": [dict(r) for r in rows],
            "counters": {k.split(".", 1)[1]: v for k, v in latest.items() if "MercCoins_" in k},
            "as_of": stats[-1][0] if stats else None}


def ships_list(conn: sqlite3.Connection, fid: str) -> list[dict]:
    current = conn.execute("SELECT json_extract(raw_json, '$.ShipID') FROM events WHERE fid = ? AND event IN "
                           "('Loadout', 'LoadGame', 'ShipyardSwap') ORDER BY ts DESC, id DESC LIMIT 1", (fid,)).fetchone()
    current_id = current[0] if current else None
    out = []
    for r in conn.execute("SELECT * FROM ships WHERE fid = ? ORDER BY disposed_ts IS NOT NULL, ship_id", (fid,)):
        d = dict(r)
        d["type_name"] = display_name(conn, r["ship_type"], None)
        d["current"] = r["ship_id"] == current_id
        d["fitted"] = conn.execute("SELECT COUNT(*) FROM module_state WHERE fid = ? AND ship_id = ?",
                                   (fid, r["ship_id"])).fetchone()[0]
        out.append(d)
    return out


def _engineer_names(conn) -> dict[int, str]:
    return {int(s): n for s, n in conn.execute("SELECT symbol, display_name FROM ref_items WHERE kind = 'engineer'")}


def _blueprint_names(conn) -> dict[str, str]:
    return {b: n for b, n in conn.execute("SELECT DISTINCT blueprint, display_name FROM ref_blueprints") if n}


def _experimental_names(conn) -> dict[str, str]:
    return dict(conn.execute("SELECT effect, display_name FROM ref_experimentals"))


def _modifiers(raw) -> list[dict]:
    out = []
    for m in raw or []:
        if not isinstance(m, dict) or "Value" not in m:
            continue
        before, after = m.get("OriginalValue"), m.get("Value")
        better = None
        if isinstance(before, (int, float)) and isinstance(after, (int, float)) and before != after:
            better = (after < before) if m.get("LessIsGood") else (after > before)
        out.append({"label": _humanize(m.get("Label", "")), "before": before, "after": after, "better": better,
                    "pct": round(100 * (after - before) / abs(before), 1)
                    if isinstance(before, (int, float)) and isinstance(after, (int, float)) and before else None})
    return out


def _humanize(label: str) -> str:
    out = ""
    for i, ch in enumerate(label):
        if ch.isupper() and i and not label[i - 1].isupper():
            out += " "
        out += ch
    return out[:1].upper() + out[1:].lower() if out else label


def module_history_view(conn: sqlite3.Connection, fid: str, change: str | None = None, limit: int = 200) -> list[dict]:
    eng, bps, exps = _engineer_names(conn), _blueprint_names(conn), _experimental_names(conn)
    sql = "SELECT * FROM module_history WHERE fid = ?"
    params: list = [fid]
    if change:
        sql += " AND change = ?"
        params.append(change)
    out = []
    for r in conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?", (*params, limit)):
        d = dict(r)
        detail = json.loads(r["detail_json"] or "{}")
        d["item_name"] = display_name(conn, r["item"], None)
        d["blueprint_name"] = bps.get(r["blueprint"], r["blueprint"])
        d["experimental_name"] = exps.get(r["experimental"], r["experimental"])
        d["engineer_name"] = eng.get(r["engineer_id"])
        d["modifiers"] = _modifiers(detail.get("modifiers"))
        out.append(d)
    return out


def unlocks_view(conn: sqlite3.Connection, fid: str) -> list[dict]:
    eng = _engineer_names(conn)
    names = rank_names()
    labels = dict(PILOT_TRACKS + NAVY_TRACKS)
    out = []
    for r in conn.execute("SELECT * FROM unlocks WHERE fid = ? ORDER BY ts DESC, id DESC", (fid,)):
        d = json.loads(r["detail_json"] or "{}")
        kind, key = r["kind"], r["key"]
        if kind in ("rank", "navy_rank"):
            n = names.get(key, [])
            text = f"{labels.get(key, key)}: {n[d['from']] if d.get('from', 99) < len(n) else d.get('from')} → " \
                   f"{n[d['to']] if d.get('to', 99) < len(n) else d.get('to')}"
        elif kind == "promotion_available":
            text = f"{labels.get(key, key)}: promotion available"
        elif kind == "engineer_stage":
            text = f"{eng.get(int(key), key)}: {d.get('from')} → {d.get('to')}"
        elif kind == "engineer_grade":
            text = f"{eng.get(int(key), key)}: grade {d.get('from')} → {d.get('to')}"
        else:
            text = f"{kind}: {key}"
        out.append({"ts": r["ts"], "kind": kind, "text": text})
    return out


# -- Ships tab --------------------------------------------------------------------

SLOT_GROUPS = [
    ("Hardpoints", lambda s: "Hardpoint" in s and not s.startswith("Tiny")),
    ("Utility", lambda s: s.startswith("TinyHardpoint")),
    ("Core internals", lambda s: s in {"Armour", "PowerPlant", "MainEngines", "FrameShiftDrive", "LifeSupport",
                                        "PowerDistributor", "Radar", "FuelTank"}),
    ("Optional internals", lambda s: s.startswith("Slot") or s.startswith("Military")),
]


def ship_fits(conn: sqlite3.Connection, fid: str) -> list[dict]:
    eng, bps, exps = _engineer_names(conn), _blueprint_names(conn), _experimental_names(conn)
    out = []
    for ship in ships_list(conn, fid):
        if not ship["fitted"] or ship["disposed_ts"]:
            continue
        groups = {g: [] for g, _ in SLOT_GROUPS} | {"Other": []}
        as_of = None
        for r in conn.execute("SELECT * FROM module_state WHERE fid = ? AND ship_id = ? ORDER BY slot",
                              (fid, ship["ship_id"])):
            as_of = r["as_of_ts"]
            ref = conn.execute("SELECT attrs_json FROM ref_items WHERE symbol = ?", (r["item"],)).fetchone()
            attrs = json.loads(ref[0]) if ref and ref[0] else {}
            row = {
                "slot": r["slot"], "item": r["item"], "name": display_name(conn, r["item"], None),
                "class": f"{attrs.get('size', '')}{attrs.get('rating', '')}" if attrs else "",
                "mount": attrs.get("mount"),
                "blueprint": bps.get(r["blueprint"], r["blueprint"]), "grade": r["grade"],
                "quality": r["quality"], "experimental": exps.get(r["experimental"], r["experimental"]),
                "engineer": eng.get(r["engineer_id"]),
                "modifiers": _modifiers(json.loads(r["modifiers_json"]) if r["modifiers_json"] else []),
            }
            group = next((g for g, test in SLOT_GROUPS if test(r["slot"])), "Other")
            groups[group].append(row)
        engineered = sum(1 for rows in groups.values() for m in rows if m["blueprint"])
        out.append(ship | {"groups": [(g, rows) for g, rows in groups.items() if rows], "as_of": as_of,
                           "engineered": engineered, "modules": sum(len(rows) for rows in groups.values()),
                           "summary": _fit_summary(groups)})
    out.sort(key=lambda s: (not s["current"], s["ship_id"]))
    return out


def _fit_summary(groups: dict[str, list[dict]]) -> list[dict]:
    """A few headline modules for a collapsed ship: FSD, power plant, shields, thrusters and the weapons."""
    def chip(m: dict, label: str) -> dict:
        return {"label": f"{m['class']} {label}".strip(), "grade": m["grade"] if m["blueprint"] else None,
                "blueprint": m["blueprint"]}

    core = {m["slot"]: m for m in groups.get("Core internals", [])}
    out = [chip(core[slot], label) for slot, label in
           (("FrameShiftDrive", "FSD"), ("PowerPlant", "Power Plant"), ("MainEngines", "Thrusters")) if slot in core]
    shield = next((m for m in groups.get("Optional internals", []) if "Shield Generator" in (m["name"] or "")), None)
    if shield:
        out.append(chip(shield, "Shields"))
    weapons: dict[str, int] = {}
    for m in groups.get("Hardpoints", []):
        weapons[m["name"]] = weapons.get(m["name"], 0) + 1
    out += [{"label": f"{n}× {name}", "grade": None, "blueprint": None} for name, n in weapons.items()]
    return out
