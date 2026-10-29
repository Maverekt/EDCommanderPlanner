import json

from app.core import db
from app.core.journal.files import discover, iter_lines
from app.core.journal.importer import Importer, ImportStatus, event_hash, run_backfill
from app.core.journal.ingest_filter import IngestFilter

from .conftest import FID, FID_2, header, line, session, write_journal


def backfill(db_path, journal_dir, filter_csv) -> ImportStatus:
    status = ImportStatus()
    run_backfill(db_path, journal_dir, filter_csv, status)
    assert status.state == "done", status.error
    return status


def rows(db_path, sql, params=()):
    conn = db.connect(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def test_orders_by_header_timestamp_not_file_name(journal_dir):
    # Name says 09:02 local; header says 13:02Z. A later-named file with an earlier header sorts first.
    write_journal(journal_dir, "Journal.2026-10-25T090238.01.log", [header("2026-10-25T13:02:34Z")])
    write_journal(journal_dir, "Journal.2026-10-25T100000.01.log", [header("2026-10-25T12:00:00Z")])
    names = [jf.path.name for jf in discover(journal_dir)]
    assert names == ["Journal.2026-10-25T100000.01.log", "Journal.2026-10-25T090238.01.log"]


def test_parts_order_after_timestamp(journal_dir):
    write_journal(journal_dir, "Journal.b.02.log", [header("2026-10-25T13:00:00Z", part=2)])
    write_journal(journal_dir, "Journal.a.01.log", [header("2026-10-25T13:00:00Z", part=1)])
    assert [jf.part for jf in discover(journal_dir)] == [1, 2]


def test_crlf_stripped_only_line_ending(journal_dir):
    p = write_journal(journal_dir, "Journal.x.01.log", [header("2026-10-25T13:00:00Z"), '{ "a":1 }  '])
    lines = list(iter_lines(p))
    assert lines[1] == '{ "a":1 }  '  # trailing spaces kept; only CRLF removed
    assert not lines[0].endswith("\r")


def test_partial_last_line_held_back(journal_dir):
    p = journal_dir / "Journal.y.01.log"
    p.write_bytes((header("2026-10-25T13:00:00Z") + "\r\n" + '{ "timestamp":"x", "ev').encode())
    assert len(list(iter_lines(p))) == 1


def test_full_import_filters_and_attributes(db_path, journal_dir, filter_csv):
    write_journal(journal_dir, "Journal.2026-10-25T090238.01.log", session("2026-10-25"))
    status = backfill(db_path, journal_dir, filter_csv)

    stored = [r["event"] for r in rows(db_path, "SELECT event FROM events ORDER BY ts")]
    assert "Music" not in stored and "ReceiveText" not in stored and "CommunityGoal" not in stored
    # Station-type signal dropped, nav beacon kept (DESIGN §5.6, Option B).
    signals = rows(db_path, "SELECT raw_json FROM events WHERE event = 'FSSSignalDiscovered'")
    assert len(signals) == 1 and json.loads(signals[0]["raw_json"])["SignalType"] == "NavBeacon"
    assert stored[0] == "Fileheader"  # pre-commander event confirmed to the commander
    assert {r["fid"] for r in rows(db_path, "SELECT fid FROM events")} == {FID}
    assert rows(db_path, "SELECT COUNT(*) AS n FROM events WHERE fid_status = 'provisional'")[0]["n"] == 0

    assert status.events_dropped == {"CommunityGoal": 2, "Music": 1, "ReceiveText": 1, "FSSSignalDiscovered": 1}
    log = rows(db_path, "SELECT * FROM import_log")
    assert len(log) == 1 and log[0]["status"] == "imported"
    assert json.loads(log[0]["events_dropped_json"])["FSSSignalDiscovered"] == 1
    assert rows(db_path, "SELECT is_selected FROM commanders WHERE fid = ?", (FID,))[0]["is_selected"] == 1


def test_rerun_adds_zero(db_path, journal_dir, filter_csv):
    write_journal(journal_dir, "Journal.2026-10-25T090238.01.log", session("2026-10-25"))
    first = backfill(db_path, journal_dir, filter_csv)
    assert first.events_added > 0
    second = backfill(db_path, journal_dir, filter_csv)
    assert second.files_total == 0 and second.events_added == 0


def test_rescan_of_changed_file_does_not_double_count(db_path, journal_dir, filter_csv):
    path = write_journal(journal_dir, "Journal.2026-10-25T090238.01.log", session("2026-10-25"), mtime=1_000_000)
    first = backfill(db_path, journal_dir, filter_csv)
    # The game appends to the file: same events plus one new one.
    lines = session("2026-10-25") + [line("2026-10-25T14:00:00Z", "Shutdown")]
    write_journal(journal_dir, path.name, lines, mtime=1_000_100)
    second = backfill(db_path, journal_dir, filter_csv)
    assert second.files_total == 1 and second.events_added == 1
    total = rows(db_path, "SELECT COUNT(*) AS n FROM events")[0]["n"]
    assert total == first.events_added + 1


def test_identical_lines_stored_once(db_path, journal_dir, filter_csv):
    # Deliberate (DESIGN §5.7, Decision Log): byte-identical lines collapse to one event.
    dup = line("2026-10-25T13:20:00Z", "UnderAttack", Target="You")
    write_journal(journal_dir, "Journal.1.01.log", session("2026-10-25") + [dup, dup])
    backfill(db_path, journal_dir, filter_csv)
    assert rows(db_path, "SELECT COUNT(*) AS n FROM events WHERE event = 'UnderAttack'")[0]["n"] == 1


def test_legacy_files_skipped(db_path, journal_dir, filter_csv):
    write_journal(journal_dir, "Journal.legacy.01.log",
                  [header("2026-10-20T10:00:00Z", gameversion="3.8.0.407")] + session("2026-10-20")[1:])
    status = backfill(db_path, journal_dir, filter_csv)
    assert status.files_skipped_legacy == 1
    assert rows(db_path, "SELECT COUNT(*) AS n FROM events")[0]["n"] == 0
    assert rows(db_path, "SELECT status FROM import_log")[0]["status"] == "skipped_legacy"


def test_file_without_commander_is_provisional_to_last_commander(db_path, journal_dir, filter_csv):
    write_journal(journal_dir, "Journal.1.01.log", session("2026-10-24"))
    write_journal(journal_dir, "Journal.2.01.log", [
        header("2026-10-25T05:47:54Z"),
        line("2026-10-25T05:48:00Z", "Shutdown"),
    ])
    backfill(db_path, journal_dir, filter_csv)
    prov = rows(db_path, "SELECT fid, event FROM events WHERE fid_status = 'provisional' ORDER BY ts")
    assert [r["event"] for r in prov] == ["Fileheader", "Shutdown"]
    assert {r["fid"] for r in prov} == {FID}
    statuses = [r["status"] for r in rows(db_path, "SELECT status FROM import_log ORDER BY id")]
    assert statuses == ["imported", "provisional_cmdr"]


def test_pre_commander_events_reattributed_to_new_commander(db_path, journal_dir, filter_csv):
    write_journal(journal_dir, "Journal.1.01.log", session("2026-10-24", fid=FID))
    write_journal(journal_dir, "Journal.2.01.log", session("2026-10-25", fid=FID_2, name="Alt"))
    backfill(db_path, journal_dir, filter_csv)
    second_header = rows(db_path, "SELECT fid, fid_status FROM events WHERE event = 'Fileheader' ORDER BY ts")[1]
    assert second_header == {"fid": FID_2, "fid_status": "confirmed"}
    # Latest LoadGame selects the second commander.
    assert rows(db_path, "SELECT fid FROM commanders WHERE is_selected = 1")[0]["fid"] == FID_2


def test_first_file_ever_without_commander_is_held(db_path, journal_dir, filter_csv):
    write_journal(journal_dir, "Journal.1.01.log", [header("2026-10-23T10:00:00Z"),
                                                    line("2026-10-23T10:01:00Z", "Shutdown")])
    write_journal(journal_dir, "Journal.2.01.log", session("2026-10-24"))
    backfill(db_path, journal_dir, filter_csv)
    first = rows(db_path, "SELECT fid, fid_status FROM events WHERE ts LIKE '2026-10-23%'")
    assert first and all(r == {"fid": FID, "fid_status": "confirmed"} for r in first)
    assert [r["status"] for r in rows(db_path, "SELECT status FROM import_log ORDER BY id")] == ["imported", "imported"]


def test_hash_is_fid_plus_stripped_line():
    raw = '{ "timestamp":"2026-10-25T13:10:01Z", "event":"Commander" }'
    import hashlib
    assert event_hash(FID, raw) == hashlib.sha256(f"{FID}\n{raw}".encode()).hexdigest()


def test_unparseable_lines_are_counted_not_fatal(db_path, journal_dir, filter_csv):
    write_journal(journal_dir, "Journal.1.01.log", session("2026-10-24") + ["not json"])
    backfill(db_path, journal_dir, filter_csv)
    assert "1 unparseable" in rows(db_path, "SELECT detail FROM import_log")[0]["detail"]


def test_ingest_filter_condition_needs_exact_match(filter_csv):
    f = IngestFilter.from_csv(filter_csv)
    assert f.drops({"event": "FSSSignalDiscovered", "IsStation": True})
    assert not f.drops({"event": "FSSSignalDiscovered"})
    assert not f.drops({"event": "FSSSignalDiscovered", "IsStation": False})
    assert f.drops({"event": "Music"})
    assert not f.drops({"event": "CommunityGoalReward"})


def test_outposts_dropped_without_isstation(filter_csv):
    # The game writes outposts with no IsStation flag (DESIGN §5.6, Decision Log).
    f = IngestFilter.from_csv(filter_csv)
    assert f.drops({"event": "FSSSignalDiscovered", "SignalType": "Outpost"})
    for kept in ("NavBeacon", "Installation", "ResourceExtraction", "Combat", "Megaship", "Generic"):
        assert not f.drops({"event": "FSSSignalDiscovered", "SignalType": kept}), kept
