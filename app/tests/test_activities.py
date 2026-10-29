"""Phase 5e: activity sessions and stats, mining favourites and ship readiness, unlock items, self-ticking steps."""

import json

import pytest
from fastapi.testclient import TestClient

from app.core import db, goals as gc, paths
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.refdata.loader import ensure_loaded
from app.server import activity_views as av
from app.server import goal_views as gv
from app.server.app import TOKEN_HEADER, create_app
from app.server.context import AppContext

from .conftest import FID, header, line, write_journal

MINER = [
    {"Slot": "MediumHardpoint1", "Item": "hpt_mininglaser_fixed_medium", "On": True, "Priority": 0},
    {"Slot": "Slot01_Size4", "Item": "int_refinery_size4_class5", "On": True, "Priority": 0},
    {"Slot": "Slot02_Size3", "Item": "int_dronecontrol_prospector_size3_class5", "On": True, "Priority": 0},
    {"Slot": "Slot03_Size3", "Item": "int_dronecontrol_collection_size3_class5", "On": True, "Priority": 0},
    {"Slot": "Slot04_Size4", "Item": "int_fuelscoop_size4_class5", "On": True, "Priority": 0},
]


def journal():
    t = "2026-10-26T1"
    return [
        header("2026-10-26T10:00:00Z"),
        line("2026-10-26T10:00:01Z", "Commander", FID=FID, Name="Test Pilot"),
        line("2026-10-26T10:00:02Z", "LoadGame", FID=FID, Commander="Test Pilot", Ship="anaconda", ShipID=6, Credits=1_000_000),
        line("2026-10-26T10:00:03Z", "Statistics", Mining={"Quantity_Mined": 520}, Trading={"Market_Profits": 9000}),
        line("2026-10-26T10:00:04Z", "EngineerProgress", Engineers=[
            {"Engineer": "Selene Jean", "EngineerID": 300210, "Progress": "Unlocked", "RankProgress": 0, "Rank": 1},
            {"Engineer": "Marsha Hicks", "EngineerID": 300150, "Progress": "Invited"}]),
        line("2026-10-26T10:00:05Z", "Loadout", Ship="anaconda", ShipID=6, ShipName="", ShipIdent="MA-26A",
             HullValue=1, ModulesValue=1, Rebuy=1, MaxJumpRange=30.5, Modules=MINER),
        line("2026-10-26T10:00:06Z", "Materials", Raw=[], Manufactured=[], Encoded=[{"Name": "decodedemissiondata", "Count": 50}]),
        # trading: buy 100 t at 1,000, sell at 1,500
        line("2026-10-26T10:05:00Z", "Docked", MarketID=1, StationName="Alpha Port", StarSystem="Alpha"),
        line("2026-10-26T10:05:10Z", "MarketBuy", MarketID=1, Type="gold", Count=100, BuyPrice=1000, TotalCost=100000),
        line("2026-10-26T10:10:00Z", "FSDJump", StarSystem="Beta", JumpDist=12.5),
        line("2026-10-26T10:15:00Z", "Docked", MarketID=2, StationName="Beta Hub", StarSystem="Beta"),
        line("2026-10-26T10:15:10Z", "MarketSell", MarketID=2, Type="gold", Count=100, SellPrice=1500, TotalSale=150000,
             AvgPricePaid=1000),
        # mining: prospect, refine 3 t of osmium, sell it
        line("2026-10-26T10:20:00Z", "LaunchDrone", Type="Prospector"),
        line("2026-10-26T10:20:05Z", "ProspectedAsteroid", Materials=[{"Name": "osmium", "Proportion": 30.0}],
             Content="$AsteroidMaterialContent_High;", Remaining=100.0),
        *[line(f"2026-10-26T10:2{i}:00Z", "MiningRefined", Type="$osmium_name;", Type_Localised="Osmium") for i in (1, 2, 3)],
        line("2026-10-26T10:30:00Z", "MarketSell", MarketID=2, Type="osmium", Count=3, SellPrice=50000, TotalSale=150000,
             AvgPricePaid=0),
        # exploration and exobiology
        line("2026-10-26T10:40:00Z", "Scan", BodyName="Beta 1", SystemAddress=9, BodyID=1, WasDiscovered=False, WasMapped=False),
        line("2026-10-26T10:41:00Z", "SAAScanComplete", BodyName="Beta 1", SystemAddress=9, BodyID=1),
        line("2026-10-26T10:50:00Z", "ScanOrganic", ScanType="Analyse", Genus="$Codex_Ent_Bacterial_Genus_Name;",
             Species="$Codex_Ent_Bacterial_01_Name;", Species_Localised="Bacterium Aurasus", SystemAddress=9, Body=1),
        line("2026-10-26T10:55:00Z", "SellOrganicData", MarketID=2, BioData=[
            {"Genus": "$Codex_Ent_Bacterial_Genus_Name;", "Species": "$Codex_Ent_Bacterial_01_Name;",
             "Species_Localised": "Bacterium Aurasus", "Value": 1000000, "Bonus": 4000000}]),
        line("2026-10-26T10:56:00Z", "ScanOrganic", ScanType="Analyse", Species="$Codex_Ent_Bacterial_01_Name;",
             Species_Localised="Bacterium Aurasus", SystemAddress=9, Body=2),
        line("2026-10-26T10:57:00Z", "Cargo", Vessel="Ship", Count=12, Inventory=[{"Name": "painite", "Count": 12, "Stolen": 0}]),
        line("2026-10-26T11:00:00Z", "SuitLoadout", SuitName="explorationsuit_class3", SuitName_Localised="Artemis Suit"),
    ]


@pytest.fixture()
def conn(db_path, journal_dir, filter_csv):
    ensure_loaded(db_path, paths.refdata_dir())
    write_journal(journal_dir, "Journal.01.log", journal())
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    c = db.connect(db_path)
    yield c
    c.close()


def test_sessions_and_trading(conn):
    s = av.sessions(conn, FID)
    assert len(s) == 1 and s[0]["hours"] == pytest.approx(1.0, abs=0.01)
    t = av.trading_view(conn, FID, "session")
    assert t["profit"] == 50000 and t["bought"] == 100 and t["sold"] == 100      # osmium sale is mining, not trading
    assert t["ly"] == 12.5 and t["jumps"] == 1 and t["cr_per_hour"] == pytest.approx(50000, rel=0.02)
    assert t["routes"][0]["from"] == "Alpha Port · Alpha" and t["routes"][0]["to"] == "Beta Hub · Beta"


def test_mining_stats_and_readiness(conn):
    m = av.mining_view(conn, FID, "session")
    assert m["tons"] == 3 and m["prospected"] == 1 and m["content"] == {"High": 1} and m["sold_cr"] == 150000
    assert m["limpets"] == {"Prospector": 1}
    assert m["busy_hours"] * 60 == pytest.approx(2.92, abs=0.01)              # first prospect 10:20:05 → last refine 10:23
    assert m["tons_per_busy_hour"] is None                                      # under 3 minutes: no rate, not a silly one
    assert m["methods"]["laser"]["ready"] and not m["methods"]["core"]["ready"]
    assert set(m["methods"]["core"]["missing"]) == {"Seismic Charge Launcher", "Pulse Wave Analyser"}
    sid = av.add_spot(conn, FID, {"commodity": "painite", "method": "core", "system": "HIP 1"})
    spot = av.mining_view(conn, FID)["spots"][0]
    assert spot["id"] == sid and not spot["readiness"]["ready"] and spot["usual"]
    with pytest.raises(ValueError):
        av.add_spot(conn, FID, {"commodity": "painite", "method": "dynamite"})
    av.delete_spot(conn, FID, sid)
    with pytest.raises(LookupError):
        av.delete_spot(conn, FID, sid)


def test_exploration_and_exobiology(conn):
    x = av.exploration_view(conn, FID, "all")
    assert x["bodies"] == 1 and x["first_discovered"] == 1 and x["mapped"] == 1 and x["first_mapped"] == 1
    assert x["jump_range"] == 30.5 and x["kit"]["ready"]                           # fuel scoop fitted
    e = av.exobiology_view(conn, FID, "all")
    assert e["species_count"] == 2 and e["sold_cr"] == 5_000_000 and e["bonus"] == 4_000_000
    assert [u["value"] for u in e["unsold"]] == [1_000_000]                      # analysed after the sale
    assert e["artemis"] == {"name": "Artemis Suit", "grade": 3}


def test_unlock_items_by_activity(conn, tmp_path):
    u = av.unlock_items(conn, FID)
    mining = {r["engineer"]: r for r in u["mining"]}
    assert "Selene Jean" not in mining                                             # already unlocked
    assert mining["Marsha Hicks"]["qty"] == 10 and mining["Marsha Hicks"]["mined"] and mining["Marsha Hicks"]["have"] == 0
    cargo = tmp_path / "Cargo.json"
    cargo.write_text(json.dumps({"Vessel": "Ship", "Inventory": [{"Name": "osmium", "Count": 11}]}), encoding="utf-8")
    assert {r["engineer"]: r for r in av.unlock_items(conn, FID, cargo)["mining"]}["Marsha Hicks"]["enough"]


def test_checklist_steps_tick_themselves(conn):
    gc.load_template_goals(conn, FID, gv.template())
    g = {x["title"]: x for x in gv.plan_view(conn, FID, 0)["goals"]}
    path = g["Goal Example - Mining path"]
    assert "Fit a laser miner: mining laser and refinery" in path["steps_auto"]
    assert "Mine 500 t of ore (Selene Jean's invite)" in path["steps_auto"]          # Statistics: 520 t
    assert "Keep 10 mined Painite for Selene Jean" in path["steps_auto"]            # Selene is unlocked (and 12 in the hold)
    assert "Keep 10 mined Osmium for Marsha Hicks" not in path["steps_auto"]
    assert "Own a Type-11 Prospector" not in path["steps_auto"] and not path["done"]
    assert g["Goal Example - Exobiology (on foot)"]["steps_auto"] == ["Buy an Artemis suit (Pioneer Supplies)"]   # G3, not G5
    assert "Fuel scoop" in g["Goal Example - Explorer kit"]["steps_auto"]
    with pytest.raises(ValueError):
        gc.parse_step_rule("fitted:warp_core")


H = {TOKEN_HEADER: "tok"}


def test_all_time_separates_lifetime_from_journal_figures(conn, db_path, journal_dir):
    life = av.lifetime(conn, FID, "mining")
    assert [(r["label"], r["value"]) for r in life["rows"]] == [("Mined", 520)] and life["as_of"]
    assert av.lifetime(conn, FID, "trading")["rows"][0]["value"] == 9000
    ctx = AppContext(data_dir=db_path.parent, journal_dir=journal_dir, port=8732, token="tok", version="test")
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8732") as client:
        page = client.get("/activities?tab=mining&s=all").text
        assert "Mining · lifetime" in page and "Mining · from your journals since 26 Oct 2026" in page
        assert page.index("Mining · lifetime") < page.index("Mining · from your journals")
        session = client.get("/activities?tab=mining&s=session").text
        assert "Mining · lifetime" not in session and "Lifetime (Statistics): mined 520 t." in session


def test_activities_pages_and_api(conn, db_path, journal_dir):
    ctx = AppContext(data_dir=db_path.parent, journal_dir=journal_dir, port=8732, token="tok", version="test")
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8732") as client:
        for tab in ("trading", "mining", "exploration", "exobiology"):
            assert client.get(f"/activities?tab={tab}&s=all").status_code == 200
        assert "Engineer unlocks</h2><span class=\"marker\">from mining" in client.get("/activities?tab=mining").text
        assert client.post("/api/mining/spots", json={"commodity": "osmium", "method": "laser"}, headers=H).status_code == 200
        assert client.post("/api/activities/mining/ship", json={"ship_id": "6"}, headers=H).status_code == 200
        assert client.post("/api/activities/mining/ship", json={"ship_id": "999"}, headers=H).status_code == 400
        assert client.post("/api/activities/fishing/ship", json={"ship_id": "6"}, headers=H).status_code == 400
