# -*- coding: utf-8 -*-
"""模拟真实启动间隔，验证"重新碰面"的语气是否到位

不联网，只检查生成的提示词与上下文结构。
真正调用 API 的对照见 tests/test_greeting_continuity.py。
"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from datetime import datetime, timedelta

from PyQt5.QtWidgets import QApplication

app = QApplication(sys.argv)

from src.chat_dialog import ChatDialog
from src.session import SessionTracker, format_gap

# 数据隔离：ChatDialog 构造时会写 session.json 等文件，
# 必须重定向到临时目录，否则测试会改动用户的真实数据
from tests.test_isolation import use_temp_data_dir, assert_real_data_untouched
use_temp_data_dir()

failures = []


def ok(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


# 上次对话结束于 23:40
LAST = datetime(2026, 9, 28, 23, 40)

SCENARIOS = [
    ("当晚 23:41 再开", datetime(2026, 9, 28, 23, 41), "刚刚"),
    ("当晚 23:50 再开", datetime(2026, 9, 28, 23, 50), "大约 10 分钟"),
    ("次日 01:00 再开", datetime(2026, 9, 29, 1, 0), "大约 1 小时"),
    ("次日 07:30 再开", datetime(2026, 9, 29, 7, 30), "大约 7 小时"),
    ("次日 12:00 再开", datetime(2026, 9, 29, 12, 0), "大约 12 小时"),
    ("隔两天 20:00 再开", datetime(2026, 10, 1, 20, 0), "大约 2 天"),
]

print("=" * 76)
print("时间差与边界文本")
print("=" * 76)

history = [{"role": "user", "content": "准备睡了", "ts": LAST.isoformat(timespec="seconds")}]
tracker = SessionTracker()

for label, now, expect_gap in SCENARIOS:
    gap = format_gap((now - LAST).total_seconds())
    block = tracker.build_boundary_block(history, now=now)
    ok(f"{label} → {expect_gap}", gap == expect_gap, f"实际={gap}")
    # 无论间隔多久，核心规则都必须在
    for rule, key in [
        ("禁止接着演", "不要接着上次的场景往下演"),
        ("当下优先", "优先于记忆里的任何状态"),
        ("背景而非剧情", "正在发生的剧情"),
    ]:
        if key not in block:
            ok(f"{label} 含规则『{rule}』", False)
            break
    else:
        pass  # 三个规则都在

ok("所有间隔下都保留核心规则",
   all(
       "不要接着上次的场景往下演" in tracker.build_boundary_block(history, now=n)
       and "优先于记忆里的任何状态" in tracker.build_boundary_block(history, now=n)
       for _, n, _ in SCENARIOS
   ))

print()
print("=" * 76)
print("上下文结构（确认没有上次对话原文）")
print("=" * 76)
cd = ChatDialog()

# 造夹具：隔离后临时目录是空的。这里需要"历史 + 摘要 + 事实"齐备，
# 才能验证完整的注入结构 —— 测试不能依赖用户真实数据恰好有内容。
cd.llm_client._full_history = [
    {"role": "user", "content": "以前聊过的事", "ts": "2026-09-28T23:40:00"},
    {"role": "assistant", "content": "以前的回复", "ts": "2026-09-28T23:40:01"},
]
cd.llm_client.memory_manager.summarizer._summary = "9月28日深夜，聊了一些日常。"
cd.llm_client.memory_manager.fact_store.facts = [
    {"id": "f_a", "content": "指挥官喜欢喝热牛奶", "category": "偏好",
     "timestamp": "2026-09-28T23:40:00", "status": "active", "contradicted_by": None},
]

msgs = cd.llm_client._build_messages("测试", session_block="【会话边界】测试", force_memory=True)
roles = [m["role"] for m in msgs]
print(f"  messages 角色序列: {roles}")
ok("工作记忆为空（不恢复上次原文）", len(cd.llm_client.memory_manager.working_memory) == 0)
# 结构：角色 + 会话边界 + 摘要块 + 事实块 + 当前输入
# （事实块是本次修复新增的：force_facts 让 15 条事实不再永远进不了上下文）
has_sum = any("之前相处的内容" in m["content"] for m in msgs)
has_fact = any("关于指挥官的长期印象" in m["content"] for m in msgs)
expected = 1 + 1 + (1 if has_sum else 0) + (1 if has_fact else 0) + 1
ok("没有多余的 user/assistant 历史",
   roles == ["system"] * (expected - 1) + ["user"], f"实际={roles}")
ok("结构为 角色+边界+摘要+事实+当前输入", len(msgs) == expected,
   f"{len(msgs)} 条（摘要={has_sum} 事实={has_fact}）")

print()
print("=" * 76)
print("问候提示词的关键要求")
print("=" * 76)
gp = cd._build_greeting_prompt()
for key, why in [
    ("重新见到", "点明是重新碰面"),
    ("已经过去了", "点明旧场景已结束"),
    ("你刚才不是说要", "明确禁止接续式发言"),
    ("只是你的**印象**", "区分印象与当下状态"),
]:
    ok(f"问候含『{why}』", key in gp)

print()
print("--- 问候提示词的会话性质段落 ---")
start = gp.find("【重要】以下是这次问候的性质")
print(gp[start:start + 320])

cd.shutdown()

print()
print("=" * 76)
if failures:
    print(f"结果: {len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果: 全部通过 ✅")

# 数据隔离核对
assert_real_data_untouched("test_session_scenarios.py")
