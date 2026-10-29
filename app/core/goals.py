"""Goal model and rules (DESIGN §16 Phase 5a). Stdlib only: the refdata build validates templates with it.

A goal row (table `goals`) is user-authored and never rebuilt. Its kind decides
how progress is read from the journal:

  ship           target_key = ship symbol (or null if the ship isn't in FDevIDs yet)
  fleet_carrier  owned once any Carrier* event appears
  rank           target_key = rank track, target_value = minimum rank
  credits        target_value = balance to reach
  checklist      detail_json.steps, ticked by hand

`detail_json` holds title-level detail (build, runner-up, requirements, steps);
`requirements_json` holds costs: {"credits": n, "merc_coins": n}.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GOAL_KINDS = {"ship", "fleet_carrier", "rank", "credits", "checklist"}
STATUSES = ("active", "done", "parked")
REPUTATION_KEYS = {"federation", "empire", "alliance", "independent"}
FLAG_VALUES = ("yes", "no", "unknown")
DEFAULT_RESERVE_REBUYS = 3
_RULE = re.compile(r"^(stat|rep|rank):([A-Za-z0-9_.]+)\s*>=\s*(-?\d+(?:\.\d+)?)$")
_HEADER = re.compile(r"^header:([A-Za-z0-9_]+)$")


def parse_auto_rule(text: str) -> tuple[str, str, float]:
    """`stat:Section.Counter>=n`, `rep:empire>=35`, `rank:empire>=7`, or `header:Field`
    (true when the newest journal's Fileheader has that field set, e.g. `header:Odyssey`)."""
    text = text.strip()
    m = _HEADER.match(text)
    if m:
        return "header", m.group(1), 1.0
    m = _RULE.match(text)
    if not m:
        raise ValueError(f"bad auto rule {text!r}")
    return m.group(1), m.group(2), float(m.group(3))


# Checklist step rules (DESIGN §16 5e). A rule is OR-groups of AND-terms: "fitted:seismic & fitted:pwa | owned:x".
STEP_GEAR = {"laser_tool", "abrasion", "seismic", "ssdm", "pwa", "refinery", "prospector", "collector", "dss",
             "fuelscoop", "afmu", "guardian_booster", "heatsink", "srv"}
_STEP_KEY = re.compile(r"^(flag|owned|fitted|engineer):([A-Za-z0-9_]+)$")
_STEP_COUNT = re.compile(r"^(cargo|mined|suit|engineer):([a-z0-9_]+)\s*>=\s*(\d+)$")


def parse_step_rule(text: str) -> list[list[tuple[str, str, float]]]:
    """`a & b | c` -> [[a, b], [c]]; each term (kind, key, n). Terms: stat/rep/rank/header (as flags),
    flag:KEY, owned:<ship>, fitted:<gear>, engineer:<id> (unlocked) or engineer:<id>>=grade,
    cargo:<commodity>>=n, mined:<commodity>>=n (refined tons), suit:artemis>=grade."""
    groups = []
    for part in str(text).split("|"):
        terms = []
        for raw in part.split("&"):
            raw = raw.strip()
            if not raw:
                raise ValueError(f"empty term in step rule {text!r}")
            m = _STEP_COUNT.match(raw)
            if m:
                terms.append((m.group(1), m.group(2), float(m.group(3))))
                continue
            m = _STEP_KEY.match(raw)
            if m:
                if m.group(1) == "fitted" and m.group(2) not in STEP_GEAR:
                    raise ValueError(f"unknown gear {m.group(2)!r} in step rule {text!r}")
                terms.append((m.group(1), m.group(2), 1.0))
                continue
            terms.append(parse_auto_rule(raw))
        groups.append(terms)
    return groups


def step_texts(steps: list) -> tuple[list[str], dict[str, str]]:
    """Template steps (strings or {"text", "auto"}) -> (texts, {text: rule})."""
    texts, rules = [], {}
    for st in steps or []:
        if isinstance(st, dict):
            texts.append(st["text"])
            if st.get("auto"):
                rules[st["text"]] = st["auto"]
        else:
            texts.append(st)
    return texts, rules


@dataclass(frozen=True)
class FlagDef:
    key: str
    label: str
    auto: str | None
    affects: str


def load_flags(path: Path) -> dict[str, FlagDef]:
    with path.open(newline="", encoding="utf-8") as f:
        return {r["key"].strip(): FlagDef(r["key"].strip(), r["label"].strip(), r["auto"].strip() or None,
                                          r["affects"].strip()) for r in csv.DictReader(f)}


def load_template(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- storage -------------------------------------------------------------------------

def goal_from_template(g: dict) -> dict:
    """Template entry → the columns of a goal row."""
    detail = {k: g[k] for k in ("build", "runner_up", "ship_label", "cost_note", "detail") if g.get(k)}
    detail["requires"] = g.get("requires", [])
    if g.get("steps"):
        detail["steps"], rules = step_texts(g["steps"])
        detail["steps_done"] = []
        if rules:
            detail["step_rules"] = rules
    if g.get("activity"):
        detail["activity"] = g["activity"]
    costs = {"credits": int(g["cost"])} if g.get("cost") else {}
    target_key = g.get("ship") if g["kind"] == "ship" else g.get("target_key")
    if g["kind"] == "fleet_carrier":
        target_key = "fleet_carrier"
    return {
        "kind": g["kind"], "title": g["title"], "priority": g["priority"], "target_key": target_key or "",
        "target_value": g.get("target_value"), "requirements_json": json.dumps(costs) if costs else None,
        "detail_json": json.dumps(detail), "template_key": g.get("key"),
    }


def insert_goal(conn: sqlite3.Connection, fid: str, cols: dict) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO goals (fid, kind, target_key, target_value, requirements_json, created_ts, due_date, notes, "
        "updated_ts, title, priority, status, depends_on, detail_json, template_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
        (fid, cols["kind"], cols.get("target_key") or "", cols.get("target_value"), cols.get("requirements_json"),
         now, cols.get("due_date"), cols.get("notes"), now, cols["title"], cols.get("priority"),
         cols.get("depends_on"), cols.get("detail_json"), cols.get("template_key")),
    )
    return cur.lastrowid


def load_template_goals(conn: sqlite3.Connection, fid: str, template: dict) -> int:
    """Add the template's goals after any existing ones; skips goals already loaded from it."""
    existing = {r[0] for r in conn.execute("SELECT template_key FROM goals WHERE fid = ? AND template_key IS NOT NULL",
                                           (fid,))}
    base = conn.execute("SELECT COALESCE(MAX(priority), 0) FROM goals WHERE fid = ?", (fid,)).fetchone()[0]
    added = 0
    with conn:
        for g in sorted(template["goals"], key=lambda g: g["priority"]):
            if g["key"] in existing:
                continue
            cols = goal_from_template(g)
            cols["priority"] = base + g["priority"]
            insert_goal(conn, fid, cols)
            added += 1
    return added


def renumber(conn: sqlite3.Connection, fid: str) -> None:
    ids = [r[0] for r in conn.execute("SELECT id FROM goals WHERE fid = ? ORDER BY priority, id", (fid,))]
    conn.executemany("UPDATE goals SET priority = ? WHERE id = ?", [(i + 1, gid) for i, gid in enumerate(ids)])


def move(conn: sqlite3.Connection, fid: str, goal_id: int, delta: int) -> None:
    with conn:
        renumber(conn, fid)
        ids = [r[0] for r in conn.execute("SELECT id FROM goals WHERE fid = ? ORDER BY priority", (fid,))]
        if goal_id not in ids:
            return
        i = ids.index(goal_id)
        j = max(0, min(len(ids) - 1, i + delta))
        ids[i], ids[j] = ids[j], ids[i]
        conn.executemany("UPDATE goals SET priority = ?, updated_ts = ? WHERE id = ?",
                         [(n + 1, _now(), gid) for n, gid in enumerate(ids)])


def get_flags(conn: sqlite3.Connection, fid: str) -> dict[str, str]:
    return {k: v for k, v in conn.execute("SELECT key, value FROM flags WHERE fid = ?", (fid,))}


def set_flag(conn: sqlite3.Connection, fid: str, key: str, value: str) -> None:
    if value not in FLAG_VALUES:
        raise ValueError(f"flag value must be one of {FLAG_VALUES}")
    with conn:
        if value == "unknown":
            conn.execute("DELETE FROM flags WHERE fid = ? AND key = ?", (fid, key))
        else:
            conn.execute("INSERT INTO flags (fid, key, value, updated_ts) VALUES (?, ?, ?, ?) "
                         "ON CONFLICT(fid, key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
                         (fid, key, value, _now()))


# -- edits from the Goals page ------------------------------------------------------------

def _clean_requires(raw) -> list[dict]:
    out = []
    for r in raw or []:
        if isinstance(r, dict) and r.get("flag"):
            out.append({"flag": str(r["flag"])})
        elif isinstance(r, dict) and r.get("rank") and str(r.get("min", "")).lstrip("-").isdigit():
            out.append({"rank": str(r["rank"]), "min": int(r["min"])})
    return out


def columns_from_form(payload: dict, existing: dict | None = None) -> dict:
    """Validated goal columns from a create/update payload; `existing` is the current row for updates."""
    kind = existing["kind"] if existing else payload.get("kind")
    if kind not in GOAL_KINDS:
        raise ValueError(f"kind must be one of {sorted(GOAL_KINDS)}")
    detail = json.loads(existing["detail_json"] or "{}") if existing else {"requires": []}
    costs = json.loads(existing["requirements_json"] or "{}") if existing else {}
    cols: dict = {}
    title = payload.get("title", existing["title"] if existing else None)
    if not title or not str(title).strip():
        raise ValueError("title is required")
    cols["title"] = str(title).strip()[:120]
    for key in ("build", "runner_up", "ship_label", "cost_note"):
        if key in payload:
            value = str(payload[key] or "").strip()
            if value:
                detail[key] = value
            else:
                detail.pop(key, None)
    if "requires" in payload:
        detail["requires"] = _clean_requires(payload["requires"])
    if "steps" in payload:
        steps = [s.strip() for s in (payload["steps"] if isinstance(payload["steps"], list)
                                     else str(payload["steps"]).splitlines()) if s and s.strip()]
        detail["steps"] = steps
        detail["steps_done"] = [s for s in detail.get("steps_done", []) if s in steps]
    if "cost" in payload:
        cost = str(payload["cost"] or "0").replace(",", "").strip()
        if not cost.isdigit():
            raise ValueError("cost must be a whole number of credits")
        if int(cost):
            costs["credits"] = int(cost)
        else:
            costs.pop("credits", None)
    if "notes" in payload:
        cols["notes"] = str(payload["notes"] or "").strip() or None
    if not existing or "target_key" in payload:
        target_key = str(payload.get("target_key") or "").strip()
        if kind == "fleet_carrier":
            target_key = "fleet_carrier"
        if kind == "rank" and not target_key:
            raise ValueError("pick a rank track")
        cols["target_key"] = target_key
    if "target_value" in payload:
        tv = str(payload["target_value"] or "").replace(",", "").strip()
        cols["target_value"] = float(tv) if tv else None
    cols["kind"] = kind
    cols["detail_json"] = json.dumps(detail)
    cols["requirements_json"] = json.dumps(costs) if costs else None
    return cols


def update_goal(conn: sqlite3.Connection, goal_id: int, cols: dict) -> None:
    allowed = ("title", "notes", "target_key", "target_value", "detail_json", "requirements_json")
    sets = {k: v for k, v in cols.items() if k in allowed}
    with conn:
        conn.execute(f"UPDATE goals SET {', '.join(f'{k} = ?' for k in sets)}, updated_ts = ? WHERE id = ?",
                     (*sets.values(), _now(), goal_id))


def set_status(conn: sqlite3.Connection, goal_id: int, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    with conn:
        conn.execute("UPDATE goals SET status = ?, updated_ts = ? WHERE id = ?", (status, _now(), goal_id))


def toggle_step(conn: sqlite3.Connection, goal_id: int, step: str, done: bool) -> None:
    row = conn.execute("SELECT detail_json FROM goals WHERE id = ?", (goal_id,)).fetchone()
    detail = json.loads(row[0] or "{}")
    steps_done = [s for s in detail.get("steps_done", []) if s != step]
    if done and step in detail.get("steps", []):
        steps_done.append(step)
    detail["steps_done"] = steps_done
    with conn:
        conn.execute("UPDATE goals SET detail_json = ?, updated_ts = ? WHERE id = ?", (json.dumps(detail), _now(), goal_id))


def delete_goal(conn: sqlite3.Connection, fid: str, goal_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM goals WHERE id = ? AND fid = ?", (goal_id, fid))
        renumber(conn, fid)
