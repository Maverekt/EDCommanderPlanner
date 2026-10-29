"""Phase 5a: goal model, flags, requirements, funding and the Goals API."""

import pytest
from fastapi.testclient import TestClient

from app.core import db, goals as gc, paths
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.refdata.loader import ensure_loaded
from app.server import goal_views as gv
from app.server.app import TOKEN_HEADER, create_app
from app.server.context import AppContext

from .conftest import FID, header, line, write_journal

PANTHER_VALUE = 290_000_000
CONDA_REBUY = 33_000_000


def journal(credits=2_000_000_000, empire=0, empire_progress=100, federation=12):
    return [
        header("2026-10-25T13:00:00Z"),  # conftest headers carry "Odyssey": true
        line("2026-10-25T13:00:01Z", "Commander", FID=FID, Name="Test Pilot"),
        line("2026-10-25T13:00:02Z", "LoadGame", FID=FID, Commander="Test Pilot", Ship="anaconda", ShipID=6,
             Credits=credits),
        line("2026-10-25T13:00:03Z", "Rank", Combat=3, Trade=10, Explore=7, Soldier=2, Exobiologist=0, Empire=empire,
             Federation=federation, CQC=0),
        line("2026-10-25T13:00:03Z", "Progress", Combat=35, Trade=58, Explore=3, Soldier=19, Exobiologist=0,
             Empire=empire_progress, Federation=8, CQC=29),
        line("2026-10-25T13:00:04Z", "Reputation", Empire=10.0, Federation=99.0, Alliance=33.0, Independent=2.0),
        line("2026-10-25T13:00:05Z", "Statistics", Bank_Account={"Current_Wealth": 3_000_000_000},
             Mining={"Quantity_Mined": 846}, Trading={"Markets_Traded_With": 23}, Combat={"Combat_Bonds": 58},
             Exploration={"Greatest_Distance_From_Start": 445.0}),
        line("2026-10-25T13:00:06Z", "Loadout", Ship="anaconda", ShipID=6, ShipName="", ShipIdent="MA-26A",
             HullValue=140_000_000, ModulesValue=514_000_000, Rebuy=CONDA_REBUY, Modules=[]),
        line("2026-10-25T13:00:07Z", "Loadout", Ship="panthermkii", ShipID=18, ShipName="", ShipIdent="MA-HL6",
             HullValue=200_000_000, ModulesValue=PANTHER_VALUE - 200_000_000, Rebuy=14_500_000, Modules=[]),
    ]


@pytest.fixture()
def conn(db_path, journal_dir, filter_csv):
    ensure_loaded(db_path, paths.refdata_dir())
    write_journal(journal_dir, "Journal.01.log", journal())
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    c = db.connect(db_path)
    yield c
    c.close()


def plan(c, credits=2_000_000_000):
    return gv.plan_view(c, FID, credits)


# -- rules and flags ----------------------------------------------------------------------

def test_auto_rules():
    assert gc.parse_auto_rule("stat:Mining.Quantity_Mined>=500") == ("stat", "Mining.Quantity_Mined", 500.0)
    assert gc.parse_auto_rule("rep:empire>=35") == ("rep", "empire", 35.0)
    assert gc.parse_auto_rule("header:Odyssey") == ("header", "Odyssey", 1.0)
    with pytest.raises(ValueError):
        gc.parse_auto_rule("stat:Mining.Quantity_Mined > 500")


def test_flags_auto_from_journal_and_manual_override(conn):
    f = gv.flag_states(conn, FID)
    assert f["ODYSSEY"]["value"] == "yes" and f["ODYSSEY"]["source"] == "auto"
    assert f["MINED500"]["value"] == "yes" and f["MINED500"]["auto_detail"] == "846 of 500"
    assert f["MARKETS50"]["value"] == "no" and f["BONDS50"]["value"] == "yes"
    assert f["ALLIANCE_FRIENDLY"]["value"] == "no" and f["FED_FRIENDLY"]["value"] == "yes"
    assert f["SUMMERLAND"]["value"] == "no"            # rank:empire>=7 (Baron)
    assert f["CONDA2_CREDITS"]["value"] == "unknown"   # manual-only flag
    gc.set_flag(conn, FID, "MARKETS50", "yes")         # you know better than the counter
    assert gv.flag_states(conn, FID)["MARKETS50"]["source"] == "manual"
    gc.set_flag(conn, FID, "MARKETS50", "unknown")     # back to auto
    assert gv.flag_states(conn, FID)["MARKETS50"]["source"] == "auto"


# -- template and funding -------------------------------------------------------------------

def test_template_loads_once(conn):
    assert gc.load_template_goals(conn, FID, gv.template()) == 11
    assert gc.load_template_goals(conn, FID, gv.template()) == 0
    titles = [r[0] for r in conn.execute("SELECT title FROM goals ORDER BY priority")]
    assert titles[0] == "Ship Build Example - Hauling & trading" and titles[-1] == "Goal Example - Explorer kit"


def test_funding_follows_the_workbook_rules(conn):
    gc.load_template_goals(conn, FID, gv.template())
    p = plan(conn)
    s = p["summary"]
    assert s["reserve"] == 3 * CONDA_REBUY and s["reserve_ship"]["type_name"] == "Anaconda"
    assert s["available"] == 2_000_000_000 - 3 * CONDA_REBUY
    g = {x["title"]: x for x in p["goals"]}
    panther = g["Ship Build Example - Hauling & trading"]
    assert panther["owned"] and panther["value"] == PANTHER_VALUE
    assert panther["remaining"] == 531_000_000 - PANTHER_VALUE
    assert panther["next_step"].startswith("Finish fitting/engineering: Panther Clipper MkII")
    corvette = g["Ship Build Example - Combat (PvE)"]
    assert corvette["reqs_met"] and corvette["cumulative"] == panther["remaining"] + 657_969_450
    assert corvette["next_step"] == "Go buy/fit: Federal Corvette" and corvette["fundable"]
    cutter = g["Ship Build Example - Imperial flagship"]
    assert not cutter["reqs_met"] and "12 to go" in cutter["next_step"]
    assert "promotion available now" in cutter["next_step"]
    carrier = g["Goal Example - Mobile base"]
    before = carrier["cumulative"] - carrier["remaining"]
    assert carrier["funded"] == pytest.approx((s["available"] - before) / 5_487_200_000)
    assert carrier["next_step"].startswith("Earn ") and not carrier["fundable"]
    conda2 = g["Ship Build Example - Multirole (new)"]
    assert conda2["price_unknown"] and conda2["remaining"] == 0
    assert s["next_goal"]["title"] == "Ship Build Example - Hauling & trading"
    assert "Promotion ready" in p["imperial_step"]


def test_reserve_setting_and_selling_ships(conn):
    gc.load_template_goals(conn, FID, gv.template())
    db.set_setting(conn, "reserve_rebuys", "5")
    db.set_setting(conn, f"selling_ships:{FID}", "[18]")
    conn.commit()
    s = plan(conn)["summary"]
    assert s["reserve"] == 5 * CONDA_REBUY
    assert s["pending"] == PANTHER_VALUE
    assert s["available"] == 2_000_000_000 + PANTHER_VALUE - 5 * CONDA_REBUY


def test_elite_tiers_meet_elite_requirement(conn):
    gc.insert_goal(conn, FID, gc.columns_from_form({"kind": "checklist", "title": "Elite trader perk",
                                                     "requires": [{"rank": "trade", "min": 8}], "steps": "Celebrate"}))
    conn.commit()
    g = next(x for x in plan(conn)["goals"] if x["title"] == "Elite trader perk")
    assert g["requires"][0]["label"] == "Trade Elite (any tier)" and g["requires"][0]["met"]  # Trade is Elite II


def test_done_parked_and_checklist_steps(conn):
    gc.load_template_goals(conn, FID, gv.template())
    ids = {r[1]: r[0] for r in conn.execute("SELECT id, title FROM goals")}
    gc.set_status(conn, ids["Ship Build Example - Hauling & trading"], "done")
    gc.set_status(conn, ids["Goal Example - Mobile base"], "parked")
    p = plan(conn)
    g = {x["title"]: x for x in p["goals"]}
    assert g["Ship Build Example - Hauling & trading"]["done"] and g["Ship Build Example - Hauling & trading"]["remaining"] == 0
    assert g["Ship Build Example - Combat (PvE)"]["cumulative"] == 657_969_450
    assert p["summary"]["still_needed"] == sum(x["remaining"] for x in p["goals"] if not x["done"] and not x["parked"])
    exo = ids["Goal Example - Exobiology (on foot)"]
    for step in gc.step_texts(gv.template()["goals"][4]["steps"])[0]:
        gc.toggle_step(conn, exo, step, True)
    assert {x["title"]: x for x in plan(conn)["goals"]}["Goal Example - Exobiology (on foot)"]["auto_done"]


def test_rank_and_credit_goals_track_live(conn):
    gc.insert_goal(conn, FID, gc.columns_from_form({"kind": "rank", "title": "Duke", "target_key": "empire",
                                                     "target_value": "12"}))
    gc.insert_goal(conn, FID, gc.columns_from_form({"kind": "credits", "title": "Buffer", "target_value": "1500000000"}))
    conn.commit()
    g = {x["title"]: x for x in plan(conn)["goals"]}
    assert not g["Duke"]["done"] and g["Duke"]["progress"] == 0
    assert g["Buffer"]["auto_done"] and g["Buffer"]["remaining"] == 0


def test_form_validation():
    with pytest.raises(ValueError):
        gc.columns_from_form({"kind": "ship", "title": "  "})
    with pytest.raises(ValueError):
        gc.columns_from_form({"kind": "ship", "title": "x", "cost": "lots"})
    with pytest.raises(ValueError):
        gc.columns_from_form({"kind": "spaceship", "title": "x"})
    cols = gc.columns_from_form({"kind": "ship", "title": "T", "cost": "1,000", "requires": [{"flag": "ODYSSEY"},
                                                                                             {"bogus": 1}]})
    assert '"credits": 1000' in cols["requirements_json"] and '"requires": [{"flag": "ODYSSEY"}]' in cols["detail_json"]


# -- API --------------------------------------------------------------------------------------

@pytest.fixture()
def client(conn, db_path, journal_dir):
    ctx = AppContext(data_dir=db_path.parent, journal_dir=journal_dir, port=8732, token="tok", version="test")
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8732") as c:
        yield c


H = {TOKEN_HEADER: "tok"}


def test_goals_api_round_trip(client, conn):
    assert client.post("/api/goals/load-template").status_code == 403
    assert client.post("/api/goals/load-template", headers=H).json() == {"added": 11}
    r = client.post("/api/goals", json={"kind": "credits", "title": "Buffer", "target_value": "5000000000"}, headers=H)
    gid = r.json()["id"]
    assert client.post(f"/api/goals/{gid}/move", json={"delta": -1}, headers=H).status_code == 200
    assert conn.execute("SELECT priority FROM goals WHERE id = ?", (gid,)).fetchone()[0] == 11
    assert client.post(f"/api/goals/{gid}/update", json={"title": "Bigger buffer", "notes": "for the carrier"},
                       headers=H).status_code == 200
    assert client.post(f"/api/goals/{gid}/status", json={"status": "nonsense"}, headers=H).status_code == 400
    assert client.post(f"/api/goals/{gid}/status", json={"status": "parked"}, headers=H).status_code == 200
    html = client.get("/goals").text
    assert "Bigger buffer" in html and "Parked" in html and "Affordable now" in html
    assert client.post(f"/api/goals/{gid}/delete", headers=H).status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 11
    assert client.post("/api/goals/99999/delete", headers=H).status_code == 404


def test_flags_and_plan_api(client):
    assert client.post("/api/flags", json={"key": "NOPE", "value": "yes"}, headers=H).status_code == 400
    assert client.post("/api/flags", json={"key": "SOL", "value": "maybe"}, headers=H).status_code == 400
    assert client.post("/api/flags", json={"key": "SOL", "value": "yes"}, headers=H).status_code == 200
    assert client.post("/api/plan", json={"reserve_rebuys": "lots"}, headers=H).status_code == 400
    assert client.post("/api/plan", json={"reserve_rebuys": "2", "selling": [18]}, headers=H).status_code == 200
    html = client.get("/partials/goals").text
    assert 'value="2" data-plan-setting="reserve_rebuys"' in html


def test_dashboard_has_next_up(client):
    client.post("/api/goals/load-template", headers=H)
    html = client.get("/").text
    assert "Next up" in html and "Hauling &amp; trading" in html
