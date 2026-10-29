"""Material counts: the login `Materials` snapshot resets them, other events apply deltas (DESIGN §9.1, §10)."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.normalize import normalize_symbol
from app.core.projectors.base import Writer

EVENTS = ("Materials", "MaterialCollected", "MaterialDiscarded", "MaterialTrade", "EngineerCraft",
          "Synthesis", "TechnologyBroker", "MissionCompleted")
DELTA_EVENTS = EVENTS[1:]

# Storage limit per grade. The game still writes MaterialCollected for loot it
# can't store, so counts are clamped here.
MATERIAL_CAPS = {1: 300, 2: 250, 3: 200, 4: 150, 5: 100}


@dataclass
class State:
    counts: dict[str, int] = field(default_factory=dict)
    caps: dict[str, int] | None = None


def _cap(state: State, w: Writer, symbol: str) -> int | None:
    if state.caps is None:
        state.caps = {s: MATERIAL_CAPS[g] for s, g in w.conn.execute(
            "SELECT symbol, grade FROM ref_items WHERE kind = 'material' AND grade IS NOT NULL") if g in MATERIAL_CAPS}
    return state.caps.get(symbol)


def _apply(state: State, w: Writer, name: str | None, delta: int) -> None:
    symbol = normalize_symbol(name)
    if not symbol or not delta:
        return
    count = max(0, state.counts.get(symbol, 0) + delta)
    cap = _cap(state, w, symbol)
    if cap is not None:
        count = min(count, cap)
    state.counts[symbol] = count
    w.snapshot("material", symbol, count)


def _items(entries, name_key: str = "Name", count_key: str = "Count"):
    for entry in entries or []:
        if isinstance(entry, dict) and isinstance(entry.get(count_key), int):
            yield entry.get(name_key), entry[count_key]


def handle(state: State, w: Writer, name: str, event: dict) -> None:
    if name == "Materials":
        fresh = {normalize_symbol(n): c for group in ("Raw", "Manufactured", "Encoded")
                 for n, c in _items(event.get(group))}
        # Anything we were tracking that the game no longer lists is now zero.
        for symbol in state.counts.keys() - fresh.keys():
            if state.counts[symbol]:
                w.snapshot("material", symbol, 0)
        state.counts = {s: c for s, c in fresh.items() if s}
        for symbol, count in state.counts.items():
            w.snapshot("material", symbol, count)
    elif name == "MaterialCollected":
        _apply(state, w, event.get("Name"), event.get("Count", 0))
    elif name == "MaterialDiscarded":
        _apply(state, w, event.get("Name"), -event.get("Count", 0))
    elif name == "MaterialTrade":
        paid, received = event.get("Paid") or {}, event.get("Received") or {}
        _apply(state, w, paid.get("Material"), -paid.get("Quantity", 0))
        _apply(state, w, received.get("Material"), received.get("Quantity", 0))
    elif name == "EngineerCraft":
        for n, c in _items(event.get("Ingredients")):
            _apply(state, w, n, -c)
    elif name in ("Synthesis", "TechnologyBroker"):
        for n, c in _items(event.get("Materials")):
            _apply(state, w, n, -c)
    elif name == "MissionCompleted":
        for n, c in _items(event.get("MaterialsReward")):
            _apply(state, w, n, c)
