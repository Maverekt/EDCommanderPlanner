"""SQLite access: connections, schema creation, migrations (DESIGN §8)."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
_HERE = Path(__file__).resolve().parent
_MIGRATION_NAME = re.compile(r"^(\d{4})_.+\.sql$")


def connect(db_path: Path, *, any_thread: bool = False) -> sqlite3.Connection:
    """`any_thread` for request connections: FastAPI may open, use and close one in different
    worker threads, though never concurrently."""
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=not any_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def current_version(conn: sqlite3.Connection) -> int:
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if not has_table:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return row[0] or 0


def init_db(db_path: Path) -> None:
    """Create the schema on first run, then apply any pending migrations."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        version = current_version(conn)
        if version == 0:
            conn.executescript((_HERE / "schema.sql").read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            conn.commit()
            version = SCHEMA_VERSION
        for path in sorted((_HERE / "migrations").glob("*.sql")):
            m = _MIGRATION_NAME.match(path.name)
            if not m or int(m.group(1)) <= version:
                continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (int(m.group(1)),))
            conn.commit()
    finally:
        conn.close()


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
