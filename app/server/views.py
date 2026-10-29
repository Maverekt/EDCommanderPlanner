"""View models for the Progress and Overview tabs (DESIGN §9.2, §9.4, §13)."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from app.core import paths
from app.core.projectors.engineers import STAGE_NAMES, STAGES
from app.core.projectors.progress import MAX_RANK
from app.core.projectors.statistics import counters

PILOT_TRACKS = [("combat", "Combat"), ("trade", "Trade"), ("explore", "Exploration"),
                ("soldier", "Mercenary"), ("exobiologist", "Exobiologist"), ("cqc", "CQC")]
NAVY_TRACKS = [("federation", "Federal Navy"), ("empire", "Imperial Navy")]
SUPERPOWERS = [("federation", "Federation"), ("empire", "Empire"), ("alliance", "Alliance"),
               ("independent", "Independent")]
# Superpower reputation runs −100…+100. Band edges as commonly used by EDMC/Inara;
# confirm against the in-game display (Phase 2 "done when").
REPUTATION_BANDS = [(-90, "Hostile"), (-35, "Unfriendly"), (4, "Neutral"), (35, "Cordial"), (90, "Friendly")]
RANGES = {"session": "Last session", "7d": "Last 7 days", "30d": "Last 30 days", "all": "All time"}
SPARK_DAYS = 30


@lru_cache(maxsize=1)
def rank_names() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    with (paths.refdata_dir() / "ranks.csv").open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.setdefault(row["track"], []).append(row["name"])
    return out


@lru_cache(maxsize=1)
def stats_map() -> dict[str, dict]:
    with (paths.refdata_dir() / "stats_map.csv").open(newline="", encoding="utf-8") as f:
        return {row["counter"]: row for row in csv.DictReader(f)}


def _latest(conn: sqlite3.Connection, fid: str, metric: str) -> dict[str, dict]:
    """Latest value per key, with the timestamp and event that produced it."""
    rows = conn.execute(
        """
        SELECT s.key, s.value, e.ts, e.event FROM snapshots s
        JOIN events e ON e.id = s.source_event_id
        WHERE s.fid = ? AND s.metric = ? AND s.date = (
          SELECT MAX(date) FROM snapshots t WHERE t.fid = s.fid AND t.metric = s.metric AND t.key = s.key)
        """,
        (fid, metric),
    )
    return {r["key"]: {"value": r["value"], "ts": r["ts"], "event": r["event"]} for r in rows}


def reputation_band(value: float) -> str:
    for edge, name in REPUTATION_BANDS:
        if value < edge:
            return name
    return "Allied"


def _rank_row(track: str, label: str, rank: dict | None, progress: dict | None) -> dict:
    names = rank_names().get(track, [])
    r = int(rank["value"]) if rank else None
    p = int(progress["value"]) if progress else None
    at_max = r is not None and r >= MAX_RANK[track]
    promoted_since = bool(rank and progress and rank["ts"] > progress["ts"] and rank["event"] == "Promotion")
    return {
        "track": track,
        "label": label,
        "rank": r,
        "name": names[r] if r is not None and r < len(names) else ("—" if r is None else f"Rank {r}"),
        "next_name": names[r + 1] if r is not None and r + 1 < len(names) else None,
        "progress": 100 if at_max else p,
        "at_max": at_max,
        "promotion_available": bool(p is not None and p >= 100 and not at_max and not promoted_since),
        "promoted_since_login": promoted_since,
    }


def progress_view(conn: sqlite3.Connection, fid: str) -> dict:
    ranks = _latest(conn, fid, "rank") | _latest(conn, fid, "navy_rank")
    progress = _latest(conn, fid, "rank_progress")
    reputation = _latest(conn, fid, "reputation")
    as_of = conn.execute(
        "SELECT MAX(ts) FROM events WHERE fid = ? AND event = 'Progress'", (fid,)
    ).fetchone()[0]

    rep_rows = []
    for key, label in SUPERPOWERS:
        if key in reputation:
            v = reputation[key]["value"]
            rep_rows.append({"key": key, "label": label, "value": v, "band": reputation_band(v),
                             "pct": round((v + 100) / 2, 1)})

    return {
        "as_of": as_of,
        "pilots": [_rank_row(k, label, ranks.get(k), progress.get(k)) for k, label in PILOT_TRACKS],
        "navy": [_rank_row(k, label, ranks.get(k), progress.get(k)) for k, label in NAVY_TRACKS],
        "reputation": rep_rows,
        "engineers": engineers_view(conn, fid),
    }


@lru_cache(maxsize=1)
def engineer_locations() -> dict[str, dict]:
    with (paths.refdata_dir() / "engineer_locations.csv").open(newline="", encoding="utf-8") as f:
        return {row["engineer_id"]: row for row in csv.DictReader(f)}


def _journal_places(conn: sqlite3.Connection, fid: str, attrs: dict[str, dict]) -> dict[str, dict]:
    """Names the commander's own journal gives for each engineer's market ID and system address."""
    by_market = {a.get("market_id"): eid for eid, a in attrs.items() if a.get("market_id")}
    by_system = {a.get("system_address"): eid for eid, a in attrs.items() if a.get("system_address")}
    seen: dict[str, dict] = {}
    for raw in conn.execute(
        "SELECT raw_json FROM events WHERE fid = ? AND event IN ('Docked', 'Location', 'FSDJump', 'CarrierJump') "
        "ORDER BY ts", (fid,)):
        e = json.loads(raw[0])
        eid = by_system.get(e.get("SystemAddress"))
        if eid and e.get("StarSystem"):
            seen.setdefault(eid, {})["system"] = e["StarSystem"]
        eid = by_market.get(e.get("MarketID"))
        if eid and e.get("StationName"):
            seen.setdefault(eid, {})["station"] = e["StationName"]
            if e.get("StarSystem"):
                seen[eid]["system"] = e["StarSystem"]
    return seen


def engineers_view(conn: sqlite3.Connection, fid: str) -> dict[str, list[dict]]:
    stage = _latest(conn, fid, "engineer_stage")
    grade = _latest(conn, fid, "engineer_grade")
    prog = _latest(conn, fid, "engineer_progress")
    groups: dict[str, list[dict]] = {"ship": [], "onfoot": []}
    order = {"Unlocked": 0, "Acquainted": 1, "Invited": 2, "Known": 3, "Barred": 4, "Unknown": 5, "Undiscovered": 6}
    rows = conn.execute("SELECT symbol, display_name, attrs_json FROM ref_items WHERE kind = 'engineer'").fetchall()
    attrs_by_id = {r[0]: json.loads(r[2] or "{}") for r in rows}
    journal = _journal_places(conn, fid, attrs_by_id)
    curated = engineer_locations()
    for symbol, name, _attrs in rows:
        scope = attrs_by_id[symbol].get("engineer_scope") or "ship"
        loc = curated.get(symbol, {})
        seen = journal.get(symbol, {})
        system = seen.get("system") or loc.get("system")
        station = seen.get("station") or loc.get("station")
        verified = bool(seen) and all(seen.get(k, loc.get(k)) == loc.get(k) for k in ("system", "station"))
        moved = bool(seen) and not verified
        st = stage.get(symbol)
        state = STAGE_NAMES.get(int(st["value"]), "Unknown") if st else "Undiscovered"
        g = int(grade[symbol]["value"]) if symbol in grade and state == "Unlocked" else None
        p = int(prog[symbol]["value"]) if symbol in prog and state == "Unlocked" else None
        groups.setdefault(scope, []).append({
            "id": symbol, "name": name, "state": state, "grade": g,
            "progress": 100 if g == 5 else p, "max": g == 5,
            "system": system, "station": station, "verified": verified, "moved": moved,
            "seen_station": "station" in seen,
        })
    for rows in groups.values():
        rows.sort(key=lambda e: (order.get(e["state"], 9), -(e["grade"] or 0), e["name"]))
    return groups


def rank_strip(conn: sqlite3.Connection, fid: str) -> dict:
    view = progress_view(conn, fid)
    wealth = _latest(conn, fid, "wealth").get("total")
    merc = _latest(conn, fid, "merc_coins").get("balance")
    return {
        "ranks": view["pilots"] + view["navy"],
        "as_of": view["as_of"],
        "wealth": wealth["value"] if wealth else None,
        "merc_coins": merc["value"] if merc else None,
    }


# -- Overview -------------------------------------------------------------------

def _stat_events(conn: sqlite3.Connection, fid: str) -> list[tuple[str, dict]]:
    return [(ts, counters(json.loads(raw))) for ts, raw in conn.execute(
        "SELECT ts, raw_json FROM events WHERE fid = ? AND event = 'Statistics' ORDER BY ts, id", (fid,))]


def _baseline(events: list[tuple[str, dict]], period: str) -> tuple[str | None, dict | None]:
    """Counters at the last login before the period started (DESIGN §9.4)."""
    if period == "all" or len(events) < 2:
        return None, None
    if period == "session":
        return events[-2]
    days = {"7d": 7, "30d": 30}[period]
    latest = datetime.fromisoformat(events[-1][0].replace("Z", "+00:00"))
    start = (latest - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    before = [e for e in events if e[0] < start]
    return before[-1] if before else events[0]


def _sparks(conn: sqlite3.Connection, fid: str, since: str) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for key, value in conn.execute(
        "SELECT key, value FROM snapshots WHERE fid = ? AND metric = 'stat' AND date >= ? ORDER BY date",
        (fid, since),
    ):
        out.setdefault(key, []).append(value)
    return out


def spark_points(values: list[float], width: int = 80, height: int = 20) -> str | None:
    if len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = width / (len(values) - 1)
    return " ".join(f"{i * step:.1f},{height - (v - lo) / span * height:.1f}" for i, v in enumerate(values))


# Statistics sections the page leaves out (by counter prefix), mapped or not.
HIDDEN_SECTIONS = {"Material_Trader_Stats"}


def overview_view(conn: sqlite3.Connection, fid: str, period: str) -> dict:
    period = period if period in RANGES else "session"
    events = _stat_events(conn, fid)
    if not events:
        return {"period": period, "ranges": RANGES, "categories": [], "as_of": None, "baseline_ts": None}
    as_of, latest = events[-1]
    baseline_ts, baseline = _baseline(events, period)
    since = (datetime.fromisoformat(as_of.replace("Z", "+00:00")) - timedelta(days=SPARK_DAYS)).strftime("%Y-%m-%d")
    sparks = _sparks(conn, fid, since)
    mapping = stats_map()

    categories: dict[str, list[dict]] = {}
    for path, value in latest.items():
        meta = mapping.get(path)
        section = path.split(".", 1)[0]
        if section in HIDDEN_SECTIONS:
            continue
        category = meta["category"] if meta else section.replace("_", " ")
        delta = None if baseline is None else value - baseline.get(path, 0)
        categories.setdefault(category, []).append({
            "counter": path,
            "label": meta["label"] if meta else path,  # unmapped counters show their raw path
            "unit": meta["unit"] if meta else "count",
            "mapped": meta is not None,
            "value": value,
            "delta": delta,
            "spark": spark_points(sparks.get(path, [])),
        })
    order = list(dict.fromkeys(m["category"] for m in mapping.values()))
    ordered = sorted(categories.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else len(order))
    return {
        "period": period,
        "ranges": RANGES,
        "as_of": as_of,
        "baseline_ts": baseline_ts,
        "categories": [{"name": name, "rows": rows, "changed": sum(1 for r in rows if r["delta"])}
                       for name, rows in ordered],
    }
