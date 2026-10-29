"""Phase 4: ledger + reconciliation, ships, modules and engineering, charts, history views."""

import json

import pytest

from app.core import db, paths, projectors
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.ledger_rules import LedgerMap, evaluate, parse_amount
from app.core.refdata.loader import ensure_loaded
from app.server import charts, history_views as hv

from .conftest import FID, header, line, write_journal

CANNON_MODS = [{"Label": "DamagePerSecond", "Value": 17.7, "OriginalValue": 16.5, "LessIsGood": 0},
               {"Label": "Damage", "Value": 37.0, "OriginalValue": 37.4, "LessIsGood": 0}]


def loadout(ts, ship_id=6, *, level=None, quality=None, experimental=None, extra=()):
    cannon = {"Slot": "LargeHardpoint2", "Item": "hpt_cannon_gimbal_large", "On": True}
    if level:
        cannon["Engineering"] = {"Engineer": "Tod 'The Blaster' McQuinn", "EngineerID": 300260,
                                 "BlueprintName": "Weapon_RapidFire", "Level": level, "Quality": quality or 0.5,
                                 "Modifiers": CANNON_MODS}
        if experimental:
            cannon["Engineering"]["ExperimentalEffect"] = experimental
    modules = [cannon, {"Slot": "PaintJob", "Item": "paintjob_anaconda_black"},
               {"Slot": "FrameShiftDrive", "Item": "int_hyperdrive_size5_class5"}, *extra]
    return line(ts, "Loadout", Ship="anaconda", ShipID=ship_id, ShipName="", ShipIdent="MA-26A", Modules=modules)


def session_lines():
    return [
        header("2026-10-24T10:00:00Z"),
        line("2026-10-24T10:00:01Z", "Commander", FID=FID, Name="Test Pilot"),
        line("2026-10-24T10:00:02Z", "LoadGame", FID=FID, Commander="Test Pilot", Ship="anaconda", ShipID=6,
             Credits=1_000_000),
        line("2026-10-24T10:00:05Z", "Statistics", Bank_Account={"Spent_On_Ships": 0, "Spent_On_Fuel": 0,
                                                                  "Current_Wealth": 2_000_000},
             Trading={"Market_Profits": 0}),
        loadout("2026-10-24T10:00:06Z"),
        line("2026-10-24T10:05:00Z", "MarketBuy", Type="gold", Count=10, BuyPrice=9000, TotalCost=90_000),
        line("2026-10-24T10:20:00Z", "MarketSell", Type="gold", Count=10, SellPrice=11000, TotalSale=110_000,
             AvgPricePaid=9000),
        line("2026-10-24T10:21:00Z", "MarketSell", Type="narcotics", Count=1, TotalSale=5_000, BlackMarket=True),
        line("2026-10-24T10:22:00Z", "RefuelAll", Cost=500, Amount=10.0),
        line("2026-10-24T10:23:00Z", "SellOrganicData", BioData=[{"Value": 1000, "Bonus": 500}, {"Value": 2000, "Bonus": 0}]),
        line("2026-10-24T10:30:00Z", "EngineerCraft", Slot="LargeHardpoint2", Module="hpt_cannon_gimbal_large",
             Ingredients=[], Engineer="Tod 'The Blaster' McQuinn", EngineerID=300260,
             BlueprintName="Weapon_RapidFire", Level=1, Quality=0.5, Modifiers=CANNON_MODS),
        loadout("2026-10-24T10:30:05Z", level=1, quality=0.5),
        line("2026-10-24T10:40:00Z", "ShipyardBuy", ShipType="krait_light", ShipPrice=40_000, StoreOldShip="anaconda",
             StoreShipID=6),
        line("2026-10-24T10:40:05Z", "ShipyardNew", ShipType="krait_light", NewShipID=19),
        line("2026-10-24T10:45:00Z", "ShipyardSell", ShipType="viper", SellShipID=10, ShipPrice=20_000),
        line("2026-10-24T10:50:00Z", "ModuleBuy", Slot="Slot01_Size6", BuyItem="$int_cargorack_size5_class1_name;",
             BuyPrice=10_000, SellItem="$int_cargorack_size4_class1_name;", SellPrice=3_000, Ship="krait_light",
             ShipID=19),
        # Next login: balance explains everything except a 1,234 cr gap
        header("2026-10-25T10:00:00Z"),
        line("2026-10-25T10:00:01Z", "Commander", FID=FID, Name="Test Pilot"),
        line("2026-10-25T10:00:02Z", "LoadGame", FID=FID, Commander="Test Pilot", Ship="anaconda", ShipID=6,
             Credits=1_000_000 - 90_000 + 110_000 + 5_000 - 500 + 3_500 - 40_000 + 20_000 - 10_000 + 3_000 + 1_234),
        line("2026-10-25T10:00:05Z", "Statistics", Bank_Account={"Spent_On_Ships": 40_000, "Spent_On_Fuel": 500,
                                                                  "Current_Wealth": 2_100_000},
             Trading={"Market_Profits": 20_000}),
    ]


@pytest.fixture()
def conn(db_path, journal_dir, filter_csv):
    ensure_loaded(db_path, paths.refdata_dir())
    write_journal(journal_dir, "Journal.01.log", session_lines())
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    c = db.connect(db_path)
    yield c
    c.close()


def rows(c, sql, params=()):
    return [tuple(r) for r in c.execute(sql, params)]


# -- ledger rules ------------------------------------------------------------------

def test_amount_expressions():
    assert parse_amount("TotalSale") == (None, ["TotalSale"])
    assert parse_amount("sum(BioData.Value+BioData.Bonus)") == ("BioData", ["Value", "Bonus"])
    assert evaluate("sum(BioData.Value+BioData.Bonus)", {"BioData": [{"Value": 1, "Bonus": 2}, {"Value": 3}]}) == 6
    with pytest.raises(ValueError):
        parse_amount("sum(A.x+B.y)")


def test_first_matching_condition_wins_and_groups_split():
    m = LedgerMap.from_csv(paths.refdata_dir() / "ledger_map.csv")
    assert [(e.category, e.amount) for e in m.entries("MarketSell", {"TotalSale": 5, "BlackMarket": True})] == [("smuggling", 5)]
    assert [(e.category, e.amount) for e in m.entries("MarketSell", {"TotalSale": 5})] == [("trade", 5)]
    both = m.entries("ModuleBuy", {"BuyPrice": 10, "SellPrice": 3, "BuyItem": "a", "SellItem": "b"})
    assert [(e.amount, e.item) for e in both] == [(-10, "a"), (3, "b")]
    assert m.entries("ModuleBuy", {"BuyPrice": 10}) and len(m.entries("ModuleBuy", {"BuyPrice": 10})) == 1


# -- ledger projector -------------------------------------------------------------

def test_ledger_rows_and_reconciliation(conn):
    by_cat = dict(rows(conn, "SELECT category, SUM(amount_delta) FROM ledger GROUP BY category"))
    assert by_cat["trade"] == 20_000 and by_cat["smuggling"] == 5_000
    assert by_cat["fuel"] == -500 and by_cat["exobiology"] == 3_500
    assert by_cat["ships"] == -40_000 + 20_000 and by_cat["modules"] == -10_000 + 3_000
    assert by_cat["unattributed"] == 1_234  # the gap at the second LoadGame (DESIGN §9.3)
    detail = json.loads(conn.execute("SELECT detail_json FROM ledger WHERE category = 'unattributed'").fetchone()[0])
    assert detail["previous_balance"] == 1_000_000


def test_ledger_view_compares_with_statistics(conn):
    v = hv.ledger_credits_view(conn, FID, "session")
    cats = {c["key"]: c for c in v["categories"]}
    assert cats["ships"]["stat_delta"] == -40_000 and cats["ships"]["itemized"] == -40_000  # spend vs spend
    assert cats["ships"]["diff"] == 0 and cats["ships"]["net"] == -20_000
    assert cats["fuel"]["diff"] == 0
    assert cats["trade"]["stat_delta"] == 20_000 and cats["trade"]["itemized"] == 20_000
    assert cats["unattributed"]["net"] == 1_234


# -- ships and modules --------------------------------------------------------------

def test_ships_lifecycle(conn):
    ships = {r[0]: r for r in rows(conn, "SELECT ship_id, ship_type, ident, acquired_ts, disposed_how FROM ships")}
    assert ships[6][1:3] == ("anaconda", "MA-26A")
    assert ships[19][3] == "2026-10-24T10:40:05Z"
    assert ships[10][4] == "sold"


def test_fit_skips_cosmetics_and_keeps_engineering(conn):
    fit = {r[0]: r for r in rows(conn, "SELECT slot, item, blueprint, grade, quality FROM module_state WHERE ship_id = 6")}
    assert "PaintJob" not in fit
    assert fit["LargeHardpoint2"][2:] == ("weapon_rapidfire", 1, 0.5)


def test_engineering_history_from_craft_not_duplicated_by_loadout(conn):
    eng = rows(conn, "SELECT change, item, blueprint, grade_to, quality, engineer_id FROM module_history "
                     "WHERE change IN ('engineered', 'experimental_applied')")
    assert eng == [("engineered", "hpt_cannon_gimbal_large", "weapon_rapidfire", 1, 0.5, 300260)]
    view = hv.module_history_view(conn, FID, "engineered")[0]
    assert view["blueprint_name"] == "Rapid fire" and view["engineer_name"] == "Tod 'The Blaster' McQuinn"
    dps = view["modifiers"][0]
    assert (dps["label"], dps["better"]) == ("Damage per second", True)
    assert view["modifiers"][1]["better"] is False


def test_loadout_diff_records_engineering_without_a_craft(db_path, journal_dir, filter_csv):
    ensure_loaded(db_path, paths.refdata_dir())
    lines = session_lines()[:5] + [loadout("2026-10-24T11:00:00Z", level=2, quality=1.0, experimental="special_auto_loader")]
    write_journal(journal_dir, "Journal.01.log", lines)
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    c = db.connect(db_path)
    assert rows(c, "SELECT change, grade_from, grade_to, experimental FROM module_history WHERE change = 'engineered'") == \
        [("engineered", None, 2, "special_auto_loader")]


def test_module_buy_sell(conn):
    assert rows(conn, "SELECT change, item, credits_delta FROM module_history WHERE change IN ('bought', 'sold') "
                      "ORDER BY change") == [("bought", "int_cargorack_size5_class1", -10_000),
                                             ("sold", "int_cargorack_size4_class1", 3_000)]


def test_ship_fits_view(conn):
    fits = hv.ship_fits(conn, FID)
    anaconda = next(f for f in fits if f["ship_id"] == 6)
    hardpoints = dict(anaconda["groups"])["Hardpoints"]
    assert hardpoints[0]["blueprint"] == "Rapid fire" and hardpoints[0]["engineer"] == "Tod 'The Blaster' McQuinn"
    assert anaconda["engineered"] == 1


# -- history and charts ---------------------------------------------------------------

def test_balance_chart_has_a_point_per_loadgame(conn):
    h = hv.history_view(conn, FID, "all", "combat", None)
    loadgames = rows(conn, "SELECT ts, json_extract(raw_json, '$.Credits') FROM events WHERE event = 'LoadGame' ORDER BY ts")
    assert [tuple(p) for p in h["balance_points"]] == loadgames
    assert [p["value"] for p in h["balance"]["points"]] == [v for _, v in loadgames]


def test_line_chart_geometry():
    c = charts.line_chart([("2026-01-01T00:00:00Z", 10), ("2026-10-01T00:00:00Z", 20), ("2026-10-02T00:00:00Z", 15)])
    xs = [p["x"] for p in c["points"]]
    assert xs[1] - xs[0] == pytest.approx(xs[2] - xs[1])  # evenly spaced despite the 9-month gap
    assert c["points"][1]["y"] < c["points"][0]["y"]       # higher value is higher up
    assert charts.line_chart([]) is None


def test_calendar_levels():
    from datetime import date
    cal = charts.calendar({"2026-10-24": 10, "2026-10-25": 1}, date(2026, 10, 25))
    cells = {c["day"]: c for c in cal["cells"]}
    assert cells["2026-10-24"]["level"] == 4 and cells["2026-10-25"]["level"] == 1 and cal["active_days"] == 2


def test_projector_version_forces_replay(conn, monkeypatch):
    assert not projectors.is_stale(conn)
    monkeypatch.setattr(projectors, "PROJECTOR_VERSION", projectors.PROJECTOR_VERSION + 1)
    assert projectors.is_stale(conn)


# -- engineer locations and material sources (operator request) ------------------------------

def test_engineer_locations_verified_and_moved(db_path, journal_dir, filter_csv):
    from app.server import views
    ensure_loaded(db_path, paths.refdata_dir())
    write_journal(journal_dir, "Journal.01.log", session_lines()[:3] + [
        # Farseer Inc's real market ID: the journal confirms the reference list.
        line("2026-10-24T11:00:00Z", "Docked", StationName="Farseer Inc", StarSystem="Deciat",
             SystemAddress=6681123623626, MarketID=128676487),
        # The Dweller's system address, but under another name: the journal wins and flags it.
        line("2026-10-24T12:00:00Z", "FSDJump", StarSystem="Wyrd Prime", SystemAddress=5031654888146),
    ])
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    c = db.connect(db_path)
    eng = {e["id"]: e for group in views.engineers_view(c, FID).values() for e in group}
    assert (eng["300100"]["system"], eng["300100"]["station"], eng["300100"]["verified"]) == ("Deciat", "Farseer Inc", True)
    assert (eng["300180"]["system"], eng["300180"]["moved"]) == ("Wyrd Prime", True)
    assert (eng["400013"]["system"], eng["400013"]["verified"], eng["400013"]["state"]) == ("Einheriar", False, "Undiscovered")
    assert all(e["system"] for e in eng.values()), "every engineer has a location, whatever its status"


def test_material_sources_match_symbol_type_and_family():
    from app.server.live_views import sources_for
    improvised = sources_for("improvisedcomponents", "Manufactured", "Mechanical Components")
    assert improvised[0] == "High Grade Emissions in systems in Civil Unrest"
    assert any("Nav Beacon" in s for s in improvised)  # shared HGE tips row (multi-target)
    shielding = sources_for("imperialshielding", "Manufactured", "Shielding")
    assert shielding[0].startswith("High Grade Emissions in Empire-controlled systems (any state)")
    # Guardian data: family notes before type notes, and the not-tradeable warning
    obelisk = sources_for("ancientbiologicaldata", "Encoded", "Guardian")
    assert "Can't be traded" in obelisk[0] and obelisk.index(next(s for s in obelisk if "Synuefe" in s)) < len(obelisk) - 1
    wake = sources_for("dataminedwake", "Encoded", "Wake Scans")
    assert wake[0].startswith("Scanning frame shift wakes")
    assert any("Koli Discii C 6 a" in s for s in sources_for("iron", "Raw", None))
    assert any("Guardian ruins" in s for s in sources_for("guardian_powercell", "Manufactured", "Guardian"))
    assert not any("Guardian ruins" in s for s in sources_for("iron", "Raw", None))


def test_bad_material_source_target_fails_build(tmp_path):
    import shutil
    from app.core.refdata.build import build
    ref = tmp_path / "refdata"
    shutil.copytree(paths.refdata_dir(), ref)
    with (ref / "material_sources.csv").open("a", encoding="utf-8", newline="") as f:
        f.write("unobtainium,Nowhere,test\n")
    assert any("unobtainium" in e for e in build(ref).errors)
