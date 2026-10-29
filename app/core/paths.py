"""Journal folder and data folder resolution (DESIGN §4)."""

from __future__ import annotations

import ctypes
import os
import sys
import uuid
from pathlib import Path

APP_DIR_NAME = "EDPlanner"
_JOURNAL_SUBDIR = Path("Frontier Developments") / "Elite Dangerous"
# FOLDERID_SavedGames
_SAVED_GAMES_GUID = "{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}"


def app_folder() -> Path:
    """Folder holding the exe when frozen, or the repository root in development."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """Root of bundled read-only resources (templates, static, refdata)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_folder()))
    return app_folder()


def refdata_dir() -> Path:
    return resource_dir() / "refdata"


def _is_writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".write-test-{uuid.uuid4().hex}"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def data_dir() -> Path:
    """`<app folder>\\data` if writable, otherwise `%LOCALAPPDATA%\\EDPlanner`."""
    preferred = app_folder() / "data"
    if _is_writable(preferred):
        return preferred
    fallback = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_DIR_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _saved_games_folder() -> Path | None:
    if sys.platform != "win32":
        return None

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    u = uuid.UUID(_SAVED_GAMES_GUID)
    guid = GUID(u.time_low, u.time_mid, u.time_hi_version, (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    out = ctypes.c_wchar_p()
    try:
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
    except (AttributeError, OSError):
        return None
    if hr != 0 or not out.value:
        return None
    path = Path(out.value)
    ctypes.windll.ole32.CoTaskMemFree(out)
    return path


def default_journal_dir() -> Path:
    saved_games = _saved_games_folder() or Path(os.environ.get("USERPROFILE", Path.home())) / "Saved Games"
    return saved_games / _JOURNAL_SUBDIR


def journal_dir(override: str | None = None) -> Path:
    return Path(override) if override else default_journal_dir()
