# -*- mode: python ; coding: utf-8 -*-

# ============================================================
# 夏安桌宠 — 快捷启动器（带图标的小 exe）
#
# 为什么单独做一个启动器，而不是给主程序换图标：
#   - 主程序 64 MB，换个图标就要重打一次，不划算
#   - 启动器只有几百 KB，图标独立，两者互不影响
#   - 启动器双击后拉起桌宠就退出，不驻留内存
#
# 图标来源：Image/idle.png（先由 tools/make_icon.py 转成 app.ico）
#
# 用法：
#     python tools/make_icon.py
#     python -m PyInstaller launcher.spec --noconfirm --distpath launcher_out
#
# 注意 --distpath：默认会输出到 dist/，和桌宠主程序混在一起。
# 用 launcher_out/ 单独存放，产物再拷到项目根目录使用。
# ============================================================

import os

ICON = 'app.ico'
if not os.path.isfile(ICON):
    raise SystemExit(
        "缺少图标文件 app.ico。\n"
        "请先运行：python tools/make_icon.py\n"
        "它会用 Image/idle.png 生成 app.ico。"
    )

# 启动器自身的 exe 名（同时用于避免递归启动自己）
LAUNCHER_NAME = '夏安桌宠'

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 启动器只用标准库，把重依赖排除掉，体积能小一个量级
        'PyQt5', 'PyQt5.QtCore', 'PyQt5.QtGui', 'PyQt5.QtWidgets',
        'requests', 'yaml', 'PIL', 'numpy', 'pandas',
    ],
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
    name=LAUNCHER_NAME,
    icon=ICON,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # windowed：双击不弹黑框
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
