"""Credit ledger with LoadGame reconciliation (DESIGN §9.3).

Mapped events become itemized rows via refdata/ledger_map.csv. At each
`LoadGame`, `gap = balance_now − (balance_at_previous_LoadGame + Σ credit rows
between them)`; a non-zero gap is written as one `unattributed` row.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.core import paths
from app.core.ledger_rules import LedgerMap
from app.core.normalize import normalize_symbol
from app.core.projectors.base import Writer


@lru_cache(maxsize=1)
def ledger_map() -> LedgerMap:
    return LedgerMap.from_csv(paths.refdata_dir() / "ledger_map.csv")


EVENTS = tuple(sorted(ledger_map().events | {"LoadGame"}))


@dataclass
class State:
    anchor: int | None = None      # balance at the last LoadGame
    since_anchor: int = 0          # Σ credit rows since then


def handle(state: State, w: Writer, name: str, event: dict) -> None:
    if name == "LoadGame":
        credits = event.get("Credits")
        if not isinstance(credits, int):
            return
        if state.anchor is not None:
            gap = credits - (state.anchor + state.since_anchor)
            if gap:
                w.ledger("cr", "unattributed", None, gap,
                         {"previous_balance": state.anchor, "explained": state.since_anchor, "balance": credits})
        state.anchor = credits
        state.since_anchor = 0
        return
    for entry in ledger_map().entries(name, event):
        w.ledger(entry.currency, entry.category, normalize_symbol(entry.item), entry.amount, {"event": name})
        if entry.currency == "cr":
            state.since_anchor += entry.amount
