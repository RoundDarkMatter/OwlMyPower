# -*- mode: python ; coding: utf-8 -*-
#
# Windows Vista / 7 build of OwlMyPower (Legacy edition).
#
# Build with a 32-bit Python 3.8 interpreter (3.8 is the last CPython that runs on
# Windows 7; it is also the most compatible choice for Vista SP2 in practice).
# A 32-bit exe runs on both 32- and 64-bit Windows. sv-ttk / darkdetect are
# intentionally NOT bundled: the Vista/7 path uses the native Aero theme instead.
#
#   py -3.8-32 -m pip install pyinstaller pystray pillow
#   py -3.8-32 -m PyInstaller OwlMyPower_1.0_Legacy.spec --noconfirm --clean

datas = [('owl_logo.png', '.'), ('owl_logo.ico', '.')]

a = Analysis(
    ['power_plan_auto_switcher.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['sv_ttk', 'darkdetect'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='OwlMyPower_1.0_Legacy',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['owl_logo.ico'],
)
