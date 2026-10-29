"""Phase 5f: notes as files per commander, the safe Markdown subset, the Overview grouping and in-place pages."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.core import notes, paths
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.refdata.loader import ensure_loaded
from app.server import charts
from app.server.app import TOKEN_HEADER, create_app
from app.server.context import AppContext

from .conftest import FID, FID_2, session, write_journal

H = {TOKEN_HEADER: "tok"}


def test_notes_are_files_per_commander(tmp_path):
    a = notes.create(tmp_path, FID, "Trade route", "Uranium **run**")
    b = notes.create(tmp_path, FID, "  Mining   spots ", "Paesia 2 A Ring")
    assert a != b and (tmp_path / "notes" / FID / f"{a}.md").is_file()
    assert notes.get(tmp_path, FID, b)["title"] == "Mining spots"            # whitespace collapsed
    assert notes.list_notes(tmp_path, FID_2) == []                             # another commander sees nothing
    notes.update(tmp_path, FID, a, {"pinned": True})
    assert [n["id"] for n in notes.list_notes(tmp_path, FID)] == [a, b]        # pinned first
    assert [n["id"] for n in notes.list_notes(tmp_path, FID, "paesia ring")] == [b]
    notes.update(tmp_path, FID, b, {"title": "", "body": "edited"})
    got = notes.get(tmp_path, FID, b)
    assert got["title"] == "Untitled note" and got["body"] == "edited" and got["created"] <= got["updated"]
    notes.delete(tmp_path, FID, b)
    assert (tmp_path / "notes" / FID / "deleted" / f"{b}.md").is_file()       # moved aside, not erased
    assert [n["id"] for n in notes.list_notes(tmp_path, FID)] == [a]
    with pytest.raises(LookupError):
        notes.get(tmp_path, FID, b)


def test_note_ids_and_commanders_cannot_escape_the_folder(tmp_path):
    for bad in ("../secret", "..\\x", "20261027-101010/../../x", ""):
        with pytest.raises(LookupError):
            notes.get(tmp_path, FID, bad)
    with pytest.raises(ValueError):
        notes.create(tmp_path, "../F1", "x")


def test_a_note_edited_outside_the_app_is_read_as_is(tmp_path):
    folder = tmp_path / "notes" / FID
    folder.mkdir(parents=True)
    (folder / "20261027-090000.md").write_bytes(b"Just text, no front matter\r\n")
    n = notes.list_notes(tmp_path, FID)[0]
    assert n["title"] == "Untitled note" and n["body"] == "Just text, no front matter\n" and not n["pinned"]


def test_markdown_subset_is_safe():
    html = str(notes.render("# Plan\n<script>alert(1)</script>\n- [x] buy **Type-11**\n- [ ] find `osmium`\n\n"
                            "[Inara](https://inara.cz) [bad](javascript:alert(1)) *mine* snake_case_name"))
    assert "<h3>Plan</h3>" in html and "&lt;script&gt;" in html and "<script>" not in html
    assert '<li class="task done"><input type="checkbox" disabled checked> buy <strong>Type-11</strong></li>' in html
    assert "<code>osmium</code>" in html and '<a href="https://inara.cz"' in html
    assert 'href="javascript' not in html and "<em>mine</em>" in html and "snake_case_name" in html


def test_excerpt_is_plain_text():
    assert notes.excerpt("## Plan\n- [x] Laser kit\n- [ ] Keep **10 Osmium**\nSee [Inara](https://inara.cz)") == \
        "Plan · Laser kit · Keep 10 Osmium · See Inara"


def test_calendar_follows_the_range():
    end = date(2026, 10, 27)                                                    # a Tuesday
    cal = charts.calendar({"2026-10-01": 3, "2026-09-01": 9}, end, first_day=date(2026, 9, 27))
    assert cal["weeks"] == 6 and cal["cells"][0]["day"] == "2026-09-27"
    assert cal["active_days"] == 1 and cal["peak"] == 3                         # September 1st is outside the range


@pytest.fixture()
def client(db_path, journal_dir, filter_csv):
    ensure_loaded(db_path, paths.refdata_dir())
    write_journal(journal_dir, "Journal.2026-10-25T090238.01.log", session("2026-10-25"))
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    ctx = AppContext(data_dir=db_path.parent, journal_dir=journal_dir, port=8732, token="tok", version="test")
    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8732") as c:
        yield c


def test_notes_api_and_page(client):
    assert "No notes yet" in client.get("/notes").text
    assert client.post("/api/notes", headers={}).status_code == 403
    r = client.post("/api/notes", headers=H)
    nid = r.json()["id"]
    assert r.json()["redirect"] == f"/notes?n={nid}&edit=1"
    editing = client.get(f"/notes?n={nid}&edit=1").text
    assert f'data-note-editor="{nid}"' in editing and 'class="note-read" hidden' in editing
    assert client.post(f"/api/notes/{nid}/update", json={"title": "Engineers", "body": "- [ ] Osmium x10"},
                       headers=H).json()["redirect"] == f"/notes?n={nid}"
    page = client.get(f"/notes?n={nid}").text
    assert '<h2 class="note-heading">Engineers</h2>' in page and 'class="task"' in page and "F0000001" not in page
    assert 'data-task="0"' in page and f'data-note-editor="{nid}" hidden' in page     # read first; to-dos tick
    assert client.post(f"/api/notes/{nid}/task", json={"index": 0, "done": True}, headers=H).status_code == 200
    assert "- [x] Osmium x10" in client.get(f"/notes?n={nid}&edit=1").text
    assert client.post(f"/api/notes/{nid}/task", json={"index": 3, "done": True}, headers=H).status_code == 404
    preview = client.post("/api/notes/preview", json={"body": "**hi** <b>x</b>\n- [ ] a"}, headers=H).json()["html"]
    assert "<strong>hi</strong>" in preview and "&lt;b&gt;" in preview and "disabled" in preview
    assert client.post("/api/notes/preview", json={"body": "x"}).status_code == 403
    assert client.post(f"/api/notes/{nid}/pin", json={"pinned": True}, headers=H).status_code == 200
    assert "Unpin" in client.get("/notes").text
    assert "No notes match" in client.get("/notes?q=nothinglikethis").text
    assert client.post("/api/notes/..%2Fx/update", json={}, headers=H).status_code == 404
    assert client.post(f"/api/notes/{nid}/delete", headers=H).json()["redirect"] == "/notes"
    assert client.post(f"/api/notes/{nid}/delete", headers=H).status_code == 404


def test_overview_groups_four_subtabs(client):
    r = client.get("/overview", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/progress"
    assert client.get("/overview?range=7d", follow_redirects=False).headers["location"] == "/statistics?range=7d"
    page = client.get("/history?range=30d").text
    sub = page[page.index('aria-label="Overview"'):]
    assert [x for x in ("Progress", "Statistics", "History", "Ledger") if x in sub[:600]] == \
        ["Progress", "Statistics", "History", "Ledger"]
    assert 'id="page-main" data-swap' in page and "this.form.submit" not in page
    top = page[page.index('aria-label="Sections"'):page.index('aria-label="Overview"')]
    assert '<a href="/progress" class="tab active' in top and top.index("Goals") < top.index("Notes") < top.index("Settings")


def test_ticking_a_todo_changes_only_that_box(tmp_path):
    body = "Plan\n- [ ] one\n```\n- [ ] in code, not a box\n```\n1. [x] two\n- three\n  - [ ] four"
    nid = notes.create(tmp_path, FID, "Tasks", body)
    assert str(notes.render(body, tasks=True)).count("data-task=") == 3 and "data-task" not in str(notes.render(body))
    notes.toggle_task(tmp_path, FID, nid, 1, False)
    notes.toggle_task(tmp_path, FID, nid, 2, True)
    assert notes.get(tmp_path, FID, nid)["body"] == body.replace("1. [x] two", "1. [ ] two").replace("- [ ] four", "- [x] four")
    with pytest.raises(LookupError):
        notes.toggle_task(tmp_path, FID, nid, 3, True)


def test_links_lists_and_quotes():
    html = str(notes.render("[Inara](inara.cz/elite) www.edsy.org/x [bad](javascript:alert(1)) [no](nodot) "
                            "[wiki](https://x.org/a_(b)) ~~old~~ ![pic](https://x.org/p.png)"))
    assert '<a href="https://inara.cz/elite"' in html and '<a href="https://www.edsy.org/x"' in html
    assert "[bad](javascript:alert(1))" in html and "[no](nodot)" in html and "javascript:" not in html.split("[bad]")[0]
    assert 'href="https://x.org/a_(b)"' in html and "<del>old</del>" in html and "!<a" not in html
    nested = str(notes.render("1. one\n2. two\n- bullet\n  - [ ] sub task\n\n- after a blank\n\n> a\n> b", tasks=True))
    assert nested.startswith("<ol><li>one</li><li>two</li></ol><ul><li>bullet<ul><li class=\"task\">")
    assert 'data-task="0"' in nested and "<li>after a blank</li></ul>" in nested and "<blockquote>a<br>b</blockquote>" in nested
    assert notes.link_target("example.com") == "https://example.com" and notes.link_target("data:text/html,x") is None
