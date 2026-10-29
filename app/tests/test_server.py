import re

import pytest
from fastapi.testclient import TestClient

from app.core import paths
from app.core.journal.importer import ImportStatus, run_backfill
from app.core.refdata.loader import ensure_loaded
from app.server.app import TOKEN_HEADER, create_app
from app.server.context import AppContext

from .conftest import line, session, write_journal

PORT = 8732
TOKEN = "test-token"


@pytest.fixture()
def ctx(tmp_path, db_path, journal_dir, filter_csv):
    write_journal(journal_dir, "Journal.2026-10-25T090238.01.log", session("2026-10-25"))
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    ctx = AppContext(data_dir=db_path.parent, journal_dir=journal_dir, port=PORT, token=TOKEN, version="test")
    ctx.refdata = ensure_loaded(db_path, paths.refdata_dir())
    return ctx


@pytest.fixture()
def client(ctx):
    with TestClient(create_app(ctx), base_url=f"http://127.0.0.1:{PORT}") as c:
        yield c


@pytest.mark.parametrize("path", ["/", "/statistics", "/progress", "/history", "/history?range=all&track=trade",
                                  "/ledger", "/ledger?tab=merc", "/ledger?tab=ships", "/ledger?tab=modules",
                                  "/ledger?tab=modules&change=engineered", "/ledger?tab=unlocks",
                                  "/ledger?tab=credits&range=all", "/ships",
                                  "/materials", "/goals", "/settings", "/settings/diagnostics",
                                  "/partials/import"])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text


def test_dashboard_shows_commander_and_login(client):
    html = client.get("/").text
    assert "Test Pilot" in html
    assert "1,234,567" in html
    assert "Anaconda" in html
    assert "F00***01" in html and "F0000001" not in html


@pytest.mark.parametrize("path", ["/", "/settings", "/settings/diagnostics"])
def test_import_card_shows_last_import_everywhere(client, path):
    html = client.get(path).text
    card = html.split("Last import</dt><dd>", 1)[1]
    assert not card.startswith("—")


def test_settings_shows_reference_data(client):
    html = client.get("/settings").text
    assert "EDCD/FDevIDs" in html and "EDCD/coriolis-data" in html and "not loaded" not in html


def test_ship_display_name_falls_back_without_refdata(tmp_path, db_path, journal_dir, filter_csv):
    from app.core import db
    from app.server import queries
    write_journal(journal_dir, "Journal.x.01.log", session("2026-10-25"))
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    conn = db.connect(db_path)
    try:
        assert queries.display_name(conn, "Anaconda", "$ANACONDA_NAME;") == "anaconda"  # raw symbol last
        assert queries.display_name(conn, "Anaconda", "Anaconda") == "Anaconda"           # localised next
    finally:
        conn.close()


def test_static_assets_are_cache_busted(client):
    html = client.get("/").text
    assert "/static/planner.css?v=" in html and "/static/app.js?v=" in html


def test_parallel_partials_do_not_cross_thread_errors(client):
    # Pages fetch several partials at once; FastAPI may open and close a request's
    # connection on different worker threads (seen in the 0.3.0 exe).
    from concurrent.futures import ThreadPoolExecutor
    paths = ["/partials/live", "/partials/gamestate", "/partials/strip", "/partials/progress",
             "/partials/materials", "/partials/import"] * 8
    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = list(pool.map(lambda p: client.get(p).status_code, paths))
    assert codes == [200] * len(paths)


def test_request_connection_can_close_on_another_thread(ctx):
    import threading
    from app.core import db
    c = db.connect(ctx.db_path, any_thread=True)
    c.execute("SELECT 1").fetchone()
    errors = []

    def use_and_close():
        try:
            c.execute("SELECT 1").fetchone()
            c.close()
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=use_and_close)
    t.start()
    t.join()
    assert errors == []


def test_rejects_foreign_host_header(ctx):
    with TestClient(create_app(ctx), base_url="http://evil.example:8732") as c:
        assert c.get("/").status_code == 400


def test_localhost_name_allowed(ctx):
    with TestClient(create_app(ctx), base_url=f"http://localhost:{PORT}") as c:
        assert c.get("/").status_code == 200


def test_state_change_requires_token(client):
    assert client.post("/api/rescan").status_code == 403
    assert client.post("/api/rescan", headers={TOKEN_HEADER: "wrong"}).status_code == 403


def test_rescan_with_token_starts(client, ctx):
    r = client.post("/api/rescan", headers={TOKEN_HEADER: TOKEN})
    assert r.status_code in (202, 409)
    ctx.wait_for_import(timeout=10)
    assert ctx.import_status.snapshot()["state"] == "done"
    assert ctx.import_status.snapshot()["events_added"] == 0


def test_rebuild_requires_token_and_runs(client):
    assert client.post("/api/rebuild").status_code == 403
    r = client.post("/api/rebuild", headers={TOKEN_HEADER: TOKEN})
    assert r.status_code == 200 and r.json()["events"] > 0


@pytest.mark.parametrize("path", ["/statistics?range=7d", "/statistics?range=30d", "/statistics?range=all",
                                  "/statistics?range=bogus"])
def test_overview_ranges_render(client, path):
    assert client.get(path).status_code == 200


def test_select_unknown_commander_404(client):
    r = client.post("/api/commanders/select", json={"key": "nope"}, headers={TOKEN_HEADER: TOKEN})
    assert r.status_code == 404


def test_hover_cards_setting(client, ctx):
    assert 'data-hover-cards="select"' in client.get("/materials").text   # selectable by default
    assert client.post("/api/settings/display", json={"hover_cards": False}).status_code == 403
    assert client.post("/api/settings/display", json={"hover_cards": False},
                       headers={TOKEN_HEADER: TOKEN}).status_code == 200
    html = client.get("/settings").text
    body = re.search(r"<body[^>]*>", html)[0]
    assert 'data-hover-cards="plain"' in body and "data-setting-hover-cards" in html
    client.post("/api/settings/display", json={"hover_cards": True}, headers={TOKEN_HEADER: TOKEN})
    assert "checked> Hover details" in client.get("/settings").text


def test_accent_colour_setting(client, ctx):
    html = client.get("/settings").text
    assert re.search(r'<html[^>]*style="--accent: #ff7a00"', html) and 'href="/icon.svg?accent=orange"' in html
    assert 'data-setting-accent="purple"' in html and re.search(
        r'aria-checked="true"\s+data-setting-accent="orange" data-hex="#ff7a00"', html)
    assert 'data-theme-name>Elite orange<' in client.get("/materials").text      # the Theme menu is on every page
    assert client.post("/api/settings/display", json={"accent": "nope"},
                       headers={TOKEN_HEADER: TOKEN}).status_code == 400
    seen = []
    ctx.on_accent = seen.append                   # the tray's hook
    assert client.post("/api/settings/display", json={"accent": "cyan"}, headers={TOKEN_HEADER: TOKEN}).status_code == 200
    assert seen[0].hex == "#00e5ff"
    assert client.get("/api/settings/display").json() == {"accent": "cyan", "hex": "#00e5ff", "live": "#ffb347",
                                                          "hover_cards": True}
    html = client.get("/materials").text
    assert 'style="--accent: #00e5ff; --live: #ffb347"' in html     # live data turns amber beside cyan
    assert 'data-hover-cards="select"' in html                          # the other display setting is untouched
    icon = client.get("/icon.svg")
    assert icon.headers["content-type"].startswith("image/svg") and "#00e5ff" in icon.text and "#ff7a00" not in icon.text
    assert "#b84dff" in client.get("/icon.svg?accent=purple").text


def test_statistics_search_box(db_path, journal_dir, filter_csv):
    lines = session("2026-10-25") + [line("2026-10-25T12:00:00Z", "Statistics", Combat={"Bounties_Claimed": 3},
                                          Material_Trader_Stats={"Trades_Completed": 9})]
    write_journal(journal_dir, "Journal.2026-10-25T090238.01.log", lines)
    run_backfill(db_path, journal_dir, filter_csv, ImportStatus())
    ctx = AppContext(data_dir=db_path.parent, journal_dir=journal_dir, port=PORT, token=TOKEN, version="test")
    ctx.refdata = ensure_loaded(db_path, paths.refdata_dir())
    with TestClient(create_app(ctx), base_url=f"http://127.0.0.1:{PORT}") as client:
        html = client.get("/statistics?range=all&q=bounty").text
        assert 'value="bounty"' in html and "data-filter-stats" in html and "range=session&amp;q=bounty" in html
        assert "Bounties claimed" in html and "Material Trader" not in html and "Trades completed" not in html
