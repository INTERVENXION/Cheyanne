# -*- coding: utf-8 -*-
"""验证会话边界的接入点（不联网）

检查：
1. 会话边界只在本次启动的第一轮 LLM 交互里注入一次
2. 启动问候拿到边界 + 记忆（问候没有用户输入，必须显式要求注入记忆）
3. 后续对话不再重复注入边界
4. 边界的措辞包含关键的"当下优先"规则
"""
import json
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication

app = QApplication(sys.argv)

from src.chat_dialog import ChatDialog, LLMWorker, SummaryWorker
from src.session import SessionTracker

# 数据隔离：ChatDialog 构造时会写 session.json 等文件，
# 必须重定向到临时目录，否则测试会改动用户的真实数据
from tests.test_isolation import use_temp_data_dir, assert_real_data_untouched
use_temp_data_dir()

# 问候是否真的发起请求，取决于有没有配置 API Key。
# 本测试验证的是"会话边界如何装配进问候"，与密钥无关；
# 而开源仓库里不会有任何真实 Key —— 所以这里把判定打桩为"已配置"，
# 保证在有 Key 和没 Key 的环境下行为一致。
import src.chat_dialog as cdmod
cdmod.has_api_key = lambda: True

failures = []


def ok(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


print("=" * 74)
print("会话边界的构建与消费")
print("=" * 74)

cd = ChatDialog()

# 造一份"有过去对话"的历史。
# 隔离后临时目录是空的，而空历史时 build_boundary_block 会返回 None
# （没有历史就谈不上"重新见面"）—— 测试必须自带夹具，
# 不能依赖用户真实数据恰好有内容。
cd.llm_client._full_history = [
    {"role": "user", "content": "以前的对话", "ts": "2026-09-28T23:40:00"},
    {"role": "assistant", "content": "以前回复", "ts": "2026-09-28T23:40:01"},
]

ok("存在 session_tracker", hasattr(cd, "session_tracker"))
ok("启动时记录了 last_start", cd.session_tracker._data.get("last_start") is not None)
ok("边界尚未消费", not cd._session_block_used)

# 第一次取用 —— 应该拿到内容
first = cd._get_session_block()
ok("第一次取到边界", bool(first))
ok("取用后标记为已消费", cd._session_block_used)

# 第二次取用 —— 应该拿到 None（避免每轮重复注入，浪费 token）
second = cd._get_session_block()
ok("第二次返回 None（只注入一次）", second is None)

print()
print("--- 边界文本内容 ---")
print(first)

print()
print("=" * 74)
print("边界措辞的关键规则")
print("=" * 74)
# 无论走哪个变体，这套规则都必须成立
ok("声明上次已结束", "已经结束" in first)
ok("声明记忆是背景而非剧情", "背景知识" in first and "正在发生的剧情" in first)
ok("禁止接着演", "不要接着上次的场景往下演" in first)
ok("当下优先于记忆", "优先于记忆里的任何状态" in first)

print()
print("--- 变体 1：完全没有时间基准（全新用户 / 时间戳与 session 都缺失）---")
blank = SessionTracker()
blank._data = {}          # 清空，模拟没有任何时间记录的存档
legacy = blank.build_boundary_block([{"role": "user", "content": "以前的对话"}])
print(legacy)
ok("老存档变体说明是过去的事", "过去某个时间" in legacy)
ok("老存档变体不编造具体间隔", "上一次说话是在" not in legacy)
ok("老存档变体仍禁止接着演", "不要接着上次的场景往下演" in legacy)
ok("老存档变体仍声明当下优先", "优先于记忆里的任何状态" in legacy)

print()
print("--- 变体 2：session.json 里只有 last_start（程序被强杀后的情况）---")
killed = SessionTracker()
killed._data = {"last_start": "2026-09-28T23:40:00"}
recovered = killed.build_boundary_block([{"role": "user", "content": "以前的对话"}])
ok("强杀后仍能判断有时间间隔", "上一次说话是在" in recovered)
ok("强杀后不退回极简变体", "不要接着上次的场景往下演" in recovered)
print(next(l for l in recovered.split("\n") if "上一次说话是在" in l))

print()
print("=" * 74)
print("启动问候的接入")
print("=" * 74)


def make_dialog_with_history():
    """造一个"有过去对话"的 ChatDialog

    隔离后临时目录是空的，而空历史时 build_boundary_block 返回 None。
    所有需要边界的用例都必须自带夹具。
    """
    d = ChatDialog()
    d.llm_client._full_history = [
        {"role": "user", "content": "以前的对话", "ts": "2026-09-28T23:40:00"},
        {"role": "assistant", "content": "以前的回复", "ts": "2026-09-28T23:40:01"},
    ]
    return d


# 重建一个干净的 dialog，拦截 worker.start 以免联网
cd2 = make_dialog_with_history()
started = []
for cls in (LLMWorker, SummaryWorker):
    cls.start = lambda self, _cls=cls: started.append(self)

cd2.generate_startup_greeting()
ok("问候 worker 已登记", len(cd2._workers) == 1, f"{len(cd2._workers)} 个")

worker = next(iter(cd2._workers))
ok("问候 worker 带会话边界", worker.session_block is not None)
ok("问候 worker 要求注入记忆", worker.force_memory is True)
ok("问候不写历史", worker.save_history is False)

# 问候**不消费**边界额度：
# 问候是唯一"只凭记忆说话"的场合，必须始终有时间锚点；
# 若用户在 1 秒问候延迟内抢先发消息，边界会被那条消息消费掉，
# 导致问候在"没有时间锚点"的情况下读旧记忆 —— 正是割裂感的成因。
ok("问候不消费边界额度", not cd2._session_block_used,
   "（保证问候一定有边界）")

# 第一条用户消息是独立的一次 API 调用，应同样带上边界
worker_msg = LLMWorker(cd2.llm_client, "测试", session_block=cd2._get_session_block())
ok("第一条用户消息也带边界", worker_msg.session_block is not None)

# 之后才不再重复注入
worker2 = LLMWorker(cd2.llm_client, "测试", session_block=cd2._get_session_block())
ok("后续消息不再带边界", worker2.session_block is None,
   "（每次启动只注入一次，避免每轮浪费 token）")

# 用户抢先发消息时，问候仍必须拿到边界
cd3 = make_dialog_with_history()
cd3.input_field.setText("抢先")
cd3._send_message()                       # 用户先消费掉额度
ok("用户抢先时额度已被消费", cd3._session_block_used)
greeting_block = cd3._greeting_session_block()
ok("用户抢先时问候仍拿到边界", greeting_block is not None)
cd3.shutdown()

print()
print("=" * 74)
print("记忆注入姿态（不通过网络，直接看组装出的 messages）")
print("=" * 74)
# 需要存在摘要，"记得"姿态的措辞才会被注入
cd2.llm_client.memory_manager.summarizer._summary = "9月28日深夜，聊了一些日常。"
msgs = cd2.llm_client._build_messages(
    "", session_block=first, force_memory=True
)
contents = [m["content"] for m in msgs if m["role"] == "system"]
joined = "\n".join(contents)
ok("注入了会话边界", any("会话边界" in c for c in contents))
ok("注入了记忆且姿态为「记得」", any("你记得的事" in c for c in contents))
ok("不再出现旧措辞「自然地融入这些信息」", "自然地融入这些信息" not in joined)
ok("记忆里说明是往事而非当前场景", "而不是「正在发生的事」" in joined)

print()
print("--- 组装出的 system 消息顺序 ---")
for i, m in enumerate(msgs[:5]):
    head = m["content"][:46].replace("\n", " ")
    print(f"  [{i}] {m['role']:9} {head}...")

print()
print("=" * 74)
print("边界的时间测算（用带 ts 的模拟历史）")
print("=" * 74)
from datetime import datetime, timedelta
from src.session import SessionTracker

t = SessionTracker()
fake_history = [
    {"role": "user", "content": "准备睡了",
     "ts": datetime(2026, 9, 28, 23, 40).isoformat(timespec="seconds")},
]
block = t.build_boundary_block(fake_history, now=datetime(2026, 9, 29, 12, 0))
ok("带时间戳的历史能算出时间差", "距今大约 12 小时" in block)
ok("能识别出『今天/昨天』", "2026年09月28日 23:40" in block)
ok("有时间戳时给出时间基准", "上一次说话是在" in block)
# 两种变体共享同一套规则 —— 这是之前修复过的一个真实缺陷
ok("两种变体都禁止接着演", "不要接着上次的场景往下演" in block and "不要接着上次的场景往下演" in first)
ok("两种变体都声明当下优先", "优先于记忆里的任何状态" in block and "优先于记忆里的任何状态" in first)
print()
print(block)

cd.shutdown()
cd2.shutdown()

print()
print("=" * 74)
if failures:
    print(f"结果: {len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果: 会话边界接入正常 ✅")

# 数据隔离核对
assert_real_data_untouched("test_session.py")
