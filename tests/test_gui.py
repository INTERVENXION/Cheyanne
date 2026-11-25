# -*- coding: utf-8 -*-
"""验证 GUI 构造链（离屏模式，不弹窗）

检查 PetWindow / ChatDialog / SpriteManager 能否正常构造，
以及状态同步、表情定时器、线程登记、节假日逻辑是否就绪。

本测试**不联网、不写任何数据文件** —— 所有会触发 API 调用的路径
都被替换为空实现，测试结束后会校验数据文件指纹未变。

运行：python tests/test_gui.py
"""
import hashlib
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"

# 项目根目录（tests/ 的上一级）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 数据文件指纹 —— 测试开始前记录，结束后比对
_DATA_FILES = ("chat_history.json", "memory_summary.json", "fact_store.json")


def _fingerprint():
    result = {}
    for name in _DATA_FILES:
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                result[name] = hashlib.sha256(f.read()).hexdigest()
    return result


_fp_before = _fingerprint()

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import Qt

app = QApplication(sys.argv)

from src.pet_window import PetWindow
from src.sprite_manager import SpriteManager

# 数据隔离：PetWindow/ChatDialog 构造时会写 session.json 等文件，
# 必须重定向到临时目录，否则测试会改动用户的真实数据
from tests.test_isolation import use_temp_data_dir, assert_real_data_untouched
use_temp_data_dir()

failures = []


def ok(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


print("=" * 72)
print("SpriteManager")
print("=" * 72)
sm = SpriteManager(sprite_size=280)
print("  已加载立绘:", sorted(sm.sprites.keys()))
ok("全部 8 个情绪立绘就绪", len(sm.sprites) == 8, f"{len(sm.sprites)} 个")
ok("初始情绪为 idle", sm.current_emotion == "idle")

sm.mark_displayed("shy")
ok("mark_displayed 生效", sm.current_emotion == "shy")
sm.mark_displayed("nonexistent_emotion")
ok("未知情绪回落到 idle（状态一致性修复）", sm.current_emotion == "idle")

print()
print("=" * 72)
print("PetWindow")
print("=" * 72)
pet = PetWindow()
ok("窗口尺寸 280x280", pet.width() == 280 and pet.height() == 280)
ok("窗口透明", pet.testAttribute(Qt.WA_TranslucentBackground))
ok("置顶", bool(pet.windowFlags() & Qt.WindowStaysOnTopHint))
ok("ChatDialog 已创建", pet.chat_dialog is not None)

cd = pet.chat_dialog
ok("表情回落定时器存在", hasattr(cd, "_emotion_timer"))
ok("定时器为单次触发", cd._emotion_timer.isSingleShot())
ok("定时器间隔 6000ms", cd._emotion_timer.interval() == 6000)
ok("定时器初始未启动", not cd._emotion_timer.isActive())
ok("线程登记表存在", isinstance(cd._workers, set))

print()
print("=" * 72)
print("情绪定时器行为（核心 bug 修复验证）")
print("=" * 72)
# 模拟：第一条回复 → 定时器启动
cd._set_emotion("happy")
ok("回复后定时器启动", cd._emotion_timer.isActive())

# 模拟：6 秒内又收到一条回复，应取消旧定时器并重新计时
cd._set_emotion("shy")
ok("新回复后定时器仍在运行（已重新计时）", cd._emotion_timer.isActive())

# 直接触发超时回调，确认会发出 idle
seen = []
cd.emotion_changed.connect(lambda e: seen.append(e))
cd._emotion_timer.timeout.emit()
ok("超时后发出 idle", seen[-1] == "idle", f"信号序列={seen}")

# idle 不应再启动定时器
seen.clear()
cd._set_emotion("idle")
ok("idle 不再启动定时器", not cd._emotion_timer.isActive())

print()
print("=" * 72)
print("后台线程登记（回归：SummaryWorker 没有 error 信号导致的崩溃）")
print("=" * 72)
from PyQt5.QtCore import QThread, pyqtSignal
from src.chat_dialog import LLMWorker, SummaryWorker

# 信号定义必须与 _track_worker 的假设一致
ok("LLMWorker 有 finished", hasattr(LLMWorker, "finished"))
ok("LLMWorker 有 error", hasattr(LLMWorker, "error"))
ok("SummaryWorker 有 finished", hasattr(SummaryWorker, "finished"))
ok("SummaryWorker 有 error", hasattr(SummaryWorker, "error"))


class _FakeWorker(QThread):
    """信号签名与真实 worker 一致的测试替身

    刻意不启动线程、不联网、不写文件 —— 只验证登记与自动移除逻辑。
    之前这里用的是真实 SummaryWorker，会真的触发一次记忆整理并把
    摘要/事实写进用户的 memory_summary.json / fact_store.json，
    测试绝不能有这种副作用。
    """

    finished = pyqtSignal(object)
    error = pyqtSignal(str)


def _fake_without_error():
    """构造一个只有 finished、没有 error 的 worker

    这正是当初触发 AttributeError 的形态，
    用来锁定 _track_worker 必须容忍缺失的 error 信号。
    """
    cls = type("_NoErrorWorker", (QThread,), {"finished": pyqtSignal()})
    return cls()


# 逐个走一遍登记流程 —— 这正是之前抛 AttributeError 的路径
w_llm = LLMWorker(cd.llm_client, "测试消息")
w_sum = SummaryWorker(cd.llm_client)
w_noerr = _fake_without_error()

for label, w in (
    ("LLMWorker", w_llm),
    ("SummaryWorker", w_sum),
    ("无 error 信号的 worker", w_noerr),
):
    try:
        cd._track_worker(w)
        ok(f"{label} 登记成功", w in cd._workers)
    except AttributeError as e:
        ok(f"{label} 登记成功", False, f"AttributeError: {e}")

ok("共登记 3 个", len(cd._workers) == 3, f"{len(cd._workers)} 个")

# 按各自签名触发 finished，应逐个自动从登记表移除
w_llm.finished.emit("happy", "测试回复")   # finished(str, str)
ok("LLMWorker finished 后移除", w_llm not in cd._workers)
w_sum.finished.emit()                      # finished()
ok("SummaryWorker finished 后移除", w_sum not in cd._workers)
ok("仅剩无 error 信号的 worker", len(cd._workers) == 1, f"剩余 {len(cd._workers)}")

# error 信号（带参数）也应能正常触发移除
w_err = LLMWorker(cd.llm_client, "测试消息")
cd._track_worker(w_err)
w_err.error.emit("模拟错误")
ok("error 触发后自动移除", w_err not in cd._workers)

# 清空登记表，避免影响后续用例
cd._workers.clear()

print()
print("=" * 72)
print("_maybe_summarize 路径（回归：启动问候后触发的崩溃）")
print("=" * 72)
# 关键：把 start() 换成空实现。
# _maybe_summarize 里真正会崩的是 _track_worker（登记阶段），
# 与线程是否真的跑起来无关；而不启动线程就不会联网、不会改数据文件。
started = []
for cls in (SummaryWorker, LLMWorker):
    cls.start = lambda self, _cls=cls: started.append(type(self).__name__)

_orig = type(cd.llm_client).has_pending_summary
type(cd.llm_client).has_pending_summary = property(lambda self: True)
try:
    cd._maybe_summarize()
    ok("_maybe_summarize 未抛异常", True)
    ok("已登记摘要线程", len(cd._workers) == 1, f"{len(cd._workers)} 个")
    # start() 被调用了（走到这一步说明整条路径通了），
    # 但被替换成空实现，所以真正的 QThread 从未运行 —— 不联网、不写文件
    ok("start 被调用一次", started == ["SummaryWorker"], f"{started}")
    ok("线程未真正运行（无副作用）",
       all(not w.isRunning() for w in cd._workers))

    # 重复调用不应堆积多个摘要线程
    cd._maybe_summarize()
    ok("不重复启动摘要线程", len(cd._workers) == 1, f"{len(cd._workers)} 个")
finally:
    type(cd.llm_client).has_pending_summary = _orig
    cd._workers.clear()

print()
print("=" * 72)
print("节假日问候逻辑")
print("=" * 72)
from datetime import datetime
from src.config_manager import load_yaml

for label, dt in [
    ("2026-02-16 除夕", datetime(2026, 2, 16, 20, 0)),
    ("2026-02-17 春节", datetime(2026, 2, 17, 9, 0)),
    ("2026-09-25 中秋", datetime(2026, 9, 25, 20, 0)),
    ("2026-04-08 夏安日", datetime(2026, 4, 8, 12, 0)),
    ("2026-03-11 普通日", datetime(2026, 3, 11, 12, 0)),
]:
    hit = cd._check_special_date(dt)
    print(f"  {label:22} -> {hit['name'] if hit else '(无)'}")

ok("除夕识别正确", (cd._check_special_date(datetime(2026, 2, 16, 20, 0)) or {}).get("name") == "除夕")
ok("中秋识别正确", (cd._check_special_date(datetime(2026, 9, 25, 20, 0)) or {}).get("name") == "中秋节")
ok("夏安日识别正确", (cd._check_special_date(datetime(2026, 4, 8, 12, 0)) or {}).get("name") == "夏安日")
ok("普通日不误报", cd._check_special_date(datetime(2026, 3, 11, 12, 0)) is None)

# 开关生效
cfg = load_yaml("holidays.yaml")
cfg["special_greeting"] = False
ok("special_greeting=false 时关闭", cd._check_special_date(datetime(2026, 2, 17, 9, 0), cfg) is None)

print()
print("=" * 72)
print("问候 prompt 构建（开关生效）")
print("=" * 72)
p = cd._build_greeting_prompt()
ok("prompt 非空", bool(p))
ok("包含 JSON 格式要求", "emotion" in p and "reply" in p)
print("  --- prompt 片段 ---")
for line in p.split("\n")[:4]:
    print("  " + line)

print()
print("=" * 72)
print("退出收尾")
print("=" * 72)
cd.shutdown()   # 无运行中线程，应立即返回
ok("shutdown 可安全调用", True)
pet.close()

print()
print("=" * 72)
print("无副作用校验")
print("=" * 72)
_fp_after = _fingerprint()
if _fp_before == _fp_after:
    ok("数据文件未被测试改动", True, f"{len(_fp_after)} 个文件指纹一致")
else:
    changed = [k for k in _fp_after if _fp_before.get(k) != _fp_after.get(k)]
    ok("数据文件未被测试改动", False, f"被改动: {changed}")

# 也核对隔离辅助模块记录的指纹（覆盖 session.json）
if not assert_real_data_untouched("GUI 测试"):
    failures.append("用户数据被改动")

print()
print("=" * 72)
if failures:
    print(f"结果: {len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果: GUI 构造链全部正常 ✅")
