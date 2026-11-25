"""桌宠主窗口 — 无边框、置顶、透明背景、可拖拽"""
from PyQt5.QtWidgets import (
    QWidget, QLabel, QMenu, QAction, QSystemTrayIcon, QApplication, QDialog,
)
from PyQt5.QtCore import Qt, QPoint, QTimer
from PyQt5.QtGui import QPixmap, QIcon, QBitmap
from src.sprite_manager import SpriteManager
from src.chat_dialog import ChatDialog


class PetWindow(QWidget):
    """虚拟桌宠主窗口"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        # 状态
        self.drag_position = QPoint()
        self.sprite_manager = SpriteManager(sprite_size=280)

        # 初始化 UI
        self._setup_ui()
        self._setup_tray()
        self._setup_chat_dialog()

        # 设置初始立绘
        self._update_sprite("idle")

        # 延迟触发启动问候（等 UI 就绪后异步调用 LLM）
        QTimer.singleShot(1000, self._trigger_startup_greeting)

    def _trigger_startup_greeting(self):
        """触发启动问候"""
        self.chat_dialog.generate_startup_greeting(pet_geometry=self.geometry())

    def _setup_ui(self):
        """初始化 UI"""
        # 立绘标签
        self.sprite_label = QLabel(self)
        self.sprite_label.setAlignment(Qt.AlignCenter)
        self.sprite_label.setAttribute(Qt.WA_TranslucentBackground)
        self.sprite_label.setStyleSheet("background: transparent;")

        # 设置窗口大小为立绘大小
        size = self.sprite_manager.sprite_size
        self.setFixedSize(size, size)

    def _setup_tray(self):
        """初始化系统托盘"""
        self.tray_available = False
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_available = True
            self.tray_icon = QSystemTrayIcon(self)

            # 使用 idle 立绘作为托盘图标
            pixmap = self.sprite_manager.get_sprite("idle")
            if pixmap:
                icon = QIcon(pixmap.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                self.tray_icon.setIcon(icon)

            self.tray_icon.setToolTip("夏安 — 虚拟桌宠")

            # 托盘菜单
            tray_menu = QMenu()
            show_action = QAction("显示桌宠", self)
            show_action.triggered.connect(self.show)
            hide_action = QAction("隐藏桌宠", self)
            hide_action.triggered.connect(self.hide)
            quit_action = QAction("退出", self)
            quit_action.triggered.connect(self._quit)

            tray_menu.addAction(show_action)
            tray_menu.addAction(hide_action)
            tray_menu.addSeparator()
            tray_menu.addAction(quit_action)

            self.tray_icon.setContextMenu(tray_menu)
            self.tray_icon.activated.connect(self._on_tray_activated)
            self.tray_icon.show()

    def _setup_chat_dialog(self):
        """初始化对话弹窗"""
        self.chat_dialog = ChatDialog()
        self.chat_dialog.emotion_changed.connect(self._update_sprite)

    def _update_sprite(self, emotion):
        """切换立绘表情"""
        pixmap = self.sprite_manager.get_sprite(emotion)
        if not pixmap:
            return

        self.sprite_label.setPixmap(pixmap)
        self.sprite_label.resize(pixmap.size())
        # 从 alpha 通道创建窗口遮罩，实现不规则形状
        alpha = pixmap.toImage().createAlphaMask()
        mask = QBitmap.fromImage(alpha)
        self.setMask(mask)
        # 状态由 SpriteManager 统一持有，避免"请求的情绪"与
        # "实际显示的情绪"（未命中时回落 idle）两份状态互相打架
        self.sprite_manager.mark_displayed(emotion)

    def _on_tray_activated(self, reason):
        """托盘图标点击事件"""
        if reason == QSystemTrayIcon.DoubleClick:
            self.show()
            self.raise_()

    def closeEvent(self, event):
        """关闭窗口时的行为

        没有托盘可用时必须真正退出：桌宠是无边框窗口、任务栏不显示条目，
        关掉它就再也没有任何入口能把它叫回来 —— 进程会变成一个
        完全看不见的僵尸（只能去任务管理器杀），而且 aboutToQuit 不触发，
        退出时间也不会被记录。

        有托盘时保持原来的"关闭即隐藏"，从托盘菜单退出。
        """
        if getattr(self, "tray_available", False):
            event.ignore()
            self.hide()
            return None
        return super().closeEvent(event)

    def _quit(self):
        """退出应用 — 先让后台线程收尾，再退出"""
        # 隐藏窗口，避免退出过程中界面还在响应
        self.hide()
        self.chat_dialog.close()
        # 等待飞行中的 LLM / 摘要线程结束，防止 QThread 被回收导致崩溃
        self.chat_dialog.shutdown()
        # 记录退出时间 —— 下次启动用它算出"距上次见面过了多久"
        try:
            self.chat_dialog.session_tracker.mark_ended()
        except Exception as e:
            print(f"[Session] 记录退出时间失败: {e}")
        if hasattr(self, 'tray_icon'):
            self.tray_icon.hide()
        QApplication.quit()

    # ==================== 鼠标事件 ====================

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.LeftButton:
            self.move(event.globalPos() - self.drag_position)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        """双击打开/关闭聊天窗口"""
        if event.button() == Qt.LeftButton:
            self._on_click()

    def contextMenuEvent(self, event):
        """右键菜单"""
        menu = QMenu(self)
        show_chat = QAction("打开聊天", self)
        show_chat.triggered.connect(self._on_click)
        menu.addAction(show_chat)

        reset_action = QAction("清空对话历史", self)
        reset_action.triggered.connect(self._reset_chat)
        menu.addAction(reset_action)

        api_key_action = QAction("设置 API Key 与模型…", self)
        api_key_action.triggered.connect(self._open_api_key_dialog)
        menu.addAction(api_key_action)

        menu.addSeparator()

        # 表情测试子菜单
        emo_menu = QMenu("切换表情 (测试)", self)
        for emo_name, emo_cn in [
            ("idle", "默认"), ("happy", "开心"), ("sad", "伤心"),
            ("angry", "生气"), ("surprised", "惊讶"), ("shy", "害羞"),
        ]:
            action = QAction(emo_cn, self)
            action.triggered.connect(lambda checked, e=emo_name: self._update_sprite(e))
            emo_menu.addAction(action)
        menu.addMenu(emo_menu)

        menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        menu.exec_(event.globalPos())

    def _on_click(self):
        """单击打开对话弹窗"""
        if self.chat_dialog.isVisible():
            self.chat_dialog.hide()
        else:
            self.chat_dialog.position_near(self.geometry())
            self.chat_dialog.show()
            self.chat_dialog.raise_()

    def _reset_chat(self):
        """清空对话历史"""
        self.chat_dialog.llm_client.clear_history()
        self.chat_dialog._add_bubble("(对话历史已清空，夏安不记得之前聊了什么)", is_user=False)

    def _open_api_key_dialog(self):
        """打开模型 / API Key 设置窗口

        保存成功后通知 ChatDialog —— 如果之前因为没配置 key 而只显示了
        提示语，这里会补上一次真正的启动问候，用户不必重启程序。
        """
        from src.settings_dialog import SettingsDialog

        dialog = SettingsDialog(self)
        if dialog.exec_() == QDialog.Accepted:
            self.chat_dialog.on_api_key_updated()
