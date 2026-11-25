# -*- coding: utf-8 -*-
"""用真实代码路径跑一次启动问候，把实际会显示给用户的文字打出来

走的是 ChatDialog 的原装逻辑（_build_greeting_prompt + 会话边界 + 记忆注入），
只是最后直接调用 API 并打印，而不是塞进气泡 ——
因为打包后的 exe 是 windowed 模式，看不到 stdout。

⚠️ 这是**手动诊断脚本**，不属于回归测试：
   - 它读取用户真实的 chat_history.json / memory_summary.json（有意为之，
     要的就是真实上下文）
   - 会调用真实 API（会产生费用）
   - 因此不要把它加进常规测试轮次

   为了不污染真实数据，这里把 session.json 重定向到临时目录，
   同时禁用历史写入。
"""
import os
import sys

os.environ["QT_QPA_PLATFORM"] = "offscreen"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication

app = QApplication(sys.argv)

from src.api import post_chat, LLMRequestError
from src.chat_dialog import ChatDialog
from src.llm_client import _parse_emotion_json, _sanitize_reply

# 只重定向 session.json（避免写用户的会话记录），
# 但保留真实的历史/摘要/事实 —— 本脚本的目的就是看真实上下文下的问候效果
import src.session as _sessmod
import tempfile
_sessmod.SESSION_FILE = os.path.join(tempfile.gettempdir(), "m200_greeting_live_session.json")

cd = ChatDialog()
# 双保险：本次诊断绝不写历史
cd.llm_client._save_full_history = lambda: True

print("=" * 78)
print("距上次见面的时间基准")
print("=" * 78)
tracker = cd.session_tracker
print(f"  session.json 内容      : {tracker._data}")
print(f"  历史最后一条消息时间   : {tracker.last_conversation_end(cd.llm_client._full_history)}")
print(f"  采用的基准 (previous)  : {tracker.previous_end}")

from src.session import format_gap
gap = format_gap(tracker.elapsed_since_last_seen(cd.llm_client._full_history))
print(f"  距上次见面             : {gap}")

print()
print("=" * 78)
print("实际发给模型的内容")
print("=" * 78)
greeting_prompt = cd._build_greeting_prompt()
session_block = cd._get_session_block()
messages = cd.llm_client._build_messages(
    greeting_prompt, session_block=session_block, force_memory=True
)
for i, m in enumerate(messages):
    c = m["content"]
    tag = f"[{i}] {m['role']}"
    if m["role"] == "system":
        print(f"{tag:14} ({len(c):4} 字) {c[:70].replace(chr(10), ' ')}...")
    else:
        print(f"{tag:14} ({len(c):4} 字) {c[:70].replace(chr(10), ' ')}...")

print()
print("=" * 78)
print("夏安的实际问候")
print("=" * 78)
try:
    raw = post_chat(messages, temperature=0.8)
except LLMRequestError as e:
    print(f"请求失败: {e.message}")
    sys.exit(1)

emotion, reply = _parse_emotion_json(raw)
clean = _sanitize_reply(reply.strip()) if reply else ""
print(f"  emotion : {emotion}")
print(f"  问候语  : {clean}")
print()

# 简单检查是否残留"接续"痕迹
import re
tell = []
for pat, why in [
    (r"你(不是)?(刚|才)(说|讲)", "把旧约定当成当下"),
    (r"(起来|下床|坐起来)", "表现成刚被叫起来"),
    (r"继续睡|接着睡|快睡|赶紧睡", "催促睡觉"),
]:
    if re.search(pat, clean or ""):
        tell.append(why)
print(f"  接续痕迹: {tell if tell else '无 ✅'}")

cd.shutdown()
