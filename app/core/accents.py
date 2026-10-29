"""Accent colours for Settings → Display: the HUD colour on every highlight, the logo and the tray icon.

Each is bright enough to stand out on the near-black panels and to carry black text on top.
Where an accent sits close to the cyan used for live data, live data turns amber instead so
the two still read apart.
"""

from __future__ import annotations

from typing import NamedTuple

DEFAULT = "orange"


class Accent(NamedTuple):
    key: str
    label: str
    hex: str
    live: str | None = None      # replaces the live-data cyan when this accent is too close to it


ACCENTS = {a.key: a for a in (
    Accent("orange", "Elite orange", "#ff7a00"),
    Accent("purple", "Neon purple", "#b84dff"),
    Accent("green", "Neon green", "#39ff14"),
    Accent("cyan", "Cyan", "#00e5ff", live="#ffb347"),
    Accent("blue", "Electric blue", "#3d8bff", live="#ffb347"),
    Accent("pink", "Hot pink", "#ff2e9a"),
    Accent("gold", "Gold", "#ffc400"),
)}


def get(key: str | None) -> Accent:
    return ACCENTS.get(key or DEFAULT) or ACCENTS[DEFAULT]


def rgb(hex_colour: str) -> tuple[int, int, int]:
    h = hex_colour.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
