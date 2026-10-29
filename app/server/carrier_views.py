"""The fleet carrier bar on Ships: name, place, balance, upkeep, how long the money lasts, services.

Read from the carrier's journal events. `CarrierStats` (written when Carrier Management opens) is the
full picture; the finance, transfer, fuel, crew, jump and name events after it keep it current.

The journal never states upkeep, so it is estimated from the installed services at the weekly rates
below (the commonly quoted figures: about 34M a week with everything active, 16.5M with every optional
service suspended). Per-jump costs aren't counted.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

CORE_UPKEEP = 5_000_000
# Crew role -> (label, weekly upkeep active, weekly upkeep suspended). Captain, Commodities and
# CarrierFuel are core services, paid for by CORE_UPKEEP.
SERVICES = {
    "Refuel": ("Refuel", 1_500_000, 750_000),
    "Repair": ("Repair", 1_500_000, 750_000),
    "Rearm": ("Rearm", 1_500_000, 750_000),
    "Shipyard": ("Shipyard", 6_500_000, 1_800_000),
    "Outfitting": ("Outfitting", 5_000_000, 1_500_000),
    "BlackMarket": ("Secure warehouse", 2_000_000, 1_250_000),
    "Exploration": ("Universal Cartographics", 1_850_000, 700_000),
    "VoucherRedemption": ("Redemption office", 850_000, 500_000),
    "VistaGenomics": ("Vista Genomics", 1_500_000, 700_000),
    "PioneerSupplies": ("Pioneer Supplies", 5_000_000, 1_500_000),
    "Bartender": ("Bar", 1_750_000, 1_250_000),
}
EVENTS = ("CarrierBuy", "CarrierStats", "CarrierFinance", "CarrierBankTransfer", "CarrierDepositFuel",
          "CarrierCrewServices", "CarrierJump", "CarrierLocation", "CarrierJumpRequest", "CarrierJumpCancelled",
          "CarrierNameChange", "CarrierDecommission", "CarrierCancelDecommission")


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def carrier_view(conn: sqlite3.Connection, fid: str, now: datetime | None = None) -> dict | None:
    """The commander's carrier as of its latest events, or None when there's none (or it was scrapped)."""
    now = now or datetime.now(timezone.utc)
    marks = ",".join("?" * len(EVENTS))
    c: dict | None = None
    for ts, raw in conn.execute(f"SELECT ts, raw_json FROM events WHERE fid = ? AND event IN ({marks}) ORDER BY ts, id",
                                (fid, *EVENTS)):
        e = json.loads(raw)
        name = e.get("event")
        cid = e.get("CarrierID", e.get("MarketID"))
        if e.get("CarrierType") not in (None, "FleetCarrier"):
            continue                                   # a squadron carrier
        if name == "CarrierBuy" or (name == "CarrierStats" and (c is None or c["id"] != cid)):
            c = {"id": cid, "callsign": e.get("Callsign"), "name": None, "system": e.get("Location"), "balance": None,
                 "reserve": None, "available": None, "fuel": None, "free_space": None, "capacity": None,
                 "services": {}, "ship_packs": 0, "module_packs": 0, "scrap_at": None, "jump": None,
                 "as_of": ts, "seen_stats": False}
        if c is None or cid != c["id"]:
            continue                                   # another commander's carrier
        c["as_of"] = ts
        if name == "CarrierStats":
            fin, space = e.get("Finance") or {}, e.get("SpaceUsage") or {}
            c.update(name=e.get("Name"), callsign=e.get("Callsign") or c["callsign"], fuel=e.get("FuelLevel"),
                     balance=fin.get("CarrierBalance"), reserve=fin.get("ReserveBalance"),
                     available=fin.get("AvailableBalance"), free_space=space.get("FreeSpace"),
                     capacity=space.get("TotalCapacity"), ship_packs=len(e.get("ShipPacks") or []),
                     module_packs=len(e.get("ModulePacks") or []), seen_stats=True)
            c["services"] = {cr["CrewRole"]: ("active" if cr.get("Enabled") else "suspended")
                             for cr in e.get("Crew") or [] if cr.get("Activated") and cr.get("CrewRole") in SERVICES}
        elif name in ("CarrierFinance", "CarrierBankTransfer"):
            for key, field in (("balance", "CarrierBalance"), ("reserve", "ReserveBalance"), ("available", "AvailableBalance")):
                if e.get(field) is not None:
                    c[key] = e[field]
        elif name == "CarrierDepositFuel":
            c["fuel"] = e.get("Total", c["fuel"])
        elif name == "CarrierCrewServices" and e.get("CrewRole") in SERVICES:
            op = e.get("Operation")
            if op in ("Activate", "Resume"):
                c["services"][e["CrewRole"]] = "active"
            elif op == "Pause":
                c["services"][e["CrewRole"]] = "suspended"
            elif op == "Deactivate":
                c["services"].pop(e["CrewRole"], None)
        elif name in ("CarrierJump", "CarrierLocation"):
            c["system"], c["jump"] = e.get("StarSystem") or c["system"], None
        elif name == "CarrierJumpRequest":
            c["jump"] = {"to": e.get("SystemName"), "at": e.get("DepartureTime")}
        elif name == "CarrierJumpCancelled":
            c["jump"] = None
        elif name == "CarrierNameChange":
            c.update(name=e.get("Name") or c["name"], callsign=e.get("Callsign") or c["callsign"])
        elif name == "CarrierDecommission":
            c["scrap_at"] = datetime.fromtimestamp(e["ScrapTime"], timezone.utc) if e.get("ScrapTime") else None
        elif name == "CarrierCancelDecommission":
            c["scrap_at"] = None
    if c is None:
        return None
    if c["scrap_at"] and c["scrap_at"] <= now:
        return {"gone": True, "callsign": c["callsign"], "name": c["name"], "scrapped": c["scrap_at"].strftime("%Y-%m-%dT%H:%M:%SZ")}

    upkeep = CORE_UPKEEP + sum(SERVICES[r][1 if s == "active" else 2] for r, s in c["services"].items())
    weeks = c["balance"] / upkeep if c["balance"] is not None else None
    ends = (_ts(c["as_of"]) + timedelta(weeks=weeks)).strftime("%Y-%m-%dT%H:%M:%SZ") if weeks is not None else None
    jump = c["jump"] if c["jump"] and c["jump"].get("at") and _ts(c["jump"]["at"]) > now else None
    return c | {
        "gone": False, "upkeep": upkeep, "weeks_left": weeks, "funds_end": ends, "jump": jump,
        "active": [SERVICES[r][0] for r, s in sorted(c["services"].items()) if s == "active"],
        "suspended": [SERVICES[r][0] for r, s in sorted(c["services"].items()) if s == "suspended"],
        "upkeep_note": "Estimate: core 5M a week plus each installed service (less while suspended). "
                       "The journal doesn't record upkeep; Carrier Management shows the real figure. Jumps cost extra.",
        "scrap_at": c["scrap_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if c["scrap_at"] else None,
    }
