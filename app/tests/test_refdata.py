import json
import shutil
from pathlib import Path

import pytest

from app.core import db, paths
from app.core.refdata import manifest
from app.core.refdata.build import build
from app.core.refdata.diff import diff, has_changes, to_markdown
from app.core.refdata.loader import ensure_loaded


@pytest.fixture(scope="module")
def vendored():
    d = build(paths.refdata_dir(), manifest.commits(paths.refdata_dir()))
    return d


@pytest.fixture()
def refcopy(tmp_path) -> Path:
    dest = tmp_path / "refdata"
    shutil.copytree(paths.refdata_dir(), dest)
    return dest


def test_vendored_refdata_builds_clean(vendored):
    assert vendored.errors == []
    assert manifest.verify(paths.refdata_dir()) == []


def test_trailing_space_name_is_trimmed(vendored):
    # FDevIDs lists "Untypical Shield Scans " with a trailing space (DESIGN §6).
    item = vendored.items["shielddensityreports"]
    assert item.display_name == "Untypical Shield Scans"
    assert any(mat == "shielddensityreports" for (_k, _r, _g, mat) in vendored.recipe_costs)


def test_engineers_split_by_id_range(vendored):
    engineers = [i for i in vendored.items.values() if i.kind == "engineer"]
    assert len(engineers) == 38
    assert sum(1 for e in engineers if e.attrs["engineer_scope"] == "onfoot") == 13


def test_guardian_and_thargoid_families(vendored):
    assert vendored.items["guardian_powercell"].family == "Guardian"
    assert vendored.items["tg_weaponparts"].family == "Thargoid"
    assert vendored.items["iron"].attrs["trader_column"] == 4


def test_mercgear_variant_shares_symbol_with_base_module(vendored):
    cannon = vendored.items["hpt_cannon_fixed_huge"]
    assert cannon.display_name == "Cannon" and cannon.category == "hardpoint"
    assert {"fdev_id": 129046187, "display_name": "Force Impact Cannon"} in cannon.attrs["mercgear_variants"]


def test_module_type_parsed_from_symbol(vendored):
    fsd = vendored.items["int_hyperdrive_size5_class5"]
    assert fsd.attrs["module_type"] == "fsd" and fsd.attrs["size"] == 5 and fsd.attrs["rating"] == "A"


def test_blueprint_engineers_resolved(vendored):
    entry = vendored.blueprints[("fsd_longrange", 5)]
    assert "fsd" in entry["module_types"]
    assert 300100 in entry["engineers"]  # Felicity Farseer, via the coriolis typo alias


def test_unmapped_recipe_material_fails_build(refcopy):
    bp_path = refcopy / "coriolis" / "blueprints.json"
    bp = json.loads(bp_path.read_text(encoding="utf-8"))
    bp["FSD_LongRange"]["grades"]["1"]["components"]["Unobtainium"] = 1
    bp_path.write_text(json.dumps(bp), encoding="utf-8")
    d = build(refcopy)
    assert not d.ok
    assert any("Unobtainium" in e for e in d.errors)


def test_missing_alias_fails_build(refcopy):
    aliases = refcopy / "name_aliases.csv"
    lines = aliases.read_text(encoding="utf-8").splitlines()
    aliases.write_text("\n".join(l for l in lines if "Encyptors" not in l) + "\n", encoding="utf-8")
    d = build(refcopy)
    assert any("Adaptive Encyptors Capture" in e for e in d.errors)


def test_duplicate_symbol_fails_build(refcopy):
    with (refcopy / "fdevids" / "shipyard.csv").open("a", encoding="utf-8", newline="") as f:
        f.write("999,Anaconda ,Not An Anaconda,\n")
    d = build(refcopy)
    assert any("duplicate symbol 'anaconda'" in e for e in d.errors)


def test_changed_columns_fail_build(refcopy):
    path = refcopy / "fdevids" / "commodity.csv"
    text = path.read_text(encoding="utf-8").replace("id,symbol,category,name", "id,symbol,category,name,extra", 1)
    path.write_text(text, encoding="utf-8")
    assert any("columns changed" in e for e in build(refcopy).errors)


def test_material_without_family_fails_build(refcopy):
    path = refcopy / "families_extra.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(l for l in lines if not l.startswith("guardian_powercell")) + "\n", encoding="utf-8")
    assert any("guardian_powercell" in e for e in build(refcopy).errors)


def test_override_becomes_redundant_when_upstream_adds_symbol(refcopy):
    with (refcopy / "local_overrides.csv").open("a", encoding="utf-8", newline="") as f:
        f.write("anaconda,ship,,Anaconda Override,,,,,test\n")
    d = build(refcopy)
    assert "anaconda" in d.redundant_overrides
    assert d.items["anaconda"].display_name == "Anaconda"  # upstream wins


def test_taxi_override_applied(vendored):
    taxi = vendored.items["vulture_taxi"]
    assert taxi.source == "local_override" and taxi.kind == "ship"


def test_recipe_fixes_replace_coriolis_recipes(vendored):
    # Thermo Block is the shield generator's recipe, not the shield booster's that coriolis has.
    thermo = {m: q for (k, r, g, m), q in vendored.recipe_costs.items() if (k, r, g) == ("experimental", "special_shield_thermic", 0)}
    assert thermo == {"wornshieldemitters": 5, "uncutfocuscrystals": 3, "heatvanes": 1}
    blast = {m for (k, r, g, m) in vendored.recipe_costs if (k, r, g) == ("blueprint", "armour_explosive", 3)}
    assert blast == {"salvagedalloys", "vanadium", "zirconium"}
    assert vendored.redundant_fixes == []


def test_recipe_fix_reports_redundant_and_unknown(refcopy):
    with (refcopy / "recipe_fixes.csv").open("a", encoding="utf-8", newline="") as f:
        f.write("blueprint,fsd_longrange,1,Atypical Disrupted Wake Echoes,1,test,\n"
                "experimental,special_nonsense,0,Carbon,1,test,\n")
    d = build(refcopy)
    assert "fsd_longrange G1" in d.redundant_fixes        # coriolis already has exactly this
    assert any("special_nonsense" in e for e in d.errors)


def test_new_ship_and_thargoid_overrides(vendored):
    assert vendored.items["anacondamkii"].display_name == "Anaconda Mk II"
    assert vendored.items["anacondamkii_armour_reactive"].attrs["module_type"] == "bh"
    assert vendored.items["anaconda_armour_mirrored"].attrs["module_type"] == "bh"   # every armour takes blueprints
    assert vendored.items["tg_causticshard"].family == "Thargoid" and vendored.items["tg_causticshard"].grade == 3


def test_manifest_detects_tampering(refcopy):
    with (refcopy / "fdevids" / "material.csv").open("a", encoding="utf-8") as f:
        f.write("\n")
    assert any("material.csv: hash mismatch" in p for p in manifest.verify(refcopy))


def test_diff_reports_added_removed_renamed(refcopy, vendored):
    path = refcopy / "fdevids" / "shipyard.csv"
    text = path.read_text(encoding="utf-8").replace(",Anaconda,Anaconda,", ",Anaconda,Anaconda Mk II,", 1)
    text = text.replace("128049255,Eagle,Eagle,\n", "")
    path.write_text(text + "999999,NewShip,New Ship,\n", encoding="utf-8")
    d = diff(vendored, build(refcopy))
    assert ("anaconda", "Anaconda", "Anaconda Mk II") in d["renamed"]
    assert ("eagle", "ship", "Eagle") in d["removed"]
    assert ("newship", "ship", "New Ship") in d["added"]
    assert has_changes(d)
    md = to_markdown(d, build(refcopy), {"refdata_version": "test"})
    assert "### Renamed (1)" in md and "Validation passed." in md


def test_diff_of_identical_builds_is_empty(vendored):
    assert not has_changes(diff(vendored, vendored))


def test_loader_loads_once_and_skips_broken_data(tmp_path, refcopy):
    db_path = tmp_path / "planner.db"
    db.init_db(db_path)
    first = ensure_loaded(db_path, refcopy)
    assert first.loaded_now and first.error is None and first.items > 2000
    again = ensure_loaded(db_path, refcopy)
    assert not again.loaded_now and again.items == first.items

    # Corrupt the data and change the manifest fingerprint: the old tables stay.
    bp_path = refcopy / "coriolis" / "blueprints.json"
    bp = json.loads(bp_path.read_text(encoding="utf-8"))
    bp["FSD_LongRange"]["grades"]["1"]["components"]["Unobtainium"] = 1
    bp_path.write_text(json.dumps(bp), encoding="utf-8")
    m = manifest.read(refcopy)
    manifest.write(refcopy, m["refdata_version"], m["sources"])
    broken = ensure_loaded(db_path, refcopy)
    assert broken.error and "Unobtainium" in broken.error
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT display_name FROM ref_items WHERE symbol = 'anaconda'").fetchone()[0] == "Anaconda"
        assert conn.execute("SELECT COUNT(*) FROM ref_recipe_costs").fetchone()[0] > 1000
    finally:
        conn.close()
