# -*- coding: utf-8 -*-
"""验证工作记忆容量：配置可覆盖、非法值有兜底、溢出行为正确

不联网、不写数据文件。做法是注入假的 requests / yaml，
再把 get_memory_config 打桩成不同取值，观察 MemoryManager 的容量。
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# --- 假 requests（summarizer / fact_store 在模块级 import 它）---
fake_requests = types.ModuleType("requests")
exc = types.ModuleType("requests.exceptions")


class RequestException(Exception):
    pass


for name in ("Timeout", "ConnectionError", "HTTPError"):
    setattr(exc, name, type(name, (RequestException,), {}))
fake_requests.exceptions = exc
fake_requests.post = lambda *a, **k: None
sys.modules["requests"] = fake_requests
sys.modules["requests.exceptions"] = exc

# --- 假 yaml ---
fake_yaml = types.ModuleType("yaml")
fake_yaml.safe_load = lambda text: {}
fake_yaml.YAMLError = type("YAMLError", (Exception,), {})
sys.modules["yaml"] = fake_yaml

import src.memory.memory_manager as mmod

failures = []


def ok(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def build(cfg):
    """用给定 memory 配置构造 MemoryManager"""
    mmod.get_memory_config = lambda: cfg
    return mmod.MemoryManager()


print("=" * 84)
print("1. 配置文件里的真实取值")
print("=" * 84)
import os
cfg_text = open(os.path.join(ROOT, "config.yaml"), encoding="utf-8-sig").read()
val = None
for line in cfg_text.splitlines():
    s = line.strip()
    if s.startswith("working_memory_turns:"):
        val = s.split(":", 1)[1].strip()
print(f"  config.yaml 中 working_memory_turns = {val}")
ok("配置为 100", val == "100", f"实际 {val!r}")

print()
print("=" * 84)
print("2. 配置生效")
print("=" * 84)
mm = build({"working_memory_turns": 100, "restore_working_memory": False})
ok("轮数 = 100", mm.working_memory_turns == 100)
ok("消息容量 = 200", mm.working_memory_size == 200, f"{mm.working_memory_size}")
ok("溢出批量 = 20", mm.overflow_batch == 20, f"{mm.overflow_batch}")

mm20 = build({"working_memory_turns": 20})
ok("设为 20 轮 -> 40 条", mm20.working_memory_size == 40)
ok("20 轮时溢出批量 = 20（窗口一半）", mm20.overflow_batch == 20,
   f"{mm20.overflow_batch}")

print()
print("=" * 84)
print("3. 配置缺失/非法时的兜底")
print("=" * 84)
CASES = [
    ("缺 key", {}, 100),
    ("None", {"working_memory_turns": None}, 100),
    ("字符串数字", {"working_memory_turns": "100"}, 100),
    ("0", {"working_memory_turns": 0}, 100),
    ("负数", {"working_memory_turns": -5}, 100),
    ("bool true", {"working_memory_turns": True}, 100),
    ("非数字字符串", {"working_memory_turns": "abc"}, 100),
    ("超大值", {"working_memory_turns": 999999}, 2000),
    ("1 轮", {"working_memory_turns": 1}, 1),
]
for label, cfg, expect in CASES:
    m = build(cfg)
    ok(f"{label} -> {expect} 轮", m.working_memory_turns == expect,
       f"实际 {m.working_memory_turns}")

print()
print("=" * 84)
print("4. 溢出行为：100 轮窗口")
print("=" * 84)
mm = build({"working_memory_turns": 100})
# 塞满到刚好 200 条
for i in range(100):
    mm.add_exchange({"role": "user", "content": f"u{i}"},
                    {"role": "assistant", "content": f"a{i}"})
ok("100 轮后正好 200 条且未溢出", len(mm.working_memory) == 200,
   f"{len(mm.working_memory)}")

overflow_job = mm.add_exchange({"role": "user", "content": "u100"},
                               {"role": "assistant", "content": "a100"})
ok("第 101 轮触发溢出", overflow_job is not None)
# add_exchange 现在返回统一任务对象 {"messages": ..., "is_archive": ..., ...}，
# 不再是裸消息列表
overflow = (overflow_job or {}).get("messages") if isinstance(overflow_job, dict) else overflow_job
ok("溢出 20 条", overflow is not None and len(overflow) == 20,
   f"{len(overflow) if overflow else None}")
ok("溢出后剩 182 条", len(mm.working_memory) == 182,
   f"{len(mm.working_memory)}")
ok("溢出的是最旧的", overflow is not None and overflow[0]["content"] == "u0",
   f"{overflow[0]['content'] if overflow else None}")
ok("运行时溢出不算归档", isinstance(overflow_job, dict)
   and overflow_job.get("is_archive") is False)
ok("最新的还在", mm.working_memory[-1]["content"] == "a100",
   f"{mm.working_memory[-1]['content']}")

print()
print("=" * 84)
print("5. 上下文注入量：200 条工作记忆能构造出正确 messages")
print("=" * 84)
mm = build({"working_memory_turns": 100})
for i in range(10):
    mm.add_exchange({"role": "user", "content": f"问题{i}"},
                    {"role": "assistant", "content": f"回答{i}"})
mm.summarizer._summary = "有一段摘要"
msgs = mm.build_context("新问题", session_block="【会话边界】测试")
roles = [m["role"] for m in msgs]

# 带 session_block 时会额外注入「记得的事」记忆块：
#   system + 会话边界 + 摘要块 +（可选）事实块 + 20 条工作记忆 + 当前输入
#
# 事实块是否存在取决于事实库里有没有内容 —— 干净检出（没有任何数据文件）时
# 事实库是空的，这一块就不会出现。所以这里不硬编码前缀长度，
# 而是按实际注入结果计算，保证在"有数据"和"干净仓库"两种环境下都能通过。
has_sum = any("之前相处的内容" in m["content"] for m in msgs)
has_fact = any("关于指挥官的长期印象" in m["content"] for m in msgs)
prefix = 1 + 1 + (1 if has_sum else 0) + (1 if has_fact else 0)
ok("结构 = system+边界+摘要(+事实)+20条记忆+输入",
   len(msgs) == prefix + 20 + 1 and has_sum,
   f"{len(msgs)} 条，前缀 {prefix}，摘要={has_sum} 事实={has_fact}")
ok("前几条都是 system", roles[:prefix] == ["system"] * prefix, f"{roles[:prefix]}")
ok("工作记忆紧接记忆块、按原顺序",
   msgs[prefix]["content"] == "问题0"
   and msgs[prefix + 19]["content"] == "回答9",
   f"{msgs[prefix]['content']} .. {msgs[prefix + 19]['content']}")
ok("最后一条是当前输入",
   msgs[-1]["role"] == "user" and msgs[-1]["content"] == "新问题")

# 不带 session_block、也不含记忆触发词时：
# 因为 memory.always_inject 默认 true，摘要现在**每轮都注入**（本次修复的核心）。
# 旧行为是"只有第一句注入"，正是导致"她每句话都失忆"的原因。
msgs2 = mm.build_context("今天天气不错")
prefix2 = 1 + (1 if any("之前相处的内容" in m["content"] for m in msgs2) else 0) \
           + (1 if any("关于指挥官的长期印象" in m["content"] for m in msgs2) else 0)
ok("普通闲聊也注入摘要（always_inject 默认开）",
   any("之前相处的内容" in m["content"] for m in msgs2)
   and len(msgs2) == prefix2 + 20 + 1, f"{len(msgs2)} 条，前缀 {prefix2}")

# 关掉 always_inject 应退回旧行为：不注入记忆块
mm_off = build({"working_memory_turns": 100, "always_inject": False})
for i in range(10):
    mm_off.add_exchange({"role": "user", "content": f"问题{i}"},
                        {"role": "assistant", "content": f"回答{i}"})
mm_off.summarizer._summary = "有一段摘要"
msgs_off = mm_off.build_context("今天天气不错")
ok("always_inject=false 时不注入记忆块",
   not any("之前相处的内容" in m["content"] for m in msgs_off)
   and not any("关于指挥官的长期印象" in m["content"] for m in msgs_off)
   and len(msgs_off) == 1 + 20 + 1, f"{len(msgs_off)} 条")

print()
print("=" * 84)
if failures:
    print(f"结果: {len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果: 工作记忆 100 轮配置生效、兜底安全、溢出正确 ✅")
