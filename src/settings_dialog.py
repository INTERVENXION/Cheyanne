"""统一的模型设置对话框

一次填完四样东西：
  服务商  —— 决定请求格式（OpenAI 兼容 / Anthropic 专用）
  接口地址 —— 用户可改（中转、私有部署、代理）
  模型名   —— 用户可改（各家命名规则不同，且经常变）
  API Key  —— 加密保存（DPAPI，见 secret_store）

## 为什么把"服务商"做成下拉而不是让用户自己判断格式

Claude 的请求格式和 OpenAI 完全不同（system 要提出来、认证用 x-api-key、
返回是 content 数组）。让用户自己填地址时"顺便"选对格式是不现实的，
所以下拉只负责选**协议**，地址仍然可编辑。

## 测试连接

改完配置最怕"看起来保存了但调不通"，所以这里提供一次最小请求：
发一句 "你好"、只要 16 个 token，几十秒内就能确认四要素是否都对。
"""
import requests
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from src import providers
from src.config_manager import (
    api_key_storage_info,
    clear_api_key,
    get_api_config,
    get_provider_settings,
    has_api_key,
    resolve_api_key,
    save_api_key,
    save_provider_settings,
)

_STYLE = """
QDialog { background: #faf7f2; }
QLabel#title   { color: #6b5b4b; font-size: 14px; font-weight: bold; }
QLabel#section { color: #8a7460; font-size: 12px; font-weight: bold; }
QLabel#hint    { color: #8c7b6b; font-size: 11px; }
QLabel#status  { color: #8c7b6b; font-size: 11px; }
QLineEdit, QComboBox {
    background: #ffffff;
    border: 1px solid #ddd2c4;
    border-radius: 6px;
    padding: 6px 8px;
    color: #4a3f36;
    selection-background-color: #e8b98c;
}
QComboBox::drop-down { border: none; width: 18px; }
QPushButton {
    background: #ffffff;
    border: 1px solid #ddd2c4;
    border-radius: 6px;
    padding: 6px 16px;
    color: #5a4c40;
}
QPushButton:hover { background: #f3ece3; }
QPushButton:disabled { color: #b8aca0; }
QPushButton#primary {
    background: #e8a87c;
    border: 1px solid #e8a87c;
    color: #ffffff;
}
QPushButton#primary:hover { background: #e09b6a; }
QFrame#sep { color: #e6ddd2; }
"""


class _TestWorker(QThread):
    """后台跑一次最小请求，避免卡住界面"""

    done = pyqtSignal(bool, str)

    def __init__(self, provider_id, api_config, parent=None):
        super().__init__(parent)
        self.provider_id = provider_id
        self.api_config = api_config

    def run(self):
        try:
            request = providers.build_request(
                self.provider_id, self.api_config,
                [{"role": "user", "content": "你好"}],
                16, 0.0,
            )
            resp = requests.post(
                request["url"], headers=request["headers"],
                json=request["json"], timeout=25,
            )
            if resp.status_code >= 400:
                try:
                    detail = providers.extract_error(self.provider_id, resp.json())
                except ValueError:
                    detail = providers.extract_error(self.provider_id, resp.text)
                self.done.emit(False, f"HTTP {resp.status_code}：{detail[:160]}")
                return

            data = resp.json()
            if not providers.looks_valid(self.provider_id, data):
                detail = providers.extract_error(self.provider_id, data)
                self.done.emit(False, f"返回结构与所选服务商不符：{detail[:160]}")
                return

            text = providers.parse_response(self.provider_id, data)
            self.done.emit(True, f"连接成功，返回：{text[:40] or '(空回复)'}")
        except requests.exceptions.Timeout:
            self.done.emit(False, "请求超时：检查网络或接口地址")
        except requests.exceptions.ConnectionError:
            self.done.emit(False, "无法连接：检查接口地址是否正确")
        except Exception as e:
            self.done.emit(False, f"{type(e).__name__}: {e}")


class SettingsDialog(QDialog):
    """设置服务商 / 接口地址 / 模型 / API Key"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置 API Key 与模型")
        self.setModal(True)
        self.setMinimumWidth(600)
        self.setStyleSheet(_STYLE)

        self._loading = True
        self._test_worker = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(10)

        title = QLabel("模型设置")
        title.setObjectName("title")
        layout.addWidget(title)

        # ---------------- 服务商 / 地址 / 模型 ----------------
        model_section = QLabel("模型服务")
        model_section.setObjectName("section")
        layout.addWidget(model_section)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        grid.addWidget(QLabel("服务商"), 0, 0)
        self.provider_combo = QComboBox(self)
        for pid, name in providers.list_providers():
            self.provider_combo.addItem(name, pid)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        grid.addWidget(self.provider_combo, 0, 1)

        grid.addWidget(QLabel("接口地址"), 1, 0)
        self.endpoint_edit = QLineEdit(self)
        self.endpoint_edit.setPlaceholderText("https://.../chat/completions")
        grid.addWidget(self.endpoint_edit, 1, 1)

        grid.addWidget(QLabel("模型名"), 2, 0)
        self.model_edit = QLineEdit(self)
        self.model_edit.setPlaceholderText("例如 gemini-2.0-flash")
        grid.addWidget(self.model_edit, 2, 1)

        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)

        self.provider_hint = QLabel("")
        self.provider_hint.setObjectName("hint")
        self.provider_hint.setWordWrap(True)
        layout.addWidget(self.provider_hint)

        line1 = QFrame()
        line1.setObjectName("sep")
        line1.setFrameShape(QFrame.HLine)
        layout.addWidget(line1)

        # ---------------- API Key ----------------
        key_section = QLabel("API Key")
        key_section.setObjectName("section")
        layout.addWidget(key_section)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.key_edit = QLineEdit(self)
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("sk-...")
        self.key_edit.setClearButtonEnabled(True)
        row.addWidget(self.key_edit, 1)

        self.show_check = QCheckBox("显示")
        self.show_check.toggled.connect(self._on_toggle_echo)
        row.addWidget(self.show_check)
        layout.addLayout(row)

        self.status_label = QLabel("")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        line2 = QFrame()
        line2.setObjectName("sep")
        line2.setFrameShape(QFrame.HLine)
        layout.addWidget(line2)

        # ---------------- 按钮 ----------------
        buttons = QHBoxLayout()
        buttons.setSpacing(8)

        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._on_test)
        buttons.addWidget(self.test_btn)

        self.clear_btn = QPushButton("清除 Key")
        self.clear_btn.clicked.connect(self._on_clear_key)
        buttons.addWidget(self.clear_btn)

        buttons.addStretch(1)

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setObjectName("primary")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._on_save)
        buttons.addWidget(save_btn)

        layout.addLayout(buttons)

        self._load_current()
        self._loading = False

    # ---------- 初始化 ----------

    def _load_current(self):
        api_config = get_api_config()
        saved = get_provider_settings()

        provider_id = providers.resolve_id(api_config.get("provider"))
        index = self.provider_combo.findData(provider_id)
        if index >= 0:
            self.provider_combo.setCurrentIndex(index)

        spec = providers.get(provider_id)
        self.endpoint_edit.setText(
            saved.get("endpoint") or api_config.get("endpoint") or spec["endpoint"]
        )
        self.model_edit.setText(
            saved.get("model") or api_config.get("model") or spec["model"]
        )
        self._update_hint(provider_id)

        key = resolve_api_key("")
        if key:
            self.key_edit.setText(key)

        self._refresh_status()

    def _refresh_status(self):
        info = api_key_storage_info()
        protection = info.get("protection")
        legacy_yaml_key = (not info.get("exists")) and has_api_key()

        if protection == "dpapi":
            state = "已用 Windows DPAPI 加密保存"
        elif protection == "plain":
            state = "⚠ 已保存，但当前系统不支持 DPAPI 加密"
        elif info.get("exists"):
            state = "⚠ 文件存在但无法解密（可能来自其他电脑或 Windows 账户）"
        else:
            state = "尚未保存 Key"

        if legacy_yaml_key:
            state += "；当前 Key 来自 config.yaml 明文，保存后会自动迁移"

        self.status_label.setText(f"{state}\n存储位置：{info.get('path')}")
        self.clear_btn.setEnabled(bool(info.get("exists")) or bool(resolve_api_key("")))

    def _update_hint(self, provider_id):
        self.provider_hint.setText(providers.get(provider_id).get("hint") or "")

    # ---------- 交互 ----------

    def _on_toggle_echo(self, visible):
        self.key_edit.setEchoMode(QLineEdit.Normal if visible else QLineEdit.Password)

    def _on_provider_changed(self, _index):
        if self._loading:
            return
        pid = self.provider_combo.currentData()
        spec = providers.get(pid)
        # 切换服务商时填入该家的默认地址与模型（用户随后仍可改）
        self.endpoint_edit.setText(spec["endpoint"])
        self.model_edit.setText(spec["model"])
        self._update_hint(pid)

    def _current_api_config(self):
        """用界面上的值拼一份临时配置，供保存/测试使用（不落盘）"""
        pid = self.provider_combo.currentData()
        api_config = get_api_config()
        api_config.update({
            "provider": pid,
            "endpoint": self.endpoint_edit.text().strip(),
            "model": self.model_edit.text().strip(),
            "api_key": self.key_edit.text().strip(),
        })
        return pid, api_config

    def _validate(self):
        pid, api_config = self._current_api_config()
        if not api_config["endpoint"]:
            QMessageBox.warning(self, "设置", "请填写接口地址。")
            return None
        if not api_config["endpoint"].startswith(("http://", "https://")):
            QMessageBox.warning(self, "设置",
                                "接口地址需要以 http:// 或 https:// 开头。")
            return None
        if not api_config["model"]:
            QMessageBox.warning(self, "设置", "请填写模型名。")
            return None
        if not api_config["api_key"]:
            QMessageBox.warning(self, "设置", "请填写 API Key。")
            return None
        return pid, api_config

    def _on_test(self):
        validated = self._validate()
        if not validated:
            return
        pid, api_config = validated

        self.test_btn.setEnabled(False)
        self.test_btn.setText("测试中…")
        self.status_label.setText("正在测试连接…")

        worker = _TestWorker(pid, api_config, self)
        worker.done.connect(self._on_test_done)
        worker.finished.connect(worker.deleteLater)
        self._test_worker = worker
        worker.start()

    def _on_test_done(self, ok, message):
        self.test_btn.setEnabled(True)
        self.test_btn.setText("测试连接")
        self._refresh_status()
        if ok:
            QMessageBox.information(self, "测试连接", message)
        else:
            QMessageBox.warning(self, "测试连接失败", message)

    def _on_save(self):
        validated = self._validate()
        if not validated:
            return
        pid, api_config = validated

        if not save_provider_settings(pid, api_config["endpoint"],
                                      api_config["model"]):
            QMessageBox.critical(
                self, "设置",
                "保存模型设置失败：无法写入程序目录。\n请确认目录可写后重试。",
            )
            return
        if not save_api_key(api_config["api_key"]):
            QMessageBox.critical(
                self, "设置",
                "保存 API Key 失败：无法写入加密存储。\n请确认程序目录可写后重试。",
            )
            return

        self.accept()

    def _on_clear_key(self):
        answer = QMessageBox.question(
            self, "清除 API Key",
            "确定要清除已保存的 API Key 吗？\n清除后夏安将无法对话，直到重新填写。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        clear_api_key()
        self.key_edit.clear()
        self._refresh_status()
        QMessageBox.information(self, "API Key", "已清除已保存的 API Key。")

    def closeEvent(self, event):
        # 测试线程还在跑时等它结束，避免 QThread 被回收导致崩溃
        worker = self._test_worker
        if worker is not None and worker.isRunning():
            worker.quit()
            worker.wait(2000)
        super().closeEvent(event)
