"""System tray icon: Open Planner, Backup Now, Open Data Folder, Exit (DESIGN §2)."""

from __future__ import annotations

import logging
import os
import webbrowser
from typing import Callable

import pystray
from PIL import Image, ImageDraw

from app.core import accents, db
from app.server.context import AppContext

log = logging.getLogger(__name__)


def make_icon_image(size: int = 64, colour: str = accents.ACCENTS[accents.DEFAULT].hex) -> Image.Image:
    """The planner mark: a hexagon outline around a solid triangle, in the accent colour."""
    fill = (*accents.rgb(colour), 255)
    img = Image.new("RGBA", (size, size), (12, 12, 15, 255))
    d = ImageDraw.Draw(img)
    s = size / 32
    hexagon = [(16, 2), (28.1, 9), (28.1, 23), (16, 30), (3.9, 23), (3.9, 9)]
    d.polygon([(x * s, y * s) for x, y in hexagon], outline=fill, width=max(2, round(2.4 * s)))
    d.polygon([(16 * s, 9 * s), (23 * s, 21.5 * s), (9 * s, 21.5 * s)], fill=fill)
    return img


def run_tray(ctx: AppContext, on_exit: Callable[[], None]) -> None:
    """Blocks on the calling thread until Exit is chosen."""

    def open_planner(icon, item):
        webbrowser.open(ctx.base_url)

    def open_data_folder(icon, item):
        os.startfile(ctx.data_dir)  # noqa: S606 - Windows only, our own folder

    def backup_now(icon, item):
        try:
            path = ctx.backup_now()
            message = f"Saved {path.name} in the data folder's backups."
        except Exception:  # noqa: BLE001 - the tray must survive; the log has the details
            log.exception("tray backup failed")
            message = "The backup failed; see the log in the data folder."
        try:
            icon.notify(message, "ED Commander Planner")
        except Exception:  # noqa: BLE001 - notifications are optional
            pass

    def exit_app(icon, item):
        icon.visible = False
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Planner", open_planner, default=True),
        pystray.MenuItem("Backup Now", backup_now),
        pystray.MenuItem("Open Data Folder", open_data_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", exit_app),
    )
    try:
        conn = db.connect(ctx.db_path)
        try:
            colour = accents.get(db.get_setting(conn, "accent")).hex
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - the default colour will do
        colour = accents.ACCENTS[accents.DEFAULT].hex
    icon = pystray.Icon("ed-commander-planner", make_icon_image(colour=colour),
                        f"ED Commander Planner · 127.0.0.1:{ctx.port}", menu)

    def recolour(accent):
        icon.icon = make_icon_image(colour=accent.hex)
    ctx.on_accent = recolour
    try:
        icon.run()
    finally:
        on_exit()
