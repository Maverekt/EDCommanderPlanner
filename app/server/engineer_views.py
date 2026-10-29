"""Engineer unlock chain and blueprint pins (DESIGN §16 Phase 5b).

Facts (who refers whom, what to meet, what to deliver) come from
refdata/engineer_unlocks.csv; the operator's plan (priority, tier, why,
recommended pins) from refdata/templates/engineers.json. Ship engineers refer
you onward once you reach grade 3 with them; on-foot engineers refer you after
their referral task.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache

from app.core import paths
from app.core.goals import parse_auto_rule
from app.server.goal_views import flag_states, _ranks, _rank_name, TRACK_LABELS
from app.server.views import engineers_view

REFERRAL_GRADE = 3


@lru_cache(maxsize=1)
def unlock_facts() -> dict[int, dict]:
    with (paths.refdata_dir() / "engineer_unlocks.csv").open(newline="", encoding="utf-8") as f:
        return {int(r["engineer_id"]): r | {"referred_by": [int(x) for x in r["referred_by"].split(";") if x.strip()]}
                for r in csv.DictReader(f)}


@lru_cache(maxsize=1)
def engineer_template() -> dict:
    return json.loads((paths.refdata_dir() / "templates" / "engineers.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def module_labels() -> dict[str, str]:
    with (paths.refdata_dir() / "module_type_names.csv").open(newline="", encoding="utf-8") as f:
        return {r["module_type"]: r["label"] for r in csv.DictReader(f)}


def pin_label(conn: sqlite3.Connection, module_type: str, blueprint: str, grade: int | None = None) -> str:
    row = conn.execute("SELECT display_name FROM ref_blueprints WHERE blueprint = ? AND display_name IS NOT NULL LIMIT 1",
                       (blueprint,)).fetchone()
    name = row[0] if row else blueprint
    return f"{module_labels().get(module_type, module_type)} — {name}" + (f" (G{grade})" if grade else "")


def pin_options(conn: sqlite3.Connection, engineer_id: int) -> list[dict]:
    rows = conn.execute("SELECT module_type, blueprint, max_grade FROM ref_engineer_blueprints WHERE engineer_id = ?",
                        (engineer_id,)).fetchall()
    out = [{"module_type": m, "blueprint": b, "grade": g, "label": pin_label(conn, m, b, g)} for m, b, g in rows]
    return sorted(out, key=lambda o: (-o["grade"], o["label"]))


def pins(conn: sqlite3.Connection, fid: str) -> dict[int, dict]:
    return {r[0]: {"module_type": r[1], "blueprint": r[2]} for r in conn.execute(
        "SELECT engineer_id, module_type, blueprint FROM engineer_pins WHERE fid = ?", (fid,))}


def set_pin(conn: sqlite3.Connection, fid: str, engineer_id: int, module_type: str | None, blueprint: str | None) -> None:
    """Pin a blueprint, or reset to the recommended pin when `blueprint` is None."""
    with conn:
        if blueprint is None:
            conn.execute("DELETE FROM engineer_pins WHERE fid = ? AND engineer_id = ?", (fid, engineer_id))
            return
        offered = conn.execute("SELECT 1 FROM ref_engineer_blueprints WHERE engineer_id = ? AND module_type = ? "
                               "AND blueprint = ?", (engineer_id, module_type, blueprint)).fetchone()
        if not offered:
            raise ValueError("that engineer doesn't offer this blueprint")
        conn.execute("INSERT INTO engineer_pins (fid, engineer_id, module_type, blueprint, updated_ts) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT(fid, engineer_id) DO UPDATE SET "
                     "module_type = excluded.module_type, blueprint = excluded.blueprint, updated_ts = excluded.updated_ts",
                     (fid, engineer_id, module_type, blueprint,
                      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))


def _meet(rule: str, flags: dict, ranks: dict) -> dict:
    if not rule:
        return {"met": None, "detail": None}
    if rule.startswith("flag:"):
        f = flags.get(rule[5:], {})
        return {"met": {"yes": True, "no": False}.get(f.get("value")), "detail": f.get("auto_detail")}
    _, track, need = parse_auto_rule(rule)
    have = ranks.get(track)
    if have is None:
        return {"met": None, "detail": "rank not seen yet"}
    return {"met": have >= need, "detail": f"{TRACK_LABELS.get(track, track)} {_rank_name(track, have)}"}


def chain_view(conn: sqlite3.Connection, fid: str) -> dict:
    facts = unlock_facts()
    tmpl = {e["engineer_id"]: e for e in engineer_template().get("engineers", [])}
    flags = flag_states(conn, fid)
    ranks = _ranks(conn, fid)
    my_pins = pins(conn, fid)
    cards = {int(e["id"]): e | {"scope": scope} for scope, group in engineers_view(conn, fid).items() for e in group}

    out = []
    for eid, card in cards.items():
        f = facts.get(eid, {})
        t = tmpl.get(eid, {})
        refs = [cards[r] for r in f.get("referred_by", []) if r in cards]
        onfoot = card["scope"] == "onfoot"
        referred = (not f.get("referred_by")) or any(
            (r["state"] == "Unlocked" and (onfoot or (r["grade"] or 0) >= REFERRAL_GRADE)) for r in refs)
        meet = _meet(f.get("meet_rule", ""), flags, ranks)
        permit = flags.get(f.get("permit") or "", None)
        rec = t.get("pin")
        mine = my_pins.get(eid)
        current = mine or rec
        entry = {
            **card, "engineer_id": eid, "onfoot": onfoot,
            "priority": t.get("priority"), "tier": t.get("tier"), "why": t.get("why"), "tip": t.get("tip"),
            "shortcut": t.get("shortcut"), "pin_note": t.get("pin_note"),
            "referred_by": [{"id": r["id"], "name": r["name"], "state": r["state"], "grade": r["grade"]} for r in refs],
            "referred": referred, "meet_text": f.get("meet"), "meet": meet, "unlock": f.get("unlock"),
            "referral_task": f.get("referral_task"),
            "permit": {"label": permit["label"], "value": permit["value"]} if permit else None,
            "pin": current, "pin_is_recommended": mine is None or bool(rec and mine == {k: rec[k] for k in mine}),
            "pin_label": pin_label(conn, current["module_type"], current["blueprint"]) if current else None,
            "recommended": rec, "recommended_label": pin_label(conn, rec["module_type"], rec["blueprint"], rec["grade"])
            if rec else None,
            "alt_label": pin_label(conn, t["alt_pin"]["module_type"], t["alt_pin"]["blueprint"], t["alt_pin"]["grade"])
            if t.get("alt_pin") else None,
            "options": pin_options(conn, eid),
        }
        entry["next_step"] = _next_step(entry)
        out.append(entry)

    planned = sorted((e for e in out if e["priority"] is not None), key=lambda e: e["priority"])
    others = sorted((e for e in out if e["priority"] is None),
                    key=lambda e: (e["onfoot"], {"Unlocked": 0, "Invited": 1, "Known": 2}.get(e["state"], 3), e["name"]))
    nxt = next((e for e in planned if not e["max"]), None)
    tiers: list[tuple[str, list]] = []
    for e in planned:
        if not tiers or tiers[-1][0] != e["tier"]:
            tiers.append((e["tier"], []))
        tiers[-1][1].append(e)
    return {"tiers": tiers, "others": others, "next": nxt, "all": out, "template_name": engineer_template().get("name"),
            "maxed": sum(1 for e in out if e["max"]), "unlocked": sum(1 for e in out if e["state"] == "Unlocked")}


def _next_step(e: dict) -> str:
    where = f"{e['station']}, {e['system']}" if e.get("station") else (e.get("system") or "the workshop")
    if e["state"] == "Unlocked":
        if e["max"]:
            return "Grade 5 — maxed"
        g = e["grade"] or 1
        pct = f" ({e['progress']}%)" if e.get("progress") is not None else ""
        how = "craft at the workshop or roll your pinned blueprint remotely" if not e["onfoot"] else "upgrade at the workshop"
        return f"Rank up to G{g + 1}{pct}: {how}"
    if e["state"] == "Barred":
        return "Barred"
    if e["permit"] and e["permit"]["value"] != "yes":
        permit = f"Get the {e['permit']['label']}"
    else:
        permit = None
    if e["state"] == "Invited":
        step = f"Unlock: deliver {e['unlock']} at {where}" if e.get("unlock") else f"Unlock at {where}"
        return f"{permit}, then {step[0].lower()}{step[1:]}" if permit else step
    if e["state"] == "Known" or e["state"] == "Acquainted":
        if e["meet"]["met"] is False or e["meet"]["met"] is None:
            detail = f" ({e['meet']['detail']})" if e["meet"].get("detail") else ""
            return f"Meet the requirement: {e['meet_text']}{detail}" if e.get("meet_text") else "Meet the invite requirement"
        nxt = f"deliver {e['unlock']} at {where}" if e.get("unlock") else f"visit {where}"
        return f"Requirement met: the invitation should show after your next login, then {nxt}"
    # Undiscovered
    if not e["referred"]:
        r = e["referred_by"][0] if e["referred_by"] else None
        if r and e["onfoot"]:
            return f"Get referred by {r['name']}: unlock them and complete their referral task"
        if r:
            have = f"G{r['grade']}" if r["state"] == "Unlocked" and r["grade"] else r["state"].lower()
            return f"Get referred: rank {r['name']} to G{REFERRAL_GRADE} (now {have})"
    return f"Discover: visit {where} or check the engineer panel"
