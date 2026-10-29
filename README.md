# ED Commander Planner (EDCP)

A local desktop app for planning your Elite Dangerous progression. It reads
the game's journal files, keeps a long-term history of your commander, and
shows it in your browser from your own PC: ranks and reputation, ships and
their engineering, materials, engineers, goals with funding, activities,
notes and backups.

**Status:** pre-1.0. It works, but it hasn't been announced and things may
change between versions.

## Privacy

- Everything stays on your PC. The app serves its pages on `127.0.0.1` only.
- Your journals are read, never written.
- No accounts and no telemetry. The only network call is a reference data
  update check, and only when you ask for one.
- Your data lives in the app's own `data\` folder: the database, logs,
  notes and `.edplanner` backups.

## Run it

**Packaged:** download the zip from [Releases](../../releases), unzip it
anywhere and double-click `ED Commander Planner.exe`. A tray icon appears and
the planner opens in your browser. Closing the tab doesn't stop it; use the
tray's **Exit**. The exe is unsigned, so on first launch Windows SmartScreen
may ask: **More info → Run anyway**.

To update, replace the app folder but keep its `data\` folder.

**From source** (Windows, Python 3.14):

```
py -3.14 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m app.main
```

- Tests: `.venv\Scripts\python -m pytest`
- Build the exe: `.venv\Scripts\python tools\build_exe.py`. It keeps the exe's
  `data\` folder; plain `pyinstaller` would delete it.

Journals are read from `Saved Games\Frontier Developments\Elite Dangerous`.

## Layout

| Folder | What goes here |
|---|---|
| `app/` | The application: entry point, tray, server, core services, tests |
| `refdata/` | Reference data: vendored EDCD files, our rule and fix CSVs, `manifest.json`, starter templates |
| `tools/` | `refdata_build.py` (validate, diff, package, journal coverage) and `build_exe.py` |
| `packaging/` | PyInstaller spec |

## Licence

MIT. See [LICENSE](LICENSE). Game data and bundled libraries keep their own
terms; see [THIRD_PARTY.md](THIRD_PARTY.md). ED Commander Planner is an
unofficial fan project, not affiliated with Frontier Developments.
