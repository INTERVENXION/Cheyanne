# -*- coding: utf-8 -*-
"""记忆覆盖与注入的回归测试

覆盖两个曾经导致"每次对话都失忆"的缺陷：

  缺陷 1：长期记忆（摘要 + 事实）只在会话第一句注入。
          原因是注入条件依附于 session_block，而它第一句就用完了。
          现象：用户说第二句时，夏安完全看不到过去。

  缺陷 2：摘要只覆盖 history[0 : 总条数 - 工作记忆窗口]。
          窗口 200 条时，166 条历史全部落在"不归档"区间，
          摘要四天没更新。现象：每次重启都像第一次见面。

运行：python tests/test_memory_coverage.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 必须先 import src.*，再重定向路径（见 test_isolation 的说明）
import src.llm_client as llmmod
import src.memory.fact_store as fsmod
import src.memory.memory_manager as mmn
import src.memory.summarizer as smod
from tests.test_isolation import assert_real_data_untouched, use_temp_data_dir

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


# ---------------------------------------------------------------- 桩：不联网
_calls = {"summarize": 0}


def _fake_summary_post(messages, **kwargs):
    """只服务摘要/合并调用，返回递增文本"""
    _calls["summarize"] += 1
    return f"第{_calls['summarize']}段摘要内容"


def _no_facts(self, messages):
    """事实提取返回空（模拟"这批没有可提取的事实"）"""
    return []


smod.post_chat = _fake_summary_post
fsmod.FactStore.extract_from_messages = _no_facts


def make_history(n):
    return [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": f"m{i}", "ts": "2026-10-22T10:00:00"}
        for i in range(n)
    ]


def reset_files():
    """清空临时数据文件，保证各段互不干扰"""
    for path in (smod.MEMORY_SUMMARY_FILE, fsmod.FACT_STORE_FILE,
                 llmmod.HISTORY_FILE):
        if os.path.exists(path):
            os.remove(path)


print("=" * 76)
print("1. 每轮都注入摘要与事实（缺陷 1）")
print("=" * 76)
reset_files()
mm = mmn.MemoryManager()
mm.summarizer._summary = "六月的一段旧记忆"
mm.fact_store.add_fact("指挥官喜欢红茶", category="喜好")

# 第 1 轮：带会话边界
msgs1 = mm.build_context("我想哭", session_block="【会话边界 — 4 天没见】")
has_sum1 = any("你和指挥官之前相处的内容" in m["content"] for m in msgs1)
has_fact1 = any("关于指挥官的长期印象" in m["content"] for m in msgs1)
ok("第 1 轮含摘要", has_sum1)
ok("第 1 轮含事实", has_fact1)

# 模拟这一轮成功入库
mm.add_exchange({"role": "user", "content": "我想哭"},
                {"role": "assistant", "content": "怎么啦？"})

# 第 2 轮：普通对话，无 session_block、无触发词
msgs2 = mm.build_context("因为你说的那些话")
has_sum2 = any("你和指挥官之前相处的内容" in m["content"] for m in msgs2)
has_fact2 = any("关于指挥官的长期印象" in m["content"] for m in msgs2)
ok("第 2 轮仍含摘要（修复前这里是 False）", has_sum2)
ok("第 2 轮仍含事实（修复前这里是 False）", has_fact2)

# 第 3 轮
mm.add_exchange({"role": "user", "content": "因为你说的那些话"},
                {"role": "assistant", "content": "嗯……"})
msgs3 = mm.build_context("我心里难受")
ok("第 3 轮仍含摘要",
   any("你和指挥官之前相处的内容" in m["content"] for m in msgs3))
ok("摘要只出现一次（不会重复堆叠）",
   sum(1 for m in msgs3 if "你和指挥官之前相处的内容" in m["content"]) == 1)

# 触发词路径仍可用
msgs4 = mm.build_context("你还记得我喜欢喝什么吗")
ok("命中触发词时改为检索式注入",
   any("你和指挥官之前聊过的内容" in m["content"] for m in msgs4))

# 关掉开关应回到旧行为
mm2 = mmn.MemoryManager()
mm2.summarizer._summary = "旧记忆"
_memory_cfg_backup = mmn.get_memory_config
mmn.get_memory_config = lambda: {"always_inject": False,
                                 "restore_working_memory": False}
msgs_off = mm2.build_context("普通一句话")
ok("always_inject=false 时不注入摘要（可退回旧行为）",
   not any("之前相处的内容" in m["content"] for m in msgs_off))
mmn.get_memory_config = _memory_cfg_backup

print()
print("=" * 76)
print("2. 摘要覆盖全量历史（缺陷 2）")
print("=" * 76)
reset_files()
HIST = 300
history = make_history(HIST)
mm3 = mmn.MemoryManager()
mm3.init_from_history(history)

job = mm3.peek_pending_job()
ok("启动后立刻排队归档（修复前 300 条也只排 100 条）", job is not None)
ok("首批大小 = SUMMARY_BATCH", job and len(job["messages"]) == mmn.SUMMARY_BATCH,
   f"{len(job['messages']) if job else 0}")
ok("首批是最旧的消息", job and job["messages"][0]["content"] == "m0")
ok("标记为归档批次", job and job["is_archive"] is True)
ok("结束下标 = 60", job and job["end_index"] == 60,
   f"{job['end_index'] if job else None}")

# 处理一批 → 游标推进 → 自动续批
j = mm3.peek_pending_job()
mm3.process_overflow(j)
mm3.ack_pending_job(j)
ok("覆盖进度推进到 60", mm3.summarizer.covered_index == 60,
   f"{mm3.summarizer.covered_index}")

job2 = mm3.peek_pending_job()
ok("自动续下一批", job2 is not None)
ok("续批从第 60 条开始", job2 and job2["messages"][0]["content"] == "m60",
   job2["messages"][0]["content"] if job2 else "-")

# 一直处理到结束
guard = 0
while mm3.peek_pending_job() and guard < 50:
    jj = mm3.peek_pending_job()
    mm3.process_overflow(jj)
    mm3.ack_pending_job(jj)
    guard += 1

ok("最终覆盖全部历史", mm3.summarizer.covered_index == HIST,
   f"{mm3.summarizer.covered_index}/{HIST}")
ok("覆盖完成后不再排队", mm3.peek_pending_job() is None)
ok("处理的批次数正确", guard + 1 == (HIST + mmn.SUMMARY_BATCH - 1)
   // mmn.SUMMARY_BATCH, f"{guard + 1} 批")

# 再次启动不应重复处理
mm4 = mmn.MemoryManager()
mm4.init_from_history(history)
ok("已全覆盖后重启不再重复摘要", mm4.peek_pending_job() is None)

print()
print("=" * 76)
print("3. 历史很短时也要补齐（修复前这是最大的坑）")
print("=" * 76)
reset_files()
SHORT = 40          # 远小于窗口 200
mm5 = mmn.MemoryManager()
mm5.init_from_history(make_history(SHORT))
job5 = mm5.peek_pending_job()
ok("40 条历史仍会排队归档（修复前直接 return，永远不处理）",
   job5 is not None)
ok("整批一起处理（不足一批）", job5 and len(job5["messages"]) == SHORT,
   f"{len(job5['messages']) if job5 else 0}")

mm5.process_overflow(job5)
mm5.ack_pending_job(job5)
ok("覆盖到 40", mm5.summarizer.covered_index == SHORT,
   f"{mm5.summarizer.covered_index}")

print()
print("=" * 76)
print("4. 覆盖进度持久化")
print("=" * 76)
raw = smod.load_json(smod.MEMORY_SUMMARY_FILE, {})
ok("memory_summary.json 存了 covered_index", "covered_index" in raw,
   str(raw.get("covered_index")))
ok("covered_index 数值正确", raw.get("covered_index") == SHORT)

mm6 = mmn.MemoryManager()
ok("新实例能读回覆盖进度", mm6.summarizer.covered_index == SHORT,
   f"{mm6.summarizer.covered_index}")

print()
print("=" * 76)
print("5. 进度越界自愈")
print("=" * 76)
reset_files()
mm7 = mmn.MemoryManager()
mm7.summarizer._covered_index = 9999       # 模拟手工改坏数据文件
mm7.fact_store.last_processed_index = 9999
mm7.init_from_history(make_history(10))
ok("摘要覆盖进度被校正到历史长度", mm7.summarizer.covered_index <= 10,
   f"{mm7.summarizer.covered_index}")
ok("事实进度被校正", mm7.fact_store.last_processed_index <= 10,
   f"{mm7.fact_store.last_processed_index}")
ok("越界校正后仍能正常排批", mm7.peek_pending_job() is not None)

print()
assert_real_data_untouched()
print("=" * 76)
if _failed:
    print(f"结果: {len(_failed)} 项失败 -> {_failed}")
    sys.exit(1)
print(f"结果: 记忆覆盖与注入全部通过 ✅（{_passed} 项）")
