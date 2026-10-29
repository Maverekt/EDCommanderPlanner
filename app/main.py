"""Entry point: lock, port, server, backfill, browser, tray (DESIGN §2)."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from app import __version__
from app.core import db, paths
from app.core.backup import archive
from app.core.refdata.loader import ensure_loaded
from app.instance import InstanceLock, clear_runtime, pick_port, read_runtime_port, write_runtime
from app.server.app import create_app
from app.server.context import AppContext

log = logging.getLogger("planner")


def setup_logging(logs_dir) -> None:
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        logs_dir / "planner.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:  # pythonw / windowed exe has no console
        root.addHandler(logging.StreamHandler())


def backup_before_update(db_path: Path, data_dir: Path) -> None:
    """The first launch of a new version backs up before migrating (DESIGN §4)."""
    if not db_path.is_file():
        return
    conn = db.connect(db_path)
    try:
        have = archive.tables(conn)
        if "settings" not in have or "commanders" not in have:
            return
        if db.get_setting(conn, "app_version") == __version__:
            return
        if not conn.execute("SELECT 1 FROM commanders LIMIT 1").fetchone():
            return
        path = archive.save_backup(conn, data_dir, app_version=db.get_setting(conn, "app_version") or "before 0.6",
                                   kind="pre-update")
        log.info("backed up before the update to %s: %s", __version__, path.name)
    except Exception:  # noqa: BLE001 - a failed backup must not stop the app; it is logged
        log.exception("backup before the update failed")
    finally:
        conn.close()


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="ED Commander Planner")
    p.add_argument("--no-tray", action="store_true", help="run without the tray icon (Ctrl+C to exit)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser tab on start")
    p.add_argument("--port", type=int, default=None, help="preferred port (default 8732)")
    p.add_argument("--journal-dir", default=None, help="override the journal folder")
    p.add_argument("--data-dir", default=None, help="override the data folder (testing against a journal copy)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data_dir) if args.data_dir else paths.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(data_dir / "logs")

    lock = InstanceLock(data_dir)
    if not lock.acquire():
        port = read_runtime_port(data_dir)
        log.info("another instance is running on port %s; opening it", port)
        if port and not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        return 0

    port = pick_port(args.port or 8732)
    ctx = AppContext(
        data_dir=data_dir,
        journal_dir=paths.journal_dir(args.journal_dir),
        port=port,
        token=secrets.token_urlsafe(32),
        version=__version__,
    )
    backup_before_update(ctx.db_path, data_dir)
    db.init_db(ctx.db_path)
    conn = db.connect(ctx.db_path)
    try:
        with conn:
            db.set_setting(conn, "app_version", __version__)
    finally:
        conn.close()
    ctx.refdata = ensure_loaded(ctx.db_path, paths.refdata_dir())
    write_runtime(data_dir, port)
    log.info("ED Commander Planner %s on %s; data %s; journals %s", __version__, ctx.base_url, data_dir, ctx.journal_dir)

    server = uvicorn.Server(uvicorn.Config(
        create_app(ctx), host="127.0.0.1", port=port, log_config=None,
        timeout_graceful_shutdown=2, access_log=False,
    ))
    server_thread = threading.Thread(target=server.run, name="web-server", daemon=True)
    server_thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)

    ctx.start_rescan()
    ctx.start_watcher()
    if not args.no_browser:
        webbrowser.open(ctx.base_url)

    stopped = threading.Event()

    def shutdown() -> None:
        if stopped.is_set():
            return
        stopped.set()
        log.info("shutting down")
        ctx.stop.set()
        ctx.wait_for_import(timeout=10)
        ctx.stop_watcher(timeout=5)
        server.should_exit = True
        server_thread.join(timeout=5)
        clear_runtime(data_dir)
        lock.release()

    try:
        if args.no_tray:
            while server_thread.is_alive():
                server_thread.join(timeout=0.5)
        else:
            from app.tray import run_tray
            run_tray(ctx, shutdown)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
