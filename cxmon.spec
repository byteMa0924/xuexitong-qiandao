# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。

这是打包的唯一配置来源：打包脚本只执行 `pyinstaller cxmon.spec`，
不再用一长串命令行参数。之前命令行参数和 spec 两份配置各说各话，
改了一处忘了另一处就会出现"本地打包的和仓库里的不一致"。

几个刻意的选择：
  * console=False  给不懂命令行的用户用，不该弹黑窗口
                   （launcher.py 里补了 stdout/stderr，否则 print 会崩）
  * upx=False      UPX 压缩会明显提高杀毒软件误报率，不值得为省几 MB 冒险
  * version=       写入 exe 的版本/来源信息。未签名的 exe 本来就可疑，
                   没有来源信息会让启发式规则更容易盯上它
"""

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('app-icon.ico', '.'), ('cxmon/toast.ps1', '.'), ('cxmon/voice_worker.ps1', '.')],
    hiddenimports=['tkinter', 'tkinter.ttk', 'tkinter.messagebox'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='cxmon',
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
    icon=['app-icon.ico'],
    version='version_info.txt',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='cxmon',
)
