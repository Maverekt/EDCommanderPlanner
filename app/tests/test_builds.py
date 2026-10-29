"""Phase 5d: build import, comparison with the real fit, engineers, materials and goal costs."""

import base64
import gzip
import json

import pytest
from fastapi.testclient import TestClient

from app.core import builds, db, goals as gc, paths
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.refdata.loader import ensure_loaded
from app.server import build_views as bv
from app.server import goal_views as gv
from app.server.app import TOKEN_HEADER, create_app
from app.server.context import AppContext

from .conftest import FID, header, line, write_journal

T11 = 21


def example() -> dict:
    return json.loads(bv.example_text())[0]["data"]


def owned_t11(fsd_grade=3, shield_booster=True):
    """The example build's Type-11 as owned: FSD engineered to `fsd_grade`, no other engineering."""
    mods = []
    for m in example()["Modules"]:
        m = {k: v for k, v in m.items() if k != "Engineering"}
        if m["Slot"] == "TinyHardpoint2" and not shield_booster:
            continue
        if m["Slot"] == "FrameShiftDrive":
            m["Engineering"] = {"BlueprintName": "FSD_LongRange", "Level": fsd_grade, "Quality": 0.5, "Modifiers": []}
        mods.append(m)
    return mods


def journal(t11_mods=None):
    lines = [
        header("2026-10-25T13:00:00Z"),
        line("2026-10-25T13:00:01Z", "Commander", FID=FID, Name="Test Pilot"),
        line("2026-10-25T13:00:02Z", "LoadGame", FID=FID, Commander="Test Pilot",
             **({"Ship": "lakonminer", "ShipID": T11} if t11_mods is not None else {"Ship": "anaconda", "ShipID": 6}),
             Credits=500_000_000),
        line("2026-10-25T13:00:03Z", "EngineerProgress", Engineers=[
            {"Engineer": "Felicity Farseer", "EngineerID": 300100, "Progress": "Unlocked", "RankProgress": 0, "Rank": 5}]),
        line("2026-10-25T13:00:04Z", "Materials", Raw=[{"Name": "arsenic", "Count": 3}], Manufactured=[], Encoded=[]),
    ]
    if t11_mods is not None:
        lines.append(line("2026-10-25T13:00:05Z", "Loadout", Ship="lakonminer", ShipID=T11, ShipName="", ShipIdent="MM-01",
                          HullValue=66_346_710, ModulesValue=20_000_000, Rebuy=4_000_000, Modules=t11_mods))
    return lines


def make_conn(db_path, journal_dir, filter_csv, t11_mods=None):
    ensure_loaded(db_path, paths.refdata_dir())
    write_journal(journal_dir, "Journal.01.log", journal(t11_mods))
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    return db.connect(db_path)


@pytest.fixture()
def conn(db_path, journal_dir, filter_csv):
    c = make_conn(db_path, journal_dir, filter_csv, owned_t11())
    yield c
    c.close()


def view(c, build_id):
    return bv.build_view(c, FID, builds.get(c, FID, build_id))


def test_parse_formats():
    slef = builds.parse(bv.example_text())
    assert slef["source"] == "edsy" and slef["loadout"]["Ship"] == "lakonminer" and slef["source_url"].startswith("https://edsy.org")
    assert builds.parse(json.dumps(example()))["source"] == "journal"           # a bare Loadout
    assert builds.parse(json.dumps({"header": {"appName": "Coriolis"}, "data": example()}))["source"] == "coriolis"
    for bad, msg in [("https://edsy.org/#/L=Kc00", "EDSY link"), ("{", "not valid JSON"), ("[]", "empty"),
                     ('{"Ship": "x", "Modules": []}', "no ship or modules")]:
        with pytest.raises(ValueError, match=msg):
            builds.parse(bad)


def test_compare_with_owned_ship(conn):
    bid = builds.load_template(conn, FID, bv.EXAMPLE_KEY, bv.example_text())
    assert builds.load_template(conn, FID, bv.EXAMPLE_KEY, bv.example_text()) is None
    v = view(conn, bid)
    assert v["ship_id"] == T11 and v["to_buy"] == 0 and not v["done"]       # same modules, engineering missing
    status = {m["slot"]: m["status"] for m in v["modules"]}
    assert status["FrameShiftDrive"] == "engineer" and status["Slot01_Size6"] == "ok" and "CargoHatch" not in status
    fsd = next(b for b in v["blueprints"] if b["blueprint"] == "fsd_longrange")
    assert fsd["slots"] == [{"slot": "FrameShiftDrive", "done_grade": 3, "experimental": "special_fsd_heavy"}]
    assert fsd["state"] == "ready"                                            # Felicity at G5 offers FSD increased range G5
    assert v["eng_left"] == 9
    # Only G4–G5 count for the FSD: arsenic is its G5 raw (1 per roll × 5 rolls), plus nothing for G4.
    arsenic = next(m for m in v["materials"] if m["symbol"] == "arsenic")
    assert arsenic["have"] == 3 and arsenic["need"] >= 5


def test_missing_module_is_bought(db_path, journal_dir, filter_csv):
    c = make_conn(db_path, journal_dir, filter_csv, owned_t11(shield_booster=False))
    bid = builds.add(c, FID, builds.parse(bv.example_text()))
    v = view(c, bid)
    assert v["to_buy"] == 281000 and {m["slot"]: m["status"] for m in v["modules"]}["TinyHardpoint2"] == "buy"
    c.close()


def test_not_owned_costs_the_hull_and_sets_the_goal_cost(db_path, journal_dir, filter_csv):
    c = make_conn(db_path, journal_dir, filter_csv, None)
    gc.load_template_goals(c, FID, gv.template())
    bid = builds.load_template(c, FID, bv.EXAMPLE_KEY, bv.example_text())
    v = view(c, bid)
    mining = c.execute("SELECT id FROM goals WHERE template_key = 'mining'").fetchone()[0]
    assert v["ship_id"] is None and v["goal_id"] == mining                    # attached to the one Type-11 goal
    assert v["to_buy"] == v["total_value"] == 109_133_630 and v["hull_cost"] == 66_346_710
    goal = next(g for g in gv.plan_view(c, FID, 500_000_000)["goals"] if g["id"] == mining)
    assert goal["cost"] == 109_133_630 and goal["remaining"] == 109_133_630 and goal["build_plan"]["id"] == bid
    c.close()


def test_matching_ship_completes_the_goal(db_path, journal_dir, filter_csv):
    c = make_conn(db_path, journal_dir, filter_csv, example()["Modules"])   # the build exactly, fitted
    gc.load_template_goals(c, FID, gv.template())
    bid = builds.load_template(c, FID, bv.EXAMPLE_KEY, bv.example_text())
    assert view(c, bid)["done"]
    goal = next(g for g in gv.plan_view(c, FID, 0)["goals"] if g["title"] == "Ship Build Example - Mining")
    assert goal["done"] and goal["auto_done"]
    c.close()


def test_share_a_current_fit(conn):
    lo = builds.ship_loadout(conn, FID, T11)
    out = builds.share(lo, "9.9.9")
    slef = json.loads(out["slef"])
    assert slef[0]["header"] == {"appName": "ED Commander Planner", "appVersion": "9.9.9"} and slef[0]["data"] == lo
    for key, prefix in (("edsy", "https://edsy.org/#/I="), ("coriolis", "https://coriolis.io/import?data=")):
        packed = out[key].removeprefix(prefix).replace("%3D", "=")
        assert json.loads(gzip.decompress(base64.urlsafe_b64decode(packed))) == lo
    assert builds.parse(out["slef"])["loadout"] == lo             # the app reads its own export back
    assert builds.share(lo, "9.9.9") == out                       # the same fit, the same links
    with pytest.raises(LookupError):
        builds.ship_loadout(conn, FID, 999)


def test_from_ship_and_plan_entries(conn):
    bid = builds.from_ship(conn, FID, T11)
    v = view(conn, bid)
    assert v["name"] == "Type-11 Prospector MM-01 (as fitted)" and v["done"] and bv.plan_entries(v) == []
    with pytest.raises(LookupError):
        builds.from_ship(conn, FID, 999)
    entries = bv.plan_entries(view(conn, builds.add(conn, FID, builds.parse(bv.example_text()))))
    fsd = next(e for e in entries if e["blueprint"] == "fsd_longrange")
    assert fsd["done_grade"] == 3 and fsd["modules"] == 1 and fsd["experimental"] == "special_fsd_heavy"
    assert next(e for e in entries if e["blueprint"] == "shieldbooster_heavyduty")["modules"] == 3


H = {TOKEN_HEADER: "tok"}


@pytest.fixture()
def client(conn, db_path, journal_dir):
    ctx = AppContext(data_dir=db_path.parent, journal_dir=journal_dir, port=8732, token="tok", version="test")
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8732") as c:
        yield c


def test_api_and_pages(client, conn):
    assert client.post("/api/builds", json={"text": bv.example_text()}).status_code == 403
    r = client.post("/api/builds", json={"text": "https://edsy.org/#/L=x"}, headers=H)
    assert r.status_code == 400 and "Export" in r.json()["error"]
    # The Type-11 is owned here, so its build becomes another fit on that ship, not a build.
    r = client.post("/api/builds", json={"text": bv.example_text(), "name": "Miner"}, headers=H).json()
    assert r["ship_id"] == T11 and r["redirect"] == f"/ships?fit={r['fit_id']}"
    again = client.post("/api/builds", json={"text": bv.example_text(), "name": "Miner"}, headers=H)
    assert again.status_code == 400 and "already saved as “Miner”" in again.json()["error"]
    page = client.get("/ships").text
    assert f'data-fit-tab="{r["fit_id"]}"' in page and ">Miner</span>" in page
    bid = builds.add(conn, FID, builds.parse(bv.example_text()), "Miner")
    assert client.post(f"/api/builds/{bid}/to-plan", headers=H).json()["added"] == 7
    assert client.post(f"/api/builds/{bid}/to-plan", headers=H).json()["added"] == 0
    assert client.post(f"/api/builds/{bid}/update", json={"goal_id": "99999"}, headers=H).status_code == 400
    assert client.post("/api/builds/from-ship", json={"ship_id": T11}, headers=H).status_code == 200
    html = client.get("/ships").text
    assert "Miner" in html and "Add to builds" in html and "Engineering left" in html
    assert "Open in EDSY ↗" in html and "https://edsy.org/#/I=" in html and "https://coriolis.io/import?data=" in html
    assert 'data-copy="slef-21"' in html and FID not in html
    assert client.post(f"/api/builds/{bid}/delete", headers=H).status_code == 200
    assert client.post(f"/api/builds/{bid}/delete", headers=H).status_code == 404


def coriolis_slef() -> str:
    """The example as Coriolis exports it: mixed-case items, no per-module Value, empty ShipName, name in `bn`."""
    lo = example()
    lo["ShipName"], lo["ShipIdent"] = "", ""
    lo["Modules"] = [{**{k: v for k, v in m.items() if k != "Value"}, "Item": m["Item"].title()} for m in lo["Modules"]]
    url = "https://coriolis.io/outfit/type_11_prospector?code=B0pk&bn=PTN%20T11%20-%20Multiminer"
    return json.dumps([{"header": {"appName": "Coriolis", "appVersion": "4.0", "appURL": url}, "data": lo}])


def test_coriolis_slef(db_path, journal_dir, filter_csv):
    c = make_conn(db_path, journal_dir, filter_csv, None)
    parsed = builds.parse(coriolis_slef())
    assert parsed["source"] == "coriolis" and parsed["name"] == "PTN T11 - Multiminer"
    v = view(c, builds.add(c, FID, parsed))
    assert v["name"] == "PTN T11 - Multiminer" and v["eng_left"] == 9
    assert v["to_buy"] == example()["HullValue"] + example()["ModulesValue"] and v["unknown_prices"] == 0
    with pytest.raises(ValueError, match="SLEF"):
        builds.parse(json.dumps({"$schema": "https://coriolis.io/schemas/ship-loadout/5.json#", "ship": "x"}))
    c.close()


def test_owned_ship_prices_come_from_the_journal(db_path, journal_dir, filter_csv):
    c = make_conn(db_path, journal_dir, filter_csv, owned_t11(shield_booster=False))
    v = view(c, builds.add(c, FID, builds.parse(coriolis_slef())))
    # The fitted shield boosters' Loadout Value (281,000) prices the missing one.
    assert v["to_buy"] == 281000 and v["unknown_prices"] == 0
    c.close()


def test_goal_takes_a_build(db_path, journal_dir, filter_csv):
    c = make_conn(db_path, journal_dir, filter_csv, None)
    gc.load_template_goals(c, FID, gv.template())
    ids = dict(c.execute("SELECT template_key, id FROM goals"))
    with pytest.raises(ValueError, match="this goal is for the Panther"):
        builds.attach(c, FID, ids["hauling"], text=bv.example_text())
    bid = builds.attach(c, FID, ids["mining"], text=coriolis_slef())
    assert builds.get(c, FID, bid)["goal_id"] == ids["mining"]
    other = builds.add(c, FID, builds.parse(bv.example_text()))
    builds.attach(c, FID, ids["mining"], build_id=other)                     # swap: one build per goal
    assert builds.get(c, FID, bid)["goal_id"] is None and builds.get(c, FID, other)["goal_id"] == ids["mining"]
    builds.attach(c, FID, ids["mining"])                                     # detach
    assert builds.get(c, FID, other)["goal_id"] is None
    c.close()


def test_a_goal_for_another_ship_asks_then_switches(db_path, journal_dir, filter_csv):
    c = make_conn(db_path, journal_dir, filter_csv, None)
    gc.load_template_goals(c, FID, gv.template())
    ids = dict(c.execute("SELECT template_key, id FROM goals"))
    bid = builds.add(c, FID, builds.parse(bv.example_text()))                  # a Type-11 build
    with pytest.raises(builds.ShipMismatch) as e:
        builds.update(c, FID, bid, {"goal_id": str(ids["hauling"])})
    assert "You have the Panther Clipper MkII assigned to" in e.value.confirm and "switches to the Type-11" in e.value.confirm
    assert builds.get(c, FID, bid)["goal_id"] == ids["mining"]                # nothing changed on "no"
    builds.update(c, FID, bid, {"goal_id": str(ids["hauling"])}, retarget=True)
    assert builds.get(c, FID, bid)["goal_id"] == ids["hauling"]
    assert c.execute("SELECT target_key FROM goals WHERE id = ?", (ids["hauling"],)).fetchone()[0] == "lakonminer"
    # Two goals for the same ship (a current fit and a future one) each keep their own build.
    second = builds.add(c, FID, builds.parse(bv.example_text()))
    builds.update(c, FID, second, {"goal_id": str(ids["mining"])})
    assert builds.get(c, FID, bid)["goal_id"] == ids["hauling"] and builds.get(c, FID, second)["goal_id"] == ids["mining"]
    c.close()


def test_goal_build_api(client, conn):
    gc.load_template_goals(conn, FID, gv.template())
    mining = conn.execute("SELECT id FROM goals WHERE template_key = 'mining'").fetchone()[0]
    assert client.post(f"/api/goals/{mining}/build", json={"text": bv.example_text()}, headers=H).status_code == 200
    html = client.get("/partials/goals").text
    assert "Change build" in html and "Add a build" in html and "Ship Build Example - Mining" in html
    assert client.post(f"/api/goals/{mining}/build", json={"build_id": "99999"}, headers=H).status_code == 404
    hauling = conn.execute("SELECT id FROM goals WHERE template_key = 'hauling'").fetchone()[0]
    bid = builds.add(conn, FID, builds.parse(bv.example_text()))
    r = client.post(f"/api/builds/{bid}/update", json={"goal_id": str(hauling)}, headers=H)
    assert r.status_code == 409 and r.json()["confirm"].startswith("You have the Panther Clipper MkII assigned to")
    assert "is detached" not in r.json()["confirm"]
    r = client.post(f"/api/builds/{bid}/update", json={"goal_id": str(hauling), "retarget": True}, headers=H)
    assert r.status_code == 200


def test_engineer_offer_fixes(conn):
    offers = {(e, m) for e, m in conn.execute("SELECT DISTINCT engineer_id, module_type FROM ref_engineer_blueprints")}
    assert (300100, "s") in offers and (300100, "cs") not in offers        # Felicity: Sensors yes, Manifest Scanner no
    grade = conn.execute("SELECT MAX(max_grade) FROM ref_engineer_blueprints WHERE engineer_id = 300010 "
                         "AND module_type = 'cs'").fetchone()[0]
    assert grade == 3                                                       # Bill Turner's Manifest Scanner capped


def test_module_in_another_slot_is_moved_not_bought(db_path, journal_dir, filter_csv):
    mods = owned_t11()
    items = {m["Slot"]: m["Item"] for m in mods}
    for m in mods:                                    # the two size-1 slots hold each other's modules
        if m["Slot"] in ("Slot09_Size1", "Slot10_Size1"):
            m["Item"] = items["Slot10_Size1" if m["Slot"] == "Slot09_Size1" else "Slot09_Size1"]
    c = make_conn(db_path, journal_dir, filter_csv, mods)
    v = view(c, builds.add(c, FID, builds.parse(bv.example_text())))
    moved = {m["slot"]: m["move_from"] for m in v["modules"] if m["status"] == "move"}
    assert moved == {"Slot09_Size1": "Slot10_Size1", "Slot10_Size1": "Slot09_Size1"} and v["to_buy"] == 0
    c.close()


def test_list_prices_fill_gaps(db_path, journal_dir, filter_csv):
    mods = [m for m in owned_t11() if m["Slot"] != "Slot09_Size1"]       # no Guardian FSD Booster fitted or bought
    c = make_conn(db_path, journal_dir, filter_csv, mods)
    cost = json.loads(c.execute("SELECT attrs_json FROM ref_items WHERE symbol = 'int_guardianfsdbooster_size1'").fetchone()[0])
    v = view(c, builds.add(c, FID, builds.parse(coriolis_slef())))      # Coriolis: no per-module prices
    assert v["to_buy"] == cost["cost"] == 405022 and v["unknown_prices"] == 0
    hull = json.loads(c.execute("SELECT attrs_json FROM ref_items WHERE symbol = 'lakonminer'").fetchone()[0])
    assert hull["hull_cost"] == 66346710 and hull["retail_cost"] > hull["hull_cost"]
    gc.load_template_goals(c, FID, gv.template())
    hauling = next(g for g in gv.plan_view(c, FID, 0)["goals"] if g["title"] == "Ship Build Example - Hauling & trading")
    assert hauling["list_price"] > 0
    c.close()


def test_build_details_carry_the_builder_link(client, conn):
    gc.load_template_goals(conn, FID, gv.template())
    mining = conn.execute("SELECT id FROM goals WHERE template_key = 'mining'").fetchone()[0]
    client.post(f"/api/goals/{mining}/build", json={"text": coriolis_slef()}, headers=H)
    html = client.get("/partials/goals").text
    assert "Open in Coriolis ↗" in html and "coriolis.io/outfit/type_11_prospector" in html
    assert 'class="build-details' in html                                  # collapsed until opened


def test_a_build_for_a_ship_not_owned_stays_a_build(client, conn):
    slef = json.loads(bv.example_text())
    slef[0]["data"]["Ship"] = "anaconda"
    slef[0]["data"]["Modules"] = [m for m in slef[0]["data"]["Modules"] if m["Slot"] in ("FrameShiftDrive", "PowerPlant")]
    r = client.post("/api/builds", json={"text": json.dumps(slef)}, headers=H).json()
    assert "id" in r and "fit_id" not in r
