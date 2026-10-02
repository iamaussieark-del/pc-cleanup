# -*- mode: python ; coding: utf-8 -*-
# PyInstaller recipe for PC Cleanup.exe. Run build.bat rather than this directly.
# It makes one file, dist\PC Cleanup.exe, that runs on a PC without Python.
import os

from PyInstaller.utils.hooks import collect_data_files

here = SPECPATH
icon = os.path.join(here, "cleanup_app", "assets", "pc_cleanup.ico")

a = Analysis(
    [os.path.join(here, "main.py")],
    pathex=[here],
    # CustomTkinter's themes and fonts are data files, not code, so they are listed here.
    datas=collect_data_files("customtkinter") + [(icon, os.path.join("cleanup_app", "assets"))],
    hiddenimports=[],
    excludes=["pytest", "unittest", "pydoc_data", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PC Cleanup",
    icon=icon,
    version=os.path.join(here, "version_info.txt"),
    console=False,
    # UPX compression makes antivirus tools far more likely to flag the exe.
    upx=False,
    debug=False,
    strip=False,
)
