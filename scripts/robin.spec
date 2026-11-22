# -*- mode: python ; coding: utf-8 -*-
# Robin: única configuração de build (PyInstaller 6, --onedir, sem console). Use o scripts\build.bat.
#
# Layout gerado em dist\Robin\ (na raiz do projeto):
#   Robin.exe
#   _internal\ui\                 interface (RESOURCE_DIR = sys._MEIPASS)
#   _internal\memory.example.md   semente da memória
# Os dados (memory.md, .env) ficam em %APPDATA%\Robin, nunca no dist.

import os

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.dirname(SPECPATH)  # o spec fica em scripts\

datas = [
    (os.path.join(ROOT, "ui"), "ui"),
    (os.path.join(ROOT, "memory.example.md"), "."),
]
# O hook do pywebview só coleta webview/lib; os scripts injetados na página ficam em webview/js.
datas += collect_data_files("webview", subdir="js")

hiddenimports = [
    # O pywebview escolhe a plataforma em tempo de execução (import dinâmico).
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
]

a = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Robin",
    icon=os.path.join(ROOT, "ui", "assets", "brand", "robin.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX costuma corromper DLLs .NET/WebView2
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Robin",
)
