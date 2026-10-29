"""Phase 5c: engineering plan, material needs and shortfalls."""

import pytest
from fastapi.testclient import TestClient

from app.core import db, engplan, paths
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.refdata.loader import ensure_loaded
from app.server import engplan_views as ev
from app.server.app import TOKEN_HEADER, create_app
from app.server.context import AppContext

from .conftest import FID, header, line, write_journal


def journal():
    return [
        header("2026-10-25T13:00:00Z"),
        line("2026-10-25T13:00:01Z", "Commander", FID=FID, Name="Test Pilot"),
        line("2026-10-25T13:00:02Z", "LoadGame", FID=FID, Commander="Test Pilot", Ship="anaconda", ShipID=6,
             Credits=1_000_000),
        line("2026-10-25T13:00:03Z", "Materials",
             Raw=[{"Name": "carbon", "Count": 10}, {"Name": "antimony", "Count": 69}],
             Manufactured=[{"Name": "gridresistors", "Count": 279}, {"Name": "hybridcapacitors", "Count": 212}],
             Encoded=[]),
    ]


@pytest.fixture()
def conn(db_path, journal_dir, filter_csv):
    ensure_loaded(db_path, paths.refdata_dir())
    write_journal(journal_dir, "Journal.01.log", journal())
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    c = db.connect(db_path)
    yield c
    c.close()


def needs(c):
    return {m["symbol"]: m for m in ev.plan_view(c, FID)["materials"]}


def test_needs_formula():
    recipes = {("blueprint", "bp", 1): {"a": 1}, ("blueprint", "bp", 2): {"a": 1, "b": 2},
               ("experimental", "x", 0): {"c": 3}}
    entry = {"blueprint": "bp", "modules": 4, "target_grade": 2, "done_grade": 0, "experimental": "x"}
    assert engplan.entry_needs(recipes, entry, (2, 3, 3, 4, 5)) == {"a": 4 * 2 + 4 * 3, "b": 2 * 3 * 4, "c": 12}
    assert engplan.entry_needs(recipes, entry | {"done_grade": 1}, (2, 3, 3, 4, 5)) == {"a": 12, "b": 24, "c": 12}


def test_rolls_setting_parse():
    assert engplan.parse_rolls(None) == (2, 2, 3, 4, 5)
    assert engplan.parse_rolls(" 1,1,2,3,4 ") == (1, 1, 2, 3, 4)
    for bad in ("1,2", "0,1,1,1,1", "a,b,c,d,e"):
        with pytest.raises(ValueError):
            engplan.parse_rolls(bad)


def test_example_plan_matches_the_workbook_hand_count(conn):
    assert engplan.load_template(conn, FID, ev.template()) == 9
    assert engplan.load_template(conn, FID, ev.template()) == 0          # only once
    n = needs(conn)
    # The workbook's "Needed for plan" column for the same seven included blueprints.
    assert sum(m["need"] for m in n.values()) == 1062 and len(n) == 51
    assert n["carbon"]["need"] == 28 and n["carbon"]["short"] == 18       # HRP G1–G3: 8 + 8 + 12; have 10
    assert n["hybridcapacitors"]["need"] == 57                            # SB G2+G3, PD G4, Drag Drives
    assert n["antimony"]["need"] == 40 and n["antimony"]["short"] == 0    # SB G5: 1 × 5 rolls × 8; have 69
    assert n["gridresistors"]["short"] == 0


def test_validation_and_edits(conn):
    eid = engplan.add(conn, FID, {"module_type": "fsd", "blueprint": "fsd_longrange", "modules": "3"})
    with pytest.raises(ValueError):
        engplan.add(conn, FID, {"module_type": "mc", "blueprint": "fsd_longrange"})
    with pytest.raises(ValueError):
        engplan.update(conn, FID, eid, {"experimental": "special_engine_overloaded"})
    with pytest.raises(ValueError):
        engplan.update(conn, FID, eid, {"done_grade": "5"})
    engplan.update(conn, FID, eid, {"done_grade": "2", "experimental": "special_fsd_heavy"})
    assert needs(conn)["disruptedwakeechoes"]["need"] == 3 * 5      # G1/G2 done; Mass Manager's 5 per module
    engplan.update(conn, FID, eid, {"include": False})
    assert needs(conn) == {}
    with pytest.raises(LookupError):
        engplan.delete(conn, "F0000002", eid)


H = {TOKEN_HEADER: "tok"}


@pytest.fixture()
def client(conn, db_path, journal_dir):
    ctx = AppContext(data_dir=db_path.parent, journal_dir=journal_dir, port=8732, token="tok", version="test")
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8732") as c:
        yield c


def test_api_and_pages(client, conn):
    assert client.post("/api/engplan/load-template").status_code == 403
    assert client.post("/api/engplan/load-template", headers=H).json() == {"added": 9}
    r = client.post("/api/engplan", json={"pick": "sg|shieldgenerator_reinforced", "modules": "1", "target_grade": "3"},
                    headers=H)
    new_id = r.json()["id"]
    assert client.post(f"/api/engplan/{new_id}/move", json={"delta": -20}, headers=H).status_code == 200
    assert conn.execute("SELECT priority FROM eng_plan WHERE id = ?", (new_id,)).fetchone()[0] == 1
    assert client.post("/api/engplan/rolls", json={"rolls": "nope"}, headers=H).status_code == 400
    assert client.post(f"/api/engplan/{new_id}/delete", headers=H).status_code == 200
    assert client.post("/api/engplan/99999/delete", headers=H).status_code == 404
    assert "1,062 units needed" in client.get("/partials/eng-plan").text
    html = client.get("/materials").text
    assert "<h2>Engineering plan</h2>" in html and "units in hand" in html and "need 28 · " in html
