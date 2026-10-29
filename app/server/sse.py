"""Server-Sent Events: `{area, fid}` messages; the page re-fetches only that area (DESIGN §13)."""

from __future__ import annotations

import asyncio
import json
import threading


class Broadcaster:
    """Thread-safe fan-out from worker threads to each open `/events/stream`."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: set[asyncio.Queue] = set()
        self._lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._queues.discard(q)

    def publish(self, area: str, fid: str | None = None) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        message = json.dumps({"area": area, "fid": fid})
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            loop.call_soon_threadsafe(_offer, q, message)


def _offer(q: asyncio.Queue, message: str) -> None:
    try:
        q.put_nowait(message)
    except asyncio.QueueFull:
        pass  # slow client; it re-fetches on the next message anyway
