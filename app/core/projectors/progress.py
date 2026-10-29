"""Ranks, navy ranks, rank progress and superpower reputation (DESIGN §9.1)."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.projectors.base import Writer

EVENTS = ("Rank", "Progress", "Promotion", "Reputation")

# Journal key -> our track key. Soldier is the Mercenary rank, Explore is Exploration.
PILOTS = {"Combat": "combat", "Trade": "trade", "Explore": "explore", "Soldier": "soldier",
          "Exobiologist": "exobiologist", "CQC": "cqc"}
NAVY = {"Empire": "empire", "Federation": "federation"}
MAX_RANK = {**{k: 13 for k in PILOTS.values()}, **{k: 14 for k in NAVY.values()}}  # Elite V; Admiral / King
REPUTATION = {"Empire": "empire", "Federation": "federation", "Alliance": "alliance", "Independent": "independent"}


@dataclass
class State:
    rank: dict[str, int] = field(default_factory=dict)
    progress: dict[str, int] = field(default_factory=dict)


def _metric(journal_key: str) -> tuple[str, str] | None:
    if journal_key in PILOTS:
        return "rank", PILOTS[journal_key]
    if journal_key in NAVY:
        return "navy_rank", NAVY[journal_key]
    return None


def handle(state: State, w: Writer, name: str, event: dict) -> None:
    if name in ("Rank", "Promotion"):
        for jkey, value in event.items():
            m = _metric(jkey)
            if m is None or not isinstance(value, int):
                continue
            metric, key = m
            previous = state.rank.get(key)
            if previous is not None and value > previous:
                w.unlock(metric, key, **{"from": previous, "to": value})
            state.rank[key] = value
            w.snapshot(metric, key, value)

    elif name == "Progress":
        for jkey, value in event.items():
            m = _metric(jkey)
            if m is None or not isinstance(value, int):
                continue
            key = m[1]
            previous = state.progress.get(key)
            state.progress[key] = value
            w.snapshot("rank_progress", key, value)
            rank = state.rank.get(key)
            if value >= 100 and (previous is None or previous < 100) and rank is not None and rank < MAX_RANK[key]:
                w.unlock("promotion_available", key, rank=rank)

    elif name == "Reputation":
        for jkey, key in REPUTATION.items():
            value = event.get(jkey)
            if isinstance(value, (int, float)):
                w.snapshot("reputation", key, value)
