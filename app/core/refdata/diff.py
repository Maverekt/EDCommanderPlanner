"""Diff report between two builds, as Markdown for the update PR body (DESIGN §7.5)."""

from __future__ import annotations

from app.core.refdata.build import RefData, item_summary

_LIST_LIMIT = 60


def diff(old: RefData | None, new: RefData) -> dict:
    a = item_summary(old) if old else {}
    b = item_summary(new)
    added = sorted(b.keys() - a.keys())
    removed = sorted(a.keys() - b.keys())
    renamed, changed = [], []
    for s in sorted(a.keys() & b.keys()):
        if a[s]["display_name"] != b[s]["display_name"]:
            renamed.append((s, a[s]["display_name"], b[s]["display_name"]))
        fields = [f for f in ("kind", "category", "family", "grade") if a[s][f] != b[s][f]]
        if fields:
            changed.append((s, {f: (a[s][f], b[s][f]) for f in fields}))
    old_recipes = set(old.recipe_costs.items()) if old else set()
    new_recipes = set(new.recipe_costs.items())
    old_bp = {k: (sorted(v["module_types"]), sorted(v["engineers"])) for k, v in (old.blueprints if old else {}).items()}
    new_bp = {k: (sorted(v["module_types"]), sorted(v["engineers"])) for k, v in new.blueprints.items()}
    return {
        "added": [(s, b[s]["kind"], b[s]["display_name"]) for s in added],
        "removed": [(s, a[s]["kind"], a[s]["display_name"]) for s in removed],
        "renamed": renamed,
        "changed": changed,
        "recipes_added": len(new_recipes - old_recipes),
        "recipes_removed": len(old_recipes - new_recipes),
        "blueprints_changed": sum(1 for k in old_bp.keys() | new_bp.keys() if old_bp.get(k) != new_bp.get(k)),
        "redundant_overrides": list(new.redundant_overrides),
        "redundant_fixes": list(new.redundant_fixes),
    }


def has_changes(d: dict) -> bool:
    return any([d["added"], d["removed"], d["renamed"], d["changed"], d["recipes_added"],
                d["recipes_removed"], d["blueprints_changed"]])


def _section(title: str, rows: list[str]) -> list[str]:
    if not rows:
        return []
    out = [f"### {title} ({len(rows)})", ""]
    out += rows[:_LIST_LIMIT]
    if len(rows) > _LIST_LIMIT:
        out.append(f"- …and {len(rows) - _LIST_LIMIT} more")
    return out + [""]


def to_markdown(d: dict, new: RefData, header: dict) -> str:
    lines = ["## Reference data update", ""]
    lines += [f"- **{k}:** {v}" for k, v in header.items()]
    lines.append("")
    if new.errors:
        lines += ["### ❌ Validation failed", ""] + [f"- {e}" for e in new.errors] + [""]
    else:
        lines += ["Validation passed.", ""]
    lines += _section("Added", [f"- `{s}` ({k}): {n}" for s, k, n in d["added"]])
    lines += _section("Removed", [f"- `{s}` ({k}): {n}" for s, k, n in d["removed"]])
    lines += _section("Renamed", [f"- `{s}`: {a!r} → {b!r}" for s, a, b in d["renamed"]])
    lines += _section("Changed category / family / grade", [
        f"- `{s}`: " + ", ".join(f"{f} {a!r} → {b!r}" for f, (a, b) in ch.items()) for s, ch in d["changed"]
    ])
    lines += _section("Overrides now redundant (upstream carries them)",
                      [f"- `{s}`: remove it from local_overrides.csv" for s in d["redundant_overrides"]])
    lines += _section("Recipe fixes now redundant (upstream matches them)",
                      [f"- `{s}`: remove its rows from recipe_fixes.csv" for s in d.get("redundant_fixes", [])])
    lines += [
        "### Recipes and blueprints", "",
        f"- Recipe cost rows added: {d['recipes_added']}, removed: {d['recipes_removed']}",
        f"- Blueprint grades with changed modules or engineers: {d['blueprints_changed']}", "",
    ]
    if new.warnings:
        lines += _section("Warnings", [f"- {w}" for w in new.warnings])
    if not has_changes(d) and not new.errors:
        lines += ["No changes to the built reference data.", ""]
    return "\n".join(lines)
