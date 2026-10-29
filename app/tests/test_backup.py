"""Phase 6: .edplanner backup and import (DESIGN §12): round trip, merge and conflicts, override,
rollback, bad files, and the Settings ▸ Backup pages."""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core import builds, db, goals as gc, notes, paths, projectors
from app.core.backup import archive, restore
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.refdata.loader import ensure_loaded
from app.server import build_views as bv
from app.server import goal_views as gv
from app.server.app import TOKEN_HEADER, create_app
from app.server.context import AppContext

from .conftest import FID, FID_2, line, session, write_journal

H = {TOKEN_HEADER: "tok"}
ALL_MERGE = {f"c{i}:{s}": "merge" for i in range(2) for s, *_ in restore.SECTIONS} | {"settings": "merge"}


def fresh(tmp_path, name):
    data_dir = tmp_path / name
    db.init_db(data_dir / "planner.db")
    ensure_loaded(data_dir / "planner.db", paths.refdata_dir())
    return data_dir, db.connect(data_dir / "planner.db")


def populated(tmp_path):
    """A data folder with history for two commanders and every kind of user data for the first."""
    journals = tmp_path / "journals"
    journals.mkdir()
    write_journal(journals, "Journal.2026-10-25T090238.01.log", session("2026-10-25"))
    write_journal(journals, "Journal.2026-10-26T090238.01.log", session("2026-10-26", fid=FID_2, name="Second"))
    data_dir, c = fresh(tmp_path, "a")
    run_backfill(data_dir / "planner.db", journals, paths.refdata_dir() / "ignored_events.csv", ImportStatus())
    gc.load_template_goals(c, FID, gv.template())
    bid = builds.load_template(c, FID, bv.EXAMPLE_KEY, bv.example_text())
    goal_ids = [r[0] for r in c.execute("SELECT id FROM goals WHERE fid = ? ORDER BY priority", (FID,))]
    with c:
        c.execute("UPDATE goals SET depends_on = ? WHERE id = ?", (json.dumps(goal_ids[:1]), goal_ids[1]))
        c.execute("INSERT INTO eng_plan (fid, priority, module_type, blueprint, modules, target_grade, include, "
                  "template_key, created_ts) VALUES (?, 1, 'fsd', 'fsd_longrange', 1, 5, 1, ?, '2026-10-25T00:00:00Z')",
                  (FID, f"build{bid}:fsd:fsd_longrange:5:0:"))
        c.execute("INSERT INTO engineer_pins VALUES (?, 300100, 'fsd', 'fsd_longrange', '2026-10-25T00:00:00Z')", (FID,))
        c.execute("INSERT INTO mining_spots (fid, commodity, method, system, body, notes, created_ts) "
                  "VALUES (?, 'platinum', 'laser', 'Paesia', '2 A Ring', 'hotspot x3', '2026-10-25T00:00:00Z')", (FID,))
        db.set_setting(c, "eng_rolls", "1,2,3,4,5")
        db.set_setting(c, "reserve_rebuys", "4")
        db.set_setting(c, f"activity_ship:mining:{FID}", "7")
        db.set_setting(c, f"selling_ships:{FID}", "[7]")
    gc.set_flag(c, FID, "ODYSSEY", "yes")
    notes.create(data_dir, FID, "Trade route", "Uranium **run**")
    notes.create(data_dir, FID_2, "Second's note", "o7")
    return data_dir, c


def state(c, data_dir):
    """Everything a restore must bring back, with row ids replaced by what they point at."""
    goal_key = {r["id"]: restore.KIND["goals"].key(dict(r)) for r in c.execute("SELECT * FROM goals")}
    build_key = {r["id"]: (r["ship_type"], r["name"]) for r in c.execute("SELECT * FROM builds")}
    out = {}
    for fid in (FID, FID_2):
        def rows(table, drop=("id", "fid")):
            return sorted(json.dumps({k: r[k] for k in r.keys() if k not in drop}, sort_keys=True)
                          for r in c.execute(f"SELECT * FROM {table} WHERE fid = ?", (fid,)))
        goals = sorted(json.dumps({**{k: r[k] for k in r.keys() if k not in ("id", "fid", "depends_on")},
                                   "depends": [goal_key[i] for i in json.loads(r["depends_on"] or "[]")]},
                                  sort_keys=True, default=str) for r in c.execute("SELECT * FROM goals WHERE fid = ?", (fid,)))
        built = sorted(json.dumps({**{k: r[k] for k in r.keys() if k not in ("id", "fid", "goal_id")},
                                   "goal": goal_key.get(r["goal_id"])}, sort_keys=True, default=str)
                       for r in c.execute("SELECT * FROM builds WHERE fid = ?", (fid,)))
        plan = sorted(json.dumps({**{k: r[k] for k in r.keys() if k not in ("id", "fid", "template_key")},
                                  "for": build_key.get(int(r["template_key"][5:].split(":")[0]))
                                  if (r["template_key"] or "").startswith("build") else r["template_key"]},
                                 sort_keys=True, default=str) for r in c.execute("SELECT * FROM eng_plan WHERE fid = ?", (fid,)))
        out[fid] = {
            "commander": tuple(c.execute("SELECT name, first_seen, last_seen FROM commanders WHERE fid = ?", (fid,)).fetchone()),
            "events": sorted(tuple(r) for r in c.execute(
                "SELECT fid_status, ts, event, raw_json, hash FROM events WHERE fid = ?", (fid,))),
            "goals": goals, "builds": built, "eng_plan": plan,
            "flags": rows("flags", ("fid", "updated_ts")), "pins": rows("engineer_pins"), "spots": rows("mining_spots"),
            "settings": archive.commander_settings(c, fid), "notes": notes.raw(data_dir, fid),
            "derived": {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE fid = ?", (fid,)).fetchone()[0]
                        for t in projectors.DERIVED_TABLES},
        }
    out["settings"] = {k: db.get_setting(c, k) for k in archive.GLOBAL_SETTINGS}
    return out


def test_round_trip_restores_everything_without_journals(tmp_path):
    a_dir, a = populated(tmp_path)
    before = state(a, a_dir)
    assert before[FID]["notes"] and before[FID]["derived"]["snapshots"] and before[FID]["settings"]
    # The links an import has to renumber: goal → goal, build → goal, plan entry → build.
    assert any('"depends": [["t", "hauling"]]' in g for g in before[FID]["goals"])
    assert '"goal": ["t", "mining"]' in before[FID]["builds"][0]
    assert '"for": ["lakonminer", "Ship Build Example - Mining"]' in before[FID]["eng_plan"][0]
    payload = archive.export_bytes(a, a_dir, None, app_version="test")

    b_dir, b = fresh(tmp_path, "b")                                      # a clean PC: no journals, no commanders
    arc = archive.Archive(payload, "everything.edplanner")
    rv = restore.review(b, b_dir, arc)
    assert [c["matched"] for c in rv["commanders"]] == [False, False]
    assert all(s["counts"]["conflicts"] == 0 for c in rv["commanders"] for s in c["sections"])
    log_id = restore.import_file(b, b_dir, arc, restore.Plan(ALL_MERGE, {}), app_version="test", rebuild=projectors.rebuild)

    assert state(b, b_dir) == before                                     # every table, note, setting and derived count
    assert {r[0] for r in b.execute("SELECT DISTINCT source FROM events")} == {"edplanner_import"}
    row = b.execute("SELECT * FROM import_log WHERE id = ?", (log_id,)).fetchone()
    assert row["kind"] == "edplanner" and row["status"] == "imported" and row["events_added"] == len(before[FID]["events"]) + len(before[FID_2]["events"])
    assert FID not in restore.summary(row["detail"])                    # the log line never shows a FID

    # The same file again changes nothing.
    again = restore.review(b, b_dir, archive.Archive(payload))
    assert all(s["counts"]["new"] == 0 and s["counts"]["conflicts"] == 0 for c in again["commanders"] for s in c["sections"])
    restore.import_file(b, b_dir, archive.Archive(payload), restore.Plan(ALL_MERGE, {}), app_version="test",
                        rebuild=projectors.rebuild)
    assert state(b, b_dir) == before


def test_merge_shows_conflicts_and_takes_each_choice(tmp_path):
    a_dir, a = populated(tmp_path)
    payload = archive.export_bytes(a, a_dir, [FID], app_version="test")
    with a:
        a.execute("UPDATE goals SET status = 'parked' WHERE fid = ? AND template_key = 'mining'", (FID,))
        a.execute("UPDATE goals SET status = 'done' WHERE fid = ? AND template_key = 'combat'", (FID,))
        a.execute("INSERT INTO mining_spots (fid, commodity, method, system, created_ts) "
                  "VALUES (?, 'painite', 'laser', 'Borann', '2026-10-27T00:00:00Z')", (FID,))
        db.set_setting(a, "reserve_rebuys", "2")
    note_id = next(iter(notes.raw(a_dir, FID)))
    notes.update(a_dir, FID, note_id, {"body": "Changed here"})

    arc = archive.Archive(payload)
    rv = restore.review(a, a_dir, arc)
    cmdr = rv["commanders"][0]
    assert cmdr["matched"] and cmdr["name"] == "Test Pilot"
    by = {s["key"]: s for s in cmdr["sections"]}
    assert by["history"]["counts"]["new"] == 0 and by["history"]["counts"]["conflicts"] == 0
    goal_conflicts = {c["title"]: c for c in by["goals"]["conflicts"]}
    assert set(goal_conflicts) == {"Ship Build Example - Mining", "Ship Build Example - Combat (PvE)"}
    assert goal_conflicts["Ship Build Example - Mining"]["diffs"] == [{"field": "status", "current": "parked", "file": "active"}]
    assert by["plans"]["counts"]["current_only"] == 1 and by["notes"]["counts"]["conflicts"] == 1
    assert rv["settings"]["conflicts"][0]["title"] == "Rebuys held in reserve"

    choices = {goal_conflicts["Ship Build Example - Mining"]["id"]: "file", goal_conflicts["Ship Build Example - Combat (PvE)"]["id"]: "current",
               by["notes"]["conflicts"][0]["id"]: "both", rv["settings"]["conflicts"][0]["id"]: "file"}
    restore.import_file(a, a_dir, arc, restore.Plan(ALL_MERGE, choices), app_version="test", rebuild=projectors.rebuild)
    status = dict(a.execute("SELECT template_key, status FROM goals WHERE fid = ?", (FID,)).fetchall())
    assert status["mining"] == "active" and status["combat"] == "done"
    assert a.execute("SELECT COUNT(*) FROM mining_spots WHERE fid = ?", (FID,)).fetchone()[0] == 2   # merge keeps yours
    bodies = sorted(n["body"] for n in notes.list_notes(a_dir, FID))
    assert bodies == ["Changed here", "Uranium **run**"]                                       # kept both
    assert db.get_setting(a, "reserve_rebuys") == "4"
    assert list((a_dir / "backups").glob("pre-import-*.edplanner"))


def test_override_then_rollback_keeps_new_journal_events(tmp_path):
    a_dir, a = populated(tmp_path)
    payload = archive.export_bytes(a, a_dir, [FID], app_version="test")
    with a:
        a.execute("DELETE FROM goals WHERE fid = ? AND template_key = 'combat'", (FID,))
    notes.create(a_dir, FID, "Made after the export", "")
    before = state(a, a_dir)

    modes = {"c0:history": "override", "c0:goals": "override", "c0:plans": "skip", "c0:notes": "override"}
    log_id = restore.import_file(a, a_dir, archive.Archive(payload, "old.edplanner"), restore.Plan(modes, {}),
                                 app_version="test", rebuild=projectors.rebuild)
    assert a.execute("SELECT COUNT(*) FROM goals WHERE fid = ? AND template_key = 'combat'", (FID,)).fetchone()[0] == 1
    assert [n["title"] for n in notes.list_notes(a_dir, FID)] == ["Trade route"]
    goal = a.execute("SELECT goal_id FROM builds WHERE fid = ?", (FID,)).fetchone()[0]
    assert a.execute("SELECT template_key FROM goals WHERE id = ?", (goal,)).fetchone()[0] == "mining"  # build re-pointed

    # The game keeps writing while the import is in place; a rollback must not lose that.
    with a:
        a.execute("INSERT INTO events (fid, ts, event, raw_json, hash, source) VALUES (?, ?, 'MaterialCollected', ?, 'h-live', 'journal')",
                  (FID, "2026-10-27T20:00:00Z", line("2026-10-27T20:00:00Z", "MaterialCollected", Category="Raw", Name="arsenic", Count=1)))
    assert restore.rollback_target(a, a_dir)["id"] == log_id
    restore.rollback(a, a_dir, log_id, app_version="test", rebuild=projectors.rebuild)
    after = state(a, a_dir)
    assert after[FID]["goals"] == before[FID]["goals"] and after[FID]["notes"] == before[FID]["notes"]
    assert set(after[FID]["events"]) - set(before[FID]["events"]) == {
        e for e in after[FID]["events"] if e[4] == "h-live"}
    assert a.execute("SELECT status FROM import_log WHERE id = ?", (log_id,)).fetchone()[0] == "rolled_back"
    assert restore.rollback_target(a, a_dir) is None
    with pytest.raises(LookupError):
        restore.rollback(a, a_dir, log_id, app_version="test", rebuild=projectors.rebuild)
    assert list((a_dir / "backups").glob("pre-rollback-*.edplanner"))


def test_rollback_removes_a_commander_the_import_created(tmp_path):
    a_dir, a = populated(tmp_path)
    payload = archive.export_bytes(a, a_dir, [FID_2], app_version="test")
    b_dir, b = fresh(tmp_path, "b")
    log_id = restore.import_file(b, b_dir, archive.Archive(payload), restore.Plan(ALL_MERGE, {}), app_version="test",
                                 rebuild=projectors.rebuild)
    assert b.execute("SELECT name FROM commanders").fetchall()[0][0] == "Second"
    restore.rollback(b, b_dir, log_id, app_version="test", rebuild=projectors.rebuild)
    assert b.execute("SELECT COUNT(*) FROM commanders").fetchone()[0] == 0
    assert b.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0 and notes.raw(b_dir, FID_2) == {}


def _rezip(payload: bytes, change) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(payload))
    files = {n: src.read(n) for n in src.namelist()}
    change(files)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for n, data in files.items():
            zf.writestr(n, data)
    return out.getvalue()


def test_bad_files_are_refused(tmp_path):
    a_dir, a = populated(tmp_path)
    good = archive.export_bytes(a, a_dir, [FID], app_version="test")
    with pytest.raises(ValueError, match="isn't a zip"):
        archive.Archive(b"not a zip")

    def tamper(files):
        files[f"commanders/{FID}/goals.json"] = b"[]"
    with pytest.raises(ValueError, match="checksum"):
        archive.Archive(_rezip(good, tamper))

    def newer(files):
        m = json.loads(files["manifest.json"])
        files["manifest.json"] = json.dumps(m | {"format_version": 99, "app_version": "9.9"}).encode()
    with pytest.raises(ValueError, match="newer version"):
        archive.Archive(_rezip(good, newer))

    def escape(files):
        m = json.loads(files["manifest.json"])
        m["commanders"][0]["fid"] = "../evil"
        files["manifest.json"] = json.dumps(m).encode()
    with pytest.raises(ValueError, match="can't hold"):
        archive.Archive(_rezip(good, escape))

    def missing(files):
        del files[f"commanders/{FID}/plans.json"]
    with pytest.raises(ValueError, match="missing"):
        archive.Archive(_rezip(good, missing))
    for bad in ("../planner.db", "incoming/../../x.edplanner", "a/b.edplanner", "x.zip"):
        with pytest.raises(LookupError):
            archive.resolve(a_dir, bad)


def test_backups_keep_the_newest(tmp_path, monkeypatch):
    a_dir, a = populated(tmp_path)
    monkeypatch.setattr(archive, "KEEP", 2)
    made = [archive.save_backup(a, a_dir, app_version="test") for _ in range(3)]
    left = sorted(p.name for p in (a_dir / "backups").glob("backup-*.edplanner"))
    assert len(left) == 2 and made[-1].name in left
    listed = archive.list_backups(a_dir)
    assert listed[0]["kind"] == "Backup" and sorted(listed[0]["commanders"]) == ["Second", "Test Pilot"]


@pytest.fixture()
def client(tmp_path):
    data_dir, c = populated(tmp_path)
    c.close()
    ctx = AppContext(data_dir=data_dir, journal_dir=tmp_path / "journals", port=8732, token="tok", version="test")
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8732") as tc:
        yield tc, data_dir


def test_backup_pages_and_api(client):
    tc, data_dir = client
    assert tc.post("/api/backup/now").status_code == 403
    r = tc.post("/api/backup/now", headers=H)
    assert r.status_code == 200 and r.json()["redirect"].startswith("/settings/backup?saved=")
    page = tc.get("/settings/backup").text
    assert r.json()["name"] in page and "Backup now" in page and FID not in page and FID_2 not in page

    keys = [k.split('"')[0] for k in page.split('data-export-key="')[1:]]
    assert len(keys) == 2
    one = tc.post("/api/backup/export", json={"commanders": keys[:1]}, headers=H)
    assert len(archive.Archive(one.content).commanders) == 1
    exported = tc.post("/api/backup/export", json={"commanders": keys}, headers=H)
    assert exported.status_code == 200 and exported.headers["content-type"] == "application/octet-stream"
    assert "attachment" in exported.headers["content-disposition"]
    assert len(archive.Archive(exported.content).commanders) == 2       # a valid file with both

    assert tc.post("/api/backup/upload", content=b"junk", headers=H).status_code == 400
    up = tc.post("/api/backup/upload", content=exported.content, headers=H | {"X-Filename": "My%20export.edplanner"})
    ref = up.json()["redirect"].split("file=")[1]
    review = tc.get(f"/settings/backup/review?file={ref}").text
    assert "My export.edplanner" in review and "Test Pilot" in review and FID not in review
    assert tc.get("/settings/backup/review?file=../planner.db").status_code == 404

    from urllib.parse import unquote
    applied = tc.post("/api/backup/apply", json={"file": unquote(ref), "modes": ALL_MERGE, "choices": {}}, headers=H)
    assert applied.status_code == 200, applied.text
    done = applied.json()["redirect"]
    result = tc.get(done).text
    assert "Roll back this import" in result and FID not in result
    log_id = int(done.split("done=")[1])
    rb = tc.post(f"/api/backup/rollback/{log_id}", headers=H)
    diag = tc.get("/settings/diagnostics").text
    assert rb.status_code == 200 and "rolled back" in diag and "Test Pilot: merged history, goals, plans and notes" in diag
    assert FID not in diag
    assert tc.post(f"/api/backup/rollback/{log_id}", headers=H).status_code == 409

    dl = tc.post("/api/backup/download", json={"name": r.json()["name"]}, headers=H)
    assert dl.status_code == 200 and dl.content[:2] == b"PK"
    assert tc.post("/api/backup/download", json={"name": "../planner.db"}, headers=H).status_code == 404


def test_first_launch_of_a_new_version_backs_up_first(tmp_path):
    from app import __version__
    from app.main import backup_before_update
    data_dir, c = populated(tmp_path)
    with c:
        db.set_setting(c, "app_version", "0.5.10")
    c.close()
    backup_before_update(data_dir / "planner.db", data_dir)
    made = list((data_dir / "backups").glob("pre-update-*.edplanner"))
    assert len(made) == 1 and archive.Archive(made[0]).manifest["app_version"] == "0.5.10"
    c = db.connect(data_dir / "planner.db")
    with c:
        db.set_setting(c, "app_version", __version__)
    c.close()
    backup_before_update(data_dir / "planner.db", data_dir)                # same version: nothing more
    assert len(list((data_dir / "backups").glob("pre-update-*.edplanner"))) == 1
