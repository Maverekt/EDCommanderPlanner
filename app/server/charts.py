"""Server-rendered SVG line charts and a calendar heatmap (no JS charting library)."""

from __future__ import annotations

from datetime import date, datetime, timedelta

WIDTH, HEIGHT = 640, 200
PAD_L, PAD_R, PAD_T, PAD_B = 64, 12, 12, 26


def _t(ts: str) -> float:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def _compact(v: float) -> str:
    a = abs(v)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if a >= div:
            return f"{v / div:.{2 if a < 10 * div else 1}f}{suffix}"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.2f}"


def line_chart(points: list[tuple[str, float]], *, label_fmt=_compact, step: bool = False,
               width: int = WIDTH, height: int = HEIGHT) -> dict | None:
    """`points` = [(iso ts, value)], drawn evenly spaced in time order (play history has long gaps;
    each point's date is on hover). Returns geometry for templates/partials/chart.html."""
    pts = sorted((ts, v) for ts, v in points if v is not None)
    if not pts:
        return None
    xs = list(range(len(pts)))
    ys = [v for _, v in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if y0 == y1:
        y0, y1 = y0 - 1, y1 + 1
    pad = (y1 - y0) * 0.08
    y0, y1 = y0 - pad, y1 + pad
    iw, ih = width - PAD_L - PAD_R, height - PAD_T - PAD_B

    def sx(x):
        return PAD_L + (iw * (x - x0) / (x1 - x0) if x1 > x0 else iw / 2)

    def sy(y):
        return PAD_T + ih - ih * (y - y0) / (y1 - y0)

    coords = [(sx(x), sy(y)) for x, y in zip(xs, ys)]
    if step:
        path_pts = []
        for i, (cx, cy) in enumerate(coords):
            if i:
                path_pts.append((cx, coords[i - 1][1]))
            path_pts.append((cx, cy))
    else:
        path_pts = coords
    ticks = [y0 + (y1 - y0) * f for f in (0.1, 0.5, 0.9)]
    return {
        "width": width, "height": height, "pad_l": PAD_L, "pad_r": PAD_R, "pad_t": PAD_T, "pad_b": PAD_B,
        "path": " ".join(f"{x:.1f},{y:.1f}" for x, y in path_pts),
        "points": [{"x": round(cx, 1), "y": round(cy, 1), "ts": ts, "value": v, "label": label_fmt(v)}
                   for (cx, cy), (ts, v) in zip(coords, pts)],
        "yticks": [{"y": round(sy(t), 1), "label": label_fmt(t)} for t in ticks],
        "first": pts[0][0], "last": pts[-1][0],
        "min": min(ys), "max": max(ys), "count": len(pts),
    }


def calendar(counts: dict[str, int], end: date, weeks: int = 53, first_day: date | None = None) -> dict:
    """GitHub-style grid: columns are weeks (Monday first), rows are weekdays.
    With `first_day` the grid covers first_day..end (whole weeks) and `weeks` is ignored."""
    if first_day is not None:
        first_day = min(first_day, end)
        mondays = (end - timedelta(days=end.weekday())) - (first_day - timedelta(days=first_day.weekday()))
        weeks = mondays.days // 7 + 1
    start = end - timedelta(days=end.weekday()) - timedelta(weeks=weeks - 1)
    shown = {d: n for d, n in counts.items() if (first_day is None or d >= first_day.isoformat()) and d <= end.isoformat()}
    peak = max(shown.values(), default=0)
    cells, months = [], []
    for w in range(weeks):
        for d in range(7):
            day = start + timedelta(weeks=w, days=d)
            if day > end or (first_day is not None and day < first_day):
                continue
            n = counts.get(day.isoformat(), 0)
            level = 0 if not n else min(4, 1 + int(3 * n / peak)) if peak else 0
            cells.append({"x": w, "y": d, "day": day.isoformat(), "n": n, "level": level})
        first = start + timedelta(weeks=w)
        if first.day <= 7:
            months.append({"x": w, "label": first.strftime("%b")})
    return {"cells": cells, "months": months, "weeks": weeks,
            "active_days": sum(1 for c in cells if c["n"]), "peak": peak}
