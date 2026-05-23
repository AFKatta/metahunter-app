# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for Metahunter.
#
# Builds a one-folder Windows distribution at dist/Metahunter/ with a
# Metahunter.exe entry point. Bundles:
#   * The Python entry script  (scripts/serve.py)
#   * The src/ tree            (added to sys.path)
#   * The built React frontend (web/dist/  ->  web/dist/  inside bundle)
#   * The archetype corpus     (data/corpus/legacy.json)
#   * Badaro's MTGOFormatData  (data/MTGOFormatData/)
#
# Run:    pyinstaller metahunter.spec
# Output: dist/Metahunter/Metahunter.exe

from pathlib import Path

block_cipher = None

ROOT = Path(SPECPATH).resolve()

datas = []

# React build output. Without this the .exe has no UI to serve.
web_dist = ROOT / "web" / "dist"
if web_dist.exists():
    datas.append((str(web_dist), "web/dist"))

# Archetype corpus snapshot (read-only inside the bundle; the user's
# updated copy lives in %LOCALAPPDATA%/Metahunter/corpus/ if they
# refresh it).
corpus = ROOT / "data" / "corpus" / "legacy.json"
if corpus.exists():
    datas.append((str(corpus), "data/corpus"))

# Badaro archetype rules.
fmt_data = ROOT / "data" / "MTGOFormatData"
if fmt_data.exists():
    datas.append((str(fmt_data), "data/MTGOFormatData"))

# Hidden imports — modules PyInstaller's static analysis misses because
# they're loaded dynamically by frameworks.
hiddenimports = [
    # uvicorn picks its event loop / HTTP protocol at runtime.
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.logging",
    # watchdog Windows backend.
    "watchdog.observers.read_directory_changes",
    "watchdog.observers.winapi",
]


a = Analysis(
    ["scripts/serve.py"],
    # The metahunter_core package is vendored at
    # packages/metahunter-core/src/. It's a separate package from
    # mtgo_meta and lives outside the default src/ path, so we add
    # its src/ directory explicitly here.
    pathex=[
        str(ROOT),
        str(ROOT / "src"),
        str(ROOT / "packages" / "metahunter-core" / "src"),
    ],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Test / dev deps we don't ship.
        "pytest",
        "httpx",          # only used by fastapi.testclient
        "IPython",
        "jupyter",
        "notebook",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Metahunter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,         # console window shows status to non-tech users
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Metahunter",
)
