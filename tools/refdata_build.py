"""Reference data pipeline: fetch → normalize → validate → diff report (DESIGN §7.5).

Stdlib only. Run from the repository root:

  python tools/refdata_build.py update [--report FILE]   fetch latest upstream, rebuild, write report
                                       [--fdevids SHA] [--coriolis SHA]   pin a source instead
  python tools/refdata_build.py check                    validate refdata/ as committed
  python tools/refdata_build.py rehash                   re-record hashes after editing our CSVs
  python tools/refdata_build.py package --out DIR        zip refdata/ for a GitHub Release
  python tools/refdata_build.py coverage --journals DIR  journal symbols missing from ref data

Exit codes: 0 ok, 1 validation failed, 2 usage or network error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.normalize import normalize_symbol  # noqa: E402
from app.core.refdata import manifest  # noqa: E402
from app.core.refdata.build import CORIOLIS_DIRS, CORIOLIS_FILES, FDEVIDS_FILES, RefData, build  # noqa: E402
from app.core.refdata.diff import diff, has_changes, to_markdown  # noqa: E402

REFDATA = ROOT / "refdata"
UPSTREAM = {
    "fdevids": ("EDCD/FDevIDs", "master", "", list(FDEVIDS_FILES)),
    "coriolis": ("EDCD/coriolis-data", "master", "modifications/", list(CORIOLIS_FILES)),
}
# Whole upstream folders (list prices), fetched at the same commit and kept at their upstream paths.
UPSTREAM_DIRS = {"coriolis": CORIOLIS_DIRS}
USER_AGENT = "ed-commander-planner-refdata-build"


def _get(url: str) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        return resp.read()


def latest_commit(repo: str, branch: str) -> str:
    return json.loads(_get(f"https://api.github.com/repos/{repo}/commits/{branch}"))["sha"]


def fetch(dest: Path, pins: dict[str, str] | None = None) -> dict[str, dict]:
    """Download every upstream file at one commit per source into dest/<source>/."""
    pins = pins or {}
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sources = {}
    for name, (repo, branch, prefix, files) in UPSTREAM.items():
        sha = pins.get(name) or latest_commit(repo, branch)
        (dest / name).mkdir(parents=True, exist_ok=True)
        for fn in files:
            raw = _get(f"https://raw.githubusercontent.com/{repo}/{sha}/{prefix}{fn}")
            (dest / name / fn).write_bytes(raw)
        for folder in UPSTREAM_DIRS.get(name, []):
            listing = json.loads(_get(f"https://api.github.com/repos/{repo}/contents/{folder}?ref={sha}"))
            (dest / name / folder).mkdir(parents=True, exist_ok=True)
            for entry in listing:
                if entry["type"] == "file" and entry["name"].endswith(".json"):
                    raw = _get(f"https://raw.githubusercontent.com/{repo}/{sha}/{folder}/{entry['name']}")
                    (dest / name / folder / entry["name"]).write_bytes(raw)
        sources[name] = {"repo": repo, "commit": sha, "fetched": fetched_at}
    return sources


def build_dir(refdata_dir: Path, commits: dict[str, str] | None = None) -> RefData:
    return build(refdata_dir, commits if commits is not None else manifest.commits(refdata_dir))


def cmd_update(args) -> int:
    old = build_dir(REFDATA) if (REFDATA / "fdevids").is_dir() else None
    old_manifest = manifest.read(REFDATA) or {}
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        pins = {k: v for k, v in (("fdevids", args.fdevids), ("coriolis", args.coriolis)) if v}
        try:
            sources = fetch(staging, pins)
        except OSError as exc:
            print(f"fetch failed: {exc}", file=sys.stderr)
            return 2
        for name in UPSTREAM:
            target = REFDATA / name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(staging / name, target)

    old_commits = {k: v.get("commit") for k, v in old_manifest.get("sources", {}).items()}
    new_commits = {k: v["commit"] for k, v in sources.items()}
    new = build_dir(REFDATA, new_commits)
    d = diff(old, new)
    if old_commits == new_commits and old_manifest:
        sources = old_manifest["sources"]  # keep the original fetch time when nothing moved
    version = old_manifest.get("refdata_version") if not has_changes(d) and old_manifest else \
        datetime.now(timezone.utc).strftime("%Y.%m.%d")
    manifest.write(REFDATA, version, sources)

    header = {
        "refdata_version": version,
        **{f"{k} commit": f"`{old_commits.get(k, 'none')[:10]}` → `{v[:10]}`" for k, v in new_commits.items()},
        "items": len(new.items),
        "recipe cost rows": len(new.recipe_costs),
    }
    report = to_markdown(d, new, header)
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    print(report)
    return 1 if new.errors else 0


def cmd_check(args) -> int:
    problems = manifest.verify(REFDATA)
    data = build_dir(REFDATA)
    for p in problems:
        print(f"manifest: {p}")
    for e in data.errors:
        print(f"error: {e}")
    for w in data.warnings:
        print(f"warning: {w}")
    for s in data.redundant_overrides:
        print(f"redundant override: {s}")
    for s in data.redundant_fixes:
        print(f"redundant recipe fix: {s}")
    ok = not problems and data.ok
    print(f"{'OK' if ok else 'FAILED'}: {len(data.items)} items, {len(data.recipe_costs)} recipe cost rows, "
          f"{len(data.blueprints)} blueprint grades, {len(data.experimentals)} experimentals")
    return 0 if ok else 1


def cmd_rehash(args) -> int:
    """Re-record file hashes after editing our own CSVs; upstream pins stay as they are."""
    m = manifest.read(REFDATA)
    if m is None:
        print("no manifest.json; run `update` first", file=sys.stderr)
        return 2
    version = datetime.now(timezone.utc).strftime("%Y.%m.%d")
    manifest.write(REFDATA, version, m["sources"])
    return cmd_check(args)


def cmd_package(args) -> int:
    if cmd_check(args) != 0:
        return 1
    m = manifest.read(REFDATA)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = m["refdata_version"].replace(".", "")
    zpath = out / f"refdata-{stamp}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(REFDATA.rglob("*")):
            if path.is_file():
                z.write(path, path.relative_to(REFDATA).as_posix())
    digest = hashlib.sha256(zpath.read_bytes()).hexdigest()
    (out / f"{zpath.name}.sha256").write_text(f"{digest}  {zpath.name}\n", encoding="utf-8")
    print(zpath)
    return 0


# Journal fields that carry reference symbols, and the kind each should resolve to.
_COVERAGE_FIELDS = {
    ("LoadGame", "Ship"): "ship",
    ("Loadout", "Ship"): "ship",
    ("ShipyardBuy", "ShipType"): "ship",
    ("ShipyardSwap", "ShipType"): "ship",
    ("ModuleBuy", "BuyItem"): "module",
    ("ModuleStore", "StoredItem"): "module",
    ("ModuleRetrieve", "RetrievedItem"): "module",
    ("MarketBuy", "Type"): "commodity",
    ("MarketSell", "Type"): "commodity",
    ("MaterialCollected", "Name"): "material",
}


def cmd_coverage(args) -> int:
    from app.core.journal.files import discover, iter_lines

    data = build_dir(REFDATA)
    cosmetic = [line.split(",", 1)[0] for line in (REFDATA / "cosmetic_slots.csv").read_text(encoding="utf-8").splitlines()[1:] if line]
    import re
    cosmetic_rx = [re.compile(p) for p in cosmetic]
    seen: Counter = Counter()
    missing: Counter = Counter()

    def check(symbol, kind):
        s = normalize_symbol(symbol)
        if not s:
            return
        seen[kind] += 1
        it = data.items.get(s)
        if it is None or (kind in ("commodity",) and it.kind not in ("commodity", "rare_commodity")):
            missing[(kind, s)] += 1

    for jf in discover(Path(args.journals)):
        for raw in iter_lines(jf.path):
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ev = e.get("event")
            for (event, fieldname), kind in _COVERAGE_FIELDS.items():
                if ev == event and e.get(fieldname):
                    check(e[fieldname], kind)
            if ev == "Loadout":
                for m in e.get("Modules", []):
                    if any(rx.search(m.get("Slot", "")) for rx in cosmetic_rx):
                        continue
                    check(m.get("Item"), "module")
            if ev == "Materials":
                for group in ("Raw", "Manufactured", "Encoded"):
                    for m in e.get(group, []):
                        check(m.get("Name"), "material")
            if ev in ("ShipLocker", "Backpack"):
                for group in ("Items", "Components", "Consumables", "Data"):
                    for m in e.get(group, []) or []:
                        check(m.get("Name"), "microresource")
            if ev == "EngineerProgress":
                for eng in e.get("Engineers", []) or []:
                    if str(eng.get("EngineerID")) not in data.items:
                        missing[("engineer", str(eng.get("EngineerID")))] += 1
                    seen["engineer"] += 1

    print("symbols checked:", dict(seen))
    if not missing:
        print("All journal symbols resolve.")
        return 0
    print(f"{len(missing)} unresolved symbols:")
    for (kind, s), n in missing.most_common():
        print(f"  {kind:14} {s}  ×{n}")
    return 1


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # reports use →; Windows consoles default to cp1252
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update")
    u.add_argument("--report", help="write the Markdown diff report here")
    u.add_argument("--fdevids", help="pin EDCD/FDevIDs to this commit instead of the latest")
    u.add_argument("--coriolis", help="pin EDCD/coriolis-data to this commit instead of the latest")
    sub.add_parser("check")
    sub.add_parser("rehash")
    pk = sub.add_parser("package")
    pk.add_argument("--out", required=True)
    cv = sub.add_parser("coverage")
    cv.add_argument("--journals", required=True)
    args = p.parse_args(argv)
    commands = {"update": cmd_update, "check": cmd_check, "rehash": cmd_rehash,
                "package": cmd_package, "coverage": cmd_coverage}
    return commands[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
