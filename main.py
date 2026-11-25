"""虚拟桌宠「夏安」— 主入口

启动方式：
    python main.py

依赖：PyQt5, requests, PyYAML, Pillow
"""
import sys
import os


# ============================================================
# 让 stdout / stderr 永不抛异常
#
# 打包成窗口版 exe 后没有控制台，Python 的输流使用系统默认编码
# （中文 Windows 上是 GBK）。此时打印 GBK 里没有的字符会抛
# UnicodeEncodeError —— 而 print 出现在正常业务路径上，
# 一次日志输出就能把整段流程打断。
#
# 真实踩过的坑：
#   memory_manager 里 print("[Memory] ✓ 后台摘要完成") 抛异常
#   → process_overflow 在"推进摘要进度"之前中断
#   → 覆盖进度永远停在 0 → 每次启动都从第 0 条重新摘要
#   → 后台线程无限重试（实测 8 个线程同时跑同一批）
#   → 界面上毫无提示，用户只觉得"她永远记不住"
#
# 同类隐患：token 日志里的 "⚠ 达到 max_tokens"、
# 各处状态打印里的 "✓"。GBK 里没有 ✓(U+2713) 和 ⚠(U+26A0)。
#
# 所以这里把两个流包一层：写入失败就丢弃。
# 日志宁可丢失，也绝不能打断业务逻辑。
# ============================================================


class _SafeStream:
    """写入失败的流包装（只吞写入异常，其余属性照常透传）"""

    def __init__(self, inner):
        self._inner = inner

    def write(self, data):
        try:
            if self._inner is not None:
                return self._inner.write(data)
        except Exception:
            pass
        return len(data) if isinstance(data, str) else 0

    def flush(self):
        try:
            if self._inner is not None:
                self._inner.flush()
        except Exception:
            pass

    def isatty(self):
        try:
            return bool(self._inner is not None and self._inner.isatty())
        except Exception:
            return False

    def writable(self):
        return True

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _install_safe_streams():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if isinstance(stream, _SafeStream):
            continue
        try:
            setattr(sys, name, _SafeStream(stream))
        except Exception:
            pass


_install_safe_streams()

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont
from src.pet_window import PetWindow
from src.config_manager import BASE_DIR


def _read_model(config_path):
    """读取指定 config.yaml 中的 api.model"""
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("api", {}).get("model")
    except (OSError, yaml.YAMLError):
        return None


def _check_config_sync():
    """检查 dist\\ 配置是否与根目录脱节

    源码运行读根目录的 config.yaml，而打包后的 exe 以自身所在目录为根目录，
    读的是 dist\\config.yaml。两者不同步时会出现"改了配置但行为没变"的困惑，
    这里在启动时直接指出来。

    只有在源码目录下运行（即同级存在 dist\\）时才检查，运行时开销可忽略。
    """
    root_config = os.path.join(BASE_DIR, "config.yaml")
    dist_config = os.path.join(BASE_DIR, "dist", "config.yaml")

    # 在 exe 内部运行时 BASE_DIR 就是 dist，谈不上"两份配置"
    if not os.path.isfile(root_config) or not os.path.isfile(dist_config):
        return

    root_model = _read_model(root_config)
    dist_model = _read_model(dist_config)

    if root_model and dist_model and root_model != dist_model:
        print()
        print("⚠ 注意：两份 config.yaml 的 model 设置不一致")
        print(f"    config.yaml        → {root_model}   （源码运行 python main.py 用这个）")
        print(f"    dist\\config.yaml   → {dist_model}   （dist\\XiaAnPet.exe 用这个）")
        print("  如需同步，运行 python tools\\sync_config.py")
        print()
    else:
        print(f"提示：当前 model = {root_model or dist_model or '(未配置)'}")


def main():
    # 老用户的 API Key 可能还明文写在 config.yaml 里。
    # 在创建任何窗口之前先把它迁进 DPAPI 加密存储并清空明文 ——
    # 必须在 PetWindow（以及它触发的启动问候）之前完成，
    # 否则第一次 has_api_key() 判断会受旧文件影响。
    try:
        from src.config_manager import migrate_api_key_from_yaml
        migrate_api_key_from_yaml()
    except Exception as e:
        print(f"[Config] API Key 迁移跳过: {e}")

    app = QApplication(sys.argv)
    app.setApplicationName("夏安桌宠")
    app.setQuitOnLastWindowClosed(False)

    # 设置默认字体
    font = QFont("Microsoft YaHei", 9)
    app.setFont(font)

    # 创建桌宠
    pet = PetWindow()

    # 默认放在屏幕右下角
    screen = app.primaryScreen()
    if screen:
        screen_geo = screen.availableGeometry()
        x = screen_geo.right() - 320
        y = screen_geo.bottom() - 320
        pet.move(x, y)

    pet.show()

    # 兜底记录退出时间：无论走托盘退出、右键退出还是关窗，
    # aboutToQuit 都会触发。这样"距上次见面多久"不会因为
    # 退出路径不同而丢失基准。
    def _on_about_to_quit():
        try:
            pet.chat_dialog.session_tracker.mark_ended()
        except Exception as e:
            print(f"[Session] 记录退出时间失败: {e}")

    app.aboutToQuit.connect(_on_about_to_quit)

    print("=== 夏安桌宠已启动 ===")
    print("  - 左键双击：打开/关闭聊天窗口")
    print("  - 左键拖拽：移动桌宠位置")
    print("  - 右键：更多菜单")
    print("  - 系统托盘：显示/隐藏/退出")
    print()
    print("提示：请在 config.yaml 中配置你的 API endpoint 和 api_key")
    print("     修改 prompt.yaml 可自定义角色性格和对话风格")
    _check_config_sync()
    print()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
