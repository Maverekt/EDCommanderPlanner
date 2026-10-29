"""Build `ref_*` data from vendored upstream files plus our CSVs, and validate it (DESIGN §7).

`build(refdata_dir)` never raises on bad data: problems land in `RefData.errors`
(build-failing) or `RefData.warnings`. Callers decide what failing means.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.core.normalize import normalize_symbol

REFDATA_SCHEMA = 1

FDEVIDS_FILES = {
    "shipyard.csv": ["id", "symbol", "name", "entitlement"],
    "outfitting.csv": ["id", "symbol", "category", "name", "mount", "guidance", "ship", "class", "rating", "entitlement"],
    "material.csv": ["id", "symbol", "rarity", "type", "category", "name"],
    "commodity.csv": ["id", "symbol", "category", "name"],
    "rare_commodity.csv": ["id", "symbol", "market_id", "category", "name"],
    "microresources.csv": ["id", "symbol", "category", "English name"],
    "engineers.csv": ["id", "system_address", "market_id", "name"],
}
CORIOLIS_FILES = {
    # file -> keys every entry must have
    "blueprints.json": {"fdname", "grades", "name"},
    "specials.json": {"edname", "name"},
    "modifications.json": {"name"},
    "modules.json": {"blueprints"},
}
# coriolis-data folders vendored whole for list prices (ships: hull and retail cost; modules: cost).
CORIOLIS_DIRS = ["ships", "modules/hardpoints", "modules/internal", "modules/standard"]
OUR_FILES = {
    "families_extra.csv": ["symbol", "family", "reason"],
    "module_types.csv": ["pattern", "module_type", "note"],
    "name_aliases.csv": ["kind", "source", "source_name", "maps_to", "reason"],
    "local_overrides.csv": ["symbol", "kind", "fdev_id", "display_name", "category", "family", "grade", "attrs_json", "reason"],
    "cosmetic_slots.csv": ["pattern", "note"],
    "ignored_events.csv": ["event", "condition", "reason"],
    "stats_map.csv": ["counter", "category", "label", "unit"],
    "ranks.csv": ["track", "rank", "name"],
    "ledger_map.csv": ["event", "condition", "currency", "category", "sign", "amount", "item"],
    "ledger_categories.csv": ["category", "label", "stat_counters", "note"],
    "engineer_locations.csv": ["engineer_id", "system", "station", "source"],
    "material_sources.csv": ["target", "where", "source"],
    "flags.csv": ["key", "label", "auto", "affects"],
    "module_type_names.csv": ["module_type", "label"],
    "engineer_offer_fixes.csv": ["engineer_id", "module_type", "max_grade", "source", "note"],
    "mining_commodities.csv": ["commodity", "methods", "rings", "note", "source"],
    "blueprints_extra.csv": ["blueprint", "display_name", "grade", "material", "qty", "merc_coins", "source", "note"],
    "engineer_offers_extra.csv": ["engineer_id", "module_type", "blueprint", "max_grade", "source", "note"],
    "recipe_fixes.csv": ["recipe_kind", "recipe", "grade", "material", "qty", "source", "note"],
    "engineer_unlocks.csv": ["engineer_id", "referred_by", "meet", "meet_rule", "unlock", "permit", "referral_task",
                             "source"],
}
STAT_UNITS = {"count", "credits", "seconds", "ly", "metres", "ratio", "merc_coins"}
MERCGEAR = "mercgear"


@dataclass
class RefItem:
    symbol: str
    kind: str
    fdev_id: int | None
    display_name: str
    category: str | None
    family: str | None
    grade: int | None
    attrs: dict
    source: str
    source_commit: str


@dataclass
class RefData:
    items: dict[str, RefItem] = field(default_factory=dict)
    # (blueprint, grade) -> {"module_types": set, "engineers": set}
    blueprints: dict[tuple[str, int], dict] = field(default_factory=dict)
    # (recipe_kind, recipe, grade, material) -> qty
    recipe_costs: dict[tuple[str, str, int, str], int] = field(default_factory=dict)
    # effect -> {"display_name": str, "module_types": set}
    experimentals: dict[str, dict] = field(default_factory=dict)
    # blueprint -> display name
    blueprint_names: dict[str, str] = field(default_factory=dict)
    # (engineer_id, module_type, blueprint) -> highest grade that engineer offers
    engineer_offers: dict[tuple[int, str, str], int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    redundant_overrides: list[str] = field(default_factory=list)
    redundant_fixes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _read_csv(path: Path, expected: list[str], data: RefData, label: str) -> list[dict]:
    if not path.is_file():
        data.errors.append(f"{label}: file missing")
        return []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        if columns != expected:
            data.errors.append(f"{label}: columns changed, expected {expected}, found {columns}")
        return list(reader)


def _read_json(path: Path, required: set[str], data: RefData, label: str) -> dict:
    if not path.is_file():
        data.errors.append(f"{label}: file missing")
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        data.errors.append(f"{label}: invalid JSON ({exc})")
        return {}
    for key, entry in doc.items():
        missing = required - set(entry)
        if missing:
            data.errors.append(f"{label}: entry {key!r} lacks keys {sorted(missing)}")
            break
    return doc


def _int(value: str | None) -> int | None:
    value = (value or "").strip()
    return int(value) if value.lstrip("-").isdigit() else None


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _add(data: RefData, item: RefItem) -> None:
    existing = data.items.get(item.symbol)
    if existing is None:
        data.items[item.symbol] = item
        return
    # The one tolerated duplicate: a module and its Merc Coin (mercgear) variant share a
    # symbol. The journal can't tell them apart, so the standard module keeps the symbol
    # and the variant is recorded on it (Decision Log).
    pair = {existing.category, item.category}
    if existing.kind == item.kind == "module" and MERCGEAR in pair and len(pair) == 2:
        base, variant = (existing, item) if item.category == MERCGEAR else (item, existing)
        base.attrs.setdefault("mercgear_variants", []).append(
            {"fdev_id": variant.fdev_id, "display_name": variant.display_name}
        )
        data.items[item.symbol] = base
        return
    data.errors.append(
        f"duplicate symbol {item.symbol!r}: {existing.kind} {existing.display_name!r} "
        f"and {item.kind} {item.display_name!r}"
    )


def build(refdata_dir: Path, commits: dict[str, str] | None = None) -> RefData:
    commits = commits or {}
    fdev_commit = commits.get("fdevids", "unknown")
    cor_commit = commits.get("coriolis", "unknown")
    data = RefData()
    fdev = refdata_dir / "fdevids"
    cor = refdata_dir / "coriolis"

    src = {name: _read_csv(fdev / name, cols, data, f"fdevids/{name}") for name, cols in FDEVIDS_FILES.items()}
    ours = {name: _read_csv(refdata_dir / name, cols, data, name) for name, cols in OUR_FILES.items()}

    module_rules = []
    for row in ours["module_types.csv"]:
        try:
            module_rules.append((re.compile(row["pattern"]), row["module_type"].strip(), row["pattern"]))
        except re.error as exc:
            data.errors.append(f"module_types.csv: bad pattern {row['pattern']!r} ({exc})")
    rule_hits = {pattern: 0 for _, _, pattern in module_rules}

    def item(kind: str, row: dict, name_col: str = "name", **kw) -> RefItem:
        return RefItem(
            symbol=normalize_symbol(row["symbol"]), kind=kind, fdev_id=_int(row.get("id")),
            display_name=(row.get(name_col) or "").strip() or row["symbol"].strip(),
            category=kw.pop("category", _text(row.get("category"))),
            family=kw.pop("family", None), grade=kw.pop("grade", None), attrs=kw.pop("attrs", {}),
            source="fdevids", source_commit=fdev_commit,
        )

    for row in src["shipyard.csv"]:
        _add(data, item("ship", row, category=None, attrs={"entitlement": _text(row["entitlement"])}))

    for row in src["outfitting.csv"]:
        symbol = normalize_symbol(row["symbol"])
        attrs = {k: _text(row[k]) for k in ("mount", "guidance", "ship", "rating", "entitlement") if _text(row[k])}
        if _int(row["class"]) is not None:
            attrs["size"] = _int(row["class"])
        for rx, module_type, pattern in module_rules:
            if rx.search(symbol):
                attrs["module_type"] = module_type
                rule_hits[pattern] += 1
                break
        _add(data, item("module", row, attrs=attrs))

    families_extra = {normalize_symbol(r["symbol"]): r["family"].strip() for r in ours["families_extra.csv"]}
    for row in src["material.csv"]:
        symbol = normalize_symbol(row["symbol"])
        mtype, category = row["type"].strip(), row["category"].strip()
        attrs = {"type": mtype}
        family = None
        if mtype == "Raw":
            attrs["trader_column"] = _int(category)
        elif category == "None":
            family = families_extra.get(symbol)
            if family is None:
                data.errors.append(f"material {symbol!r} has no EDCD category and no families_extra.csv row")
        else:
            family = category
        _add(data, item("material", row, category=category, family=family, grade=_int(row["rarity"]), attrs=attrs))

    for row in src["commodity.csv"]:
        _add(data, item("commodity", row))
    for row in src["rare_commodity.csv"]:
        _add(data, item("rare_commodity", row, attrs={"market_id": _int(row["market_id"])}))
    for row in src["microresources.csv"]:
        _add(data, item("microresource", row, name_col="English name"))

    for row in src["engineers.csv"]:
        eid = _int(row["id"])
        scope = "onfoot" if eid and 400000 <= eid < 500000 else "ship" if eid and 300000 <= eid < 400000 else None
        if scope is None:
            data.warnings.append(f"engineer {row['id']} is outside the 3xxxxx/4xxxxx ranges")
        data.items[str(eid)] = RefItem(
            symbol=str(eid), kind="engineer", fdev_id=eid, display_name=row["name"].strip(),
            category=None, family=None, grade=None,
            attrs={"engineer_scope": scope, "system_address": _int(row["system_address"]),
                   "market_id": _int(row["market_id"])},
            source="fdevids", source_commit=fdev_commit,
        )

    for symbol in families_extra:
        found = data.items.get(symbol)
        if found is None or found.kind != "material":
            data.errors.append(f"families_extra.csv: {symbol!r} is not a material")
    for pattern, hits in rule_hits.items():
        if hits == 0:
            data.errors.append(f"module_types.csv: pattern {pattern!r} matches no module")

    for row in ours["stats_map.csv"]:
        if row["unit"] not in STAT_UNITS:
            data.errors.append(f"stats_map.csv: {row['counter']} has unknown unit {row['unit']!r}")
    tracks: dict[str, list[int]] = {}
    for row in ours["ranks.csv"]:
        tracks.setdefault(row["track"], []).append(_int(row["rank"]))
    for track, ranks in tracks.items():
        if ranks != list(range(len(ranks))):
            data.errors.append(f"ranks.csv: {track} ranks are not 0..{len(ranks) - 1} in order")

    _check_ledger(data, ours)
    _check_locations_and_sources(data, ours)
    _check_goal_templates(data, ours, refdata_dir)

    _apply_overrides(data, ours["local_overrides.csv"])
    _build_recipes(data, cor, ours["name_aliases.csv"], cor_commit)
    _read_prices(data, refdata_dir / "coriolis")
    _apply_extra_blueprints(data, ours["blueprints_extra.csv"], ours["engineer_offers_extra.csv"],
                            {r["module_type"] for r in ours["module_type_names.csv"]})
    _apply_offer_fixes(data, ours["engineer_offer_fixes.csv"])
    _apply_recipe_fixes(data, ours["recipe_fixes.csv"])
    _check_engineers(data, ours, refdata_dir)  # needs engineer_offers from the recipes
    _check_engineering_plan(data, refdata_dir)
    _check_example_builds(data, refdata_dir)
    return data


def _check_ledger(data: RefData, ours: dict[str, list[dict]]) -> None:
    from app.core.ledger_rules import CURRENCIES, parse_amount

    categories = {r["category"] for r in ours["ledger_categories.csv"]}
    counters = {r["counter"] for r in ours["stats_map.csv"]}
    for row in ours["ledger_categories.csv"]:
        for counter in filter(None, (c.strip() for c in row["stat_counters"].split(";"))):
            if counter not in counters:
                data.errors.append(f"ledger_categories.csv: {row['category']} uses unknown counter {counter!r}")
    for n, row in enumerate(ours["ledger_map.csv"], start=2):
        where = f"ledger_map.csv line {n} ({row['event']})"
        if row["currency"] not in CURRENCIES:
            data.errors.append(f"{where}: currency {row['currency']!r} is not cr or mc")
        if row["sign"] not in ("+", "-"):
            data.errors.append(f"{where}: sign {row['sign']!r} is not + or -")
        if row["category"] not in categories:
            data.errors.append(f"{where}: category {row['category']!r} is not in ledger_categories.csv")
        try:
            parse_amount(row["amount"])
        except ValueError as exc:
            data.errors.append(f"{where}: {exc}")
        if row["condition"].strip() and "=" not in row["condition"]:
            data.errors.append(f"{where}: condition {row['condition']!r} is not Field=value")


def _check_locations_and_sources(data: RefData, ours: dict[str, list[dict]]) -> None:
    engineers = {s for s, it in data.items.items() if it.kind == "engineer"}
    listed = set()
    for row in ours["engineer_locations.csv"]:
        eid = row["engineer_id"].strip()
        listed.add(eid)
        if eid not in engineers:
            data.errors.append(f"engineer_locations.csv: {eid} is not an FDevIDs engineer")
        if not row["system"].strip():
            data.errors.append(f"engineer_locations.csv: {eid} has no system")
    for eid in sorted(engineers - listed):
        data.warnings.append(f"engineer_locations.csv: no location for engineer {eid} ({data.items[eid].display_name})")

    materials = {s: it for s, it in data.items.items() if it.kind == "material"}
    types = {it.attrs.get("type") for it in materials.values()}
    families = {it.family for it in materials.values() if it.family}
    for row in ours["material_sources.csv"]:
        for target in filter(None, (x.strip() for x in row["target"].split(";"))):
            kind, _, value = target.partition(":")
            ok = (value in types) if kind == "type" else (value in families) if kind == "family" else                 normalize_symbol(target) in materials
            if not ok:
                data.errors.append(f"material_sources.csv: {target!r} is not a material, "
                                   "type:<Raw|Manufactured|Encoded> or family:<name>")


def _check_goal_templates(data: RefData, ours: dict[str, list[dict]], refdata_dir: Path) -> None:
    from app.core.goals import GOAL_KINDS, REPUTATION_KEYS, parse_auto_rule, parse_step_rule

    counters = {r["counter"] for r in ours["stats_map.csv"]}
    tracks = {r["track"] for r in ours["ranks.csv"]}
    flags = set()
    for row in ours["flags.csv"]:
        key = row["key"].strip()
        if key in flags:
            data.errors.append(f"flags.csv: duplicate key {key}")
        flags.add(key)
        if row["auto"].strip():
            try:
                kind, target, _ = parse_auto_rule(row["auto"])
            except ValueError as exc:
                data.errors.append(f"flags.csv: {key}: {exc}")
                continue
            known = {"stat": counters, "rank": tracks, "rep": REPUTATION_KEYS, "header": {"Odyssey"}}[kind]
            if target not in known:
                data.errors.append(f"flags.csv: {key}: unknown {kind} {target!r}")

    path = refdata_dir / "templates" / "goals.json"
    if not path.is_file():
        data.errors.append("templates/goals.json: file missing")
        return
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        data.errors.append(f"templates/goals.json: invalid JSON ({exc})")
        return
    seen_keys, seen_prio = set(), set()
    for g in doc.get("goals", []):
        where = f"templates/goals.json: {g.get('key')!r}"
        if g.get("kind") not in GOAL_KINDS:
            data.errors.append(f"{where}: kind {g.get('kind')!r} is not one of {sorted(GOAL_KINDS)}")
        if g.get("key") in seen_keys or g.get("priority") in seen_prio:
            data.errors.append(f"{where}: duplicate key or priority")
        seen_keys.add(g.get("key"))
        seen_prio.add(g.get("priority"))
        ship = g.get("ship")
        if ship and (ship not in data.items or data.items[ship].kind != "ship"):
            data.errors.append(f"{where}: ship {ship!r} is not an FDevIDs ship")
        for st in g.get("steps", []):
            if isinstance(st, dict) and st.get("auto"):
                try:
                    for group in parse_step_rule(st["auto"]):
                        for kind, key, _n in group:
                            known = {"flag": flags, "stat": counters,
                                     "owned": {s for s, it in data.items.items() if it.kind == "ship"},
                                     "engineer": {s for s, it in data.items.items() if it.kind == "engineer"}}.get(kind)
                            if known is not None and key not in known:
                                data.errors.append(f"{where}: step {st['text']!r}: unknown {kind} {key!r}")
                except ValueError as exc:
                    data.errors.append(f"{where}: {exc}")
        for req in g.get("requires", []):
            if "flag" in req and req["flag"] not in flags:
                data.errors.append(f"{where}: unknown flag {req['flag']!r}")
            if "rank" in req and req["rank"] not in tracks:
                data.errors.append(f"{where}: unknown rank track {req['rank']!r}")


def _check_engineers(data: RefData, ours: dict[str, list[dict]], refdata_dir: Path) -> None:
    from app.core.goals import parse_auto_rule

    engineers = {s for s, it in data.items.items() if it.kind == "engineer"}
    flags = {r["key"].strip() for r in ours["flags.csv"]}
    tracks = {r["track"] for r in ours["ranks.csv"]}
    codes = {r["module_type"] for r in ours["module_type_names.csv"]}
    listed = set()
    for row in ours["engineer_unlocks.csv"]:
        eid = row["engineer_id"].strip()
        where = f"engineer_unlocks.csv: {eid}"
        listed.add(eid)
        if eid not in engineers:
            data.errors.append(f"{where}: not an FDevIDs engineer")
        for ref in filter(None, (x.strip() for x in row["referred_by"].split(";"))):
            if ref not in engineers:
                data.errors.append(f"{where}: referred_by {ref} is not an engineer")
        rule = row["meet_rule"].strip()
        if rule.startswith("flag:"):
            if rule[5:] not in flags:
                data.errors.append(f"{where}: unknown flag {rule[5:]!r}")
        elif rule:
            try:
                kind, target, _ = parse_auto_rule(rule)
            except ValueError as exc:
                data.errors.append(f"{where}: {exc}")
            else:
                if kind != "rank" or target not in tracks:
                    data.errors.append(f"{where}: meet_rule must be flag:<KEY> or rank:<track>>=n")
        if row["permit"].strip() and row["permit"].strip() not in flags:
            data.errors.append(f"{where}: permit {row['permit']!r} is not a flag")
    for eid in sorted(engineers - listed):
        data.warnings.append(f"engineer_unlocks.csv: no unlock facts for engineer {eid}")

    path = refdata_dir / "templates" / "engineers.json"
    if not path.is_file():
        data.errors.append("templates/engineers.json: file missing")
        return
    doc = json.loads(path.read_text(encoding="utf-8"))
    for e in doc.get("engineers", []):
        eid = e.get("engineer_id")
        where = f"templates/engineers.json: {eid}"
        if str(eid) not in engineers:
            data.errors.append(f"{where}: not an engineer")
        for key in ("pin", "alt_pin"):
            pin = e.get(key)
            if not pin:
                continue
            if pin["module_type"] not in codes:
                data.errors.append(f"{where}: {key} module type {pin['module_type']!r} has no label")
            offered = data.engineer_offers.get((eid, pin["module_type"], pin["blueprint"]))
            if offered is None or pin["grade"] > offered:
                data.errors.append(f"{where}: {key} {pin['module_type']}/{pin['blueprint']} G{pin['grade']} "
                                   "isn't offered by this engineer")


def _read_prices(data: RefData, cor: Path) -> None:
    """List prices from coriolis-data: `cost` on modules (by symbol, else FDev ID), `hull_cost`/`retail_cost` on ships.

    A module listed at 0 gets no price (coriolis has cargo racks and the DSS at 0); stock armour is genuinely free.
    """
    by_id = {it.fdev_id: it for it in data.items.values() if it.fdev_id is not None}
    priced = missing = 0
    for path in sorted((cor / "ships").glob("*.json")):
        for ship in json.loads(path.read_text(encoding="utf-8")).values():
            it = by_id.get(ship.get("edID"))
            if it is None:
                missing += 1
                continue
            it.attrs["hull_cost"] = int(ship["properties"]["hullCost"])
            if ship.get("retailCost"):
                it.attrs["retail_cost"] = int(ship["retailCost"])
            priced += 1
            for b in ship.get("bulkheads") or []:
                armour = by_id.get(b.get("edID"))
                if armour is not None and b.get("cost") is not None:
                    armour.attrs["cost"] = int(b["cost"])
    for path in sorted((cor / "modules").rglob("*.json")):
        for group in json.loads(path.read_text(encoding="utf-8")).values():
            for m in group if isinstance(group, list) else []:
                it = data.items.get(normalize_symbol(m.get("symbol"))) or by_id.get(m.get("edID"))
                if it is None or not m.get("cost"):   # 0 is unreliable (cargo racks, DSS are listed at 0)
                    missing += it is None and bool(m.get("cost"))
                    continue
                it.attrs["cost"] = int(m["cost"])
                priced += 1
    if not priced:
        data.errors.append("coriolis ships/ and modules/: no list prices read")
    elif missing:
        data.warnings.append(f"coriolis prices: {missing} ships or modules not in FDevIDs")


def _apply_recipe_fixes(data: RefData, rows: list[dict]) -> None:
    """Recipes coriolis-data has wrong. The rows for one recipe and grade are its whole recipe and replace coriolis's."""
    materials = {it.display_name.lower(): s for s, it in data.items.items() if it.kind == "material"}
    fixes: dict[tuple[str, str, int], dict[str, int]] = {}
    for n, row in enumerate(rows, start=2):
        kind, recipe, grade, qty = row["recipe_kind"].strip(), row["recipe"].strip(), _int(row["grade"]), _int(row["qty"])
        where = f"recipe_fixes.csv line {n} ({recipe} G{row['grade']})"
        if kind == "blueprint":
            known = (recipe, grade) in data.blueprints
        elif kind == "experimental":
            known = grade == 0 and recipe in data.experimentals
        else:
            data.errors.append(f"{where}: recipe_kind must be blueprint or experimental")
            continue
        symbol = materials.get(row["material"].strip().lower())
        if not known:
            data.errors.append(f"{where}: no such {kind} (experimentals take grade 0)")
        elif symbol is None or not qty:
            data.errors.append(f"{where}: material {row['material']!r} unknown or qty missing")
        else:
            fixes.setdefault((kind, recipe, grade), {})[symbol] = qty
    for key, recipe in fixes.items():
        current = {k[3]: q for k, q in data.recipe_costs.items() if k[:3] == key}
        if current == recipe:
            data.redundant_fixes.append(f"{key[1]} G{key[2]}")
            continue
        for k in [k for k in data.recipe_costs if k[:3] == key]:
            del data.recipe_costs[k]
        for symbol, qty in recipe.items():
            data.recipe_costs[(*key, symbol)] = qty


def _apply_extra_blueprints(data: RefData, recipes: list[dict], offers: list[dict], labelled: set[str]) -> None:
    """Blueprints and engineer offers coriolis-data doesn't have yet (newer Merc-module and Guardian blueprints)."""
    materials = {it.display_name.lower(): s for s, it in data.items.items() if it.kind == "material"}
    known: dict[str, set[int]] = {}
    for row in recipes:
        bp, grade = row["blueprint"].strip(), _int(row["grade"])
        where = f"blueprints_extra.csv: {bp} G{row['grade']}"
        if not bp or grade is None or not 1 <= grade <= 5 or _int(row["qty"]) is None:
            data.errors.append(f"{where}: blueprint, grade (1-5) and qty are required")
            continue
        if any(b == bp for b, _g in data.blueprints) and bp not in known:
            data.errors.append(f"{where}: coriolis already has this blueprint; drop the extra rows")
            continue
        merc_only = "merc modules only" in row["note"].lower()
        data.blueprint_names[bp] = row["display_name"].strip() + (" (Merc modules only)" if merc_only else "")
        known.setdefault(bp, set()).add(grade)
        data.blueprints.setdefault((bp, grade), {"module_types": set(), "engineers": set()})
        symbol = materials.get(row["material"].strip().lower())
        if symbol is None:
            data.warnings.append(f"{where}: material {row['material']!r} isn't in FDevIDs yet; that ingredient is left out")
            continue
        data.recipe_costs[("blueprint", bp, grade, symbol)] = _int(row["qty"])
    for row in offers:
        eid, code, bp, cap = _int(row["engineer_id"]), row["module_type"].strip(), row["blueprint"].strip(), _int(row["max_grade"])
        where = f"engineer_offers_extra.csv: {row['engineer_id']}/{code}/{bp}"
        if eid is None or str(eid) not in data.items or data.items[str(eid)].kind != "engineer":
            data.errors.append(f"{where}: not an engineer")
        elif code not in labelled:
            data.errors.append(f"{where}: module type {code!r} has no label in module_type_names.csv")
        elif bp not in known or cap is None or cap not in known[bp] and cap > max(known[bp]):
            data.errors.append(f"{where}: blueprint or grade not in blueprints_extra.csv")
        else:
            data.engineer_offers[(eid, code, bp)] = cap
            for g in known[bp]:
                if g <= cap:
                    data.blueprints[(bp, g)]["module_types"].add(code)
                    data.blueprints[(bp, g)]["engineers"].add(eid)


def _apply_offer_fixes(data: RefData, rows: list[dict]) -> None:
    """Cap (or, at 0, remove) an engineer's offers on one module type where coriolis disagrees with another source."""
    for row in rows:
        where = f"engineer_offer_fixes.csv: {row['engineer_id']}/{row['module_type']}"
        eid, code, cap = _int(row["engineer_id"]), row["module_type"].strip(), _int(row["max_grade"])
        if eid is None or cap is None or not 0 <= cap <= 5:
            data.errors.append(f"{where}: engineer_id and max_grade (0-5) must be whole numbers")
            continue
        keys = [k for k in data.engineer_offers if k[0] == eid and k[1] == code]
        if not keys:
            data.warnings.append(f"{where}: no such offer in coriolis any more; the fix can go")
            continue
        if cap and all(data.engineer_offers[k] <= cap for k in keys):
            data.warnings.append(f"{where}: coriolis already offers G{cap} or less; the fix can go")
        for k in keys:
            if cap:
                data.engineer_offers[k] = min(data.engineer_offers[k], cap)
            else:
                del data.engineer_offers[k]


def _check_engineering_plan(data: RefData, refdata_dir: Path) -> None:
    from app.core.engplan import parse_rolls

    path = refdata_dir / "templates" / "engineering_plan.json"
    if not path.is_file():
        data.errors.append("templates/engineering_plan.json: file missing")
        return
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        parse_rolls(",".join(str(n) for n in doc.get("rolls_per_grade", [])))
    except ValueError as exc:
        data.errors.append(f"templates/engineering_plan.json: {exc}")
        return
    keys = set()
    for e in doc.get("entries", []):
        where = f"templates/engineering_plan.json: {e.get('key')!r}"
        if e.get("key") in keys:
            data.errors.append(f"{where}: duplicate key")
        keys.add(e.get("key"))
        best = max((g for (_eid, m, b), g in data.engineer_offers.items()
                    if m == e.get("module_type") and b == e.get("blueprint")), default=0)
        if not best:
            data.errors.append(f"{where}: no engineer offers {e.get('module_type')}/{e.get('blueprint')}")
        elif not 1 <= int(e.get("target_grade", 0)) <= best:
            data.errors.append(f"{where}: target grade must be 1–{best}")
        if int(e.get("modules", 0)) < 1:
            data.errors.append(f"{where}: modules must be at least 1")
        exp = e.get("experimental")
        if exp and e.get("module_type") not in data.experimentals.get(exp, {}).get("module_types", set()):
            data.errors.append(f"{where}: experimental {exp!r} doesn't fit {e.get('module_type')!r}")


def _check_example_builds(data: RefData, refdata_dir: Path) -> None:
    from app.core.builds import parse

    blueprints = {bp for bp, _g in data.blueprints}
    for path in sorted((refdata_dir / "templates").glob("*.slef.json")):
        where = f"templates/{path.name}"
        try:
            lo = parse(path.read_text(encoding="utf-8"))["loadout"]
        except ValueError as exc:
            data.errors.append(f"{where}: {exc}")
            continue
        ship = normalize_symbol(lo["Ship"])
        if ship not in data.items or data.items[ship].kind != "ship":
            data.errors.append(f"{where}: unknown ship {lo['Ship']!r}")
        for m in lo["Modules"]:
            bp = normalize_symbol((m.get("Engineering") or {}).get("BlueprintName"))
            if bp and bp not in blueprints:
                data.errors.append(f"{where}: {m.get('Slot')}: unknown blueprint {bp!r}")
            exp = normalize_symbol((m.get("Engineering") or {}).get("ExperimentalEffect"))
            if exp and exp not in data.experimentals:
                data.errors.append(f"{where}: {m.get('Slot')}: unknown experimental {exp!r}")


def _apply_overrides(data: RefData, rows: list[dict]) -> None:
    for row in rows:
        symbol = normalize_symbol(row["symbol"])
        if not symbol:
            continue
        if symbol in data.items and data.items[symbol].source != "local_override":
            data.redundant_overrides.append(symbol)
            continue  # upstream now carries it; upstream wins on identity
        try:
            attrs = json.loads(row["attrs_json"]) if (row.get("attrs_json") or "").strip() else {}
        except json.JSONDecodeError:
            data.errors.append(f"local_overrides.csv: {symbol!r} has invalid attrs_json")
            attrs = {}
        data.items[symbol] = RefItem(
            symbol=symbol, kind=row["kind"].strip(), fdev_id=_int(row["fdev_id"]),
            display_name=row["display_name"].strip() or symbol, category=_text(row["category"]),
            family=_text(row["family"]), grade=_int(row["grade"]), attrs=attrs,
            source="local_override", source_commit="local",
        )


def _build_recipes(data: RefData, cor: Path, alias_rows: list[dict], commit: str) -> None:
    blueprints = _read_json(cor / "blueprints.json", CORIOLIS_FILES["blueprints.json"], data, "coriolis/blueprints.json")
    specials = _read_json(cor / "specials.json", CORIOLIS_FILES["specials.json"], data, "coriolis/specials.json")
    _read_json(cor / "modifications.json", CORIOLIS_FILES["modifications.json"], data, "coriolis/modifications.json")
    modules = _read_json(cor / "modules.json", CORIOLIS_FILES["modules.json"], data, "coriolis/modules.json")

    aliases: dict[tuple[str, str], str] = {}
    for row in alias_rows:
        key = (row["kind"].strip(), row["source_name"].strip().lower())
        target = row["maps_to"].strip().lower()
        aliases[key] = target
        if target not in data.items:
            data.errors.append(f"name_aliases.csv: {row['source_name']!r} maps to unknown symbol {target!r}")
    used_aliases: set[tuple[str, str]] = set()

    material_by_name = {it.display_name.strip().lower(): it.symbol
                        for it in data.items.values() if it.kind == "material"}
    engineer_by_name = {it.display_name.strip().lower(): it.symbol
                        for it in data.items.values() if it.kind == "engineer"}

    def resolve(kind: str, name: str, table: dict[str, str], context: str) -> str | None:
        key = name.strip().lower()
        if (kind, key) in aliases:
            used_aliases.add((kind, key))
            return aliases[(kind, key)]
        if key in table:
            return table[key]
        data.errors.append(f"{context}: {kind} {name!r} does not map to any FDevIDs {kind}")
        return None

    for fdname, bp in blueprints.items():
        recipe = normalize_symbol(bp["fdname"])
        data.blueprint_names[recipe] = (bp.get("name") or fdname).strip()
        for grade, g in bp["grades"].items():
            for name, qty in (g.get("components") or {}).items():
                symbol = resolve("material", name, material_by_name, f"blueprint {fdname} grade {grade}")
                if symbol:
                    data.recipe_costs[("blueprint", recipe, int(grade), symbol)] = int(qty)

    for edname, sp in specials.items():
        effect = normalize_symbol(sp["edname"])
        data.experimentals[effect] = {"display_name": sp["name"].strip(), "module_types": set()}
        for name, qty in (sp.get("components") or {}).items():
            symbol = resolve("material", name, material_by_name, f"experimental {edname}")
            if symbol:
                data.recipe_costs[("experimental", effect, 0, symbol)] = int(qty)

    for code, module in modules.items():
        for fdname, bp in (module.get("blueprints") or {}).items():
            # A few coriolis keys differ from the journal's name (MC_Overcharged is Weapon_Overcharged).
            recipe = normalize_symbol((blueprints.get(fdname) or {}).get("fdname") or fdname)
            if fdname not in blueprints:
                data.errors.append(f"coriolis/modules.json: {code} lists unknown blueprint {fdname!r}")
            for grade, g in bp.get("grades", {}).items():
                entry = data.blueprints.setdefault((recipe, int(grade)), {"module_types": set(), "engineers": set()})
                entry["module_types"].add(code)
                for name in g.get("engineers", []):
                    eid = resolve("engineer", name, engineer_by_name, f"blueprint {fdname} grade {grade}")
                    if eid:
                        entry["engineers"].add(int(eid))
                        key = (int(eid), code, recipe)
                        data.engineer_offers[key] = max(data.engineer_offers.get(key, 0), int(grade))
        for edname in module.get("specials") or []:
            effect = normalize_symbol(edname)
            if effect in data.experimentals:
                data.experimentals[effect]["module_types"].add(code)
            else:
                data.errors.append(f"coriolis/modules.json: {code} lists unknown experimental {edname!r}")

    for key in set(aliases) - used_aliases:
        data.warnings.append(f"name_aliases.csv: alias for {key[1]!r} ({key[0]}) is no longer needed")
    for (recipe, grade) in {(r, g) for (k, r, g, _m) in data.recipe_costs if k == "blueprint"}:
        if (recipe, grade) not in data.blueprints:
            data.warnings.append(f"blueprint {recipe} grade {grade} has a recipe but no module/engineer listing")


# -- serialisation for diffing -------------------------------------------------

def item_summary(data: RefData) -> dict[str, dict]:
    return {
        s: {"kind": it.kind, "display_name": it.display_name, "category": it.category,
            "family": it.family, "grade": it.grade}
        for s, it in data.items.items()
    }
