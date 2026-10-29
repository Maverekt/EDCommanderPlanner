"""Single-instance lock, port selection and runtime.json (DESIGN §2, process lifecycle)."""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PORT = 8732
PORT_TRIES = 10
LOCK_NAME = "planner.lock"
RUNTIME_NAME = "runtime.json"


class InstanceLock:
    """An OS-level lock on a file in the data folder; released when the process exits."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / LOCK_NAME
        self._fh = None

    def acquire(self) -> bool:
        fh = open(self.path, "a+b")
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_port(preferred: int = DEFAULT_PORT) -> int:
    """Preferred port, then the next 10, then whatever the OS assigns."""
    for port in range(preferred, preferred + PORT_TRIES + 1):
        if _port_free(port):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_runtime(data_dir: Path, port: int) -> None:
    payload = {"port": port, "pid": os.getpid(),
               "started": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    (data_dir / RUNTIME_NAME).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_runtime_port(data_dir: Path) -> int | None:
    try:
        return int(json.loads((data_dir / RUNTIME_NAME).read_text(encoding="utf-8"))["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def clear_runtime(data_dir: Path) -> None:
    try:
        (data_dir / RUNTIME_NAME).unlink()
    except OSError:
        pass
