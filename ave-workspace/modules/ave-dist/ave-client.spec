# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


dist_root = Path(SPECPATH).resolve()
client_root = Path(os.environ["AVE_CLIENT_DIR"]).resolve()
env_file = Path(os.environ["AVE_ENV_FILE"]).resolve()
icon_file = Path(os.environ["AVE_ICON_FILE"]).resolve()
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("pystray")
    + ["app.main", "PIL._tkinter_finder"]
)

a = Analysis(
    [str(dist_root / "launcher.py")],
    pathex=[str(client_root)],
    binaries=[],
    datas=[(str(env_file), ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ave_client",
    icon=str(icon_file),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
