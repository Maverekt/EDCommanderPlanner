"""Shared state for one running instance: paths, port, token, import and live status."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.core import db, paths
from app.core.backup import archive
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.live import LiveState, Watcher
from app.core.projectors import LiveProjection
from app.core.refdata.loader import RefdataStatus
from app.server.sse import Broadcaster


@dataclass
class AppContext:
    data_dir: Path
    journal_dir: Path
    port: int
    token: str
    version: str
    import_status: ImportStatus = field(default_factory=ImportStatus)
    broadcaster: Broadcaster = field(default_factory=Broadcaster)
    stop: threading.Event = field(default_factory=threading.Event)
    refdata: RefdataStatus | None = None
    projection: LiveProjection = field(default_factory=LiveProjection)
    live: LiveState = field(default_factory=LiveState)
    backup_lock: threading.Lock = field(default_factory=threading.Lock)
    on_accent: Callable | None = None          # the tray recolours its icon when the accent changes
    _import_thread: threading.Thread | None = None
    _watcher: Watcher | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "planner.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def filter_csv(self) -> Path:
        return paths.refdata_dir() / "ignored_events.csv"

    def backup_now(self, kind: str = "backup") -> Path:
        """Backup Now (Settings or the tray): everything, to data/backups/ (DESIGN §12.1)."""
        conn = db.connect(self.db_path)
        try:
            return archive.save_backup(conn, self.data_dir, app_version=self.version, kind=kind)
        finally:
            conn.close()

    def _backfill(self) -> None:
        run_backfill(self.db_path, self.journal_dir, self.filter_csv, self.import_status,
                     lambda: self.broadcaster.publish("import"), self.stop)
        # A rescan may have rebuilt derived data underneath the live projection.
        self.projection.invalidate()
        for area in ("progress", "materials", "live"):
            self.broadcaster.publish(area)

    def start_rescan(self) -> bool:
        """Run a backfill in the background; False if one is already running."""
        if self._import_thread and self._import_thread.is_alive():
            return False
        self._import_thread = threading.Thread(target=self._backfill, name="journal-import", daemon=True)
        self._import_thread.start()
        return True

    def wait_for_import(self, timeout: float | None = None) -> None:
        if self._import_thread:
            self._import_thread.join(timeout)

    def start_watcher(self) -> None:
        """Tail the journal and Status.json once the first backfill has finished."""
        self._watcher = Watcher(self.db_path, self.journal_dir, self.filter_csv, self.projection, self.live,
                                self.broadcaster.publish, self.stop, wait_for=self.wait_for_import)
        self._watcher.start()

    def stop_watcher(self, timeout: float = 5) -> None:
        if self._watcher:
            self._watcher.join(timeout)
