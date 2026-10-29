"""Phase 2 projectors and views, on synthetic journals shaped like the verified Live journal."""

import pytest

from app.core import db, paths, projectors
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.refdata.loader import ensure_loaded
from app.server import views
from app.server.app import _unit

from .conftest import FID, header, line, write_journal

RANK = dict(Combat=3, Trade=10, Explore=7, Soldier=2, Exobiologist=0, Empire=0, Federation=12, CQC=0)
PROGRESS = dict(Combat=35, Trade=58, Explore=3, Soldier=19, Exobiologist=0, Empire=100, Federation=8, CQC=29)


def login(day: str, *, rank=None, progress=None, wealth=3_870_086_568, ships=607_758_932, extra=None,
          engineers=None, hh="13") -> list[str]:
    stats = {"Bank_Account": {"Current_Wealth": wealth, "Spent_On_Ships": ships, "MercCoins_Current": 0},
             "Combat": {"Bounties_Claimed": 339}, "Trading": {"Average_Profit": 1234.5}}
    if extra:
        stats.setdefault("Future_Section", {}).update(extra)
    return [
        header(f"{day}T{hh}:00:00Z"),
        line(f"{day}T{hh}:00:01Z", "Commander", FID=FID, Name="Test Pilot"),
        line(f"{day}T{hh}:00:02Z", "LoadGame", FID=FID, Commander="Test Pilot", Ship="Anaconda", Credits=2_783_991_595,
             GameMode="Group"),
        line(f"{day}T{hh}:00:03Z", "Rank", **(rank or RANK)),
        line(f"{day}T{hh}:00:03Z", "Progress", **(progress or PROGRESS)),
        line(f"{day}T{hh}:00:04Z", "Reputation", Empire=10.2557, Federation=97.573997, Independent=1.94491,
             Alliance=28.024401),
        line(f"{day}T{hh}:00:05Z", "EngineerProgress", Engineers=engineers or [
            {"Engineer": "Felicity Farseer", "EngineerID": 300100, "Progress": "Unlocked", "RankProgress": 60, "Rank": 2},
            {"Engineer": "Tod 'The Blaster' McQuinn", "EngineerID": 300260, "Progress": "Unlocked", "RankProgress": 10, "Rank": 1},
            {"Engineer": "Hero Ferrari", "EngineerID": 400003, "Progress": "Known"},
        ]),
        line(f"{day}T{hh}:00:06Z", "Statistics", **stats),
    ]


@pytest.fixture()
def build(db_path, journal_dir, filter_csv):
    ensure_loaded(db_path, paths.refdata_dir())

    def run(*files):
        for i, lines in enumerate(files):
            write_journal(journal_dir, f"Journal.{i:02d}.01.log", lines)
        run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
        return db.connect(db_path)
    return run


def test_promotion_available_for_verified_empire_case(build):
    conn = build(login("2026-10-25"))
    view = views.progress_view(conn, FID)
    empire = next(r for r in view["navy"] if r["track"] == "empire")
    assert empire["name"] == "None" and empire["progress"] == 100 and empire["promotion_available"]
    trade = next(r for r in view["pilots"] if r["track"] == "trade")
    assert trade["name"] == "Elite II" and trade["next_name"] == "Elite III" and not trade["promotion_available"]
    assert [tuple(r) for r in conn.execute("SELECT kind, key FROM unlocks WHERE kind = 'promotion_available'")] == [
        ("promotion_available", "empire")]


def test_live_promotion_clears_the_tag(build):
    first = login("2026-10-25")
    conn = build(first + [line("2026-10-25T14:00:00Z", "Promotion", Empire=1)])
    empire = next(r for r in views.progress_view(conn, FID)["navy"] if r["track"] == "empire")
    assert empire["rank"] == 1 and empire["name"] == "Outsider"
    assert not empire["promotion_available"] and empire["promoted_since_login"]
    assert conn.execute("SELECT detail_json FROM unlocks WHERE kind = 'navy_rank'").fetchone()[0] == '{"from": 0, "to": 1}'


def test_rank_up_between_logins_is_an_unlock(build):
    conn = build(login("2026-10-24"), login("2026-10-25", rank=RANK | {"Combat": 4}))
    rows = [tuple(r) for r in conn.execute("SELECT kind, key, detail_json FROM unlocks WHERE kind = 'rank'")]
    assert rows == [("rank", "combat", '{"from": 3, "to": 4}')]


def test_no_promotion_tag_at_max_rank(build):
    conn = build(login("2026-10-25", rank=RANK | {"Trade": 13}, progress=PROGRESS | {"Trade": 100}))
    trade = next(r for r in views.progress_view(conn, FID)["pilots"] if r["track"] == "trade")
    assert trade["name"] == "Elite V" and trade["at_max"] and not trade["promotion_available"]


def test_reputation_bands():
    assert views.reputation_band(97.57) == "Allied"
    assert views.reputation_band(28.0) == "Cordial"
    assert views.reputation_band(1.9) == "Neutral"
    assert views.reputation_band(-50) == "Unfriendly"
    assert views.reputation_band(-95) == "Hostile"


def test_engineers_states_and_live_grade_update(build):
    live = line("2026-10-25T15:00:00Z", "EngineerProgress", Engineer="Tod 'The Blaster' McQuinn",
                EngineerID=300260, Rank=3)  # live updates can carry only Rank
    conn = build(login("2026-10-25") + [live])
    eng = views.engineers_view(conn, FID)
    by_id = {e["id"]: e for group in eng.values() for e in group}
    assert by_id["300260"]["state"] == "Unlocked" and by_id["300260"]["grade"] == 3
    assert by_id["300100"]["grade"] == 2 and by_id["300100"]["progress"] == 60
    assert by_id["400003"]["state"] == "Known" and by_id["400003"]["grade"] is None
    assert by_id["300000"]["state"] == "Undiscovered"
    assert len(eng["ship"]) == 25 and len(eng["onfoot"]) == 13
    grades = [tuple(r) for r in conn.execute(
        "SELECT key, detail_json FROM unlocks WHERE kind = 'engineer_grade' ORDER BY ts")]
    assert grades == [("300260", '{"from": 1, "to": 3}')]


def test_statistics_counters_wealth_and_overview_delta(build):
    conn = build(login("2026-10-24", wealth=3_000_000_000, ships=600_000_000),
                 login("2026-10-25", wealth=3_870_086_568, ships=607_758_932, extra={"New_Counter": 5}))
    strip = views.rank_strip(conn, FID)
    assert strip["wealth"] == 3_870_086_568 and strip["merc_coins"] == 0

    view = views.overview_view(conn, FID, "session")
    rows = {r["counter"]: r for cat in view["categories"] for r in cat["rows"]}
    assert rows["Bank_Account.Current_Wealth"]["delta"] == 870_086_568
    assert rows["Bank_Account.Current_Wealth"]["label"] == "Current wealth"
    assert rows["Bank_Account.Spent_On_Ships"]["unit"] == "credits"
    assert rows["Trading.Average_Profit"]["value"] == 1234.5
    # A counter the game adds later shows up under its raw path (DESIGN §9.4).
    assert rows["Future_Section.New_Counter"]["label"] == "Future_Section.New_Counter"
    assert not rows["Future_Section.New_Counter"]["mapped"]
    assert rows["Bank_Account.Current_Wealth"]["spark"]  # two daily points

    all_time = views.overview_view(conn, FID, "all")
    assert all(r["delta"] is None for cat in all_time["categories"] for r in cat["rows"])


def test_rebuild_is_repeatable_and_tracks_staleness(build, db_path):
    conn = build(login("2026-10-25"))
    assert not projectors.is_stale(conn)
    first = projectors.rebuild(conn)
    second = projectors.rebuild(conn)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM unlocks").fetchone()[0] == first["unlocks"]
    conn.execute("INSERT INTO events (fid, ts, event, raw_json, hash, source) VALUES (?, ?, 'X', '{}', 'h', 'journal')",
                 (FID, "2026-10-25T16:00:00Z"))
    conn.commit()
    assert projectors.is_stale(conn)


@pytest.mark.parametrize("value, unit, signed, expected", [
    (1_234_567, "credits", False, "1,234,567 cr"),
    (870_086_568, "credits", True, "+870,086,568 cr"),
    (-5, "count", True, "−5"),
    (93_784, "seconds", False, "26h 03m"),
    (1234.56, "ly", False, "1,234.6 ly"),
    (2500, "metres", False, "2.5 km"),
    (1.3333, "ratio", False, "1.33"),
    (0, "merc_coins", False, "0 MC"),
])
def test_unit_formatting(value, unit, signed, expected):
    assert _unit(value, unit, signed) == expected


def test_checkmark_only_on_grade5_engineers(build):
    from fastapi.testclient import TestClient
    from app.server.app import create_app
    from app.server.context import AppContext
    engineers = [
        {"Engineer": "Felicity Farseer", "EngineerID": 300100, "Progress": "Unlocked", "RankProgress": 0, "Rank": 5},
        {"Engineer": "Tod 'The Blaster' McQuinn", "EngineerID": 300260, "Progress": "Unlocked", "RankProgress": 10, "Rank": 4},
    ]
    conn = build(login("2026-10-25", engineers=engineers))
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    from pathlib import Path
    ctx = AppContext(data_dir=Path(db_path).parent, journal_dir=Path("."), port=8732, token="t", version="test")
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8732") as c:
        import html as html_lib
        html = html_lib.unescape(c.get("/partials/progress").text)
    farseer = html.split("Felicity Farseer", 1)[1].split("</li>", 1)[0]
    tod = html.split("Tod 'The Blaster' McQuinn", 1)[1].split("</li>", 1)[0]
    assert "eng-done" in html.split("Felicity Farseer", 1)[1][:120] and "Grade 5 · Max" in farseer
    assert "eng-done" not in html.split("Tod 'The Blaster' McQuinn", 1)[1][:120]
    assert "Kuk · Prospector's Rest" in html  # Selene Jean, from Inara
