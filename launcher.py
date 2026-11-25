# -*- coding: utf-8 -*-
"""夏安桌宠 — 快捷启动器

## 为什么需要它

`dist\\XiaAnPet.exe` 是 PyInstaller 打的包，但它没有自定义图标。
这个启动器是一个**很小的带图标 exe**，双击后找到真正的桌宠程序并拉起，
然后自己立刻退出（不驻留、不占内存）。

## 它会找哪些位置

按顺序查找，找到第一个就启动：

    <启动器所在目录>\\dist\\XiaAnPet.exe      源码目录的布局
    <启动器所在目录>\\XiaAnPet.exe            已打包/发布目录的布局

所以你既可以把它放在项目根目录（`D:\\M200\\`），
也可以把它和 `XiaAnPet.exe` 一起拷贝到发布包目录里。

## 为什么不直接给 XiaAnPet.exe 换图标

也可以（改 `XiaAnPet.spec` 里的 `icon=`），但那样每次重新打包都要连带
重建主程序，而且主程序有 64 MB —— 换个图标就重打一次不划算。
启动器只有几百 KB，图标独立，互不影响。

## 找不到目标时

弹一个系统提示框说明往哪放，而不是静默失败。
"""
import os
import subprocess
import sys

# 候选目标（相对启动器所在目录）
CANDIDATES = (
    os.path.join("dist", "XiaAnPet.exe"),
    "XiaAnPet.exe",
)

# 启动器自身文件名，避免递归启动自己
SELF_NAME = os.path.basename(sys.executable).lower()


def app_dir():
    """启动器所在目录

    打包成 exe 后用 sys.executable 的目录；
    直接跑 .py 时用脚本所在目录。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_target(base):
    """在 base 下按候选列表找桌宠主程序"""
    for rel in CANDIDATES:
        path = os.path.join(base, rel)
        if os.path.isfile(path) and os.path.basename(path).lower() != SELF_NAME:
            return path
    return None


def show_error(message):
    """弹窗提示（打包后没有控制台，只能靠弹窗）"""
    print(message)
    try:
        import ctypes
        # 0x10 = MB_ICONERROR
        ctypes.windll.user32.MessageBoxW(None, message, "夏安桌宠 — 启动失败", 0x10)
    except Exception:
        pass


def main():
    base = app_dir()
    target = find_target(base)

    if target is None:
        show_error(
            "没有找到桌宠主程序。\n\n"
            f"查找位置：{base}\n"
            "期望存在以下之一：\n"
            "  dist\\XiaAnPet.exe\n"
            "  XiaAnPet.exe\n\n"
            "请把本启动器放在项目根目录，或与 XiaAnPet.exe 放在同一目录。"
        )
        return 1

    try:
        # DETACHED_PROCESS：让桌宠独立于启动器运行。
        # 否则启动器退出时可能连带影响子进程。
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            [target],
            cwd=os.path.dirname(target),
            creationflags=DETACHED_PROCESS,
            close_fds=True,
        )
    except OSError as e:
        show_error(f"启动失败：{e}\n\n目标：{target}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
