"""Engineering plan (DESIGN §16 Phase 5c): blueprint × modules × target grade × experimental → materials.

A plan entry (table `eng_plan`) says "put this blueprint on N modules up to grade G,
optionally with this experimental". Its material need is, for every grade above
`done_grade` up to the target:

    recipe quantity × rolls for that grade × modules

plus the experimental recipe once per module. Rolls per grade are an estimate
(the workbook's 2, 2, 3, 4, 5): grade progress depends on roll quality, which
the journal doesn't expose ahead of time. They are a setting.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

DEFAULT_ROLLS = (2, 2, 3, 4, 5)
ROLLS_SETTING = "eng_rolls"
FIELDS = ("module_type", "blueprint", "modules", "target_grade", "done_grade", "experimental", "include", "note")


def parse_rolls(text: str | None) -> tuple[int, ...]:
    if not text:
        return DEFAULT_ROLLS
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 5 or not all(p.isdigit() and 1 <= int(p) <= 50 for p in parts):
        raise ValueError("rolls per grade must be five whole numbers from 1 to 50, e.g. 2,2,3,4,5")
    return tuple(int(p) for p in parts)


def entry_needs(recipes: dict[tuple[str, str, int], dict[str, int]], entry: dict,
                rolls: tuple[int, ...]) -> dict[str, int]:
    """Materials one entry still needs. `recipes` maps (kind, recipe, grade) → {material: qty}."""
    need: dict[str, int] = defaultdict(int)
    n = int(entry["modules"])
    for grade in range(int(entry.get("done_grade") or 0) + 1, int(entry["target_grade"]) + 1):
        for mat, qty in recipes.get(("blueprint", entry["blueprint"], grade), {}).items():
            need[mat] += qty * rolls[grade - 1] * n
    if entry.get("experimental"):
        for mat, qty in recipes.get(("experimental", entry["experimental"], 0), {}).items():
            need[mat] += qty * n
    return dict(need)


def load_recipes(conn: sqlite3.Connection) -> dict[tuple[str, str, int], dict[str, int]]:
    out: dict[tuple[str, str, int], dict[str, int]] = defaultdict(dict)
    for kind, recipe, grade, mat, qty in conn.execute(
            "SELECT recipe_kind, recipe, grade, material, qty FROM ref_recipe_costs"):
        out[(kind, recipe, grade)][mat] = qty
    return out


def validate(conn: sqlite3.Connection, cols: dict) -> dict:
    """Check an entry against reference data; returns the cleaned columns."""
    out = {k: cols[k] for k in FIELDS if k in cols}
    mt, bp = str(out.get("module_type") or ""), str(out.get("blueprint") or "")
    max_grade = conn.execute("SELECT MAX(max_grade) FROM ref_engineer_blueprints WHERE module_type = ? AND blueprint = ?",
                             (mt, bp)).fetchone()[0]
    if not max_grade:
        raise ValueError("no engineer offers that blueprint for that module")
    for key, lo, hi in (("modules", 1, 99), ("target_grade", 1, max_grade), ("done_grade", 0, max_grade)):
        if key in out:
            v = str(out[key]).strip()
            if not v.lstrip("-").isdigit() or not lo <= int(v) <= hi:
                raise ValueError(f"{key.replace('_', ' ')} must be {lo}–{hi}")
            out[key] = int(v)
    exp = out.get("experimental") or None
    done, target = out.get("done_grade", 0), out.get("target_grade", 99)
    if done > target or (done == target and not exp):
        raise ValueError("already-done grade must be below the target grade (or equal, to plan just the experimental)")
    if exp:
        row = conn.execute("SELECT module_types FROM ref_experimentals WHERE effect = ?", (exp,)).fetchone()
        if not row or mt not in json.loads(row[0] or "[]"):
            raise ValueError("that experimental doesn't fit this module")
    out["experimental"] = exp
    if "include" in out:
        out["include"] = 1 if out["include"] in (True, 1, "1", "true", "on", "yes") else 0
    if "note" in out:
        out["note"] = (str(out["note"]).strip() or None) if out["note"] is not None else None
    return out


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add(conn: sqlite3.Connection, fid: str, cols: dict, template_key: str | None = None) -> int:
    cols = validate(conn, {"modules": 1, "target_grade": 5, "done_grade": 0, "include": 1} | cols)
    prio = conn.execute("SELECT COALESCE(MAX(priority), 0) + 1 FROM eng_plan WHERE fid = ?", (fid,)).fetchone()[0]
    with conn:
        cur = conn.execute(
            "INSERT INTO eng_plan (fid, priority, module_type, blueprint, modules, target_grade, done_grade, "
            "experimental, include, note, template_key, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fid, prio, cols["module_type"], cols["blueprint"], cols["modules"], cols["target_grade"],
             cols["done_grade"], cols["experimental"], cols["include"], cols.get("note"), template_key, _now()))
    return cur.lastrowid


def update(conn: sqlite3.Connection, fid: str, entry_id: int, cols: dict) -> None:
    row = get(conn, fid, entry_id)
    merged = validate(conn, dict(row) | cols)
    with conn:
        conn.execute("UPDATE eng_plan SET modules = ?, target_grade = ?, done_grade = ?, experimental = ?, "
                     "include = ?, note = ? WHERE id = ?",
                     (merged["modules"], merged["target_grade"], merged["done_grade"], merged["experimental"],
                      merged["include"], merged.get("note"), entry_id))


def get(conn: sqlite3.Connection, fid: str, entry_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM eng_plan WHERE id = ? AND fid = ?", (entry_id, fid)).fetchone()
    if not row:
        raise LookupError("no such plan entry")
    return row


def delete(conn: sqlite3.Connection, fid: str, entry_id: int) -> None:
    get(conn, fid, entry_id)
    with conn:
        conn.execute("DELETE FROM eng_plan WHERE id = ?", (entry_id,))


def move(conn: sqlite3.Connection, fid: str, entry_id: int, delta: int) -> None:
    ids = [r[0] for r in conn.execute("SELECT id FROM eng_plan WHERE fid = ? ORDER BY priority, id", (fid,))]
    i = ids.index(get(conn, fid, entry_id)["id"])
    j = max(0, min(len(ids) - 1, i + delta))
    ids.insert(j, ids.pop(i))
    with conn:
        conn.executemany("UPDATE eng_plan SET priority = ? WHERE id = ?", [(n + 1, x) for n, x in enumerate(ids)])


def load_template(conn: sqlite3.Connection, fid: str, template: dict) -> int:
    """Add the template's entries that aren't loaded yet; returns how many were added."""
    have = {r[0] for r in conn.execute("SELECT template_key FROM eng_plan WHERE fid = ? AND template_key IS NOT NULL",
                                       (fid,))}
    added = 0
    for e in template.get("entries", []):
        if e["key"] in have:
            continue
        add(conn, fid, {k: e.get(k) for k in FIELDS if k in e} | {"include": 1 if e.get("include", True) else 0},
            template_key=e["key"])
        added += 1
    return added
