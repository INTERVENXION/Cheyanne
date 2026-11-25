# -*- mode: python ; coding: utf-8 -*-

import os

# ============================================================
# conda 的 OpenSSL DLL —— 必须显式声明，不能靠自动探测
#
# 出过的事故：打出来的 exe 里带了 _ssl.pyd，却没有它依赖的
# libssl-3-x64.dll / libcrypto-3-x64.dll。运行到任何 https 请求时：
#   ImportError: Can't connect to HTTPS URL because the SSL module
#   is not available.
# 表现为对话报错、启动问候静默消失。
#
# 为什么自动探测会漏：
#   conda 的布局是 _ssl.pyd 在 <prefix>\DLLs\，而它依赖的 OpenSSL 库在
#   <prefix>\Library\bin\。PyInstaller 只会在 _ssl.pyd 同目录和 PATH 里
#   找依赖，找不到就**静默跳过**（打包照样成功，问题拖到运行时才暴露）。
#
# 所以这里直接把两个 DLL 钉死在 binaries 里，不再依赖 PATH 是否正确。
# 路径写死为 C:\Anaconda 是本机事实；换机器时改这一处即可。
# ============================================================
CONDA_LIB = r'C:\Anaconda\Library\bin'

_OPENSSL_DLLS = [
    'libssl-3-x64.dll',
    'libcrypto-3-x64.dll',
]

binaries = []
_missing = []
for _dll in _OPENSSL_DLLS:
    _src = os.path.join(CONDA_LIB, _dll)
    if os.path.isfile(_src):
        binaries.append((_src, '.'))
    else:
        _missing.append(_src)

# 缺少 OpenSSL 时报错退出，而不是打出一个"能启动但联网全废"的 exe。
# 上次就是靠"能启动"误判为成功，才让这个 bug 溜到用户手里。
if _missing:
    raise SystemExit(
        "缺少 OpenSSL 运行库，拒绝打包（否则 exe 会无法访问 https）：\n  "
        + "\n  ".join(_missing)
        + f"\n\n请确认 Anaconda 安装在 {os.path.dirname(CONDA_LIB)}"
    )

# 随 exe 一起分发的外部资源。
# 注意：配置和立绘**故意不打进 exe 内部**，而是放在 exe 同目录。
# config_manager.py / sprite_manager.py 在 frozen 模式下用
# os.path.dirname(sys.executable) 作为 BASE_DIR，就是为了让用户能直接
# 编辑 exe 旁边的 yaml 来改配置（热更新、改 prompt 无需重新打包）。
# 这里声明 datas 只是让 PyInstaller 在构建时把它们复制到 dist/，
# 避免重新打包后 dist 里的配置被清空或遗漏。
datas = [
    ('config.yaml', '.'),
    ('prompt.yaml', '.'),
    ('holidays.yaml', '.'),
    ('Image', 'Image'),
]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=[],
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
    a.binaries,
    a.datas,
    [],
    name='XiaAnPet',
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
