"""Ship builds (DESIGN §16 Phase 5d): import, storage and the link to goals and owned ships.

A build is a journal-style `Loadout` object. EDSY and Coriolis both export one
wrapped in SLEF (`[{"header": {...}, "data": {Loadout}}]`); the game writes the
same shape to the journal, so a current ship can become a build too. EDSY's own
`#/L=` links are not read: they encode modules as EDSY-internal IDs.

A current fit can be shared the other way (`share`): as SLEF, or as the import
links EDSY and Coriolis accept (the Loadout gzipped into the link, as
EDMarketConnector makes them). Builds themselves are never exported.
"""

from __future__ import annotations

import base64
import gzip
import json
import sqlite3
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone

from app.core.normalize import normalize_symbol

EDSY_LINK_HELP = ("That's an EDSY link, which only EDSY can read. In EDSY open the build, choose Export…, "
                  "and copy the SLEF box instead.")


def parse(text: str) -> dict:
    """Read pasted SLEF or Loadout JSON. Returns {loadout, source, source_url}."""
    text = (text or "").strip()
    if not text:
        raise ValueError("paste a SLEF or Loadout JSON export")
    if "edsy.org/#" in text and not text.startswith(("[", "{")):
        raise ValueError(EDSY_LINK_HELP)
    if "coriolis.io/outfit" in text and not text.startswith(("[", "{")):
        raise ValueError("That's a Coriolis link. In Coriolis open the build, choose Export, and copy the SLEF JSON instead.")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc.msg}, line {exc.lineno})") from None
    if isinstance(doc, list):
        if not doc:
            raise ValueError("the SLEF list is empty")
        doc = doc[0]
    if not isinstance(doc, dict):
        raise ValueError("expected a SLEF list or a Loadout object")
    if "coriolis.io/schemas" in str(doc.get("$schema", "")):
        raise ValueError("That's Coriolis's own JSON format. In Coriolis's export, press SLEF and copy that instead.")
    header = doc.get("header") if isinstance(doc.get("header"), dict) else {}
    loadout = doc.get("data") if isinstance(doc.get("data"), dict) else doc
    if not loadout.get("Ship") or not isinstance(loadout.get("Modules"), list) or not loadout["Modules"]:
        raise ValueError("no ship or modules found; is this a Loadout or SLEF export?")
    app = str(header.get("appName") or "").lower()
    source = "edsy" if "edsy" in app else "coriolis" if "coriolis" in app else "slef" if header else "journal"
    url = header.get("appURL")
    # Coriolis leaves ShipName empty; its link carries the build name as `bn`.
    name = (loadout.get("ShipName") or "").strip() or (parse_qs(urlparse(url).query).get("bn", [""])[0] if url else "")
    return {"loadout": loadout, "source": source, "source_url": url, "name": name.strip() or None}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_ship(conn: sqlite3.Connection, fid: str, ship_type: str) -> int | None:
    """The owned ship of this type to compare with: the current one if it matches, else the lowest ID."""
    rows = [r[0] for r in conn.execute("SELECT ship_id FROM ships WHERE fid = ? AND ship_type = ? AND disposed_ts IS NULL "
                                       "ORDER BY ship_id", (fid, ship_type))]
    return rows[0] if rows else None


def default_goal(conn: sqlite3.Connection, fid: str, ship_type: str) -> int | None:
    """The one ship goal for this ship type that has no build yet, if exactly one exists."""
    rows = [r[0] for r in conn.execute(
        "SELECT id FROM goals WHERE fid = ? AND kind = 'ship' AND target_key = ? "
        "AND id NOT IN (SELECT goal_id FROM builds WHERE fid = ? AND goal_id IS NOT NULL)", (fid, ship_type, fid))]
    return rows[0] if len(rows) == 1 else None


def add(conn: sqlite3.Connection, fid: str, parsed: dict, name: str | None = None,
        template_key: str | None = None) -> int:
    lo = parsed["loadout"]
    ship_type = normalize_symbol(lo["Ship"])
    if not conn.execute("SELECT 1 FROM ref_items WHERE symbol = ? AND kind = 'ship'", (ship_type,)).fetchone():
        raise ValueError(f"unknown ship {lo['Ship']!r}")
    if not name or not name.strip():
        ship = conn.execute("SELECT display_name FROM ref_items WHERE symbol = ?", (ship_type,)).fetchone()
        name = parsed.get("name") or (ship[0] if ship else ship_type)
    with conn:
        cur = conn.execute(
            "INSERT INTO builds (fid, name, ship_type, source, source_url, loadout_json, goal_id, ship_id, "
            "template_key, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fid, name[:80], ship_type, parsed["source"], parsed.get("source_url"), json.dumps(lo),
             default_goal(conn, fid, ship_type), None, template_key, _now()))   # ship_id NULL: compare automatically
    return cur.lastrowid


def ship_loadout(conn: sqlite3.Connection, fid: str, ship_id: int) -> dict:
    """The ship's most recent journal `Loadout` event."""
    row = conn.execute("SELECT raw_json FROM events WHERE fid = ? AND event = 'Loadout' "
                       "AND json_extract(raw_json, '$.ShipID') = ? ORDER BY ts DESC, id DESC LIMIT 1",
                       (fid, ship_id)).fetchone()
    if not row:
        raise LookupError("no Loadout recorded for that ship yet")
    return json.loads(row[0])


def share(loadout: dict, app_version: str) -> dict:
    """A fit to hand to others: SLEF text, and links that open it in EDSY and Coriolis."""
    slef = json.dumps([{"header": {"appName": "ED Commander Planner", "appVersion": app_version},
                        "data": loadout}], ensure_ascii=False, indent=2)
    raw = json.dumps(loadout, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    packed = base64.urlsafe_b64encode(gzip.compress(raw, mtime=0)).decode().replace("=", "%3D")
    return {"slef": slef, "edsy": f"https://edsy.org/#/I={packed}", "coriolis": f"https://coriolis.io/import?data={packed}"}


def from_ship(conn: sqlite3.Connection, fid: str, ship_id: int) -> int:
    lo = ship_loadout(conn, fid, ship_id)
    ship = conn.execute("SELECT display_name FROM ref_items WHERE symbol = ?", (normalize_symbol(lo.get("Ship")),)).fetchone()
    label = " ".join(x for x in ((lo.get("ShipName") or "").strip() or (ship[0] if ship else lo.get("Ship")),
                                 (lo.get("ShipIdent") or "").strip()) if x)
    name = f"{label} (as fitted)"
    return add(conn, fid, {"loadout": lo, "source": "journal", "source_url": None}, name)


def get(conn: sqlite3.Connection, fid: str, build_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM builds WHERE id = ? AND fid = ?", (build_id, fid)).fetchone()
    if not row:
        raise LookupError("no such build")
    return row


class ShipMismatch(ValueError):
    """A build for one ship offered to a goal for another. `confirm` asks whether to switch the goal's ship."""

    def __init__(self, message: str, confirm: str):
        super().__init__(message)
        self.confirm = confirm


def update(conn: sqlite3.Connection, fid: str, build_id: int, cols: dict, retarget: bool = False) -> None:
    """Change a build. With `retarget`, a goal for another ship takes this build's ship."""
    row = get(conn, fid, build_id)
    name, goal_id, ship_id = row["name"], row["goal_id"], row["ship_id"]
    if "name" in cols:
        name = str(cols["name"] or "").strip()[:80] or name
    retarget_goal = None
    if "goal_id" in cols:
        goal_id = int(cols["goal_id"]) if str(cols["goal_id"] or "").isdigit() else None
        if goal_id is not None:
            try:
                check_goal(conn, fid, goal_id, row["ship_type"], build_id)
            except ShipMismatch:
                if not retarget:
                    raise
                retarget_goal = goal_id
    if "ship_id" in cols:
        ship_id = int(cols["ship_id"]) if str(cols["ship_id"] or "").isdigit() else None
        if ship_id is not None and not conn.execute(
                "SELECT 1 FROM ships WHERE fid = ? AND ship_id = ? AND ship_type = ? AND disposed_ts IS NULL",
                (fid, ship_id, row["ship_type"])).fetchone():
            raise ValueError("compare with an owned ship of the same type")
    with conn:
        if retarget_goal is not None:
            conn.execute("UPDATE goals SET target_key = ? WHERE id = ? AND fid = ?", (row["ship_type"], retarget_goal, fid))
        if goal_id is not None:   # one build per goal
            conn.execute("UPDATE builds SET goal_id = NULL WHERE fid = ? AND goal_id = ? AND id != ?",
                         (fid, goal_id, build_id))
        conn.execute("UPDATE builds SET name = ?, goal_id = ?, ship_id = ? WHERE id = ?",
                     (name, goal_id, ship_id, build_id))


def check_goal(conn: sqlite3.Connection, fid: str, goal_id: int, ship_type: str, build_id: int | None = None) -> None:
    """A build serves a ship goal for the same ship (or one whose ship isn't in reference data yet). A goal
    for another ship raises ShipMismatch, which the user can confirm to switch the goal to the build's ship."""
    g = conn.execute("SELECT target_key, title FROM goals WHERE id = ? AND fid = ? AND kind = 'ship'",
                     (goal_id, fid)).fetchone()
    if not g:
        raise ValueError("that isn't one of your ship goals")
    if g[0] and g[0] != ship_type:
        names = dict(conn.execute("SELECT symbol, display_name FROM ref_items WHERE symbol IN (?, ?)", (g[0], ship_type)))
        goal_ship, build_ship = names.get(g[0], g[0]), names.get(ship_type, ship_type)
        other = conn.execute("SELECT name FROM builds WHERE fid = ? AND goal_id = ? AND id IS NOT ?",
                             (fid, goal_id, build_id)).fetchone()
        question = (f"You have the {goal_ship} assigned to “{g[1]}”. Continue? The goal switches to the {build_ship}"
                    + (f", and its build “{other[0]}” is detached." if other else "."))
        raise ShipMismatch(f"this goal is for the {goal_ship}; that build is a {build_ship}", question)


def attach(conn: sqlite3.Connection, fid: str, goal_id: int, text: str | None = None, build_id: int | None = None,
           retarget: bool = False) -> int | None:
    """Give a ship goal its build: import pasted SLEF, pick an imported build, or (neither) detach."""
    parsed = parse(text) if text and text.strip() else None
    ship_type = (normalize_symbol(parsed["loadout"]["Ship"]) if parsed else
                 get(conn, fid, build_id)["ship_type"] if build_id is not None else None)
    if ship_type:
        try:
            check_goal(conn, fid, goal_id, ship_type, None if parsed else build_id)
        except ShipMismatch:
            if not retarget:
                raise
    if parsed:
        build_id = add(conn, fid, parsed)
    with conn:
        conn.execute("UPDATE builds SET goal_id = NULL WHERE fid = ? AND goal_id = ?", (fid, goal_id))
    if build_id is not None:
        update(conn, fid, build_id, {"goal_id": str(goal_id)}, retarget=retarget)
    return build_id


def delete(conn: sqlite3.Connection, fid: str, build_id: int) -> None:
    get(conn, fid, build_id)
    with conn:
        conn.execute("DELETE FROM builds WHERE id = ?", (build_id,))


def load_template(conn: sqlite3.Connection, fid: str, key: str, text: str) -> int | None:
    """Add an example build once; returns its id, or None when it's already loaded."""
    if conn.execute("SELECT 1 FROM builds WHERE fid = ? AND template_key = ?", (fid, key)).fetchone():
        return None
    return add(conn, fid, parse(text), template_key=key)
