"""Phase 5b: engineer unlock chain, next steps and blueprint pins."""

import pytest
from fastapi.testclient import TestClient

from app.core import db, paths
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.refdata.loader import ensure_loaded
from app.server import engineer_views as ev
from app.server.app import TOKEN_HEADER, create_app
from app.server.context import AppContext

from .conftest import FID, header, line, write_journal

FELICITY, TOD, LIZ, MARSHA, SELENE, JURI, HERO = 300100, 300260, 300080, 300150, 300210, 300250, 400003


def journal():
    return [
        header("2026-10-25T13:00:00Z"),
        line("2026-10-25T13:00:01Z", "Commander", FID=FID, Name="Test Pilot"),
        line("2026-10-25T13:00:02Z", "LoadGame", FID=FID, Commander="Test Pilot", Ship="anaconda", ShipID=6,
             Credits=1_000_000),
        line("2026-10-25T13:00:03Z", "Rank", Combat=3, Trade=10, Explore=7, Soldier=2, Exobiologist=0, Empire=0,
             Federation=12, CQC=0),
        line("2026-10-25T13:00:03Z", "Progress", Combat=35, Trade=58, Explore=3, Soldier=19, Exobiologist=0, Empire=0,
             Federation=8, CQC=0),
        line("2026-10-25T13:00:04Z", "Statistics", Mining={"Quantity_Mined": 100}),
        line("2026-10-25T13:00:05Z", "EngineerProgress", Engineers=[
            {"Engineer": "Felicity Farseer", "EngineerID": FELICITY, "Progress": "Unlocked", "RankProgress": 60, "Rank": 2},
            {"Engineer": "Tod 'The Blaster' McQuinn", "EngineerID": TOD, "Progress": "Unlocked", "RankProgress": 4, "Rank": 3},
            {"Engineer": "Liz Ryder", "EngineerID": LIZ, "Progress": "Known"},
            {"Engineer": "Marsha Hicks", "EngineerID": MARSHA, "Progress": "Invited"},
            {"Engineer": "Hero Ferrari", "EngineerID": HERO, "Progress": "Known"},
        ]),
    ]


@pytest.fixture()
def conn(db_path, journal_dir, filter_csv):
    ensure_loaded(db_path, paths.refdata_dir())
    write_journal(journal_dir, "Journal.01.log", journal())
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    c = db.connect(db_path)
    yield c
    c.close()


def rows(c):
    return {e["engineer_id"]: e for e in ev.chain_view(c, FID)["all"]}


def test_offers_loaded_from_coriolis(conn):
    offers = {(m, b): g for m, b, g in conn.execute(
        "SELECT module_type, blueprint, max_grade FROM ref_engineer_blueprints WHERE engineer_id = ?", (TOD,))}
    assert offers[("mc", "weapon_overcharged")] == 5     # coriolis key MC_Overcharged resolves by fdname
    assert offers[("fc", "weapon_overcharged")] == 3


def test_next_steps_follow_the_chain(conn):
    r = rows(conn)
    assert r[FELICITY]["next_step"].startswith("Rank up to G3 (60%): ")
    # Tod at G3 refers Selene Jean; Felicity at G2 doesn't refer Juri Ishmaak yet.
    assert r[SELENE]["referred"] and r[SELENE]["next_step"].startswith("Discover")
    assert not r[JURI]["referred"] and r[JURI]["next_step"] == "Get referred: rank Felicity Farseer to G3 (now G2)"
    assert r[LIZ]["next_step"].startswith("Meet the requirement: Cordial or Friendly with Eurybia Blue Mafia")
    assert r[MARSHA]["next_step"].startswith("Unlock: deliver 10 mined Osmium")
    assert r[HERO]["onfoot"] and r[HERO]["priority"] is None


def test_rank_meet_rule_and_permit(conn):
    r = rows(conn)
    assert r[FELICITY]["meet"] == {"met": True, "detail": "Exploration Pioneer"}
    assert r[SELENE]["meet"]["met"] is False and r[SELENE]["meet"]["detail"] == "100 of 500"
    assert rows(conn)[300140]["permit"]["label"].startswith("Sol")


def test_next_engineer_is_first_unmaxed_in_priority(conn):
    v = ev.chain_view(conn, FID)
    assert v["next"]["engineer_id"] == FELICITY and v["unlocked"] == 2 and v["maxed"] == 0


def test_pins_default_change_and_reset(conn):
    assert rows(conn)[FELICITY]["pin_is_recommended"]
    assert rows(conn)[FELICITY]["pin"]["blueprint"] == "fsd_longrange"
    ev.set_pin(conn, FID, FELICITY, "fsd", "fsd_fastboot")
    r = rows(conn)[FELICITY]
    assert not r["pin_is_recommended"] and r["pin_label"] == "FSD — Faster boot sequence"
    with pytest.raises(ValueError):
        ev.set_pin(conn, FID, FELICITY, "mc", "weapon_overcharged")
    ev.set_pin(conn, FID, FELICITY, None, None)
    assert rows(conn)[FELICITY]["pin_is_recommended"]


H = {TOKEN_HEADER: "tok"}


@pytest.fixture()
def client(conn, db_path, journal_dir):
    ctx = AppContext(data_dir=db_path.parent, journal_dir=journal_dir, port=8732, token="tok", version="test")
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8732") as c:
        yield c


def test_engineer_pages_and_pin_api(client, conn):
    assert client.post(f"/api/engineers/{FELICITY}/pin", json={"pin": "fsd|fsd_fastboot"}).status_code == 403
    assert client.post(f"/api/engineers/{FELICITY}/pin", json={"pin": "mc|weapon_overcharged"},
                       headers=H).status_code == 400
    assert client.post(f"/api/engineers/{FELICITY}/pin", json={"pin": "fsd|fsd_fastboot"}, headers=H).status_code == 200
    html = client.get("/partials/engineer-plan").text
    assert 'value="fsd|fsd_fastboot" selected' in html and "Reset to recommended" in html
    assert client.post(f"/api/engineers/{FELICITY}/pin", json={"pin": ""}, headers=H).status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM engineer_pins").fetchone()[0] == 0
    assert "Next engineer" in client.get("/").text
    assert "Get referred: rank Felicity Farseer to G3" in client.get("/progress").text
    assert "Engineers not in the plan" in client.get("/goals").text


def test_extra_offers_from_inara(conn):
    offers = {(e, m, b): g for e, m, b, g in conn.execute(
        "SELECT engineer_id, module_type, blueprint, max_grade FROM ref_engineer_blueprints "
        "WHERE blueprint IN ('cargorack_extended', 'mininglaser_longrange', 'guardianweapon_antiguardianzone')")}
    assert offers[(300010, "cr", "cargorack_extended")] == 4                 # Bill Turner
    assert offers[(300250, "ml", "mininglaser_longrange")] == 5             # Juri Ishmaak
    assert offers[(300080, "ml", "mininglaser_longrange")] == 3             # Liz Ryder
    assert offers[(300110, "ggc", "guardianweapon_antiguardianzone")] == 1  # Ram Tah
    assert ev.pin_label(conn, "cr", "cargorack_extended") == "Cargo Rack — Extended (Merc modules only)"
    recipe = dict(conn.execute("SELECT material, qty FROM ref_recipe_costs WHERE recipe = 'cargorack_extended' AND grade = 4"))
    assert recipe == {"mechanicalequipment": 1, "highdensitycomposites": 1, "tin": 1, "niobium": 1}
