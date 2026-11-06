# -*- mode: python ; coding: utf-8 -*-
block_cipher = None
excludes = [
    "numpy", "pandas", "scipy", "PIL", "cv2", "matplotlib",
    "sklearn", "torch", "tensorflow", "IPython", "jupyter",
    "pytest", "setuptools", "pkg_resources", "distutils",
    "pydoc", "doctest", "unittest", "xmlrpc",
    "multiprocessing.popen_spawn_posix", "curses", "dbm", "sqlite3",
]
a = Analysis(
    ["swap_gui.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=["swap_core"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
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
    name="HikIpSwap",
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
)
