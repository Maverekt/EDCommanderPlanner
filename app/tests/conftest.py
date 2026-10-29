"""Synthetic journals shaped like the verified Live journal (DESIGN Appendix A).

The FID is a placeholder; nothing here comes from a real commander.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core import db, paths

FID = "F0000001"
FID_2 = "F0000002"


def line(ts: str, event: str, **fields) -> str:
    # Same key order and spacing as the game: `{ "timestamp":"…", "event":"…", … }`
    body = {"timestamp": ts, "event": event, **fields}
    return "{ " + ", ".join(f'"{k}":{json.dumps(v)}' for k, v in body.items()) + " }"


def header(ts: str, gameversion: str = "4.4.2.1", part: int = 1) -> str:
    return line(ts, "Fileheader", part=part, language="English/UK", Odyssey=True,
                gameversion=gameversion, build="r334925/r0 ")


def write_journal(folder: Path, name: str, lines: list[str], mtime: float | None = None) -> Path:
    path = folder / name
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def session(ts_prefix: str, fid: str = FID, name: str = "Test Pilot", *, menu_events: int = 2) -> list[str]:
    """A login with pre-commander menu noise, signals and chat."""
    out = [header(f"{ts_prefix}T13:02:34Z")]
    for i in range(menu_events):
        out.append(line(f"{ts_prefix}T13:0{3 + i}:00Z", "CommunityGoal", CurrentGoals=[]))
    out.append(line(f"{ts_prefix}T13:10:00Z", "Music", MusicTrack="MainMenu"))
    out.append(line(f"{ts_prefix}T13:10:01Z", "Commander", FID=fid, Name=name))
    out.append(line(f"{ts_prefix}T13:10:02Z", "LoadGame", FID=fid, Commander=name, Ship="Anaconda",
                    Ship_Localised="Anaconda", ShipID=7, ShipName=" ", ShipIdent="TP-01",
                    Credits=1234567, Loan=0, GameMode="Solo", gameversion="4.4.2.1"))
    out.append(line(f"{ts_prefix}T13:10:03Z", "Rank", Combat=5, Trade=4, Empire=0, Federation=3))
    out.append(line(f"{ts_prefix}T13:11:00Z", "FSSSignalDiscovered", SystemAddress=1,
                    SignalName="CARRIER X1Y-2Z3", SignalType="FleetCarrier", IsStation=True))
    out.append(line(f"{ts_prefix}T13:11:01Z", "FSSSignalDiscovered", SystemAddress=1,
                    SignalName="$MULTIPLAYER_SCENARIO42_TITLE;", SignalType="NavBeacon"))
    out.append(line(f"{ts_prefix}T13:12:00Z", "ReceiveText", From="someone", Message="o7", Channel="local"))
    out.append(line(f"{ts_prefix}T13:13:00Z", "MaterialCollected", Category="Raw", Name="arsenic", Count=3))
    return out


@pytest.fixture()
def journal_dir(tmp_path: Path) -> Path:
    d = tmp_path / "journals"
    d.mkdir()
    return d


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "planner.db"
    db.init_db(path)
    return path


@pytest.fixture()
def filter_csv() -> Path:
    return paths.refdata_dir() / "ignored_events.csv"
