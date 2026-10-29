"""Live watcher: tail the journal and Status.json every second, project, push SSE (DESIGN §5.8, §10)."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.core import db
from app.core.journal.ingest_filter import IngestFilter
from app.core.journal.tail import JournalTail, StoredEvent, TailStats
from app.core.projectors import LiveProjection

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0

# Which page areas each event refreshes (§13: the page re-fetches only that area).
AREAS = {
    "materials": {"Materials", "MaterialCollected", "MaterialDiscarded", "MaterialTrade", "EngineerCraft",
                  "Synthesis", "TechnologyBroker", "MissionCompleted"},
    "progress": {"Rank", "Progress", "Promotion", "Reputation", "EngineerProgress"},
    "overview": {"Statistics"},
    "live": {"LoadGame", "Loadout", "ShipyardSwap", "ShipyardNew", "ShipyardBuy", "Shutdown", "Statistics",
             "Commander", "Fileheader"},
    "activities": {"MiningRefined", "ProspectedAsteroid", "AsteroidCracked", "LaunchDrone", "MarketBuy", "MarketSell",
                   "FSDJump", "Docked", "Cargo", "Scan", "FSSDiscoveryScan", "SAAScanComplete", "CodexEntry",
                   "MultiSellExplorationData", "SellExplorationData", "ScanOrganic", "SellOrganicData", "LoadGame",
                   "MaterialCollected", "SuitLoadout", "BuySuit"},
    "ships": {"Loadout", "EngineerCraft", "ModuleBuy", "ModuleSell", "ModuleStore", "ModuleRetrieve",
              "ModuleSwap", "MassModuleStore", "ShipyardSwap", "ShipyardNew", "ShipyardSell", "StoredModules",
              "FetchRemoteModule", "ModuleSellRemote", "CarrierBuy", "CarrierStats", "CarrierFinance",
              "CarrierBankTransfer", "CarrierDepositFuel", "CarrierCrewServices", "CarrierJump", "CarrierLocation",
              "CarrierJumpRequest", "CarrierJumpCancelled", "CarrierNameChange", "CarrierDecommission",
              "CarrierCancelDecommission"},
}


def areas_for(names: set[str]) -> set[str]:
    return {area for area, events in AREAS.items() if names & events}


@dataclass
class LiveState:
    """What the watcher knows right now, read by the pages."""

    status: dict | None = None          # last parsed Status.json
    status_read_at: float | None = None
    tail: TailStats = field(default_factory=TailStats)
    running: bool = False
    last_tick: float | None = None
    error: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": dict(self.status) if self.status else None,
                "running": self.running,
                "last_tick": self.last_tick,
                "error": self.error,
                "tail_path": self.tail.path,
                "tail_offset": self.tail.offset,
                "tail_added": self.tail.events_added,
                "tail_dropped": sum(self.tail.events_dropped.values()),
                "tail_last_event_ts": self.tail.last_event_ts,
                "tail_reattributed": self.tail.reattributed,
            }


class Watcher:
    def __init__(self, db_path: Path, journal_dir: Path, filter_csv: Path, projection: LiveProjection,
                 state: LiveState, publish: Callable[[str, str | None], None], stop: threading.Event,
                 wait_for: Callable[[], None] = lambda: None):
        self.db_path = db_path
        self.journal_dir = journal_dir
        self.filter_csv = filter_csv
        self.projection = projection
        self.state = state
        self.publish = publish
        self.stop = stop
        self.wait_for = wait_for
        self._status_mtime: float | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="journal-watcher", daemon=True)
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        self.wait_for()  # let the first backfill finish before tailing
        conn = db.connect(self.db_path)
        tail = JournalTail(conn, self.journal_dir, IngestFilter.from_csv(self.filter_csv))
        self.state.tail = tail.stats
        with self.state._lock:
            self.state.running = True
        try:
            while not self.stop.is_set():
                try:
                    self.tick(conn, tail)
                    with self.state._lock:
                        self.state.error = None
                except Exception as exc:  # keep watching; surface in Diagnostics
                    log.exception("live tick failed")
                    with self.state._lock:
                        self.state.error = str(exc)
                self.stop.wait(POLL_SECONDS)
        finally:
            with self.state._lock:
                self.state.running = False
            conn.close()

    def tick(self, conn, tail: JournalTail) -> set[str]:
        events: list[StoredEvent] = tail.tick()
        if events:
            self.projection.apply(conn, events)
        names = {e[3] for e in events}
        areas = areas_for(names)
        if self._read_status():
            areas.add("live")
        with self.state._lock:
            self.state.last_tick = time.time()
        fids = {e[1] for e in events} or {None}
        for area in sorted(areas):
            for fid in fids:
                self.publish(area, fid)
        return areas

    def _read_status(self) -> bool:
        path = self.journal_dir / "Status.json"
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._status_mtime:
            return False
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False  # mid-write; the next tick reads it
        self._status_mtime = mtime
        with self.state._lock:
            self.state.status = status
            self.state.status_read_at = time.time()
        return True
