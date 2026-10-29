"""Engineering plan views (DESIGN §16 Phase 5c): entries, material needs vs live counts, shortfalls."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from functools import lru_cache

from app.core import db, engplan, paths
from app.core.projectors.materials import MATERIAL_CAPS
from app.server.engineer_views import module_labels, pin_label
from app.server.live_views import sources_for
from app.server.views import _latest, engineers_view


@lru_cache(maxsize=1)
def template() -> dict:
    return json.loads((paths.refdata_dir() / "templates" / "engineering_plan.json").read_text(encoding="utf-8"))


def rolls(conn: sqlite3.Connection) -> tuple[int, ...]:
    try:
        return engplan.parse_rolls(db.get_setting(conn, engplan.ROLLS_SETTING))
    except ValueError:
        return engplan.DEFAULT_ROLLS


def _materials(conn: sqlite3.Connection) -> dict[str, dict]:
    out = {}
    for symbol, name, family, grade, attrs in conn.execute(
            "SELECT symbol, display_name, family, grade, attrs_json FROM ref_items WHERE kind = 'material'"):
        out[symbol] = {"symbol": symbol, "name": name, "family": family, "grade": grade,
                       "type": json.loads(attrs or "{}").get("type", "Raw")}
    return out


def _experimental_names(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT effect, display_name FROM ref_experimentals"))


def plan_view(conn: sqlite3.Connection, fid: str) -> dict:
    r = rolls(conn)
    recipes = engplan.load_recipes(conn)
    mats = _materials(conn)
    have = {s: int(c["value"]) for s, c in _latest(conn, fid, "material").items()}
    exp_names = _experimental_names(conn)
    engineers = {int(e["id"]): e for group in engineers_view(conn, fid).values() for e in group}

    entries, totals = [], defaultdict(int)
    for row in conn.execute("SELECT * FROM eng_plan WHERE fid = ? ORDER BY priority, id", (fid,)):
        e = dict(row)
        need = engplan.entry_needs(recipes, e, r)
        if e["include"]:
            for m, q in need.items():
                totals[m] += q
        offered = [{"name": engineers[eid]["name"], "max_grade": g, "state": engineers[eid]["state"],
                    "grade": engineers[eid]["grade"]}
                   for eid, g in conn.execute("SELECT engineer_id, max_grade FROM ref_engineer_blueprints "
                                              "WHERE module_type = ? AND blueprint = ? ORDER BY max_grade DESC",
                                              (e["module_type"], e["blueprint"])) if eid in engineers]
        nxt = e["done_grade"] + 1
        next_recipe = recipes.get(("blueprint", e["blueprint"], nxt), {})
        e |= {
            "label": pin_label(conn, e["module_type"], e["blueprint"]),
            "experimental_name": exp_names.get(e["experimental"]) if e["experimental"] else None,
            "engineers": offered,
            "can_roll": any(o["state"] == "Unlocked" and (o["grade"] or 0) >= min(nxt, o["max_grade"]) for o in offered),
            "next_grade": nxt,
            "next_recipe": [{"name": mats.get(m, {}).get("name", m), "qty": q, "have": have.get(m, 0)}
                            for m, q in sorted(next_recipe.items())],
            "rolls_now": min((have.get(m, 0) // q for m, q in next_recipe.items()), default=0) if next_recipe else 0,
            "need": [{"symbol": m, "name": mats.get(m, {}).get("name", m), "qty": q, "have": have.get(m, 0)}
                     for m, q in sorted(need.items(), key=lambda kv: (mats.get(kv[0], {}).get("grade") or 0, kv[0]))],
            "units": sum(need.values()),
        }
        entries.append(e)

    rows = []
    for m, q in totals.items():
        info = mats.get(m, {"symbol": m, "name": m, "family": None, "grade": None, "type": "?"})
        h = have.get(m, 0)
        rows.append(info | {"need": q, "have": h, "short": max(0, q - h), "cap": MATERIAL_CAPS.get(info["grade"] or 0),
                            "sources": sources_for(m, info["type"], info["family"])})
    order = {"Raw": 0, "Manufactured": 1, "Encoded": 2}
    rows.sort(key=lambda x: (order.get(x["type"], 3), x["family"] or "", x["grade"] or 0, x["name"]))
    short = [x for x in rows if x["short"]]
    return {
        "entries": entries, "rolls": r, "materials": rows, "short": short,
        "short_count": len(short), "short_units": sum(x["short"] for x in short),
        "need_units": sum(x["need"] for x in rows),
        "template_name": template().get("name"),
    }


def needs_by_material(conn: sqlite3.Connection, fid: str) -> dict[str, dict]:
    """For the Materials tab: symbol → {need, short} across included entries."""
    return {x["symbol"]: x for x in plan_view(conn, fid)["materials"]}


def form_options(conn: sqlite3.Connection) -> dict:
    labels = module_labels()
    blueprints = []
    for mt, bp, g in conn.execute("SELECT module_type, blueprint, MAX(max_grade) FROM ref_engineer_blueprints "
                                  "GROUP BY module_type, blueprint"):
        blueprints.append({"value": f"{mt}|{bp}", "label": pin_label(conn, mt, bp), "max_grade": g,
                           "group": labels.get(mt, mt)})
    blueprints.sort(key=lambda b: b["label"])
    experimentals = []
    for effect, name, types in conn.execute("SELECT effect, display_name, module_types FROM ref_experimentals"):
        for mt in json.loads(types or "[]"):
            experimentals.append({"value": effect, "label": name, "module_type": mt})
    experimentals.sort(key=lambda x: (x["module_type"], x["label"]))
    return {"bp_options": blueprints, "exp_options": experimentals}
