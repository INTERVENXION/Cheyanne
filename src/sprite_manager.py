"""立绘管理器 — 加载、缓存、切换角色表情立绘"""
import os
import sys
from PyQt5.QtGui import QPixmap
from PyQt5.QtCore import Qt


def _get_base_dir():
    """获取应用根目录（兼容 PyInstaller 打包模式）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _get_base_dir()
IMAGE_DIR = os.path.join(BASE_DIR, "Image")

# 情绪标签 → 文件名映射
EMOTION_FILES = {
    "idle": "idle.png",
    "neutral": "idle.png",   # 日常闲聊复用 idle 立绘（显式映射，代码语义清晰）
    "happy": "happy.png",
    "sad": "sad.png",
    "angry": "angry.png",
    "surprised": "surprised.png",
    "shy": "shy.png",
    "talking": "talking.png",
}


class SpriteManager:
    """管理角色立绘的加载和切换"""

    def __init__(self, sprite_size=300):
        self.sprite_size = sprite_size
        self.sprites = {}
        self.current_emotion = "idle"
        self._load_all()

    def _load_all(self):
        """预加载所有立绘"""
        for emotion, filename in EMOTION_FILES.items():
            path = os.path.join(IMAGE_DIR, filename)
            if os.path.exists(path):
                pixmap = QPixmap(path)
                if not pixmap.isNull():
                    pixmap = pixmap.scaled(
                        self.sprite_size, self.sprite_size,
                        Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                    self.sprites[emotion] = pixmap
                else:
                    self.sprites[emotion] = self._fallback(emotion)
            else:
                self.sprites[emotion] = self._fallback(emotion)

    def _fallback(self, emotion):
        """当立绘文件不存在时，生成纯色占位图"""
        from PyQt5.QtGui import QPainter, QColor, QFont, QPen
        from PyQt5.QtCore import QRectF

        colors = {
            "idle": "#6B9BD2",
            "happy": "#F0C040",
            "sad": "#7090B0",
            "angry": "#D2654A",
            "surprised": "#E8985A",
            "shy": "#E8A0B0",
            "talking": "#60B070",
        }

        pixmap = QPixmap(self.sprite_size, self.sprite_size)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        color = QColor(colors.get(emotion, "#6B9BD2"))
        painter.setBrush(color)
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(QRectF(10, 10, self.sprite_size - 20, self.sprite_size - 20), 20, 20)

        # 绘制情绪文字
        emotion_cn = {
            "idle": "待机中", "happy": "开心", "sad": "伤心",
            "angry": "生气", "surprised": "惊讶", "shy": "害羞", "talking": "说话中",
        }
        painter.setPen(QPen(Qt.white, 2))
        font = QFont("Microsoft YaHei", 18)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, self.sprite_size, self.sprite_size),
                         Qt.AlignCenter, emotion_cn.get(emotion, emotion))

        painter.end()
        return pixmap

    def get_sprite(self, emotion="idle"):
        """获取指定情绪的立绘 QPixmap"""
        return self.sprites.get(emotion, self.sprites.get("idle"))

    def mark_displayed(self, emotion):
        """记录实际显示出来的情绪

        未命中的情绪名（如回退到 idle）会记为 "idle"，
        保证 current_emotion 始终反映屏幕上真实显示的内容。
        """
        self.current_emotion = emotion if emotion in self.sprites else "idle"

    def set_emotion(self, emotion):
        """切换当前情绪（仅更新状态，不触发绘制）"""
        self.mark_displayed(emotion)
