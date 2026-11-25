# -*- coding: utf-8 -*-
"""对比测试：当前 model 的端到端表现（不写入任何历史文件）"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api import post_chat, LLMRequestError
from src.config_manager import get_api_config, get_prompt_config
from src.llm_client import _parse_emotion_json, _sanitize_reply, _truncate_reply, _reply_char_limit

cfg = get_api_config()
print("=" * 74)
print("当前配置")
print("=" * 74)
print(f"  model       = {cfg.get('model')}")
print(f"  thinking    = {cfg.get('thinking')}")
print(f"  temperature = {cfg.get('temperature')}")
print(f"  max_tokens  = {cfg.get('max_tokens')}")
print(f"  api_key     = {str(cfg.get('api_key'))[:8]}...（未改动）")

system_prompt = get_prompt_config().get("system_prompt", "")

CASES = [
    ("普通闲聊", "指挥官：夏安，今天过得怎么样？"),
    ("情绪测试", "指挥官：夏安，你今天真好看，我特别喜欢你这个样子。"),
]

results = []
for label, user_msg in CASES:
    print()
    print("=" * 74)
    print(f"{label}：{user_msg}")
    print("=" * 74)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]
    start = time.time()
    try:
        raw = post_chat(messages)
    except LLMRequestError as e:
        print(f"  请求失败 [{e.kind}]: {e.message}")
        results.append((label, None, None, None))
        continue
    elapsed = time.time() - start

    emotion, reply = _parse_emotion_json(raw)
    clean = _sanitize_reply(reply.strip()) if reply else ""
    final = _truncate_reply(clean, max_chars=_reply_char_limit(user_msg))

    print(f"  耗时   : {elapsed:.2f} 秒")
    print(f"  emotion: {emotion}")
    print(f"  reply  : {final}  ({len(final)} 字，上限 {_reply_char_limit(user_msg)})")
    results.append((label, emotion, final, elapsed))

print()
print("=" * 74)
print("汇总")
print("=" * 74)
bad = 0
valid = {"happy", "sad", "angry", "surprised", "shy", "neutral", "idle"}
for label, emotion, reply, elapsed in results:
    if emotion is None:
        print(f"  [FAIL] {label}：请求失败")
        bad += 1
        continue
    problems = []
    if emotion not in valid:
        problems.append(f"emotion 非法({emotion})")
    if not reply:
        problems.append("reply 为空")
    status = "OK  " if not problems else "FAIL"
    if problems:
        bad += 1
    print(f"  [{status}] {label}  emotion={emotion}  {elapsed:.2f}s  {len(reply)}字"
          + (f"  <- {', '.join(problems)}" if problems else ""))

print()
print("结果:", f"{bad} 项失败" if bad else "全部正常 ✅")
sys.exit(1 if bad else 0)
