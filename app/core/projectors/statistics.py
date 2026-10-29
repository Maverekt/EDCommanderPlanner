"""Every numeric `Statistics` counter, plus wealth, Merc Coins and the login balance (DESIGN §9.1, §9.4)."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.projectors.base import Writer

EVENTS = ("Statistics", "LoadGame")


@dataclass
class State:
    pass


def counters(event: dict) -> dict[str, float]:
    """`{"Section.Counter": value}` for every numeric counter; new ones are picked up automatically."""
    out = {}
    for section, values in event.items():
        if not isinstance(values, dict):
            continue
        for name, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[f"{section}.{name}"] = value
    return out


def handle(state: State, w: Writer, name: str, event: dict) -> None:
    if name == "LoadGame":
        if isinstance(event.get("Credits"), int):
            w.snapshot("credits", "balance", event["Credits"])
        return
    values = counters(event)
    for path, value in values.items():
        w.snapshot("stat", path, value)
    if "Bank_Account.Current_Wealth" in values:
        w.snapshot("wealth", "total", values["Bank_Account.Current_Wealth"])
    if "Bank_Account.MercCoins_Current" in values:
        w.snapshot("merc_coins", "balance", values["Bank_Account.MercCoins_Current"])
