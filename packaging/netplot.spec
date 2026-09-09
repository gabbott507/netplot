# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: single-file netplot-agent.exe (Windows)
a = Analysis(
    ['netplot/__main__.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    excludes=['tkinter', 'test', 'email', 'html', 'http.server'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='netplot-agent',
    console=True,
)
