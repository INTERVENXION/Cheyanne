# -*- coding: utf-8 -*-
"""首次启动提示 / 填完 key 补问候 的 GUI 回归测试

用户要求的行为：
  - 没有配置 API Key 时，夏安在对话框里说出指定的一句提示
  - 此时**不能**去请求 API（必然失败，只会弹内部错误）
  - 在设置里填好 key 之后，不需要重启就能补上一次问候

运行：python tests/test_api_key_ui.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication
from PyQt5.QtTest import QTest

app = QApplication.instance() or QApplication(sys.argv)

import src.chat_dialog as cdmod
from src.chat_dialog import NO_API_KEY_MESSAGE, ChatDialog
from tests.test_isolation import assert_real_data_untouched, use_temp_data_dir

# 构造 ChatDialog 会读写 history/summary/fact/session —— 先重定向
use_temp_data_dir()

_passed = 0
_failed = []


def ok(name, cond, detail=""):
    global _passed
    if cond:
        _passed += 1
        print(f"[PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        _failed.append(name)
        print(f"[FAIL] {name}" + (f"  ({detail})" if detail else ""))


def make_dialog():
    """构造一个不显示、不联网的 ChatDialog"""
    dlg = ChatDialog()
    recorded = []
    dlg._add_bubble = lambda text, is_user=False: recorded.append((text, is_user))
    dlg.show = lambda: None
    dlg.raise_ = lambda: None
    dlg.position_near = lambda geo: None
    return dlg, recorded


_orig_has_key = cdmod.has_api_key
_orig_start = cdmod.LLMWorker.start

try:
    print("=" * 74)
    print("1. 未配置 API Key 时的首次提示")
    print("=" * 74)
    cdmod.has_api_key = lambda: False
    dlg, recorded = make_dialog()

    # 记录是否调用了 chat（不应该）
    calls = []
    dlg.llm_client.chat = lambda *a, **k: calls.append(1) or ("idle", "")

    dlg.generate_startup_greeting(None)

    ok("说出了指定的提示语",
       recorded == [(NO_API_KEY_MESSAGE, False)],
       f"实际 {recorded}")
    ok("提示语内容与要求一致",
       recorded and recorded[0][0] == (
           "指挥官，您好像还没有设置我的apikey，"
           "请右击我，在设置apikey中填写您的apikey。"
       ))
    ok("以夏安（非用户）气泡形式发出",
       recorded and recorded[0][1] is False)
    ok("没有发起 LLM 请求", not calls and not getattr(dlg, "greeting_worker", None))
    ok("标记为已走过问候流程（避免重复）", dlg._greeted_once is True)

    dlg.shutdown()
    dlg.close()

    print()
    print("=" * 74)
    print("2. 已配置 API Key 时正常问候")
    print("=" * 74)
    cdmod.has_api_key = lambda: True
    started = []
    cdmod.LLMWorker.start = lambda self: started.append(True)

    dlg2, recorded2 = make_dialog()
    dlg2._add_bubble = lambda text, is_user=False: recorded2.append((text, is_user))
    dlg2.generate_startup_greeting(None)

    ok("不出现未配置提示",
       all(text != NO_API_KEY_MESSAGE for text, _ in recorded2))
    ok("启动了问候线程", bool(started))
    ok("持有 greeting_worker 引用",
       getattr(dlg2, "greeting_worker", None) is not None)

    print()
    print("=" * 74)
    print("3. 填完 key 后无需重启即可补问候")
    print("=" * 74)
    greeted = []
    dlg2._greeting_succeeded = False          # 模拟之前因缺 key 没能问候
    dlg2.generate_startup_greeting = lambda geo=None: greeted.append(geo)

    dlg2.on_api_key_updated()
    QTest.qWait(600)                          # 等 300ms 的 singleShot 触发

    ok("给出保存成功提示",
       any("API Key 已保存" in text for text, _ in recorded2),
       str(recorded2[-1] if recorded2 else None))
    ok("补了一次启动问候", len(greeted) == 1, f"{len(greeted)} 次")

    print()
    print("=" * 74)
    print("4. 已经问候过时，改 key 不重复问候")
    print("=" * 74)
    dlg3, recorded3 = make_dialog()
    dlg3._greeting_succeeded = True
    re_greet = []
    dlg3.generate_startup_greeting = lambda geo=None: re_greet.append(geo)

    dlg3.on_api_key_updated()
    QTest.qWait(600)

    ok("仍给出保存提示",
       any("API Key 已保存" in text for text, _ in recorded3))
    ok("不重复问候", len(re_greet) == 0, f"{len(re_greet)} 次")

    dlg3.shutdown()
    dlg3.close()

finally:
    cdmod.has_api_key = _orig_has_key
    cdmod.LLMWorker.start = _orig_start

assert_real_data_untouched()

print()
print("=" * 74)
if _failed:
    print(f"结果: {len(_failed)} 项失败 -> {_failed}")
    sys.exit(1)
print(f"结果: 首次启动提示与补问候全部通过 ✅（{_passed} 项）")
