"""Current fit per ship, and module history including engineering (DESIGN §9.1, §6.1).

`EngineerCraft` is the authoritative record of engineering (engineer, roll,
quality, before/after stats). A `Loadout` diff only adds an `engineered` or
`experimental_applied` row when no craft explains the change, e.g. a
pre-engineered module.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from functools import lru_cache

from app.core import paths
from app.core.normalize import normalize_symbol
from app.core.projectors.base import Writer

EVENTS = ("Loadout", "LoadGame", "ShipyardSwap", "EngineerCraft", "ModuleBuy", "ModuleSell", "ModuleSellRemote",
          "ModuleStore", "ModuleRetrieve", "ModuleSwap", "MassModuleStore", "FetchRemoteModule")


@lru_cache(maxsize=1)
def cosmetic_patterns() -> list[re.Pattern]:
    with (paths.refdata_dir() / "cosmetic_slots.csv").open(newline="", encoding="utf-8") as f:
        return [re.compile(row["pattern"]) for row in csv.DictReader(f)]


def is_cosmetic(slot: str | None) -> bool:
    return bool(slot) and any(p.search(slot) for p in cosmetic_patterns())


@dataclass
class State:
    ship_id: int | None = None
    fits: dict[int, dict[str, dict]] = field(default_factory=dict)        # ship -> slot -> module row
    crafted: set[tuple[int, str]] = field(default_factory=set)            # (ship, slot) crafted since last Loadout


def _row(m: dict) -> dict:
    eng = m.get("Engineering") or {}
    return {
        "slot": m.get("Slot"),
        "item": normalize_symbol(m.get("Item")),
        "blueprint": normalize_symbol(eng.get("BlueprintName")),
        "blueprint_id": eng.get("BlueprintID"),
        "grade": eng.get("Level"),
        "quality": eng.get("Quality"),
        "experimental": normalize_symbol(eng.get("ExperimentalEffect")),
        "engineer_id": eng.get("EngineerID"),
        "modifiers": eng.get("Modifiers"),
    }


def _loadout(state: State, w: Writer, e: dict) -> None:
    ship_id = e.get("ShipID")
    if not isinstance(ship_id, int):
        return
    state.ship_id = ship_id
    rows = [_row(m) for m in e.get("Modules") or [] if not is_cosmetic(m.get("Slot"))]
    previous = state.fits.get(ship_id)
    if previous is not None:
        for r in rows:
            before = previous.get(r["slot"])
            if not before or before["item"] != r["item"] or (ship_id, r["slot"]) in state.crafted:
                continue  # new module in the slot, or a craft already recorded this change
            if r["blueprint"] and (r["blueprint"], r["grade"]) != (before["blueprint"], before["grade"]):
                w.module_event("engineered", r["item"], ship_id=ship_id, slot=r["slot"], blueprint=r["blueprint"],
                               grade_from=before["grade"], grade_to=r["grade"], quality=r["quality"],
                               experimental=r["experimental"], engineer_id=r["engineer_id"],
                               detail={"modifiers": r["modifiers"], "source": "loadout"})
            elif r["experimental"] and r["experimental"] != before["experimental"]:
                w.module_event("experimental_applied", r["item"], ship_id=ship_id, slot=r["slot"],
                               blueprint=r["blueprint"], experimental=r["experimental"], engineer_id=r["engineer_id"],
                               detail={"modifiers": r["modifiers"], "source": "loadout"})
    state.fits[ship_id] = {r["slot"]: r for r in rows}
    state.crafted = {k for k in state.crafted if k[0] != ship_id}
    w.replace_fit(ship_id, rows)


def _craft(state: State, w: Writer, e: dict) -> None:
    slot = e.get("Slot")
    ship_id = state.ship_id
    before = (state.fits.get(ship_id) or {}).get(slot) if ship_id is not None else None
    grade_to = e.get("Level")
    blueprint = normalize_symbol(e.get("BlueprintName"))
    experimental = normalize_symbol(e.get("ApplyExperimentalEffect"))
    same_blueprint = before is not None and before.get("blueprint") == blueprint
    w.module_event(
        "experimental_applied" if experimental else "engineered", normalize_symbol(e.get("Module")),
        ship_id=ship_id, slot=slot, blueprint=blueprint,
        grade_from=before.get("grade") if same_blueprint else None, grade_to=grade_to,
        quality=e.get("Quality"), experimental=experimental, engineer_id=e.get("EngineerID"),
        detail={"modifiers": e.get("Modifiers"), "source": "craft"},
    )
    if ship_id is not None and slot:
        state.crafted.add((ship_id, slot))
        fit = state.fits.setdefault(ship_id, {})
        if before is not None:
            fit[slot] = before | {"blueprint": blueprint, "grade": grade_to, "quality": e.get("Quality"),
                                  "experimental": experimental or before.get("experimental")}


def _price(e: dict, key: str, sign: int) -> int | None:
    v = e.get(key)
    return sign * v if isinstance(v, int) and v else None


def handle(state: State, w: Writer, name: str, e: dict) -> None:
    ship = e.get("ShipID") if isinstance(e.get("ShipID"), int) else None
    if name == "Loadout":
        _loadout(state, w, e)
    elif name in ("LoadGame", "ShipyardSwap"):
        if ship is not None:
            state.ship_id = ship
    elif name == "EngineerCraft":
        _craft(state, w, e)
    elif name == "ModuleBuy":
        w.module_event("bought", normalize_symbol(e.get("BuyItem")), ship_id=ship, slot=e.get("Slot"),
                       credits_delta=_price(e, "BuyPrice", -1))
        w.module_event("sold", normalize_symbol(e.get("SellItem")), ship_id=ship, slot=e.get("Slot"),
                       credits_delta=_price(e, "SellPrice", 1))
        w.module_event("stored", normalize_symbol(e.get("StoredItem")), ship_id=ship, slot=e.get("Slot"))
    elif name == "ModuleSell":
        w.module_event("sold", normalize_symbol(e.get("SellItem")), ship_id=ship, slot=e.get("Slot"),
                       credits_delta=_price(e, "SellPrice", 1))
    elif name == "ModuleSellRemote":
        w.module_event("sold", normalize_symbol(e.get("SellItem")), credits_delta=_price(e, "SellPrice", 1),
                       detail={"remote": True})
    elif name == "ModuleStore":
        w.module_event("stored", normalize_symbol(e.get("StoredItem")), ship_id=ship, slot=e.get("Slot"))
    elif name == "ModuleRetrieve":
        w.module_event("retrieved", normalize_symbol(e.get("RetrievedItem")), ship_id=ship, slot=e.get("Slot"))
        w.module_event("stored", normalize_symbol(e.get("SwapOutItem")), ship_id=ship, slot=e.get("Slot"))
    elif name == "ModuleSwap":
        w.module_event("swapped", normalize_symbol(e.get("FromItem")), ship_id=ship, slot=e.get("FromSlot"),
                       detail={"to_slot": e.get("ToSlot")})
    elif name == "MassModuleStore":
        for item in e.get("Items") or []:
            w.module_event("stored", normalize_symbol(item.get("Name")), ship_id=ship, slot=item.get("Slot"))
    elif name == "FetchRemoteModule":
        w.module_event("transferred", normalize_symbol(e.get("StoredItem")), ship_id=ship,
                       credits_delta=_price(e, "TransferCost", -1))
