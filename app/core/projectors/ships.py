"""Ships owned over time: bought, seen, named, sold (DESIGN §9.1)."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.normalize import display_text, normalize_symbol
from app.core.projectors.base import Writer

EVENTS = ("LoadGame", "Loadout", "ShipyardNew", "ShipyardBuy", "ShipyardSell", "SellShipOnRebuy",
          "ShipyardSwap", "ShipyardTransfer", "StoredShips")


@dataclass
class State:
    pass


def _id(v) -> int | None:
    return v if isinstance(v, int) else None


def handle(state: State, w: Writer, name: str, e: dict) -> None:
    if name in ("LoadGame", "Loadout"):
        if _id(e.get("ShipID")) is not None:
            w.ship(e["ShipID"], normalize_symbol(e.get("Ship")), name=display_text(e.get("ShipName")),
                   ident=display_text(e.get("ShipIdent")))
    elif name == "ShipyardNew":
        if _id(e.get("NewShipID")) is not None:
            w.ship(e["NewShipID"], normalize_symbol(e.get("ShipType")), acquired=True)
    elif name == "ShipyardBuy":
        if _id(e.get("SellShipID")) is not None:
            w.ship(e["SellShipID"], normalize_symbol(e.get("SellOldShip")), disposed_how="sold")
        if _id(e.get("StoreShipID")) is not None:
            w.ship(e["StoreShipID"], normalize_symbol(e.get("StoreOldShip")))
    elif name == "ShipyardSell":
        if _id(e.get("SellShipID")) is not None:
            w.ship(e["SellShipID"], normalize_symbol(e.get("ShipType")), disposed_how="sold")
    elif name == "SellShipOnRebuy":
        sid = _id(e.get("SellShipId")) if "SellShipId" in e else _id(e.get("SellShipID"))
        if sid is not None:
            w.ship(sid, normalize_symbol(e.get("ShipType")), disposed_how="sold_on_rebuy")
    elif name == "ShipyardSwap":
        if _id(e.get("ShipID")) is not None:
            w.ship(e["ShipID"], normalize_symbol(e.get("ShipType")))
        if _id(e.get("StoreShipID")) is not None:
            w.ship(e["StoreShipID"], normalize_symbol(e.get("StoreOldShip")))
    elif name == "ShipyardTransfer":
        if _id(e.get("ShipID")) is not None:
            w.ship(e["ShipID"], normalize_symbol(e.get("ShipType")))
    elif name == "StoredShips":
        for ship in (e.get("ShipsHere") or []) + (e.get("ShipsRemote") or []):
            if _id(ship.get("ShipID")) is not None:
                w.ship(ship["ShipID"], normalize_symbol(ship.get("ShipType")), name=display_text(ship.get("Name")))
