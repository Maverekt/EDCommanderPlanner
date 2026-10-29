"""Goals evaluated against the journal: flags, requirements, ownership, funding (DESIGN §16 Phase 5a).

Funding follows the operator's workbook:
  available   = credits on hand + pending sales − reserve
  reserve     = N × the biggest rebuy among owned ships (N a setting, default 3)
  remaining   = cost if not owned; cost − current value if owned; 0 when done
  cumulative  = remaining of this goal + every higher-priority goal still open
  fundable    = requirements met and cumulative ≤ available
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from functools import lru_cache

from app.core import db, paths
from app.core.goals import (DEFAULT_RESERVE_REBUYS, FlagDef, load_flags, load_template, parse_auto_rule,
                             parse_step_rule, step_texts)
from app.core.normalize import normalize_symbol
from app.core.projectors.statistics import counters
from app.server.queries import display_name
from app.server.views import NAVY_TRACKS, PILOT_TRACKS, _latest, engineers_view, progress_view, rank_names

TRACK_LABELS = dict(PILOT_TRACKS + NAVY_TRACKS)


@lru_cache(maxsize=1)
def flag_defs() -> dict[str, FlagDef]:
    return load_flags(paths.refdata_dir() / "flags.csv")


@lru_cache(maxsize=1)
def template() -> dict:
    return load_template(paths.refdata_dir() / "templates" / "goals.json")


def reserve_rebuys(conn: sqlite3.Connection) -> int:
    v = db.get_setting(conn, "reserve_rebuys")
    return int(v) if v and v.isdigit() else DEFAULT_RESERVE_REBUYS


def selling_ships(conn: sqlite3.Connection, fid: str) -> set[int]:
    return set(json.loads(db.get_setting(conn, f"selling_ships:{fid}") or "[]"))


# -- inputs from the journal ---------------------------------------------------------

def _latest_counters(conn, fid) -> dict[str, float]:
    row = conn.execute("SELECT raw_json FROM events WHERE fid = ? AND event = 'Statistics' ORDER BY ts DESC, id DESC "
                       "LIMIT 1", (fid,)).fetchone()
    return counters(json.loads(row[0])) if row else {}


def _ranks(conn, fid) -> dict[str, int]:
    return {k: int(v["value"]) for k, v in (_latest(conn, fid, "rank") | _latest(conn, fid, "navy_rank")).items()}


def fleet(conn: sqlite3.Connection, fid: str) -> list[dict]:
    """Owned ships with value and rebuy from each ship's latest Loadout."""
    selling = selling_ships(conn, fid)
    out = []
    for s in conn.execute("SELECT * FROM ships WHERE fid = ? AND disposed_ts IS NULL ORDER BY ship_id", (fid,)):
        row = conn.execute(
            "SELECT raw_json FROM events WHERE fid = ? AND event = 'Loadout' "
            "AND json_extract(raw_json, '$.ShipID') = ? ORDER BY ts DESC, id DESC LIMIT 1", (fid, s["ship_id"])).fetchone()
        lo = json.loads(row[0]) if row else {}
        value = (lo.get("HullValue") or 0) + (lo.get("ModulesValue") or 0) if lo else None
        out.append({
            "ship_id": s["ship_id"], "ship_type": s["ship_type"], "name": s["name"], "ident": s["ident"],
            "type_name": display_name(conn, s["ship_type"], None), "value": value, "rebuy": lo.get("Rebuy"),
            "selling": s["ship_id"] in selling,
        })
    return out


def flag_states(conn: sqlite3.Connection, fid: str) -> dict[str, dict]:
    manual = {k: v for k, v in conn.execute("SELECT key, value FROM flags WHERE fid = ?", (fid,))}
    stats = _latest_counters(conn, fid)
    rep = {k: v["value"] for k, v in _latest(conn, fid, "reputation").items()}
    ranks = _ranks(conn, fid)
    header_row = conn.execute("SELECT raw_json FROM events WHERE fid = ? AND event = 'Fileheader' "
                              "ORDER BY ts DESC, id DESC LIMIT 1", (fid,)).fetchone()
    header = {k: 1.0 if v else 0.0 for k, v in json.loads(header_row[0]).items() if isinstance(v, bool)} \
        if header_row else {}
    out = {}
    for key, d in flag_defs().items():
        auto, auto_detail = None, None
        if d.auto:
            kind, target, threshold = parse_auto_rule(d.auto)
            current = {"stat": stats, "rep": rep, "rank": ranks, "header": header}[kind].get(target)
            if current is not None:
                auto = current >= threshold
                if kind == "rank":
                    auto_detail = f"{_rank_name(target, int(current))}, needs {_rank_name(target, int(threshold))}"
                elif kind == "header":
                    auto_detail = "from your journal header"
                else:
                    auto_detail = f"{current:,.0f} of {threshold:,.0f}"
        value = manual.get(key) or ({True: "yes", False: "no"}.get(auto) if auto is not None else "unknown")
        out[key] = {"key": key, "label": d.label, "affects": d.affects, "value": value,
                    "source": "manual" if key in manual else ("auto" if auto is not None else "unset"),
                    "auto_rule": d.auto, "auto_detail": auto_detail, "auto": auto}
    return out


def _rank_name(track: str, rank: int) -> str:
    names = rank_names().get(track, [])
    return names[rank] if 0 <= rank < len(names) else f"rank {rank}"


# -- evaluation -----------------------------------------------------------------------

class StepFacts:
    """What checklist step rules are checked against, read lazily once per plan view (DESIGN §16 5e)."""

    def __init__(self, conn: sqlite3.Connection, fid: str, flags: dict, ranks: dict, cargo_file=None):
        self.conn, self.fid, self.flags, self.ranks, self.cargo_file = conn, fid, flags, ranks, cargo_file
        self._cache: dict = {}

    def _get(self, key, make):
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    def term(self, kind: str, key: str, n: float, activity: str | None) -> bool:
        from app.server import activity_views as av   # deferred: activity_views → engineer_views → this module
        c, fid = self.conn, self.fid
        if kind in ("stat", "rep", "rank", "header"):
            values = {"stat": lambda: _latest_counters(c, fid),
                      "rep": lambda: {k: v["value"] for k, v in _latest(c, fid, "reputation").items()},
                      "rank": lambda: self.ranks, "header": lambda: {}}[kind]
            have = self._get(kind, values).get(key)
            return have is not None and have >= n
        if kind == "flag":
            return self.flags.get(key, {}).get("value") == "yes"
        if kind == "owned":
            return key in self._get("owned", lambda: {r[0] for r in c.execute(
                "SELECT ship_type FROM ships WHERE fid = ? AND disposed_ts IS NULL", (fid,))})
        if kind == "fitted":
            def gear():
                if activity in av.ACTIVITIES:
                    return set(av.gear(c, fid, av.activity_ship(c, fid, activity)))
                return {k for (sid,) in c.execute("SELECT ship_id FROM ships WHERE fid = ? AND disposed_ts IS NULL", (fid,))
                        for k in av.gear(c, fid, sid)}
            return key in self._get(("gear", activity), gear)
        if kind == "cargo":
            return self._get("cargo", lambda: av._cargo(c, fid, self.cargo_file)).get(key, 0) >= n
        if kind == "mined":
            mined = self._get("mined", lambda: Counter(normalize_symbol(r[0]) for r in c.execute(
                "SELECT json_extract(raw_json, '$.Type') FROM events WHERE fid = ? AND event = 'MiningRefined'", (fid,))))
            return mined.get(key, 0) >= n
        if kind == "engineer":
            states = self._get("engineers", lambda: {e["id"]: e for g in engineers_view(c, fid).values() for e in g})
            e = states.get(key)
            return bool(e) and e["state"] == "Unlocked" and (e["grade"] or 1) >= n
        if kind == "suit":
            return (self._get("artemis", lambda: av.artemis_grade(c, fid)) or 0) >= n
        return False

    def met(self, rule: str, activity: str | None) -> bool:
        return any(all(self.term(k, key, n, activity) for k, key, n in group) for group in parse_step_rule(rule))


def step_rules(detail: dict, template_key: str | None) -> dict[str, str]:
    """A checklist's step rules: its own, then the template's for steps with the same text."""
    rules = dict(detail.get("step_rules") or {})
    if template_key:
        tmpl = next((g for g in template().get("goals", []) if g.get("key") == template_key), None)
        if tmpl:
            rules = step_texts(tmpl.get("steps", []))[1] | rules
    return rules

def _requirement(req: dict, flags: dict, ranks: dict, promo: dict) -> dict:
    if "flag" in req:
        f = flags.get(req["flag"], {"label": req["flag"], "value": "unknown"})
        return {"label": f["label"], "met": f["value"] == "yes", "state": f["value"], "flag": req["flag"]}
    if "rank" in req:
        track, need = req["rank"], int(req["min"])
        have = ranks.get(track)
        need_name = "Elite (any tier)" if need == 8 and track not in ("empire", "federation") else _rank_name(track, need)
        label = f"{TRACK_LABELS.get(track, track)} {need_name}"
        if have is None:
            return {"label": label, "met": False, "state": "unknown", "detail": "rank not seen yet"}
        met = have >= need  # any Elite tier counts as Elite
        detail = None if met else f"{need - have} to go"
        if not met and promo.get(track):
            detail += " · promotion available now"
        return {"label": label, "met": met, "state": "yes" if met else "no", "detail": detail,
                "have": _rank_name(track, have)}
    return {"label": json.dumps(req), "met": False, "state": "unknown"}


def _list_price(conn: sqlite3.Connection, ship: str) -> int | None:
    row = conn.execute("SELECT attrs_json FROM ref_items WHERE symbol = ? AND kind = 'ship'", (ship,)).fetchone()
    return json.loads(row[0] or "{}").get("retail_cost") if row else None


def plan_view(conn: sqlite3.Connection, fid: str, balance: float | None, cargo_file=None) -> dict:
    flags = flag_states(conn, fid)
    ranks = _ranks(conn, fid)
    facts = StepFacts(conn, fid, flags, ranks, cargo_file)
    auto_steps, rules = set(), {}
    pv = progress_view(conn, fid)
    promo = {r["track"]: r["promotion_available"] for r in pv["pilots"] + pv["navy"]}
    ships = fleet(conn, fid)
    owned_types: dict[str, dict] = {}
    for s in ships:
        best = owned_types.get(s["ship_type"])
        if best is None or (s["value"] or 0) > (best["value"] or 0):
            owned_types[s["ship_type"]] = s
    carrier = conn.execute("SELECT 1 FROM events WHERE fid = ? AND event LIKE 'Carrier%' LIMIT 1", (fid,)).fetchone()

    n = reserve_rebuys(conn)
    rebuys = [(s["rebuy"], s) for s in ships if s["rebuy"]]
    biggest = max(rebuys, key=lambda r: r[0]) if rebuys else (0, None)
    reserve = n * biggest[0]
    pending = sum(s["value"] or 0 for s in ships if s["selling"])
    credits = balance or 0
    available = credits + pending - reserve
    fleet_value = sum(s["value"] or 0 for s in ships)

    from app.server.build_views import by_goal   # deferred: build_views → engineer_views → this module
    attached = by_goal(conn, fid)

    goals, cumulative, done_count = [], 0, 0
    for g in conn.execute("SELECT * FROM goals WHERE fid = ? ORDER BY priority, id", (fid,)):
        detail = json.loads(g["detail_json"] or "{}")
        costs = json.loads(g["requirements_json"] or "{}")
        cost = costs.get("credits") or 0
        reqs = [_requirement(r, flags, ranks, promo) for r in detail.get("requires", [])]
        reqs_met = all(r["met"] for r in reqs)
        kind = g["kind"]
        owned, value, progress, auto_done = None, None, None, False
        build = attached.get(g["id"]) if kind == "ship" else None
        if build:
            cost = build["total_value"]
        if kind == "ship":
            ship = owned_types.get(g["target_key"]) if g["target_key"] else None
            owned, value = ship is not None, (ship["value"] if ship else None)
            auto_done = bool(build and build["done"])   # the compared ship now matches its build
        elif kind == "fleet_carrier":
            owned = bool(carrier)
        elif kind == "rank":
            have, need = ranks.get(g["target_key"]), int(g["target_value"] or 0)
            auto_done = have is not None and have >= need
            progress = min(1.0, (have or 0) / need) if need else None
        elif kind == "credits":
            need = g["target_value"] or 0
            auto_done = credits >= need > 0
            progress = min(1.0, credits / need) if need else None
        elif kind == "checklist":
            steps = detail.get("steps", [])
            rules = step_rules(detail, g["template_key"])
            auto_steps = {st for st in steps if st in rules and facts.met(rules[st], detail.get("activity"))}
            done_steps = set(detail.get("steps_done", [])) | auto_steps
            detail["steps_done"] = [st for st in steps if st in done_steps]   # this view only; not saved
            auto_done = bool(steps) and all(s in done_steps for s in steps)
            progress = len(done_steps & set(steps)) / len(steps) if steps else None

        done = g["status"] == "done" or auto_done
        parked = g["status"] == "parked"
        price_unknown = not cost and kind in ("ship", "fleet_carrier") and bool(detail.get("cost_note"))
        if done or parked or kind == "credits" or kind == "rank":
            remaining = 0
        elif build and build["ship_id"]:
            remaining = build["to_buy"]          # exact: modules the compared ship doesn't have yet
        elif owned and kind == "ship":
            remaining = max(0, cost - (value or 0))
        elif owned:
            remaining = 0
        else:
            remaining = cost
        if not done and not parked:
            cumulative += remaining
        done_count += done
        before = cumulative - remaining
        funded = 1.0 if remaining == 0 else max(0.0, min(1.0, (available - before) / remaining))
        fundable = reqs_met and remaining > 0 and cumulative <= available

        name = (display_name(conn, g["target_key"], None) if kind == "ship" and g["target_key"]
                else detail.get("ship_label") or detail.get("detail") or ("Fleet Carrier" if kind == "fleet_carrier" else ""))
        goals.append({
            "id": g["id"], "priority": g["priority"], "kind": kind, "title": g["title"], "name": name,
            "status": g["status"], "done": done, "auto_done": auto_done, "parked": parked,
            "build": detail.get("build"), "runner_up": detail.get("runner_up"), "cost_note": detail.get("cost_note"),
            "steps": detail.get("steps", []),
            "steps_done": list(set(detail.get("steps_done", [])) | (auto_steps if kind == "checklist" else set())),
            "steps_auto": sorted(auto_steps) if kind == "checklist" else [],
            "steps_ruled": sorted(rules) if kind == "checklist" else [],
            "requires": reqs, "reqs_met": reqs_met, "owned": owned, "value": value, "cost": cost,
            "build_plan": build,
            "list_price": _list_price(conn, g["target_key"]) if kind == "ship" and g["target_key"] else None,
            "remaining": remaining, "cumulative": cumulative if not (done or parked) else None,
            "funded": funded, "fundable": fundable, "price_unknown": price_unknown, "progress": progress,
            "target_key": g["target_key"], "target_value": g["target_value"], "notes": g["notes"],
            "next_step": _next_step(kind, name, done, parked, owned, reqs, remaining, cumulative, available,
                                    price_unknown, detail),
        })

    open_goals = [x for x in goals if not x["done"] and not x["parked"]]
    return {
        "goals": goals,
        "summary": {
            "credits": credits, "pending": pending, "reserve": reserve, "reserve_n": n,
            "reserve_ship": biggest[1], "biggest_rebuy": biggest[0], "available": available,
            "fleet_value": fleet_value, "net_worth": credits + fleet_value,
            "still_needed": sum(x["remaining"] for x in open_goals),
            "done": done_count, "total": len(goals),
            "next_goal": open_goals[0] if open_goals else None,
        },
        "fleet": ships, "flags": flags,
        "imperial_step": _imperial_step(pv, goals),
        "has_template": bool(template().get("goals")),
        "new_examples": [t["title"] for t in template().get("goals", []) if t.get("key") not in
                         {r[0] for r in conn.execute("SELECT template_key FROM goals WHERE fid = ? AND template_key IS NOT NULL",
                                                     (fid,))}],
        "template_name": template().get("name"),
    }


def _money(v: float) -> str:
    return f"{v:,.0f} cr"


def _next_step(kind, name, done, parked, owned, reqs, remaining, cumulative, available, price_unknown, detail) -> str:
    if done:
        return "Done"
    if parked:
        return "Parked"
    unmet = [r for r in reqs if not r["met"]]
    if unmet:
        r = unmet[0]
        return f"Unlock: {r['label']}" + (f" ({r['detail']})" if r.get("detail") else "")
    if kind == "checklist":
        left = [s for s in detail.get("steps", []) if s not in set(detail.get("steps_done", []))]
        return left[0] if left else "Mark done when complete"
    if kind == "rank":
        return "Rank up"
    if kind == "credits":
        return "Earn credits"
    if price_unknown:
        return "Price not announced yet"
    if owned and kind == "ship":
        return f"Finish fitting/engineering: {name}" + (f" ({_money(remaining)} to go)" if remaining else "")
    if owned:
        return "Owned"
    if cumulative <= available:
        return f"Go buy/fit: {name}"
    return f"Earn {_money(cumulative - available)} more (incl. higher priorities)"


def _imperial_step(pv: dict, goals: list[dict]) -> str | None:
    empire = next((r for r in pv["navy"] if r["track"] == "empire"), None)
    if empire is None or empire["rank"] is None:
        return None
    if empire["promotion_available"]:
        step = f"Promotion ready: take an Imperial Navy promotion mission ({empire['name']} → {empire['next_name']})"
    elif empire["at_max"]:
        return "Imperial Navy: King (max)"
    else:
        step = f"Imperial Navy {empire['name']}, {empire['progress'] or 0}% to {empire['next_name']}"
    needs = [r for g in goals if not g["done"] for r in g["requires"] if r["label"].startswith("Imperial Navy")
             and not r["met"]]
    if needs:
        step += f" · {needs[0]['label']}: {needs[0].get('detail')}"
    return step
