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

import sys
from pathlib import Path

block_cipher = None

ROOT = Path(SPECPATH).resolve()

# Pull the build version out of src/mtgo_meta/__init__.py — single
# source of truth shared by the runtime updater + installer.iss +
# build.ps1.
sys.path.insert(0, str(ROOT / "src"))
from mtgo_meta import __version__ as VERSION  # noqa: E402

# VS_VERSIONINFO struct — Windows reads this for Properties → Details.
def _vs_version_tuple(s: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in s.split(".") if p.isdigit()]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])  # type: ignore[return-value]

VERSION_FILE_TUPLE = _vs_version_tuple(VERSION)

from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
    StringStruct, VarFileInfo, VarStruct,
)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=VERSION_FILE_TUPLE,
        prodvers=VERSION_FILE_TUPLE,
        mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("CompanyName", "AFKatta"),
                StringStruct("FileDescription", "Metahunter — MTGO Legacy match analyser"),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", "Metahunter"),
                StringStruct("LegalCopyright", "© AFKatta"),
                StringStruct("OriginalFilename", "Metahunter.exe"),
                StringStruct("ProductName", "Metahunter"),
                StringStruct("ProductVersion", VERSION),
            ]),
        ]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

datas = []

# React build output. Without this the .exe has no UI to serve.
web_dist = ROOT / "web" / "dist"
if web_dist.exists():
    datas.append((str(web_dist), "web/dist"))

# Archetype corpus snapshot (read-only inside the bundle; the user's
# updated copy lives in %LOCALAPPDATA%/Metahunter/corpus/ if they
# refresh it).
# Ship every corpus we have, not just Legacy. The classifier picks the
# one matching the match's own format at request time, and a format
# whose corpus is absent silently degrades to colour-code labels — so
# naming a single file here quietly capped the app at one format.
corpus_dir = ROOT / "data" / "corpus"
if corpus_dir.is_dir():
    for corpus_file in sorted(corpus_dir.glob("*.json")):
        datas.append((str(corpus_file), "data/corpus"))

# Badaro archetype rules.
fmt_data = ROOT / "data" / "MTGOFormatData"
if fmt_data.exists():
    datas.append((str(fmt_data), "data/MTGOFormatData"))

# Hidden imports — modules PyInstaller's static analysis misses because
# they're loaded dynamically by frameworks.
hiddenimports = [
    # Carried explicitly: net.py imports it dynamically, so
    # PyInstaller cannot see the dependency by analysis alone.
    "certifi",
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
    # metahunter_core lives in a sibling repo cloned next to this
    # one. Build expects the layout:
    #
    #   C:\Code\metahunter-core\        (the sibling clone)
    #   C:\Code\metahunter-app\         (this repo, ROOT)
    #
    # The build.ps1 script verifies the sibling is present before
    # invoking PyInstaller.
    pathex=[
        str(ROOT),
        str(ROOT / "src"),
        str(ROOT.parent / "metahunter-core" / "src"),
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
    # Windows Properties → Details metadata.
    version=version_info,
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
