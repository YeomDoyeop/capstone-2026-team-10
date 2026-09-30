# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

dist_root = Path(SPECPATH).resolve()
icon_file = Path(os.environ["AVE_UPDATER_ICON_FILE"]).resolve()

a = Analysis(
    [str(dist_root / "updater.py")], pathex=[str(dist_root)], binaries=[],
    datas=[], hiddenimports=[],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="ave_updater", icon=str(icon_file), debug=False, console=False,
    bootloader_ignore_signals=False, strip=False, upx=False,
    disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None,
)
