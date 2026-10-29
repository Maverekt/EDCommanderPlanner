"""Phase 3: live tail, live attribution, incremental projection, live values (DESIGN §5.4, §5.8, §10)."""

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.core import db, paths, projectors
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.journal.ingest_filter import IngestFilter
from app.core.journal.tail import JournalTail
from app.core.live import LiveState, Watcher, areas_for
from app.core.refdata.loader import ensure_loaded
from app.server import live_views

from .conftest import FID, FID_2, header, line, write_journal

LOGIN_TS = "2026-10-25T13:00:02Z"


def login_lines(fid=FID, credits=1_000_000, shielding=90):
    return [
        header("2026-10-25T13:00:00Z"),
        line("2026-10-25T13:00:01Z", "Commander", FID=fid, Name="Test Pilot"),
        line(LOGIN_TS, "LoadGame", FID=fid, Commander="Test Pilot", Ship="Anaconda", Credits=credits, GameMode="Solo"),
        line("2026-10-25T13:00:03Z", "Materials", Raw=[{"Name": "iron", "Count": 5}],
             Manufactured=[{"Name": "imperialshielding", "Count": shielding}], Encoded=[]),
        line("2026-10-25T13:00:04Z", "Statistics", Bank_Account={"Current_Wealth": 5_000_000, "MercCoins_Current": 7}),
    ]


def append(path, *lines, newline=True):
    with path.open("ab") as f:
        f.write(("\r\n".join(lines) + ("\r\n" if newline else "")).encode("utf-8"))


@pytest.fixture()
def env(db_path, journal_dir, filter_csv):
    ensure_loaded(db_path, paths.refdata_dir())
    journal = write_journal(journal_dir, "Journal.2026-10-25T090000.01.log", login_lines())
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    conn = db.connect(db_path)
    tail = JournalTail(conn, journal_dir, IngestFilter.from_csv(filter_csv))
    projection = projectors.LiveProjection()
    yield conn, tail, projection, journal, journal_dir
    conn.close()


def material(conn, symbol):
    row = conn.execute("SELECT value FROM snapshots WHERE metric = 'material' AND key = ? ORDER BY date DESC",
                       (symbol,)).fetchone()
    return int(row[0]) if row else None


def test_first_tick_rereads_without_duplicating(env):
    conn, tail, *_ = env
    before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert tail.tick() == []
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_pickup_is_stored_and_projected(env):
    conn, tail, projection, journal, _ = env
    tail.tick()
    append(journal, line("2026-10-25T13:05:00Z", "MaterialCollected", Category="Manufactured",
                         Name="imperialshielding", Count=3))
    events = tail.tick()
    assert [e[3] for e in events] == ["MaterialCollected"]
    assert projection.apply(conn, events) == {"MaterialCollected"}
    assert material(conn, "imperialshielding") == 93


def test_pickup_at_cap_does_not_exceed_cap(env):
    # Reported in play: 99/100 + 3 looted showed 102. The game keeps writing
    # MaterialCollected for loot it can't store.
    conn, tail, projection, journal, _ = env
    tail.tick()
    append(journal, line("2026-10-25T13:05:00Z", "MaterialCollected", Category="Manufactured",
                         Name="imperialshielding", Count=9))   # 90 + 9 = 99 (grade 5, cap 100)
    projection.apply(conn, tail.tick())
    assert material(conn, "imperialshielding") == 99
    append(journal, line("2026-10-25T13:05:30Z", "MaterialCollected", Category="Manufactured",
                         Name="imperialshielding", Count=3))
    projection.apply(conn, tail.tick())
    assert material(conn, "imperialshielding") == 100
    # Spending from the cap works from the true count, not the overflow.
    append(journal, line("2026-10-25T13:06:00Z", "EngineerCraft", Ingredients=[{"Name": "imperialshielding", "Count": 4}],
                         Engineer="X", EngineerID=300100, BlueprintName="b", Level=1))
    projection.apply(conn, tail.tick())
    assert material(conn, "imperialshielding") == 96


def test_partial_line_is_held_until_complete(env):
    conn, tail, projection, journal, _ = env
    tail.tick()
    full = line("2026-10-25T13:06:00Z", "MaterialCollected", Category="Raw", Name="iron", Count=2)
    append(journal, full[:30], newline=False)
    assert tail.tick() == []
    append(journal, full[30:])
    events = tail.tick()
    projection.apply(conn, events)
    assert [e[3] for e in events] == ["MaterialCollected"] and material(conn, "iron") == 7


def test_crafting_spends_and_next_login_snaps_back(env):
    conn, tail, projection, journal, _ = env
    tail.tick()
    append(journal, line("2026-10-25T13:07:00Z", "EngineerCraft", Ingredients=[{"Name": "imperialshielding", "Count": 4}],
                         Engineer="X", EngineerID=300100, BlueprintName="b", Level=1))
    projection.apply(conn, tail.tick())
    assert material(conn, "imperialshielding") == 86
    append(journal, *login_lines(shielding=50)[1:])  # the game's own count wins at login
    projection.apply(conn, tail.tick())
    assert material(conn, "imperialshielding") == 50 and material(conn, "iron") == 5


def test_new_journal_file_is_picked_up(env):
    conn, tail, projection, journal, journal_dir = env
    tail.tick()
    newer = write_journal(journal_dir, "Journal.2026-10-25T100000.01.log", [header("2026-10-25T14:00:00Z")])
    events = tail.tick()
    assert tail.stats.path == newer.name
    assert [e[3] for e in events] == ["Fileheader"]


def test_pre_commander_events_are_provisional_then_confirmed(env):
    conn, tail, projection, journal, journal_dir = env
    tail.tick()
    newer = write_journal(journal_dir, "Journal.2026-10-25T100000.01.log", [header("2026-10-25T14:00:00Z")])
    append(newer, line("2026-10-25T14:00:05Z", "Shutdown"))  # stand-in for any menu event
    tail.tick()
    row = conn.execute("SELECT fid, fid_status FROM events WHERE ts = '2026-10-25T14:00:05Z'").fetchone()
    assert tuple(row) == (FID, "provisional")
    append(newer, line("2026-10-25T14:01:00Z", "Commander", FID=FID, Name="Test Pilot"))
    tail.tick()
    row = conn.execute("SELECT fid, fid_status FROM events WHERE ts = '2026-10-25T14:00:05Z'").fetchone()
    assert tuple(row) == (FID, "confirmed")


def test_pre_commander_events_reattributed_to_another_commander(env):
    conn, tail, projection, journal, journal_dir = env
    tail.tick()
    newer = write_journal(journal_dir, "Journal.2026-10-25T100000.01.log", [header("2026-10-25T14:00:00Z")])
    tail.tick()
    append(newer, line("2026-10-25T14:01:00Z", "Commander", FID=FID_2, Name="Alt"))
    tail.tick()
    row = conn.execute("SELECT fid, fid_status, hash FROM events WHERE ts = '2026-10-25T14:00:00Z'").fetchone()
    assert (row["fid"], row["fid_status"]) == (FID_2, "confirmed")
    from app.core.journal.importer import event_hash
    raw = conn.execute("SELECT raw_json FROM events WHERE ts = '2026-10-25T14:00:00Z'").fetchone()[0]
    assert row["hash"] == event_hash(FID_2, raw)
    assert tail.stats.reattributed == 1


def test_projection_replays_when_events_were_never_projected(env):
    conn, tail, projection, journal, _ = env
    tail.tick()
    # A rescan stored a pickup behind the live projection's back...
    conn.execute("INSERT INTO events (fid, ts, event, raw_json, hash, source) VALUES (?, ?, 'MaterialCollected', ?, 'x', 'journal')",
                 (FID, "2026-10-25T13:08:00Z", json.dumps({"event": "MaterialCollected", "Name": "iron", "Count": 1})))
    conn.commit()
    append(journal, line("2026-10-25T13:09:00Z", "MaterialCollected", Category="Raw", Name="iron", Count=2))
    projection.apply(conn, tail.tick())
    assert material(conn, "iron") == 8  # 5 + 1 (replayed) + 2


def test_areas_for_events():
    assert areas_for({"MaterialCollected"}) == {"materials", "activities"}
    assert areas_for({"Promotion"}) == {"progress"}
    assert areas_for({"Statistics"}) == {"overview", "live"}
    assert areas_for({"Loadout"}) == {"live", "ships"}
    assert areas_for({"FSDJump"}) == {"activities"}
    assert areas_for({"Music"}) == set()


def test_watcher_tick_publishes_areas_and_reads_status(env):
    conn, tail, projection, journal, journal_dir = env
    published = []
    state = LiveState()
    watcher = Watcher(None, journal_dir, None, projection, state, lambda a, f: published.append((a, f)),
                      threading.Event())
    tail.tick()
    (journal_dir / "Status.json").write_text(json.dumps({"timestamp": "2026-10-25T13:10:00Z", "Flags": 16842765,
                                                         "Balance": 900_000}), encoding="utf-8")
    append(journal, line("2026-10-25T13:10:00Z", "MaterialCollected", Category="Raw", Name="iron", Count=1))
    areas = watcher.tick(conn, tail)
    assert areas == {"materials", "live", "activities"}
    assert ("materials", FID) in published and ("live", FID) in published
    assert state.snapshot()["status"]["Balance"] == 900_000
    assert watcher.tick(conn, tail) == set()  # nothing new, Status.json unchanged


def test_live_view_uses_status_balance_only_after_login(env):
    conn, *_ = env
    fid = FID
    stale = {"timestamp": "2026-10-25T12:00:00Z", "Flags": 1, "Balance": 1}
    v = live_views.live_view(conn, fid, stale)
    assert v["balance"] == {"value": 1_000_000, "live": False, "ts": LOGIN_TS}
    fresh = {"timestamp": "2026-10-25T13:30:00Z", "Flags": 1, "Balance": 750_000}
    v = live_views.live_view(conn, fid, fresh)
    assert v["balance"]["live"] and v["balance"]["value"] == 750_000
    assert v["session_delta"] == -250_000
    assert v["wealth"]["value"] == 4_750_000 and v["wealth"]["live"]
    assert v["merc_coins"]["value"] == 7
    assert v["ship"]["name"] == "Anaconda"


def test_game_state(env):
    conn, tail, projection, journal, _ = env
    assert live_views.game_state(conn, FID, {"Flags": 16842765})["state"] == "ingame"
    assert live_views.game_state(conn, FID, {"Flags": 0})["state"] == "menu"
    assert live_views.game_state(conn, FID, None)["state"] == "unknown"
    tail.tick()
    append(journal, line("2026-10-25T13:20:00Z", "Shutdown"))
    tail.tick()
    assert live_views.game_state(conn, FID, {"Flags": 0})["state"] == "offline"


def test_materials_view_marks_live_changes(env):
    conn, tail, projection, journal, _ = env
    tail.tick()
    append(journal, line("2026-10-25T13:05:00Z", "MaterialCollected", Category="Manufactured",
                         Name="imperialshielding", Count=3))
    projection.apply(conn, tail.tick())
    view = live_views.materials_view(conn, FID)
    rows = {r["symbol"]: r for fam in view["groups"]["Manufactured"] for r in fam[1]}
    s = rows["imperialshielding"]
    assert (s["count"], s["cap"], s["near"], s["live"]) == (93, 100, True, True)
    iron = {r["symbol"]: r for fam in view["groups"]["Raw"] for r in fam[1]}["iron"]
    assert iron["count"] == 5 and not iron["live"] and iron["cap"] == 300


def test_materials_gained_today(env):
    conn, tail, projection, journal, _ = env
    tail.tick()
    append(journal,
           line("2026-10-25T13:05:00Z", "MaterialCollected", Category="Manufactured", Name="imperialshielding", Count=3),
           line("2026-10-25T13:06:00Z", "MaterialTrade", MarketID=1, TraderType="manufactured",
                Paid={"Material": "imperialshielding", "Quantity": 6}, Received={"Material": "Iron", "Quantity": 2}),
           line("2026-10-25T13:07:00Z", "EngineerCraft", Ingredients=[{"Name": "imperialshielding", "Count": 4}],
                Engineer="X", EngineerID=300100, BlueprintName="b", Level=1))
    projection.apply(conn, tail.tick())
    now = datetime(2026, 10, 25, 13, 30, tzinfo=timezone.utc)                  # within minutes: today in any time zone
    assert live_views.gained_since(conn, FID, "2026-10-25T13:00:00Z") == {"imperialshielding": 3, "iron": 2}
    view = live_views.materials_view(conn, FID, now)
    rows = {r["symbol"]: r for t in ("Raw", "Manufactured") for fam in view["groups"][t] for r in fam[1]}
    assert rows["imperialshielding"]["today"] == 3 and rows["iron"]["today"] == 2   # gains only; spending isn't netted
    assert view["today"] == {"Raw": 2, "Manufactured": 3, "Encoded": 0}
    tomorrow = live_views.materials_view(conn, FID, now + timedelta(days=2))
    assert not any(tomorrow["today"].values())


def test_today_starts_at_the_login_of_a_session_played_through_midnight(env, monkeypatch):
    conn, tail, projection, journal, _ = env
    tail.tick()
    append(journal,
           line("2026-10-25T13:05:00Z", "MaterialCollected", Category="Manufactured", Name="imperialshielding", Count=3),
           line("2026-10-25T13:06:00Z", "MaterialCollected", Category="Raw", Name="iron", Count=1))
    projection.apply(conn, tail.tick())
    monkeypatch.setattr(live_views, "_local_midnight_utc", lambda now=None: "2026-10-25T13:05:30Z")
    assert live_views.today_start(conn, FID) == LOGIN_TS                         # still playing at "midnight"
    assert live_views.materials_view(conn, FID)["today"]["Manufactured"] == 3
    append(journal, line("2026-10-25T13:07:00Z", "Shutdown"),
           line("2026-10-25T13:10:00Z", "Commander", FID=FID, Name="Test Pilot"),
           line("2026-10-25T13:10:01Z", "LoadGame", FID=FID, Commander="Test Pilot", Ship="Anaconda", Credits=1))
    tail.tick()
    monkeypatch.setattr(live_views, "_local_midnight_utc", lambda now=None: "2026-10-25T13:08:00Z")
    assert live_views.today_start(conn, FID) == "2026-10-25T13:08:00Z"           # quit before, logged in after
