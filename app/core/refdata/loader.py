"""Load refdata/ into the `ref_*` tables, only when its manifest changed (DESIGN §7.3, §7.5).

The bundled refdata/ is the source until the in-app updater (Phase 7) adds
`refdata_active\\`. A build with errors is never loaded; the previous tables stay.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.core import db
from app.core.refdata import manifest
from app.core.refdata.build import build

log = logging.getLogger(__name__)
SETTING_KEY = "refdata_loaded"
# Bump when the loader writes something new, so existing databases reload.
LOADER_VERSION = 3


@dataclass(frozen=True)
class RefdataStatus:
    version: str | None
    sources: dict
    items: int
    loaded_now: bool
    error: str | None = None


def _fingerprint(m: dict) -> str:
    payload = {"files": m.get("files", {}), "loader": LOADER_VERSION}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def status(conn: sqlite3.Connection, refdata_dir: Path) -> RefdataStatus:
    m = manifest.read(refdata_dir) or {}
    items = conn.execute("SELECT COUNT(*) FROM ref_items").fetchone()[0]
    return RefdataStatus(m.get("refdata_version"), m.get("sources", {}), items, loaded_now=False)


def ensure_loaded(db_path: Path, refdata_dir: Path) -> RefdataStatus:
    conn = db.connect(db_path)
    try:
        m = manifest.read(refdata_dir)
        if m is None:
            return RefdataStatus(None, {}, 0, False, error="refdata/manifest.json missing")
        fingerprint = _fingerprint(m)
        if db.get_setting(conn, SETTING_KEY) == fingerprint:
            return status(conn, refdata_dir)

        problems = manifest.verify(refdata_dir)
        data = build(refdata_dir, manifest.commits(refdata_dir))
        if problems or data.errors:
            error = "; ".join((problems + data.errors)[:5])
            log.error("reference data not loaded: %s", error)
            return RefdataStatus(m.get("refdata_version"), m.get("sources", {}),
                                 conn.execute("SELECT COUNT(*) FROM ref_items").fetchone()[0], False, error)

        with conn:
            conn.execute("DELETE FROM ref_recipe_costs")
            conn.execute("DELETE FROM ref_blueprints")
            conn.execute("DELETE FROM ref_experimentals")
            conn.execute("DELETE FROM ref_items")
            conn.execute("DELETE FROM ref_engineer_blueprints")
            conn.executemany(
                "INSERT INTO ref_items (symbol, kind, fdev_id, display_name, category, family, grade, "
                "attrs_json, source, source_commit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(i.symbol, i.kind, i.fdev_id, i.display_name, i.category, i.family, i.grade,
                  json.dumps(i.attrs, sort_keys=True) if i.attrs else None, i.source, i.source_commit)
                 for i in data.items.values()],
            )
            conn.executemany(
                "INSERT INTO ref_blueprints (blueprint, grade, module_types, engineers, display_name) "
                "VALUES (?, ?, ?, ?, ?)",
                [(bp, g, json.dumps(sorted(v["module_types"])), json.dumps(sorted(v["engineers"])),
                  data.blueprint_names.get(bp)) for (bp, g), v in data.blueprints.items()],
            )
            conn.executemany(
                "INSERT INTO ref_recipe_costs (recipe_kind, recipe, grade, material, qty) VALUES (?, ?, ?, ?, ?)",
                [(k, r, g, mat, q) for (k, r, g, mat), q in data.recipe_costs.items()],
            )
            conn.executemany(
                "INSERT INTO ref_experimentals (effect, display_name, module_types) VALUES (?, ?, ?)",
                [(e, v["display_name"], json.dumps(sorted(v["module_types"]))) for e, v in data.experimentals.items()],
            )
            conn.executemany(
                "INSERT INTO ref_engineer_blueprints (engineer_id, module_type, blueprint, max_grade) VALUES (?, ?, ?, ?)",
                [(e, m, b, g) for (e, m, b), g in data.engineer_offers.items()],
            )
            db.set_setting(conn, SETTING_KEY, fingerprint)
            db.set_setting(conn, "refdata_version", m.get("refdata_version") or "")
        log.info("reference data %s loaded: %d items", m.get("refdata_version"), len(data.items))
        return RefdataStatus(m.get("refdata_version"), m.get("sources", {}), len(data.items), True)
    finally:
        conn.close()


def display_name(conn: sqlite3.Connection, symbol: str | None) -> str | None:
    """Display name from ref_items, or None when the symbol is unknown."""
    if not symbol:
        return None
    row = conn.execute("SELECT display_name FROM ref_items WHERE symbol = ?", (symbol,)).fetchone()
    return row[0] if row else None
