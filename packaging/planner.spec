# PyInstaller spec: onedir build of "ED Commander Planner.exe" (DESIGN §15).
# Build with tools\build_exe.py, not pyinstaller directly: PyInstaller wipes dist\ED Commander Planner\ including data\.
# Output: dist\ED Commander Planner\  (zip that folder to distribute).

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
sys.path.insert(0, str(ROOT))

from app.tray import make_icon_image  # noqa: E402

build_dir = ROOT / "build"
build_dir.mkdir(exist_ok=True)
icon_path = build_dir / "planner.ico"
make_icon_image(256).save(icon_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT)],
    datas=[
        (str(ROOT / "app" / "server" / "templates"), "app/server/templates"),
        (str(ROOT / "app" / "server" / "static"), "app/server/static"),
        (str(ROOT / "app" / "core" / "db" / "schema.sql"), "app/core/db"),
        (str(ROOT / "app" / "core" / "db" / "migrations"), "app/core/db/migrations"),
        (str(ROOT / "refdata"), "refdata"),
    ],
    hiddenimports=["pystray._win32"],
    excludes=["pytest", "app.tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ED Commander Planner",
    console=False,
    icon=str(icon_path),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ED Commander Planner", upx=False)
