"""`.edplanner` files: collect, write, list and read them (DESIGN §12.1).

A zip holding manifest.json (format, versions, commanders and a SHA-256 per file), then per
commander `commanders/<fid>/` with commander.json, events.jsonl.gz, goals.json, plans.json and
notes/<id>.md, and a global settings.json. Derived tables are not written: an import rebuilds
them from the events, so a file never carries rows an older or newer projector made.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app.core import db, notes

FORMAT = "edplanner"
FORMAT_VERSION = 1
SUFFIX = ".edplanner"
KEEP = 20                       # backups of each kind kept in data/backups (§12.1)
KEEP_INCOMING = 5               # uploaded files waiting for review
MAX_MEMBER = 1 << 30            # 1 GiB uncompressed per file, far above any real history
KINDS = {"backup": "Backup", "pre-import": "Before an import", "pre-rollback": "Before a rollback",
         "pre-update": "Before an update", "export": "Export"}

# Per-commander tables the user writes (§8, migrations 0005–0010). Goals have their own file.
PLAN_TABLES = ("flags", "engineer_pins", "mining_spots", "builds", "eng_plan", "ship_fits")
# Settings that travel. Global ones go in settings.json; per-commander ones are stored as
# "<name>:<fid>" and travel in plans.json without the FID. Paths, the port and internal
# markers (last_fid, refdata_*, derived_built_through, app_version) never do.
GLOBAL_SETTINGS = ("eng_rolls", "reserve_rebuys", "hover_cards", "accent")
COMMANDER_SETTING = re.compile(r"^(activity_ship:[a-z]+|selling_ships):(.+)$")
EVENT_COLS = ("fid_status", "ts", "event", "raw_json", "hash", "source")
REF_RE = re.compile(r"^(incoming/)?[\w.() -]+\.edplanner$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8")


def tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _rows(conn: sqlite3.Connection, have: set[str], table: str, fid: str) -> list[dict]:
    if table not in have:          # an older database (the pre-update backup runs before migrations)
        return []
    return [{k: r[k] for k in r.keys() if k != "fid"}
            for r in conn.execute(f"SELECT * FROM {table} WHERE fid = ? ORDER BY rowid", (fid,))]


def commander_settings(conn: sqlite3.Connection, fid: str) -> dict[str, str]:
    out = {}
    for key, value in conn.execute("SELECT key, value FROM settings"):
        m = COMMANDER_SETTING.match(key)
        if m and m.group(2) == fid:
            out[m.group(1)] = value
    return out


def collect(conn: sqlite3.Connection, data_dir: Path, fids: list[str] | None = None) -> dict:
    """Everything a backup holds, read in one transaction so it is one consistent moment."""
    conn.row_factory = sqlite3.Row
    have = tables(conn)
    began = not conn.in_transaction
    if began:
        conn.execute("BEGIN")
    try:
        cmdrs = [dict(r) for r in conn.execute(
            "SELECT fid, name, first_seen, last_seen FROM commanders ORDER BY fid")]
        if fids is not None:
            cmdrs = [c for c in cmdrs if c["fid"] in set(fids)]
        out = {"commanders": {}, "settings": {}}
        for c in cmdrs:
            fid = c["fid"]
            events = [tuple(r) for r in conn.execute(
                f"SELECT {', '.join(EVENT_COLS)} FROM events WHERE fid = ? ORDER BY ts, id", (fid,))]
            plans = {t: _rows(conn, have, t, fid) for t in PLAN_TABLES}
            plans["settings"] = commander_settings(conn, fid)
            out["commanders"][fid] = {"commander": c, "events": events, "goals": _rows(conn, have, "goals", fid),
                                      "plans": plans, "notes": notes.raw(data_dir, fid)}
        for key in GLOBAL_SETTINGS:
            value = db.get_setting(conn, key)
            if value is not None:
                out["settings"][key] = value
        out["schema_version"] = db.current_version(conn)
        out["refdata_version"] = db.get_setting(conn, "refdata_version") or ""
    finally:
        if began:
            conn.rollback()
    return out


def write(fileobj, data: dict, *, app_version: str, kind: str = "backup") -> dict:
    """Write `data` (from `collect`) as a .edplanner zip; returns the manifest."""
    files: dict[str, bytes] = {}
    summary = []
    for fid, c in data["commanders"].items():
        base = f"commanders/{fid}/"
        lines = (json.dumps(dict(zip(EVENT_COLS, e)), ensure_ascii=False).encode("utf-8") + b"\n" for e in c["events"])
        files[base + "commander.json"] = _json(c["commander"])
        files[base + "events.jsonl.gz"] = gzip.compress(b"".join(lines), mtime=0)
        files[base + "goals.json"] = _json(c["goals"])
        files[base + "plans.json"] = _json(c["plans"])
        for note_id, text in c["notes"].items():
            files[f"{base}notes/{note_id}.md"] = text.encode("utf-8")
        summary.append({"fid": fid, "name": c["commander"]["name"], "events": len(c["events"]),
                        "goals": len(c["goals"]), "notes": len(c["notes"])})
    files["settings.json"] = _json(data["settings"])
    manifest = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "kind": kind,
        "app_version": app_version, "schema_version": data.get("schema_version"),
        "refdata_version": data.get("refdata_version"), "exported_at": _now(),
        "commanders": summary, "sections": ["history", "goals", "plans", "notes", "settings"],
        "files": {name: hashlib.sha256(payload).hexdigest() for name, payload in files.items()},
    }
    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", _json(manifest))
        for name, payload in files.items():
            zf.writestr(name, payload, compress_type=zipfile.ZIP_STORED if name.endswith(".gz") else zipfile.ZIP_DEFLATED)
    return manifest


def export_bytes(conn: sqlite3.Connection, data_dir: Path, fids: list[str] | None, *, app_version: str) -> bytes:
    buf = io.BytesIO()
    write(buf, collect(conn, data_dir, fids), app_version=app_version, kind="export")
    return buf.getvalue()


def backups_dir(data_dir: Path) -> Path:
    return data_dir / "backups"


def _unique(folder: Path, stem: str) -> Path:
    path, n = folder / f"{stem}{SUFFIX}", 1
    while path.exists():
        n += 1
        path = folder / f"{stem}-{n}{SUFFIX}"
    return path


def _prune(folder: Path, prefix: str, keep: int) -> None:
    old = sorted(folder.glob(f"{prefix}-*{SUFFIX}"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True)[keep:]
    for path in old:
        path.unlink(missing_ok=True)


def save_backup(conn: sqlite3.Connection, data_dir: Path, *, app_version: str, kind: str = "backup") -> Path:
    """Backup Now and the automatic backups: everything, to data/backups/<kind>-<time>.edplanner."""
    folder = backups_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = _unique(folder, f"{kind}-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    tmp = path.with_suffix(SUFFIX + ".tmp")
    with open(tmp, "wb") as f:
        write(f, collect(conn, data_dir), app_version=app_version, kind=kind)
    os.replace(tmp, path)
    _prune(folder, kind, KEEP)
    return path


def stage_upload(data_dir: Path, filename: str, payload: bytes) -> str:
    """Keep an uploaded file for the review page; returns its reference (`incoming/<name>`)."""
    folder = backups_dir(data_dir) / "incoming"
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w.() -]", "_", Path(filename or "import").name)[:80]
    stem = stem[: -len(SUFFIX)] if stem.lower().endswith(SUFFIX) else stem
    path = _unique(folder, f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{stem.strip(' .') or 'import'}")
    path.write_bytes(payload)
    _prune_incoming(folder)
    return f"incoming/{path.name}"


def _prune_incoming(folder: Path) -> None:
    for path in sorted(folder.glob(f"*{SUFFIX}"), key=lambda p: p.stat().st_mtime, reverse=True)[KEEP_INCOMING:]:
        path.unlink(missing_ok=True)


def resolve(data_dir: Path, ref: str) -> Path:
    """A backup reference from a page (`<name>` or `incoming/<name>`) as a path inside data/backups."""
    if not REF_RE.match(ref or "") or ".." in ref.split("/"):
        raise LookupError(ref)
    path = backups_dir(data_dir) / ref
    if not path.is_file():
        raise LookupError(ref)
    return path


def display_name(ref: str) -> str:
    """An uploaded file's own name, without the staging prefix."""
    name = ref.rsplit("/", 1)[-1]
    return name.split("_", 1)[1] if ref.startswith("incoming/") and "_" in name else name


def list_backups(data_dir: Path) -> list[dict]:
    folder = backups_dir(data_dir)
    out = []
    for path in sorted(folder.glob(f"*{SUFFIX}"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []:
        kind = next((k for k in sorted(KINDS, key=len, reverse=True) if path.name.startswith(k + "-")), "")
        row = {"name": path.name, "kind": KINDS.get(kind, "File"), "size": path.stat().st_size,
               "made": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "commanders": None, "error": None}
        try:
            with zipfile.ZipFile(path) as zf:
                manifest = json.loads(zf.read("manifest.json"))
            row["commanders"] = [c.get("name") for c in manifest.get("commanders", [])]
            row["made"] = manifest.get("exported_at") or row["made"]
        except (zipfile.BadZipFile, KeyError, ValueError, OSError):
            row["error"] = "unreadable"
        out.append(row)
    return out


class Archive:
    """A validated .edplanner: zip intact, manifest supported, every listed file present and
    matching its SHA-256. Only listed files are ever read."""

    def __init__(self, source: Path | bytes, name: str = ""):
        raw = source if isinstance(source, bytes) else Path(source).read_bytes()
        self.name = name or (Path(source).name if not isinstance(source, bytes) else "")
        self.size = len(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile:
            raise ValueError("This isn't a .edplanner file: it isn't a zip.") from None
        with zf:
            try:
                manifest = json.loads(zf.read("manifest.json"))
            except KeyError:
                raise ValueError("This isn't a .edplanner file: it has no manifest.") from None
            except (ValueError, zipfile.BadZipFile):
                raise ValueError("The file's manifest is damaged.") from None
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                raise ValueError("This isn't a .edplanner file.")
            version = manifest.get("format_version")
            if not isinstance(version, int) or version < 1:
                raise ValueError("The file's format version is missing.")
            if version > FORMAT_VERSION:
                raise ValueError(f"This file was made by a newer version of the planner "
                                 f"({manifest.get('app_version') or 'unknown'}); update the app to import it.")
            listed = manifest.get("files")
            if not isinstance(listed, dict):
                raise ValueError("The file's manifest has no file list.")
            self._files: dict[str, bytes] = {}
            for member, digest in listed.items():
                try:
                    info = zf.getinfo(member)
                except KeyError:
                    raise ValueError(f"The file is incomplete: {member} is missing.") from None
                if info.file_size > MAX_MEMBER:
                    raise ValueError(f"{member} is too large.")
                try:
                    payload = zf.read(member)
                except (zipfile.BadZipFile, OSError):
                    raise ValueError(f"The file is damaged: {member} can't be read.") from None
                if hashlib.sha256(payload).hexdigest() != digest:
                    raise ValueError(f"The file is damaged: {member} doesn't match its checksum.")
                self._files[member] = payload
        self.manifest = manifest
        self.commanders: list[dict] = []
        seen = set()
        for c in manifest.get("commanders") or []:
            fid = c.get("fid") if isinstance(c, dict) else None
            if not isinstance(fid, str) or not notes.FID_RE.match(fid) or fid in seen:
                raise ValueError("The file lists a commander it can't hold.")
            if f"commanders/{fid}/commander.json" not in self._files:
                raise ValueError("The file is incomplete: a commander's details are missing.")
            seen.add(fid)
            self.commanders.append({"fid": fid, "name": str(c.get("name") or "")})

    def _json(self, member: str, default):
        if member not in self._files:
            return default
        try:
            return json.loads(self._files[member])
        except ValueError:
            raise ValueError(f"The file is damaged: {member} isn't valid JSON.") from None

    def settings(self) -> dict[str, str]:
        raw = self._json("settings.json", {})
        return {k: str(v) for k, v in raw.items() if k in GLOBAL_SETTINGS} if isinstance(raw, dict) else {}

    def commander(self, fid: str) -> dict:
        """Same shape as one commander in `collect`."""
        base = f"commanders/{fid}/"
        events = []
        if base + "events.jsonl.gz" in self._files:
            with gzip.GzipFile(fileobj=io.BytesIO(self._files[base + "events.jsonl.gz"])) as gz:
                text = gz.read(MAX_MEMBER + 1)
            if len(text) > MAX_MEMBER:
                raise ValueError("The file's history is too large.")
            for raw in text.splitlines():
                if not raw.strip():
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    raise ValueError("The file is damaged: a history line isn't valid JSON.") from None
                if not all(isinstance(e.get(k), str) for k in ("ts", "event", "raw_json", "hash")):
                    raise ValueError("The file is damaged: a history line is incomplete.")
                events.append((str(e.get("fid_status") or "confirmed"), e["ts"], e["event"], e["raw_json"], e["hash"],
                               str(e.get("source") or "journal")))
        goals = self._json(base + "goals.json", [])
        plans = self._json(base + "plans.json", {})
        if not isinstance(goals, list) or not isinstance(plans, dict):
            raise ValueError("The file is damaged: a commander's goals or plans aren't readable.")
        plans = {t: [r for r in plans.get(t) or [] if isinstance(r, dict)] for t in PLAN_TABLES} | {
            "settings": {k: str(v) for k, v in (plans.get("settings") or {}).items()
                         if COMMANDER_SETTING.match(f"{k}:{fid}")}}
        note_texts = {}
        prefix = base + "notes/"
        for member in sorted(self._files):
            if member.startswith(prefix) and member.endswith(".md"):
                note_id = member[len(prefix):-3]
                if notes.ID_RE.match(note_id):
                    note_texts[note_id] = self._files[member].decode("utf-8", errors="replace")
        return {"commander": self._json(base + "commander.json", {}) | {"fid": fid}, "events": events,
                "goals": [g for g in goals if isinstance(g, dict)], "plans": plans, "notes": note_texts}
