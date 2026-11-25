"""对话弹窗 — 点击桌宠时弹出，提供 LLM 聊天界面"""
from datetime import datetime
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QLineEdit,
    QPushButton, QLabel, QScrollArea, QFrame, QSizePolicy, QLayout,
)
from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QPoint, QSize, QRect, QEvent,
)
from PyQt5.QtGui import QFont, QColor, QPalette, QFontMetrics
from src.llm_client import LLMClient
from src.config_manager import load_yaml, has_api_key
from src.lunar import matches, table_coverage_warning
from src.session import SessionTracker, format_gap

# 未配置 API Key 时夏安说的第一句话。
# 文案由用户指定：说清楚"缺什么"和"去哪里填"，不要出现技术术语。
NO_API_KEY_MESSAGE = (
    "指挥官，您好像还没有设置我的apikey，"
    "请右击我，在设置apikey中填写您的apikey。"
)

# 农历节日表超出覆盖范围时提前提示（否则节日问候会静默失效）
_coverage_warning = table_coverage_warning()
if _coverage_warning:
    print(f"[ChatDialog] 警告: {_coverage_warning}")



class LLMWorker(QThread):
    """在后台线程中调用 LLM API"""
    finished = pyqtSignal(str, str)   # (emotion, reply)
    error = pyqtSignal(str)           # error message

    def __init__(self, client, message, save_history=True, session_block=None,
                 force_memory=False):
        super().__init__()
        self.client = client
        self.message = message
        self.save_history = save_history
        self.session_block = session_block
        self.force_memory = force_memory

    def run(self):
        """在工作线程里跑一次对话

        整个函数体必须包在 try/except 里。PyQt5 对**逃出槽函数/虚函数**的
        未捕获异常会调用 qFatal() —— 不是打印堆栈后继续，而是直接 abort
        整个进程（Windows 上是 0xC0000409）。用户看到的现象是"桌宠在
        发送消息后凭空消失"，连 aboutToQuit 都不会跑。

        配置被存成 GBK、历史文件里混入非法条目、写盘时遇到孤立代理字符等
        都会走到这里，所以这道防线是必需的，不能只依赖上游不出错。
        """
        try:
            emotion, reply = self.client.chat(
                self.message,
                save_history=self.save_history,
                session_block=self.session_block,
                force_memory=self.force_memory,
            )
        except Exception as e:
            # 关键：把异常转成信号，而不是让它逃出 run()
            print(f"[LLMWorker] 未预期的错误: {type(e).__name__}: {e}")
            try:
                self.error.emit(f"内部错误：{type(e).__name__}: {e}")
            except Exception:
                pass
            return

        if emotion is not None:
            self.finished.emit(emotion, reply)
        else:
            self.error.emit(reply)


class SummaryWorker(QThread):
    """后台线程 — 异步执行记忆摘要，不阻塞对话"""

    finished = pyqtSignal()
    error = pyqtSignal(str)   # error message

    # 单次运行最多处理几批。
    # 启动时可能要补齐几百条历史（每批 SUMMARY_BATCH=60 条），
    # 分批做完而不是一次请求塞爆；设上限避免极端情况下线程长时间不退出，
    # 剩下的批次会在下一次 _maybe_summarize 时继续。
    MAX_BATCHES_PER_RUN = 5

    def __init__(self, client):
        super().__init__()
        self.client = client

    def run(self):
        # 摘要/事实提取内部已各自捕获 API 异常，理论上不会抛到这里；
        # 但不能让意外异常直接杀死线程（那会静默中断记忆整理且无任何提示）
        try:
            for _ in range(self.MAX_BATCHES_PER_RUN):
                job = self.client.peek_pending_summary()
                if not job:
                    break
                # 先处理，成功后才 ack 出队。
                # 旧实现是"先 pop 再处理"，处理中抛异常或程序退出，
                # 那批消息就永久消失（既不在工作记忆，也不在队列）。
                self.client.run_summarization(job)
                self.client.ack_pending_summary(job)
        except Exception as e:
            print(f"[SummaryWorker] 记忆整理失败: {e}")
            self.error.emit(str(e))
        finally:
            self.finished.emit()


class _WrapLabel(QLabel):
    """按给定宽度自行计算高度的正文标签

    为什么不用普通 QLabel + setWordWrap(True)：
    实测在本项目的布局栈里（ScrollArea → chat_widget → QVBoxLayout → 气泡
    → QVBoxLayout → QLabel），QLabel 的 heightForWidth 不可靠 ——
    文本明明要折成两行，heightForWidth(400) 却返回 19px（一行），
    于是布局按一行给高度，第二行被直接裁掉。

    用户看到的现象就是消息只显示前半句：
      「好，指挥官先忙，我」 ← 后半句「等你。别太晚哦。」被裁掉

    QLabel 内部是逐行绘制的，无法对"整体文本"做整形，所以这里用
    QFontMetrics + Qt.TextWordWrap 精确计算换行后的高度，
    并让 sizeHint / minimumSizeHint / heightForWidth 三者保持一致。
    """

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(True)
        # 允许被压窄（否则宽度撑不住时会溢出窗口）
        self.setMinimumWidth(60)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        # 外部（气泡）告诉它最多能用多宽；不用 maximumWidth，
        # 因为那会在布局算完高度之后才生效，导致高度不重算。
        self._avail_width = 0

    # ---------- 宽度约束 ----------

    def set_available_width(self, width):
        """由气泡告知可用的最大宽度"""
        width = max(60, int(width))
        if width == self._avail_width:
            return
        self._avail_width = width
        self.updateGeometry()

    def _natural_width(self):
        """文本不换行时需要的宽度"""
        fm = QFontMetrics(self.font())
        return fm.boundingRect(self.text() or " ").width()

    def _effective_width(self):
        """实际排版宽度 = min(自然宽度, 可用宽度)

        这是修掉「气泡太窄、文字被右边缘裁掉」的关键：
        气泡按自然宽度要空间（上面 sizeHint 返回的就是它），
        超过可用宽度时才退化为换行。
        """
        natural = self._natural_width()
        if self._avail_width > 0:
            return max(60, min(natural, self._avail_width))
        return natural

    # ---------- 度量 ----------

    def _wrapped_height(self, width):
        """给定宽度下，文本换行后需要的高度"""
        text = self.text()
        if not text:
            return 0

        fm = QFontMetrics(self.font())
        flags = int(Qt.TextWordWrap) | int(self.alignment())
        rect = fm.boundingRect(QRect(0, 0, max(1, width), 10000), flags, text)
        # 加一点余量，避免字体度量在不同 DPI 下差一两个像素导致末行被裁
        return rect.height() + 2

    # ---------- QWidget 接口 ----------

    def sizeHint(self):
        width = self._effective_width()
        return QSize(width, self._wrapped_height(width))

    def minimumSizeHint(self):
        """最小尺寸 = 最窄宽度 + 一行高度

        注意：**不能**用最小宽度去算换行高度。
        早先写成 self._wrapped_height(60) 时，文本在 60px 下会折成 5 行，
        返回 98px —— 配合布局的 SetMinimumSize 约束，
        气泡会被强行撑到 98px 高，整屏都是空白。
        最小高度就该是一行。
        """
        fm = QFontMetrics(self.font())
        return QSize(min(60, self._natural_width()), fm.height() + 2)

    def heightForWidth(self, width):
        return self._wrapped_height(width)

    def hasHeightForWidth(self):
        return True

    # ---------- 自校正高度 ----------

    def _sync_height(self):
        """按当前宽度把自己钉到"刚好放得下"的高度

        为什么必须这么做：
        布局是本项目里最不可靠的一环。它按旧宽度算好高度之后，
        若之后宽度变窄（用户缩小窗口、或气泡被重新约束），
        文字会多折出一行，但布局**不会**重算高度 —— 那一行就被裁掉。
        用户看到的就是消息只显示前半句。

        这里在每次尺寸/几何变化后主动核对一次：
        拿到布局给的高度，若不足以容纳换行后的文本，
        就用 setMinimumHeight / setMaximumHeight 把它顶到正确值，
        并让父级重新布局。
        """
        w = self.width()
        if w <= 0:
            return
        needed = self._wrapped_height(w)
        if needed <= 0:
            return

        if self.minimumHeight() == needed and self.maximumHeight() == needed:
            return

        self.setMinimumHeight(needed)
        self.setMaximumHeight(needed)

        parent = self.parentWidget()
        if parent is not None:
            lay = parent.layout()
            if lay is not None:
                lay.invalidate()
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_height()

    def event(self, event):
        """几何/字体变化后都要重新核对高度"""
        result = super().event(event)
        if event.type() in (QEvent.FontChange, QEvent.StyleChange,
                            QEvent.LayoutRequest):
            self._sync_height()
        return result


class ChatBubble(QFrame):
    """单条聊天气泡"""

    def __init__(self, text, is_user=False, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)
        # 让布局把"高度依赖宽度"这件事传递上去
        self.setAttribute(Qt.WA_StaticContents, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSizeConstraint(QLayout.SetMinimumSize)

        role = "指挥官" if is_user else "夏安"
        header = QLabel(role)
        header_font = QFont("Microsoft YaHei", 9)
        header_font.setBold(True)
        header.setFont(header_font)

        body = _WrapLabel(text)
        body.setFont(QFont("Microsoft YaHei", 10))
        body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._body = body

        layout.addWidget(header)
        layout.addWidget(body)

        if is_user:
            self.setStyleSheet("""
                ChatBubble {
                    background-color: #D4E8FF;
                    border-radius: 10px;
                    margin-left: 40px;
                }
            """)
        else:
            self.setStyleSheet("""
                ChatBubble {
                    background-color: #FFE0D0;
                    border-radius: 10px;
                    margin-right: 40px;
                }
            """)

    def resizeEvent(self, event):
        """窗口宽度变化时，把可用宽度告诉正文标签

        宽度要按**实际可用宽度**算，不能拍魔法数字：
        早先用 window().width() - 110，比真实可用宽度小太多，
        长句被多折一行，布局高度却按预估行数给，多出来的那行就被裁掉。

        这里取滚动区视口宽度，减去滚动条余量与用户气泡的 40px 偏移。
        """
        super().resizeEvent(event)

        dialog = self.window()
        scroll = getattr(dialog, "scroll_area", None)
        if scroll is not None and scroll.viewport().width() > 0:
            base = scroll.viewport().width()
        else:
            base = dialog.width()

        avail = max(160, base - 16 - 40 - 16)
        if avail == getattr(self, "_bubble_avail", None):
            return
        self._bubble_avail = avail

        body = getattr(self, "_body", None)
        if body is not None:
            # 气泡自身的内容宽度 = 可用宽度 - 左右内边距
            body.set_available_width(avail - 20)
        self.setMaximumWidth(avail)


class ChatDialog(QWidget):
    """对话弹窗 — 无边框窗口，显示在桌宠旁边"""

    emotion_changed = pyqtSignal(str)

    # 回复结束后表情回落到 default 的延迟（毫秒）
    _EMOTION_RESET_MS = 6000

    def __init__(self, parent=None):
        super().__init__(parent)
        self.llm_client = LLMClient()
        # 进行中的后台线程集合。
        # QThread 被 Python 垃圾回收而线程仍在运行时，Qt 会直接崩溃
        # ("QThread: Destroyed while thread is still running")，
        # 所以必须持有引用，并在退出时统一收尾。
        self._workers = set()
        self._is_shutting_down = False
        # 是否有 LLM 请求正在进行 —— _send_message 的硬防护依据。
        # 不能只看控件禁用状态：returnPressed 在控件禁用时仍可被 emit。
        self._is_requesting = False
        self._greeting_pet_geometry = None
        # 启动问候是否已经真正成功过（用于"填完 key 后补一次问候"）
        self._greeting_succeeded = False
        self._greeted_once = False

        # 会话边界 —— 让夏安分清"记得的事"和"正在发生的事"
        self.session_tracker = SessionTracker()
        self.session_tracker.mark_started()
        self._session_block = None      # 惰性构建，只用一次
        self._session_block_used = False

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMinimumSize(320, 400)

        self._setup_ui()
        self._connect_signals()

        # 表情回落定时器 —— 必须是可取消的常驻定时器。
        # 旧实现每次回复都新建 QTimer.singleShot(6000, ...)，旧定时器无法取消：
        # 连续对话时上一条的定时器会把新表情提前打回 idle。
        self._emotion_timer = QTimer(self)
        self._emotion_timer.setSingleShot(True)
        self._emotion_timer.setInterval(self._EMOTION_RESET_MS)
        self._emotion_timer.timeout.connect(lambda: self.emotion_changed.emit("idle"))

    def _setup_ui(self):
        """构建 UI"""
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        # 主容器
        container = QFrame(self)
        container.setObjectName("chatContainer")
        container.setStyleSheet("""
            #chatContainer {
                background-color: rgba(255, 255, 255, 230);
                border: 1px solid #ccc;
                border-radius: 12px;
            }
        """)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)

        # 标题栏
        title_bar = QHBoxLayout()
        title = QLabel("💬 与夏安聊天")
        title.setFont(QFont("Microsoft YaHei", 11))
        title.setStyleSheet("font-weight: bold; color: #333;")

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setStyleSheet("""
            QPushButton {
                border: none; background: transparent;
                font-size: 14px; color: #888;
            }
            QPushButton:hover { color: #333; }
        """)
        close_btn.clicked.connect(self.hide)

        title_bar.addWidget(title)
        title_bar.addStretch()
        title_bar.addWidget(close_btn)
        layout.addLayout(title_bar)

        # 对话历史区
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self.chat_widget = QWidget()
        self.chat_widget.setStyleSheet("background: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_widget)
        self.chat_layout.setAlignment(Qt.AlignTop)
        self.chat_layout.setSpacing(8)
        self.chat_layout.addStretch()

        self.scroll_area.setWidget(self.chat_widget)
        layout.addWidget(self.scroll_area, 1)

        # 输入区
        input_layout = QHBoxLayout()
        self.input_field = QLineEdit()
        self.input_field.setFont(QFont("Microsoft YaHei", 10))
        self.input_field.setPlaceholderText("输入消息，按回车发送...")
        self.input_field.setStyleSheet("""
            QLineEdit {
                border: 1px solid #ccc;
                border-radius: 8px;
                padding: 8px 12px;
                background: white;
            }
            QLineEdit:focus { border-color: #F0A060; }
        """)

        self.send_btn = QPushButton("发送")
        self.send_btn.setFont(QFont("Microsoft YaHei", 10))
        self.send_btn.setStyleSheet("""
            QPushButton {
                background-color: #F0A060;
                color: white; border: none;
                border-radius: 8px; padding: 8px 16px;
            }
            QPushButton:hover { background-color: #D89050; }
            QPushButton:disabled { background-color: #ccc; }
        """)

        input_layout.addWidget(self.input_field, 1)
        input_layout.addWidget(self.send_btn)
        layout.addLayout(input_layout)

        outer_layout.addWidget(container)

    def _connect_signals(self):
        """连接信号"""
        self.input_field.returnPressed.connect(self._send_message)
        self.send_btn.clicked.connect(self._send_message)

        # 滚动到最底部
        self.scroll_area.verticalScrollBar().rangeChanged.connect(
            lambda: self.scroll_area.verticalScrollBar().setValue(
                self.scroll_area.verticalScrollBar().maximum()
            )
        )

    def _get_session_block(self, consume=True):
        """构建会话边界提示

        Args:
            consume: True 表示"用掉"这次机会，后续调用返回 None

        它只该出现在本次启动的**第一轮正式对话**里：
        之后模型已经知道"现在是什么时候"，重复注入只是啰嗦且浪费 token。
        """
        if self._session_block is None:
            self._session_block = self.session_tracker.build_boundary_block(
                self.llm_client._full_history
            )

        if consume:
            if self._session_block_used:
                return None
            self._session_block_used = True
            if self._session_block:
                print("[Session] 已注入会话边界提示（本次启动仅一次）")

        return self._session_block

    def _greeting_session_block(self):
        """给启动问候用的会话边界（不消费额度）

        问候必须**始终**带上时间基准，原因有两个：

        1. 问候是唯一"没有任何用户输入、只凭记忆说话"的场合，
           没有时间锚点时最容易把上次的场景当成正在发生
        2. 如果用户在 1 秒问候延迟内抢先发了消息，边界会被那条消息
           消费掉（见 _get_session_block 的 consume 逻辑）。
           而问候仍然带着 force_memory=True 去读摘要 ——
           等于"没有时间锚点却读旧记忆"，正是割裂感的成因。
           所以问候这里绕过消费，保证自己一定有。
        """
        return self._get_session_block(consume=False)

    def _send_message(self):
        """发送消息到 LLM

        必须显式检查"是否已有请求在飞"，不能只依赖控件禁用状态。
        实测：请求进行中若再次触发本方法（回车键的 returnPressed
        可以在控件禁用状态下被 emit、程序化调用、
        或将来某个新入口忘了同步禁用按钮），就会有两个 worker 同时跑，
        后发先至时对话顺序会被打乱：

            气泡: 指挥官: 第一条 / 指挥官: 第二条
                  夏安: 回复给第二条  ← 答非所问的位置
                  夏安: 回复给第一条

        而且这份错乱的历史会被**写进 chat_history.json**，
        影响后续所有上下文。所以这里做硬防护。
        """
        if self._is_shutting_down:
            return

        if self._is_requesting:
            print("[ChatDialog] 已有请求进行中，忽略本次发送")
            return

        text = self.input_field.text().strip()
        if not text:
            return

        self._is_requesting = True
        self.input_field.clear()
        self.input_field.setEnabled(False)

        # 显示用户消息
        self._add_bubble(text, is_user=True)

        self.send_btn.setEnabled(False)

        # 触发情绪切换为 talking（思考中）
        self._set_emotion("talking")

        # 后台线程调用 LLM
        worker = LLMWorker(
            self.llm_client, text, session_block=self._get_session_block()
        )
        worker.finished.connect(self._on_reply)
        worker.error.connect(self._on_error)
        self._track_worker(worker)
        worker.start()

    def _finish_request(self):
        """请求结束（成功或失败）后恢复可输入状态"""
        self._is_requesting = False
        if self._is_shutting_down:
            return
        self.input_field.setEnabled(True)
        self.send_btn.setEnabled(True)

    def _set_emotion(self, emotion):
        """统一切换表情 —— 先取消待执行的回落定时器

        这样"思考中(talking) → 正式表情"的过渡不会被上一条回复的
        6 秒定时器打断。
        """
        self._emotion_timer.stop()
        self.emotion_changed.emit(emotion)
        if emotion != "idle":
            self._emotion_timer.start()

    def _track_worker(self, worker):
        """登记后台线程，线程结束后自动移除

        注意：并不是所有 worker 都有 error 信号
        （SummaryWorker 曾经只有 finished），所以这里必须先判断信号是否存在，
        不能无条件连接 —— 否则 QThread 没有该属性会直接 AttributeError。
        """
        self._workers.add(worker)

        def _drop(*_args, w=worker):
            # 同时兼容 finished() 的无参形式和 error(str) 的单参形式
            self._workers.discard(w)

        for signal_name in ("finished", "error"):
            signal = getattr(worker, signal_name, None)
            if signal is None:
                continue
            try:
                signal.connect(_drop)
            except TypeError:
                # 信号签名与回调不匹配时跳过，不影响线程登记本身
                print(f"[ChatDialog] 无法连接 {type(worker).__name__}.{signal_name}")

    def _on_reply(self, emotion, reply):
        """收到 LLM 回复"""
        self._add_bubble(reply, is_user=False)
        self._set_emotion(emotion)
        self._finish_request()
        self.input_field.setFocus()

        # 异步处理记忆摘要（如有溢出）
        self._maybe_summarize()

    def _on_error(self, error_msg):
        """LLM 调用出错"""
        self._add_bubble(f"(系统) {error_msg}", is_user=False)
        self._set_emotion("idle")
        self._finish_request()
        self.input_field.setFocus()

    def _add_bubble(self, text, is_user=False):
        """添加聊天气泡"""
        # 移除底部的 stretch
        if self.chat_layout.count() > 0:
            item = self.chat_layout.itemAt(self.chat_layout.count() - 1)
            if item and item.spacerItem():
                self.chat_layout.removeItem(item)

        bubble = ChatBubble(text, is_user)
        if is_user:
            self.chat_layout.addWidget(bubble, 0, Qt.AlignRight)
        else:
            self.chat_layout.addWidget(bubble, 0, Qt.AlignLeft)

        self.chat_layout.addStretch()

    # ==================== 启动问候 ====================

    def generate_startup_greeting(self, pet_geometry=None):
        """生成启动问候语并显示

        没配置 API Key 时不发请求（必然失败），改为在对话框里提示用户
        去右键菜单填写 —— 否则用户只会看到一句"内部错误"，不知道该怎么办。
        """
        # 先记录桌宠位置 —— 必须在启动 worker 之前赋值。
        # 否则若 worker 极快返回，回调里读到的还是 None，对话窗会定位到 (0,0)。
        self._greeting_pet_geometry = pet_geometry
        self._greeted_once = True

        if not has_api_key():
            self._show_missing_api_key_notice()
            return

        prompt = self._build_greeting_prompt()

        # 问候是本次启动的第一轮 LLM 交互，所以由它"用掉"会话边界。
        # 这样启动后的第一句就是"重新碰面"的姿态，
        # 而不是接着上次的场景往下演。
        # 问候始终带会话边界（不消费额度，见 _greeting_session_block）
        worker = LLMWorker(
            self.llm_client,
            prompt,
            save_history=False,
            session_block=self._greeting_session_block(),
            force_memory=True,   # 问候没有用户输入，靠关键词取不到记忆
        )
        worker.finished.connect(self._on_greeting_reply)
        worker.error.connect(self._on_greeting_error)
        self._track_worker(worker)
        self.greeting_worker = worker
        worker.start()

    def _show_missing_api_key_notice(self):
        """未配置 API Key 时的默认提示语

        用户第一次打开程序、还没填 key 时，她应该说清楚"缺什么、去哪儿填"，
        而不是抛一个技术错误。文案由用户指定，不要随意改写。
        """
        self._add_bubble(NO_API_KEY_MESSAGE, is_user=False)
        self._set_emotion("shy")
        if self._greeting_pet_geometry:
            self.position_near(self._greeting_pet_geometry)
        self.show()
        self.raise_()

    def on_api_key_updated(self):
        """设置界面保存了新的 API Key 之后调用

        如果之前因为缺 key 而没能真正问候过，这里补一次正式的启动问候，
        用户不必重启程序。
        """
        self._add_bubble("API Key 已保存，我现在可以说话了。", is_user=False)
        if not self._greeting_succeeded:
            QTimer.singleShot(
                300, lambda: self.generate_startup_greeting(self._greeting_pet_geometry)
            )

    def _build_greeting_prompt(self):
        """构建问候 prompt，包含日期/时间/节日信息"""
        now = datetime.now()
        weekday_map = {
            0: "星期一", 1: "星期二", 2: "星期三",
            3: "星期四", 4: "星期五", 5: "星期六", 6: "星期日",
        }
        weekday_cn = weekday_map[now.weekday()]
        is_weekend = now.weekday() >= 5

        config = load_yaml("holidays.yaml")

        # 时段判断（受 time_of_day_greeting 开关控制）
        hour = now.hour
        if config.get("time_of_day_greeting", True):
            if 5 <= hour < 9:
                time_period = "清晨"
            elif 9 <= hour < 12:
                time_period = "上午"
            elif 12 <= hour < 14:
                time_period = "中午"
            elif 14 <= hour < 18:
                time_period = "下午"
            elif 18 <= hour < 22:
                time_period = "晚上"
            else:
                time_period = "深夜"
        else:
            time_period = ""

        date_str = (
            f"{now.year}年{now.month}月{now.day}日 {weekday_cn}"
            f" {time_period}{now.hour:02d}:{now.minute:02d}"
        ).replace("  ", " ").strip()

        # 检查特殊日期
        special_info = self._check_special_date(now, config)

        # 距上次见面过了多久 —— 让问候有"重新碰面"的时间感
        gap = format_gap(
            self.session_tracker.elapsed_since_last_seen(self.llm_client._full_history)
        )

        # 构建 prompt
        prompt = f"【系统通知】现在是 {date_str}。"
        if special_info:
            prompt += (
                f"\n今天是{special_info['name']}——{special_info['description']}。"
                f"{special_info.get('greeting_hint', '')}"
            )
        elif is_weekend and config.get("weekend_greeting", True):
            prompt += "\n今天是周末休息日。"
        else:
            prompt += "\n今天是普通工作日。"

        if gap and gap != "刚刚":
            prompt += (
                f"\n距离你们上次说话已经过去了{gap}。"
                "这段时间你们各自在忙自己的事，你不知道具体发生了什么。"
            )

        prompt += (
            "\n\n【重要】以下是这次问候的性质，请务必理解："
            "\n你是在**隔了一段时间之后重新见到**指挥官，不是在继续上一次的对话。"
            "\n上一轮对话早就结束了。如果它结束在某个场景里（比如上次正在道晚安、"
            "准备睡觉、或者刚从外面回来），那个场景**已经过去了**，"
            "绝对不要表现成它还在进行中，也不要说“你刚才不是说要……”这类接续的话。"
            "\n如果有记忆提到指挥官之前的状态（累了、要睡了、在忙），"
            "那只是你的**印象**，不代表他现在还在那个状态。"
        )

        prompt += (
            "\n\n作为夏安，请用自然、亲切的语气向指挥官发送一条主动问候语。"
            "根据时间和日期调整问候风格：早晨问早安、中午问午安、晚上问晚安。"
            "如果隔了很久（比如跨了夜或隔了好几天），可以自然地带出一点“好久没见”"
            "或“你昨晚休息得怎么样”的感觉，但不要追问上次的细节。"
            "\n\n【重要】你必须用 JSON 格式回复（与系统提示词要求一致）："
            "{\"emotion\": \"情绪值\", \"reply\": \"你的问候正文\"}。"
            "emotion 请使用 \"neutral\"。"
            "reply 控制在 50 字以内，简洁自然。"
            "不要回复\"系统通知\"这个词，直接对指挥官说话即可。"
            "\n\n记住：你现在是主动发起对话，不是在回复别人的消息。"
        )
        return prompt

    def _check_special_date(self, now, config=None):
        """检查当前日期是否为特殊日期，返回节日信息 dict 或 None

        支持两种定义方式（见 holidays.yaml）：
        - month/day        公历固定日期
        - lunar_month/day  农历日期，由 src.lunar 自动换算
        """
        if config is None:
            config = load_yaml("holidays.yaml")

        # 总开关
        if not config.get("special_greeting", True):
            return None

        for entry in config.get("special_dates", []):
            if not isinstance(entry, dict):
                continue

            lunar_month = entry.get("lunar_month")
            if lunar_month is not None:
                hit = matches(
                    now.year,
                    now.month,
                    now.day,
                    lunar_month,
                    int(entry.get("lunar_day", 1)),
                    int(entry.get("offset_days", 0)),
                )
            else:
                hit = (
                    entry.get("month") == now.month
                    and entry.get("day") == now.day
                )

            if hit:
                return entry

        return None

    def _on_greeting_reply(self, emotion, reply):
        """启动问候的 LLM 回复回调"""
        self._greeting_succeeded = True
        self._add_bubble(reply, is_user=False)
        self._set_emotion(emotion)

        # 显示对话窗在桌宠旁边
        if self._greeting_pet_geometry:
            self.position_near(self._greeting_pet_geometry)
        self.show()
        self.raise_()

        # 清理
        self._disconnect_greeting_worker()

        # 异步处理记忆摘要（初始归档等）
        self._maybe_summarize()

    def _on_greeting_error(self, error_msg):
        """启动问候 LLM 错误回调 — 静默处理"""
        self._disconnect_greeting_worker()

    def _disconnect_greeting_worker(self):
        """断开问候 worker 的信号连接"""
        worker = getattr(self, "greeting_worker", None)
        if worker is None:
            return
        for signal, slot in (
            (worker.finished, self._on_greeting_reply),
            (worker.error, self._on_greeting_error),
        ):
            try:
                signal.disconnect(slot)
            except TypeError:
                pass
        self.greeting_worker = None

    def shutdown(self, timeout_ms=400):
        """退出前收尾：取消定时器并等待后台线程结束

        旧实现直接 QApplication.quit()，飞行中的 QThread 会被 Python 回收，
        Qt 会打印 "QThread: Destroyed while thread is still running"，
        在 Windows 上可能直接异常退出。

        关于等待时长：run() 里是阻塞的 requests.post，没有事件循环，
        所以 `quit()` 无法打断它 —— 等待必然耗满超时时间。
        之前设 3000ms，意味着用户点"退出"后进程有 3 秒无响应
        （窗口已经隐藏，看起来像卡死）。改成 400ms：
        这点时间足够让"已经跑完、正在回主线程"的 worker 收尾，
        而真正卡在网络上的请求会被 terminate 掉。

        被 terminate 不会损坏数据文件 —— atomic_write_json 的
        临时文件 + os.replace 保证目标文件要么是完整旧内容、
        要么是完整新内容，最多丢掉一次未完成的写入。
        """
        self._is_shutting_down = True
        self._emotion_timer.stop()

        for worker in list(self._workers):
            if not worker.isRunning():
                continue
            worker.quit()
            if not worker.wait(timeout_ms):
                # 超时（例如 requests 还卡在网络上）：强制终止，避免卡住退出
                print("[ChatDialog] 后台线程未及时结束，强制终止")
                worker.terminate()
                worker.wait(300)
        self._workers.clear()

    def position_near(self, pet_geometry):
        """将对话窗定位在桌宠旁边"""
        pet_center = pet_geometry.center()
        dialog_w = self.width()
        dialog_h = self.height()

        # 优先放在桌宠右侧
        x = pet_geometry.right() + 10
        y = pet_geometry.top()

        # 获取屏幕尺寸，防止超出
        screen = self.screen()
        if screen:
            screen_geo = screen.availableGeometry()
            if x + dialog_w > screen_geo.right():
                x = pet_geometry.left() - dialog_w - 10
            if y + dialog_h > screen_geo.bottom():
                y = screen_geo.bottom() - dialog_h

        self.move(x, y)

    def _maybe_summarize(self):
        """如果存在待处理的摘要任务，在后台异步执行"""
        if self._is_shutting_down:
            return
        # 已有摘要任务在跑时不重复启动，避免堆积多个后台请求
        if any(isinstance(w, SummaryWorker) for w in self._workers):
            return
        if self.llm_client.has_pending_summary:
            worker = SummaryWorker(self.llm_client)
            # 先登记（会把 worker 加入 _workers 并在结束时移除），
            # 再把 finished 接到 _maybe_summarize 上 —— 连接顺序决定了
            # 「先移除登记、再判断是否还有待处理批次」，否则第二次判断
            # 会因为当前 worker 仍在集合里而直接返回，剩余批次要等到
            # 下一句回复才继续。
            self._track_worker(worker)
            worker.finished.connect(self._maybe_summarize)
            worker.start()

    def showEvent(self, event):
        """显示时自动聚焦输入框"""
        super().showEvent(event)
        self.input_field.setFocus()

    def closeEvent(self, event):
        """关闭窗口时取消待执行的表情回落，避免操作已销毁的控件"""
        self._emotion_timer.stop()
        super().closeEvent(event)
