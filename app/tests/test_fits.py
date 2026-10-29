"""Saved fits: save, rename and delete, storage, and a saved fit beside the current one."""

import pytest
from fastapi.testclient import TestClient

from app.core import fits
from app.core.backup import archive
from app.core.journal.importer import ImportStatus, run_backfill
from app.server import fit_views
from app.server.app import TOKEN_HEADER, create_app
from app.server.context import AppContext

from .conftest import FID, header, line, write_journal
from .test_builds import T11, make_conn, owned_t11

H = {TOKEN_HEADER: "tok"}


def refit():
    """The same Type-11 after a refit: one booster stored, the DSS moved up a slot, a cargo rack in its place,
    the refinery sold and the FSD at G5."""
    mods = []
    for m in owned_t11():
        if m["Slot"] in ("TinyHardpoint2", "Slot08_Size2", "Slot09_Size1"):
            continue
        if m["Slot"] == "Slot10_Size1":
            mods.append(m | {"Slot": "Slot09_Size1"})
            m = {"Slot": "Slot10_Size1", "Item": "int_cargorack_size1_class1", "On": True, "Priority": 1, "Value": 1000}
        if m["Slot"] == "FrameShiftDrive":
            m = m | {"Engineering": m["Engineering"] | {"Level": 5}}
        mods.append(m)
    return mods


def second_session():
    return [
        header("2026-10-26T10:00:00Z"),
        line("2026-10-26T10:00:01Z", "Docked", StationName="Ray Gateway", StarSystem="Deciat", MarketID=1),
        line("2026-10-26T10:00:02Z", "StoredModules", MarketID=1, StationName="Ray Gateway", StarSystem="Deciat", Items=[
            {"Name": "$int_guardianfsdbooster_size1_name;", "StorageSlot": 7, "StarSystem": "Shinrarta Dezhra",
             "MarketID": 2, "TransferCost": 100, "TransferTime": 900, "BuyPrice": 405020, "Hot": False}]),
        line("2026-10-26T10:00:03Z", "ModuleStore", MarketID=1, Slot="TinyHardpoint2",
             StoredItem="$hpt_shieldbooster_size0_class5_name;", Ship="lakonminer", ShipID=T11, Hot=False),
        line("2026-10-26T10:00:04Z", "Loadout", Ship="lakonminer", ShipID=T11, ShipName="", ShipIdent="MM-01",
             HullValue=66_346_710, ModulesValue=19_000_000, Rebuy=4_000_000, Modules=refit()),
    ]


@pytest.fixture()
def refitted(db_path, journal_dir, filter_csv):
    """Fit 1 saved before the refit, then the refit played."""
    c = make_conn(db_path, journal_dir, filter_csv, owned_t11())
    fits.save(c, FID, T11)
    write_journal(journal_dir, "Journal.02.log", second_session())
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    yield c
    c.close()


def test_save_rename_delete(refitted, db_path):
    c = refitted
    first = fits.for_ship(c, FID, T11)[0]
    assert first["name"] == "Fit 1"
    second = fits.save(c, FID, T11)
    assert fits.get(c, FID, second)["name"] == "Fit 2"
    with pytest.raises(ValueError, match="already saved as “Fit 2”"):
        fits.save(c, FID, T11)
    with pytest.raises(ValueError, match="already has a fit called"):
        fits.rename(c, FID, second, "fit 1")
    fits.rename(c, FID, first["id"], "Mining (old)")
    with pytest.raises(ValueError, match="name"):
        fits.rename(c, FID, first["id"], "  ")
    fits.delete(c, FID, second)
    assert [r["name"] for r in fits.for_ship(c, FID, T11)] == ["Mining (old)"]
    with pytest.raises(LookupError):
        fits.delete(c, FID, second)
    with pytest.raises(LookupError):
        fits.save(c, FID, 999)
    saved = archive.collect(c, db_path.parent, [FID])["commanders"][FID]["plans"]["ship_fits"]
    assert [(r["ship_id"], r["name"]) for r in saved] == [(T11, "Mining (old)")]     # travels in a backup


def test_storage_follows_events(refitted):
    store = fits.storage(refitted, FID)
    assert store["as_of"] == "2026-10-26T10:00:02Z"
    where = {s["item"]: s["system"] for s in store["items"]}
    assert where == {"int_guardianfsdbooster_size1": "Shinrarta Dezhra", "hpt_shieldbooster_size0_class5": "Deciat"}


def test_saved_fit_beside_current(refitted):
    c = refitted
    ctx = fit_views.context(c, FID)
    old, = fit_views.ship_fits(c, FID, T11, ctx, "9.9.9")
    rows = {r["slot"]: r for r in old["rows"]}
    assert rows["TinyHardpoint2"]["status"] == "stored" and rows["TinyHardpoint2"]["note"] == "Stored at Deciat"
    assert rows["Slot09_Size1"]["note"] == "Stored at Shinrarta Dezhra"
    assert rows["Slot10_Size1"]["note"] == "Move from Optional 9 (size 1)"
    assert rows["Slot08_Size2"]["note"] == "Buy · 1,020,600 cr"
    assert rows["FrameShiftDrive"]["status"] == "differs" and "G3" in rows["FrameShiftDrive"]["saved_eng"]
    assert rows["Radar"]["status"] == "fitted" and rows["Radar"]["label"] == "Sensors"
    assert old["summary"] == "To refit: 2 in storage, 1 to move, 1 to buy, 1 engineered differently"
    fits.save(c, FID, T11)
    now = fit_views.ship_fits(c, FID, T11, fit_views.context(c, FID), "9.9.9")[1]
    assert now["matches"] and now["summary"] == "Matches the current fit"


def test_slot_labels():
    assert [fit_views.slot_label(s) for s in ("LargeHardpoint2", "TinyHardpoint3", "Slot04_Size5", "Military01",
                                              "FrameShiftDrive", "CargoHatch")] == \
        ["Large hardpoint 2", "Utility 3", "Optional 4 (size 5)", "Military 1", "Frame shift drive", "Cargo hatch"]


def test_api_and_page(db_path, journal_dir, filter_csv):
    make_conn(db_path, journal_dir, filter_csv, owned_t11()).close()
    ctx = AppContext(data_dir=db_path.parent, journal_dir=journal_dir, port=8732, token="tok", version="9.9.9")
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8732") as client:
        assert client.post(f"/api/ships/{T11}/fits").status_code == 403
        fid_ = client.post(f"/api/ships/{T11}/fits", headers=H).json()["id"]
        assert client.post(f"/api/ships/{T11}/fits", headers=H).status_code == 400
        assert client.post(f"/api/fits/{fid_}/rename", json={"name": "Core miner"}, headers=H).status_code == 200
        html = client.get("/ships").text
        assert "Save current fit" in html and f'data-fit-tab="{fid_}"' in html and "Core miner" in html
        assert "Matches the current fit" in html and f"slef-fit-{fid_}" in html
        assert client.post(f"/api/fits/{fid_}/delete", headers=H).status_code == 200
        assert client.post(f"/api/fits/{fid_}/delete", headers=H).status_code == 404
        assert client.post(f"/api/fits/{fid_}/explode", headers=H).status_code == 404


def test_carrier_bar(db_path, journal_dir, filter_csv):
    from datetime import datetime, timezone
    from app.server import carrier_views as cv
    c = make_conn(db_path, journal_dir, filter_csv, owned_t11())
    assert cv.carrier_view(c, FID) is None                                       # no carrier: the bar says so
    crew = [{"CrewRole": r, "Activated": True, "Enabled": on, "CrewName": "x"} for r, on in
            (("Captain", True), ("Refuel", True), ("Shipyard", False), ("Commodities", True))]
    write_journal(journal_dir, "Journal.03.log", [
        header("2026-10-27T10:00:00Z"),
        line("2026-10-27T10:00:01Z", "CarrierBuy", BoughtAtMarket=1, Location="Deciat", SystemAddress=1, Price=5_000_000_000,
             Variant="CarrierDockB", CarrierID=3700000000, Callsign="ABC-123"),
        line("2026-10-27T10:05:00Z", "CarrierStats", CarrierID=3700000000, Callsign="ABC-123", Name="WANDERER",
             DockingAccess="all", AllowNotorious=False, FuelLevel=500, JumpRangeCurr=500.0, JumpRangeMax=500.0,
             PendingDecommission=False, SpaceUsage={"TotalCapacity": 25000, "FreeSpace": 23000},
             Finance={"CarrierBalance": 120_000_000, "ReserveBalance": 0, "AvailableBalance": 120_000_000},
             Crew=crew, ShipPacks=[], ModulePacks=[{"PackTheme": "Weapons", "PackTier": 1}]),
        line("2026-10-27T10:06:00Z", "CarrierStats", CarrierID=999, CarrierType="SquadronCarrier", Name="NOT MINE",
             Finance={"CarrierBalance": 1}, Crew=[]),
        line("2026-10-27T10:07:00Z", "CarrierCrewServices", CarrierID=3700000000, CrewRole="Repair", Operation="Activate", CrewName="y"),
        line("2026-10-27T10:08:00Z", "CarrierDepositFuel", CarrierID=3700000000, Amount=200, Total=700),
        line("2026-10-27T10:09:00Z", "CarrierLocation", CarrierType="FleetCarrier", CarrierID=3700000000, StarSystem="Sol"),
    ])
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    v = cv.carrier_view(c, FID, now=datetime(2026, 10, 28, tzinfo=timezone.utc))
    assert (v["name"], v["callsign"], v["system"], v["balance"], v["fuel"]) == ("WANDERER", "ABC-123", "Sol", 120_000_000, 700)
    assert v["active"] == ["Refuel", "Repair"] and v["suspended"] == ["Shipyard"] and v["module_packs"] == 1
    assert v["upkeep"] == 5_000_000 + 1_500_000 + 1_500_000 + 1_800_000
    assert round(v["weeks_left"], 1) == round(120 / 9.8, 1) and v["funds_end"].startswith("2027-01-")
    write_journal(journal_dir, "Journal.04.log", [
        header("2026-10-28T10:00:00Z"),
        line("2026-10-28T10:00:01Z", "CarrierDecommission", CarrierID=3700000000, ScrapRefund=4_000_000_000, ScrapTime=1793777600),
    ])
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    assert cv.carrier_view(c, FID, now=datetime(2026, 10, 29, tzinfo=timezone.utc))["scrap_at"]
    assert cv.carrier_view(c, FID, now=datetime(2027, 1, 1, tzinfo=timezone.utc))["gone"]
