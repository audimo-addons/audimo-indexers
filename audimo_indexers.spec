# PyInstaller spec for the audimo-indexers addon.
#
# Build with:
#   pyinstaller audimo_indexers.spec --clean --noconfirm
#
# This addon is public-host-safe — it has no libtorrent dependency.
# Build prerequisites are just `.venv/` populated from requirements.txt
# (fastapi, uvicorn, httpx).
#
# Output: dist/audimo-indexers (single-file binary).
#
# `clients/` and `indexers/` each ship 6+ submodules (one per debrid /
# tracker). They're imported via static `from clients import …` lines
# in server.py + the package __init__s, but PyInstaller's tree-shaker
# has been observed to drop one or two on certain CI runners — this
# breaks every search/resolve from the affected backend at runtime
# instead of at build time. `collect_submodules` is the cheap safety
# net: bundle every module in the package, accept the +~200 KB tax.

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None


a = Analysis(
    ['run.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=(
        [
            'server',
            'bep15',
            'cache_db',
            'uvicorn.lifespan.on',
            'uvicorn.lifespan.off',
            'uvicorn.loops.auto',
            'uvicorn.loops.asyncio',
            'uvicorn.loops.uvloop',
            'uvicorn.protocols.http.auto',
            'uvicorn.protocols.http.h11_impl',
            'uvicorn.protocols.http.httptools_impl',
            'uvicorn.protocols.websockets.auto',
            'uvicorn.protocols.websockets.websockets_impl',
            'uvicorn.protocols.websockets.wsproto_impl',
        ]
        + collect_submodules('clients')
        + collect_submodules('indexers')
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='audimo-indexers',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
