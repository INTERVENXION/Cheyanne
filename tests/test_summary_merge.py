# -*- coding: utf-8 -*-
"""回归测试：摘要合并不得丢掉用户已有的长期记忆

## 背景

用户从备份恢复了一份 **565 字符**的长期摘要（4 段用 `---` 累积而成）。

而 `Summarizer.merge()` 的兜底分支曾经这样写：

    room = 600 - len(new_summary)
    if room >= 100:
        combined = f"{old_summary[:room]}…\\n---\\n{new_summary}"
    else:
        combined = new_summary          # ← 旧摘要整个丢弃

旧摘要一旦超过 500 字符（长期使用必然如此），`room` 就小于 100，
每次都走 else 分支把旧摘要**整个丢掉**。而兜底分支正是为
"网络抖动导致 LLM 合并失败"准备的 —— 所以这不是极端情况，是常态。
用户最早期、最基础的那段记忆会被优先牺牲掉。

同时 `_SUMMARY_CHAR_LIMIT = 300` 也让"正常路径"有同样风险：
LLM 返回的长摘要会被无条件裁到 300 字符。

## 本测试守住什么

1. 合并失败时，旧摘要与新摘要**都必须完整保留**
2. 正常路径的上限不能小于用户可能已有的摘要长度
3. 摘要超限只是变长，绝不静默截断用户内容
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src.memory.summarizer as sm

failures = []


def ok(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


# 造一段"长期使用后"的长摘要：4 段、565 字符量级
LONG_OLD = "\n---\n".join([
    "第一段：早期相处的基本情况，" * 6,
    "第二段：日常照顾与陪伴的细节，" * 6,
    "第三段：亲密关系的发展过程，" * 6,
    "第四段：最近的相处与约定，" * 6,
])
NEW = "新增一段摘要内容，" * 12

print("=" * 88)
print(f"模拟长期摘要: {len(LONG_OLD)} 字符 / 新增: {len(NEW)} 字符")
print("=" * 88)

_orig = sm.post_chat


def _fail(*a, **k):
    raise sm.LLMRequestError("模拟断网")


def _long(*a, **k):
    return "合并后的内容。" * 80


s = sm.Summarizer()

# ① 合并失败 → 兜底拼接必须两段都保留
sm.post_chat = _fail
try:
    combined = s.merge(LONG_OLD, NEW)
finally:
    sm.post_chat = _orig

ok("合并失败时不抛异常", combined is not None)
if combined:
    ok("旧摘要完整保留", LONG_OLD in combined,
       f"结果 {len(combined)} 字符")
    ok("新摘要完整保留", NEW in combined)
    ok("兜底结果不超上限", len(combined) <= sm._MERGE_FALLBACK_LIMIT,
       f"{len(combined)} <= {sm._MERGE_FALLBACK_LIMIT}")

# ② 合并成功但结果超长 → 必须被裁到正常上限内，且不报错
sm.post_chat = _long
try:
    merged = s.merge(LONG_OLD, NEW)
finally:
    sm.post_chat = _orig

ok("合并成功时结果在上限内", merged is not None and len(merged) <= sm._SUMMARY_CHAR_LIMIT,
   f"{len(merged) if merged else 'None'} <= {sm._SUMMARY_CHAR_LIMIT}")

# ③ 正常上限必须能容纳"长期累积"的摘要，否则每次合并都会静默缩水
ok("正常上限足够大（>= 用户长期摘要量级）", sm._SUMMARY_CHAR_LIMIT >= 600,
   f"当前 {sm._SUMMARY_CHAR_LIMIT}")
ok("兜底上限比正常上限更宽松", sm._MERGE_FALLBACK_LIMIT > sm._SUMMARY_CHAR_LIMIT,
   f"{sm._MERGE_FALLBACK_LIMIT} > {sm._SUMMARY_CHAR_LIMIT}")

# ④ 用磁盘上真实的摘要（若存在）做一次冒烟
real_path = os.path.join(ROOT, "memory_summary.json")
if os.path.isfile(real_path):
    with open(real_path, encoding="utf-8") as f:
        real = (json.load(f) or {}).get("summary") or ""
    if real.strip():
        ok("磁盘真实摘要长度在正常上限内",
           len(real) <= sm._SUMMARY_CHAR_LIMIT,
           f"{len(real)} <= {sm._SUMMARY_CHAR_LIMIT}")
        sm.post_chat = _fail
        try:
            got = s.merge(real, NEW)
        finally:
            sm.post_chat = _orig
        ok("磁盘真实摘要在兜底路径下不丢内容", real in got,
           f"真实 {len(real)} 字符 -> 拼接 {len(got)} 字符")

print()
print("=" * 88)
if failures:
    print(f"结果: {len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果: 摘要合并不丢记忆 ✅")
