# Third-party material

The MIT licence in `LICENSE` covers ED Commander Planner's own code. It does
not cover the material below, which keeps its owners' terms.

## Elite Dangerous

ED Commander Planner is an unofficial fan project. It is not affiliated with,
endorsed by or sponsored by Frontier Developments plc. Elite Dangerous and
related names, logos and game data are trademarks and intellectual property
of Frontier Developments plc.

## Reference data in `refdata/`

- **`refdata/fdevids/`**: from [EDCD/FDevIDs](https://github.com/EDCD/FDevIDs),
  a community list of Frontier's IDs and names. It has no licence file. The
  data itself is Frontier's.
- **`refdata/coriolis/`**: from [EDCD/coriolis-data](https://github.com/EDCD/coriolis-data).
  Its data and JSON files are Frontier Developments' intellectual property,
  subject to [Frontier's terms](https://www.frontierstore.net/terms-and-conditions/).
  Only Coriolis's own code is MIT-licensed.
- **Our own CSVs** (the other files in `refdata/`) are covered by `LICENSE`.
  Some recipe, material and engineer facts in them were checked against or
  taken from these community sources:
  - [Inara](https://inara.cz/)
  - the [Elite Dangerous Wiki](https://elite-dangerous.fandom.com/)
  - [EDDiscovery's EliteDangerousCore](https://github.com/EDDiscovery/EliteDangerousCore) (Apache-2.0)
  - [EDEngineer](https://github.com/msarilar/EDEngineer) (MIT)

  No code was copied from any of them.

## Libraries bundled in the exe

| Library | Licence |
|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | MIT |
| [Starlette](https://github.com/encode/starlette) | BSD-3-Clause |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause |
| [Jinja2](https://github.com/pallets/jinja) | BSD-3-Clause |
| [Pillow](https://github.com/python-pillow/Pillow) | MIT-CMU |
| [pystray](https://github.com/moses-palmer/pystray) | LGPL-3.0 |
| [Python](https://www.python.org/) runtime | PSF License |

pystray is used unmodified under the LGPL-3.0, and its source is at the link
above. This repository holds the app's full source and the build script
(`tools/build_exe.py`), so the app can be run from source or rebuilt with
any compatible pystray.
