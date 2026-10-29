"""Importing a `.edplanner` (DESIGN §12.2–12.3): review, apply in one transaction, roll back.

Per commander the sections are History (events), Goals, Plans (engineer pins, the engineering
plan, builds, saved fits, mining favourites, flags, activity ships, ships for sale) and Notes; Settings are
global. Each is Skip, Merge or Override. History merges as a union by hash and never conflicts:
the journal is the master record (§12.3). Everything else is matched by key; the same key with
different content is a conflict the user settles: keep current, take the file's, or keep both
(rows only, not single values). Derived data is rebuilt afterwards from the merged events.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.core import db, notes
from app.core.backup import archive
from app.core.projectors import DERIVED_TABLES

MODES = ("skip", "merge", "override")
CHOICES = ("current", "file", "both")
SECTIONS = (
    ("history", "History", "Journal events: ranks, materials, credits, ships and everything worked out from them"),
    ("goals", "Goals", "Goals, their priorities, steps and requirements"),
    ("plans", "Plans", "Engineer pins, the engineering plan, builds, saved fits, mining favourites, flags and ship choices"),
    ("notes", "Notes", "Your notes"),
)
SETTING_LABELS = {"eng_rolls": "Rolls per grade", "reserve_rebuys": "Rebuys held in reserve",
                  "hover_cards": "Selectable hover details", "accent": "Accent colour",
                  "selling_ships": "Ships marked for sale"}
FIELD_LABELS = {"target_key": "target", "target_value": "target value", "requirements_json": "costs",
                "detail_json": "details", "loadout_json": "loadout", "ship_id": "ship", "module_type": "module",
                "target_grade": "target grade", "done_grade": "grades done", "body": "text", "value": "value"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fold(value) -> str:
    return " ".join(str(value or "").split()).casefold()


@dataclass(frozen=True)
class Kind:
    name: str                        # table, or cmdr_settings / notes / settings
    section: str
    label: str
    key: Callable[[dict], tuple]
    title: Callable[[dict], str]
    ignore: frozenset = field(default_factory=lambda: frozenset({"id", "created_ts", "updated_ts"}))
    pk: str = "id"                   # how a current row is found again
    both: bool = True                # rows can be kept twice; single values can't


KINDS = (
    Kind("goals", "goals", "Goal",
         lambda r: ("t", r["template_key"]) if r.get("template_key") else
         ("g", r.get("kind"), _fold(r.get("target_key")), _fold(r.get("title"))),
         lambda r: r.get("title") or r.get("target_key") or r.get("kind") or "Goal",
         frozenset({"id", "created_ts", "updated_ts", "depends_on"})),
    Kind("flags", "plans", "Flag", lambda r: (r.get("key"),), lambda r: r.get("key") or "",
         frozenset({"updated_ts"}), pk="key", both=False),
    Kind("engineer_pins", "plans", "Engineer pin", lambda r: (r.get("engineer_id"),),
         lambda r: f"Engineer {r.get('engineer_id')}", frozenset({"updated_ts"}), pk="engineer_id", both=False),
    Kind("mining_spots", "plans", "Mining favourite",
         lambda r: (r.get("commodity"), r.get("method"), _fold(r.get("system")), _fold(r.get("body"))),
         lambda r: " · ".join(str(x) for x in (r.get("commodity"), r.get("system"), r.get("body")) if x)),
    Kind("builds", "plans", "Build", lambda r: (r.get("ship_type"), _fold(r.get("name"))),
         lambda r: r.get("name") or "Build", frozenset({"id", "created_ts", "goal_id"})),
    Kind("eng_plan", "plans", "Engineering plan entry",
         lambda r: (r.get("module_type"), r.get("blueprint"), _fold(r.get("note"))),
         lambda r: f"{r.get('blueprint')} G{r.get('target_grade')}" + (f" · {r['note']}" if r.get("note") else ""),
         frozenset({"id", "created_ts", "template_key"})),
    Kind("ship_fits", "plans", "Saved fit", lambda r: (r.get("ship_id"), _fold(r.get("name"))),
         lambda r: r.get("name") or "Saved fit"),
    Kind("cmdr_settings", "plans", "Choice", lambda r: (r["key"],),
         lambda r: SETTING_LABELS.get(r["key"]) or r["key"].replace("activity_ship:", "").capitalize() + " ship",
         frozenset(), pk="key", both=False),
    Kind("notes", "notes", "Note", lambda r: (r["id"],), lambda r: r.get("title") or "Note",
         frozenset({"id", "created", "updated"})),
    Kind("settings", "settings", "Setting", lambda r: (r["key"],), lambda r: SETTING_LABELS.get(r["key"], r["key"]),
         frozenset(), pk="key", both=False),
)
KIND = {k.name: k for k in KINDS}


# -- items on each side --------------------------------------------------------------------

def _note_items(texts: dict[str, str]) -> list[dict]:
    out = []
    for note_id, text in texts.items():
        meta, body = notes.parse(text)
        out.append({"id": note_id, "title": notes._clean_title(meta.get("title")), "pinned": meta["pinned"],
                    "created": meta.get("created"), "updated": meta.get("updated"), "body": body, "_raw": text})
    return out


def _items(data: dict) -> dict[str, list[dict]]:
    """One commander's data (archive.collect / Archive.commander shape) as rows per kind."""
    plans = data["plans"]
    out = {t: list(plans.get(t) or []) for t in archive.PLAN_TABLES}
    out["goals"] = list(data["goals"])
    out["cmdr_settings"] = [{"key": k, "value": v} for k, v in sorted(plans.get("settings", {}).items())]
    out["notes"] = _note_items(data["notes"])
    return out


def _settings_items(values: dict[str, str]) -> list[dict]:
    return [{"key": k, "value": v} for k, v in sorted(values.items())]


def _content(kind: Kind, row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in kind.ignore and not k.startswith("_")}


def _same(kind: Kind, a: dict, b: dict) -> bool:
    ca, cb = _content(kind, a), _content(kind, b)
    shared = ca.keys() & cb.keys()       # a column only one schema has is not a difference
    return all(ca[k] == cb[k] for k in shared)


@dataclass
class Match:
    pairs: list[tuple[dict, dict]]       # same key, same content
    conflicts: list[tuple[dict, dict]]   # same key, different content
    new: list[dict]                      # only in the file
    current_only: list[dict]


def match(kind: Kind, current: list[dict], incoming: list[dict]) -> Match:
    """Pair rows by key, in order; extra rows with a repeated key pair with nothing."""
    by_key: dict[tuple, list[dict]] = {}
    for row in current:
        by_key.setdefault(kind.key(row), []).append(row)
    m = Match([], [], [], [])
    for row in incoming:
        mates = by_key.get(kind.key(row))
        if not mates:
            m.new.append(row)
            continue
        cur = mates.pop(0)
        (m.pairs if _same(kind, cur, row) else m.conflicts).append((cur, row))
    m.current_only = [r for rows in by_key.values() for r in rows]
    return m


def _short(value) -> str:
    text = "—" if value is None or value == "" else str(value)
    text = " ".join(text.split())
    return text if len(text) <= 120 else text[:117] + "…"


def _diffs(kind: Kind, cur: dict, row: dict) -> list[dict]:
    ca, cb = _content(kind, cur), _content(kind, row)
    return [{"field": FIELD_LABELS.get(k, k.replace("_", " ")), "current": _short(ca[k]), "file": _short(cb[k])}
            for k in ca if k in cb and ca[k] != cb[k]]


# -- current state ---------------------------------------------------------------------------

def _current(conn: sqlite3.Connection, data_dir: Path, fid: str | None) -> dict:
    if fid is None:
        return {"commander": None, "events": [], "goals": [], "notes": {},
                "plans": {t: [] for t in archive.PLAN_TABLES} | {"settings": {}}}
    return archive.collect(conn, data_dir, [fid])["commanders"].get(fid) or _current(conn, data_dir, None)


def _known_hashes(conn: sqlite3.Connection, hashes: list[str]) -> set[str]:
    known = set()
    for i in range(0, len(hashes), 500):
        chunk = hashes[i:i + 500]
        known.update(r[0] for r in conn.execute(
            f"SELECT hash FROM events WHERE hash IN ({','.join('?' * len(chunk))})", chunk))
    return known


def _commander_handle(i: int) -> str:
    return f"c{i}"


def review(conn: sqlite3.Connection, data_dir: Path, arc: archive.Archive) -> dict:
    """What an import would do, per commander and section, with every conflict listed."""
    existing = {r[0] for r in conn.execute("SELECT fid FROM commanders")}
    out = {"commanders": [], "settings": None}
    for i, c in enumerate(arc.commanders):
        fid, handle = c["fid"], _commander_handle(i)
        data = arc.commander(fid)
        matched = fid in existing
        current = _current(conn, data_dir, fid if matched else None)
        cur_items, file_items = _items(current), _items(data)
        sections = []
        for key, label, desc in SECTIONS:
            if key == "history":
                known = _known_hashes(conn, [e[4] for e in data["events"]])
                in_file = {e[4] for e in data["events"]}
                counts = {"file": len(data["events"]), "new": sum(e[4] not in known for e in data["events"]),
                          "same": sum(e[4] in known for e in data["events"]), "conflicts": 0,
                          "current_only": sum(e[4] not in in_file for e in current["events"])}
                sections.append({"key": key, "label": label, "desc": desc, "counts": counts, "conflicts": [],
                                 "mode_name": f"{handle}:{key}"})
                continue
            counts = {"file": 0, "new": 0, "same": 0, "conflicts": 0, "current_only": 0}
            conflicts = []
            for kind in (k for k in KINDS if k.section == key):
                m = match(kind, cur_items[kind.name], file_items[kind.name])
                counts["file"] += len(file_items[kind.name])
                counts["new"] += len(m.new)
                counts["same"] += len(m.pairs)
                counts["conflicts"] += len(m.conflicts)
                counts["current_only"] += len(m.current_only)
                for n, (cur, row) in enumerate(m.conflicts):
                    conflicts.append({"id": f"{handle}:{kind.name}:{n}", "kind": kind.label, "title": kind.title(row),
                                      "diffs": _diffs(kind, cur, row), "both": kind.both})
            sections.append({"key": key, "label": label, "desc": desc, "counts": counts, "conflicts": conflicts,
                             "mode_name": f"{handle}:{key}"})
        out["commanders"].append({"handle": handle, "fid": fid, "name": data["commander"].get("name") or c["name"],
                                  "matched": matched, "sections": sections,
                                  "first_seen": data["commander"].get("first_seen"),
                                  "last_seen": data["commander"].get("last_seen")})
    kind = KIND["settings"]
    cur_settings = {k: db.get_setting(conn, k) for k in archive.GLOBAL_SETTINGS}
    m = match(kind, _settings_items({k: v for k, v in cur_settings.items() if v is not None}),
              _settings_items(arc.settings()))
    out["settings"] = {
        "mode_name": "settings",
        "counts": {"file": len(m.new) + len(m.pairs) + len(m.conflicts), "new": len(m.new), "same": len(m.pairs),
                   "conflicts": len(m.conflicts), "current_only": len(m.current_only)},
        "conflicts": [{"id": f"settings:settings:{n}", "kind": kind.label, "title": kind.title(row),
                       "diffs": _diffs(kind, cur, row), "both": False} for n, (cur, row) in enumerate(m.conflicts)],
    }
    return out


# -- writing ---------------------------------------------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


class _Writer:
    """Row writes for one commander inside the import's transaction."""

    def __init__(self, conn: sqlite3.Connection, fid: str):
        self.conn, self.fid = conn, fid
        self._cols: dict[str, list[str]] = {}

    def cols(self, table: str) -> list[str]:
        if table not in self._cols:
            self._cols[table] = _columns(self.conn, table)
        return self._cols[table]

    def _values(self, table: str, row: dict) -> dict:
        return {c: row[c] for c in self.cols(table) if c in row and c not in ("id", "fid")}

    def insert(self, table: str, row: dict) -> int:
        vals = self._values(table, row)
        names = ", ".join(["fid", *vals])
        cur = self.conn.execute(f"INSERT INTO {table} ({names}) VALUES ({', '.join('?' * (len(vals) + 1))})",
                                (self.fid, *vals.values()))
        return cur.lastrowid

    def replace(self, kind: Kind, cur: dict, row: dict) -> int | None:
        """Take the file's content for an existing row, keeping the row (and its id) where it is."""
        if kind.pk == "id":
            vals = self._values(kind.name, row)
            self.conn.execute(f"UPDATE {kind.name} SET {', '.join(f'{c} = ?' for c in vals)} WHERE id = ? AND fid = ?",
                              (*vals.values(), cur["id"], self.fid))
            return cur["id"]
        self.conn.execute(f"DELETE FROM {kind.name} WHERE fid = ? AND {kind.pk} = ?", (self.fid, cur[kind.pk]))
        self.insert(kind.name, row)
        return None

    def update_cols(self, table: str, row_id: int, values: dict) -> None:
        self.conn.execute(f"UPDATE {table} SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ? AND fid = ?",
                          (*values.values(), row_id, self.fid))


def _set_commander_settings(conn: sqlite3.Connection, fid: str, rows: list[dict], *, clear: bool) -> None:
    if clear:
        for key in [k for k, in conn.execute("SELECT key FROM settings")]:
            m = archive.COMMANDER_SETTING.match(key)
            if m and m.group(2) == fid:
                conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    for r in rows:
        db.set_setting(conn, f"{r['key']}:{fid}", str(r["value"]))


def _ensure_commander(conn: sqlite3.Connection, c: dict) -> bool:
    """Create the commander, or widen its seen-range; the newer side's name wins. True if created."""
    row = conn.execute("SELECT name, first_seen, last_seen FROM commanders WHERE fid = ?", (c["fid"],)).fetchone()
    if row is None:
        conn.execute("INSERT INTO commanders (fid, name, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                     (c["fid"], c.get("name") or "Commander", c.get("first_seen"), c.get("last_seen")))
        return True
    firsts = [x for x in (row["first_seen"], c.get("first_seen")) if x]
    newer = (c.get("last_seen") or "") > (row["last_seen"] or "")
    conn.execute("UPDATE commanders SET name = ?, first_seen = ?, last_seen = ? WHERE fid = ?",
                 ((c.get("name") or row["name"]) if newer else row["name"], min(firsts) if firsts else None,
                  c.get("last_seen") if newer else row["last_seen"], c["fid"]))
    return False


def _history(conn: sqlite3.Connection, fid: str, events: list[tuple], mode: str, *, source: str | None,
             keep_after_id: int | None) -> int:
    """Union by hash (merge) or replace (override). `source` None keeps each event's own source."""
    kept = []
    if mode == "override":
        if keep_after_id is not None:      # a rollback keeps what the game wrote since the import
            kept = [tuple(r) for r in conn.execute(
                f"SELECT {', '.join(archive.EVENT_COLS)} FROM events WHERE fid = ? AND id > ? AND source = 'journal'",
                (fid, keep_after_id))]
        for table in DERIVED_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE fid = ?", (fid,))
        conn.execute("DELETE FROM events WHERE fid = ?", (fid,))
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO events (fid, fid_status, ts, event, raw_json, hash, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(fid, e[0], e[1], e[2], e[3], e[4], source or e[5]) for e in events + kept])
    return conn.total_changes - before - len(kept)


def _apply_kind(w: _Writer, kind: Kind, mode: str, cur_rows: list[dict], file_rows: list[dict],
                choices: dict[str, str], handle: str) -> tuple[dict[int, int], set[int]]:
    """Write one kind. Returns file row id → row id here (for kinds other rows point at), and
    the file ids whose content was written, so links are fixed only on rows taken from the file."""
    ids: dict[int, int] = {}
    written: set[int] = set()

    def took(row, new):
        if "id" in row and new is not None:
            ids[row["id"]] = new
            written.add(row["id"])

    if mode == "skip":
        m = match(kind, cur_rows, file_rows)
        for cur, row in m.pairs + m.conflicts:
            if "id" in row and "id" in cur:
                ids[row["id"]] = cur["id"]
        return ids, written
    if mode == "override":
        w.conn.execute(f"DELETE FROM {kind.name} WHERE fid = ?", (w.fid,))
        for row in file_rows:
            took(row, w.insert(kind.name, row))
        return ids, written
    m = match(kind, cur_rows, file_rows)
    for cur, row in m.pairs:
        if "id" in row:
            ids[row["id"]] = cur["id"]
    for n, (cur, row) in enumerate(m.conflicts):
        choice = choices.get(f"{handle}:{kind.name}:{n}", "current")
        if choice == "both" and kind.both:
            took(row, w.insert(kind.name, row))
        elif choice == "file":
            took(row, w.replace(kind, cur, row))
        elif "id" in row and "id" in cur:
            ids[row["id"]] = cur["id"]
    for row in m.new:
        took(row, w.insert(kind.name, row))
    return ids, written


def _remap_ids(text, ids: dict[int, int]):
    try:
        old = json.loads(text) if text else None
    except ValueError:
        return None
    if not isinstance(old, list):
        return text
    return json.dumps([ids[i] for i in old if isinstance(i, int) and i in ids])


def _build_key(template_key, build_ids: dict[int, int]):
    """`build<id>:…` plan entries point at a build; follow it to its id here, or drop the link."""
    if not template_key or not str(template_key).startswith("build"):
        return template_key
    head, sep, rest = str(template_key)[5:].partition(":")
    if not sep or not head.isdigit():
        return template_key
    new = build_ids.get(int(head))
    return f"build{new}:{rest}" if new is not None else None


def _notes_ops(kind: Kind, mode: str, cur_rows: list[dict], file_rows: list[dict], choices: dict[str, str],
               handle: str) -> list[tuple]:
    """Note file writes, run once the database has committed."""
    if mode == "skip":
        return []
    if mode == "override":
        return [("delete", r["id"]) for r in cur_rows] + [("write", r["id"], r["_raw"]) for r in file_rows]
    m = match(kind, cur_rows, file_rows)
    ops = [("write", r["id"], r["_raw"]) for r in m.new]
    for n, (cur, row) in enumerate(m.conflicts):
        choice = choices.get(f"{handle}:notes:{n}", "current")
        if choice == "file":
            ops.append(("write", row["id"], row["_raw"]))
        elif choice == "both":
            ops.append(("copy", row["id"], row["_raw"]))
    return ops


@dataclass
class Plan:
    """The user's choices: modes keyed `<handle>:<section>` and `settings`, conflict choices by id."""
    modes: dict[str, str]
    choices: dict[str, str]

    def mode(self, name: str) -> str:
        mode = self.modes.get(name, "skip")
        return mode if mode in MODES else "skip"


def apply(conn: sqlite3.Connection, data_dir: Path, arc: archive.Archive, plan: Plan, *,
          source: str | None = "edplanner_import", keep_after_id: int | None = None) -> dict:
    """Apply `plan` in one transaction, then write note files. Returns what changed; the caller
    rebuilds derived data for `result["rebuild"]`."""
    result = {"events_added": 0, "created": [], "rebuild": [], "modes": {}, "written": 0}
    note_ops: dict[str, list[tuple]] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for i, c in enumerate(arc.commanders):
            fid, handle = c["fid"], _commander_handle(i)
            modes = {key: plan.mode(f"{handle}:{key}") for key, *_ in SECTIONS}
            if all(m == "skip" for m in modes.values()):
                continue
            result["modes"][fid] = modes
            data = arc.commander(fid)
            exists = conn.execute("SELECT 1 FROM commanders WHERE fid = ?", (fid,)).fetchone() is not None
            current = _current(conn, data_dir, fid if exists else None)
            if _ensure_commander(conn, data["commander"] | {"name": data["commander"].get("name") or c["name"]}):
                result["created"].append(fid)
            cur_items, file_items = _items(current), _items(data)
            w = _Writer(conn, fid)
            if modes["history"] != "skip":
                result["events_added"] += _history(conn, fid, data["events"], modes["history"], source=source,
                                                   keep_after_id=keep_after_id)
                result["rebuild"].append(fid)
            # Goals first: builds and other goals point at goal ids, which are new here.
            goal_kind = KIND["goals"]
            old_goal_keys = {g["id"]: goal_kind.key(g) for g in cur_items["goals"]}
            goal_ids, goals_written = _apply_kind(w, goal_kind, modes["goals"], cur_items["goals"],
                                                  file_items["goals"], plan.choices, handle)
            if modes["goals"] != "skip":
                for row in file_items["goals"]:
                    if row.get("depends_on") and row.get("id") in goals_written:
                        w.update_cols("goals", goal_ids[row["id"]], {"depends_on": _remap_ids(row["depends_on"], goal_ids)})
            if modes["goals"] == "override":
                # Builds that stay point at goals that are gone: follow them by key, or detach.
                by_key = {goal_kind.key(r): goal_ids.get(r.get("id")) for r in file_items["goals"]}
                for b_id, g_id in conn.execute("SELECT id, goal_id FROM builds WHERE fid = ? AND goal_id IS NOT NULL",
                                               (fid,)).fetchall():
                    w.update_cols("builds", b_id, {"goal_id": by_key.get(old_goal_keys.get(g_id))})
            if modes["plans"] != "skip":
                for name in ("flags", "engineer_pins", "mining_spots", "ship_fits"):
                    _apply_kind(w, KIND[name], modes["plans"], cur_items[name], file_items[name], plan.choices, handle)
                _apply_settings_kind(conn, fid, modes["plans"], cur_items["cmdr_settings"],
                                     file_items["cmdr_settings"], plan.choices, handle)
                build_ids, builds_written = _apply_kind(w, KIND["builds"], modes["plans"], cur_items["builds"],
                                                        file_items["builds"], plan.choices, handle)
                for row in file_items["builds"]:
                    if row.get("id") in builds_written and "goal_id" in row:
                        w.update_cols("builds", build_ids[row["id"]], {"goal_id": goal_ids.get(row["goal_id"])})
                plan_ids, plan_written = _apply_kind(w, KIND["eng_plan"], modes["plans"], cur_items["eng_plan"],
                                                     file_items["eng_plan"], plan.choices, handle)
                for row in file_items["eng_plan"]:
                    if row.get("id") in plan_written and row.get("template_key"):
                        w.update_cols("eng_plan", plan_ids[row["id"]],
                                      {"template_key": _build_key(row["template_key"], build_ids)})
            note_ops[fid] = _notes_ops(KIND["notes"], modes["notes"], cur_items["notes"], file_items["notes"],
                                       plan.choices, handle)
        settings_mode = plan.mode("settings")
        if settings_mode != "skip":
            result["modes"]["settings"] = settings_mode
            cur = _settings_items({k: v for k in archive.GLOBAL_SETTINGS
                                   if (v := db.get_setting(conn, k)) is not None})
            incoming = _settings_items(arc.settings())
            if settings_mode == "override":
                for k in archive.GLOBAL_SETTINGS:
                    conn.execute("DELETE FROM settings WHERE key = ?", (k,))
                rows = incoming
            else:
                m = match(KIND["settings"], cur, incoming)
                rows = m.new + [row for n, (_, row) in enumerate(m.conflicts)
                                if plan.choices.get(f"settings:settings:{n}") == "file"]
            for r in rows:
                db.set_setting(conn, r["key"], r["value"])
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    for fid, ops in note_ops.items():
        for op in ops:
            if op[0] == "delete":
                notes.delete(data_dir, fid, op[1])
            elif op[0] == "write":
                notes.write_raw(data_dir, fid, op[1], op[2])
            else:
                notes.write_raw(data_dir, fid, notes.new_id(data_dir, fid), op[2])
            result["written"] += 1
    return result


def _apply_settings_kind(conn, fid, mode, cur_rows, file_rows, choices, handle) -> None:
    kind = KIND["cmdr_settings"]
    if mode == "override":
        _set_commander_settings(conn, fid, file_rows, clear=True)
        return
    m = match(kind, cur_rows, file_rows)
    take = m.new + [row for n, (_, row) in enumerate(m.conflicts)
                    if choices.get(f"{handle}:cmdr_settings:{n}") == "file"]
    _set_commander_settings(conn, fid, take, clear=False)


def remove_commander(conn: sqlite3.Connection, data_dir: Path, fid: str) -> None:
    """Undo a commander an import created: rows, settings and (moved aside) notes."""
    with conn:
        for table in (*DERIVED_TABLES, "events", "goals", *archive.PLAN_TABLES):
            if table in archive.tables(conn):
                conn.execute(f"DELETE FROM {table} WHERE fid = ?", (fid,))
        _set_commander_settings(conn, fid, [], clear=True)
        conn.execute("DELETE FROM commanders WHERE fid = ?", (fid,))
    for note_id in notes.raw(data_dir, fid):
        notes.delete(data_dir, fid, note_id)


# -- the whole flow: pre-import backup, apply, rebuild, log; and rolling it back -------------

def _log(conn: sqlite3.Connection, arc: archive.Archive, status: str, events_added: int, detail: dict) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO import_log (kind, ref, size, sha256, status, events_added, at, detail) "
            "VALUES ('edplanner', ?, ?, ?, ?, ?, ?, ?)",
            (arc.name, arc.size, arc.sha256, status, events_added, _now(), json.dumps(detail)))
    return cur.lastrowid


def import_file(conn: sqlite3.Connection, data_dir: Path, arc: archive.Archive, plan: Plan, *, app_version: str,
                rebuild: Callable[[sqlite3.Connection, str], object]) -> int:
    """§12.2 steps 2–6. Returns the import_log id, which is what a rollback names."""
    pre = archive.save_backup(conn, data_dir, app_version=app_version, kind="pre-import")
    top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    result = apply(conn, data_dir, arc, plan)
    for fid in result["rebuild"]:
        rebuild(conn, fid)
    detail = {"pre_import": pre.name, "max_event_id": top, "created": result["created"], "modes": result["modes"],
              "names": {c["fid"]: c["name"] for c in arc.commanders if c["fid"] in result["modes"]},
              "notes_written": result["written"]}
    return _log(conn, arc, "imported", result["events_added"], detail)


def rollback_target(conn: sqlite3.Connection, data_dir: Path) -> dict | None:
    """The import that can be rolled back: the newest .edplanner import, while its backup exists."""
    row = conn.execute("SELECT * FROM import_log WHERE kind = 'edplanner' ORDER BY id DESC LIMIT 1").fetchone()
    if row is None or row["status"] != "imported":
        return None
    detail = json.loads(row["detail"] or "{}")
    if not (archive.backups_dir(data_dir) / str(detail.get("pre_import") or "-")).is_file():
        return None
    return dict(row) | {"detail_json": detail}


def rollback(conn: sqlite3.Connection, data_dir: Path, log_id: int, *, app_version: str,
             rebuild: Callable[[sqlite3.Connection, str], object]) -> dict:
    """Put back what the import changed, from its pre-import backup. Journal events the game wrote
    since the import stay. A backup of the state being rolled back is taken first."""
    target = rollback_target(conn, data_dir)
    if target is None or target["id"] != log_id:
        raise LookupError("Only the latest import can be rolled back, while its backup is kept.")
    detail = target["detail_json"]
    pre = archive.Archive(archive.backups_dir(data_dir) / detail["pre_import"])
    archive.save_backup(conn, data_dir, app_version=app_version, kind="pre-rollback")
    created = set(detail.get("created") or [])
    for fid in created:
        remove_commander(conn, data_dir, fid)
    handles = {c["fid"]: _commander_handle(i) for i, c in enumerate(pre.commanders)}
    modes = {}
    for fid, sections in (detail.get("modes") or {}).items():
        if fid == "settings":
            modes["settings"] = "override"
        elif fid in handles and fid not in created:
            modes.update({f"{handles[fid]}:{s}": "override" for s, m in sections.items() if m != "skip"})
    result = apply(conn, data_dir, pre, Plan(modes, {}), source=None, keep_after_id=detail.get("max_event_id"))
    for fid in result["rebuild"]:
        rebuild(conn, fid)
    with conn:
        conn.execute("UPDATE import_log SET status = 'rolled_back', detail = ? WHERE id = ?",
                     (json.dumps(detail | {"rolled_back_at": _now()}), log_id))
    return {"removed": sorted(created), "restored": sorted(f for f in result["modes"] if f != "settings")}


def _and(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def summary(detail_text: str | None, names: dict[str, str] | None = None) -> str:
    """A one-line description of an .edplanner import_log row, without FIDs (§15)."""
    try:
        d = json.loads(detail_text or "{}")
    except ValueError:
        return detail_text or ""
    names = (d.get("names") or {}) | (names or {})
    verbs = {"merge": "merged", "override": "replaced"}
    parts = []
    for fid, sections in (d.get("modes") or {}).items():
        if fid == "settings":
            parts.append(f"settings {verbs.get(sections, sections)}")
            continue
        by_mode: dict[str, list[str]] = {}
        for section, mode in sections.items():
            if mode in verbs:
                by_mode.setdefault(mode, []).append(section)
        chosen = "; ".join(f"{verbs[m]} {_and(s)}" for m, s in by_mode.items())
        new = " (new)" if fid in (d.get("created") or []) else ""
        parts.append(f"{names.get(fid, 'a commander')}{new}: {chosen}")
    text = "; ".join(parts) or "nothing chosen"
    if d.get("pre_import"):
        text += f" · backed up first to {d['pre_import']}"
    return text
