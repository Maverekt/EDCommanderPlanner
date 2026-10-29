"""Symbol normalization (DESIGN §6)."""

from __future__ import annotations

import re

# `$int_hyperdrive_size5_class5_name;` -> `int_hyperdrive_size5_class5`
_LOCALISATION_WRAPPER = re.compile(r"^\$(.*?)(?:_name)?;$")


def normalize_symbol(symbol: str | None) -> str | None:
    """Trim, lowercase, strip Frontier localisation wrappers."""
    if symbol is None:
        return None
    s = symbol.strip().lower()
    m = _LOCALISATION_WRAPPER.match(s)
    if m:
        s = m.group(1)
    return s


def display_text(value: str | None) -> str | None:
    """Trim free text for display; blank becomes None (unnamed ships are `""` or `" "`)."""
    if value is None:
        return None
    s = value.strip()
    return s or None


def mask_fid(fid: str | None) -> str:
    """Partially mask a Frontier ID for the UI and logs (DESIGN §15)."""
    if not fid:
        return ""
    if len(fid) <= 5:
        return fid[0] + "*" * (len(fid) - 1)
    return fid[:3] + "*" * (len(fid) - 5) + fid[-2:]
