# -*- coding: utf-8 -*-
"""回归测试 —— 覆盖本次修复的所有问题点

运行：python tests/test_fixes.py
"""
import json
import os
import sys
import tempfile
from datetime import date

# 项目根目录（tests/ 的上一级）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.llm_client import (
    _reply_char_limit,
    _truncate_reply,
    _sanitize_reply,
    _parse_emotion_json,
)
from src.persistence import salvage_json_objects, strip_code_fence, atomic_write_json, load_json
from src.lunar import matches, lunar_to_solar, table_coverage_warning
from src.memory.fact_store import FactStore

failures = []


def check(name, got, expect):
    ok = got == expect
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"        got    = {got!r}")
        print(f"        expect = {expect!r}")
        failures.append(name)


def ok(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


print("=" * 72)
print("1. 回复长度上限 —— 按场景区分（旧代码写死 80）")
print("=" * 72)
from src.llm_client import _prompt_char_limits, _reply_char_limit as _rcl
check("解析出的 (普通, 亲密) 上限", _prompt_char_limits(), (80, 200))
check("普通聊天上限", _rcl("今天天气不错"), 80)
check("亲密场景上限", _rcl("我想抱抱你"), 200)

# 亲密场景的 180 字回复不应被砍（旧代码会砍到 80）
long_text = "指挥官今天真的辛苦了。" * 15
ok("180 字亲密回复不再被砍",
   len(_truncate_reply(long_text, max_chars=200)) == len(long_text),
   f"{len(long_text)} 字")
# 普通场景仍受 80 字约束
ok("普通回复仍受 80 字约束",
   len(_truncate_reply(long_text, max_chars=80)) <= 80,
   f"{len(_truncate_reply(long_text, max_chars=80))} 字")

print()
print("=" * 72)
print("2. 术语过滤 / 空格清理 / 换行保留")
print("=" * 72)
for s in ["我把传感器模块关了", "嗯？指挥官，战术背心我脱了", "我抱着你，心跳 好快",
          "核心温度有点高...不对，我不说那个，就是有点热"]:
    print(f"  {s!r} -> {_sanitize_reply(s)!r}")
ok("换行保留", "\n\n" in _sanitize_reply("第一行\n\n第二行"))
ok("空格已清理", _sanitize_reply("心跳 好快") == "心跳好快")

print()
print("=" * 72)
print("3. JSON 解析与截断抢救")
print("=" * 72)
check("完整 JSON", _parse_emotion_json('{"emotion": "happy", "reply": "嘿嘿，今天靶场打了满分。"}'),
      ("happy", "嘿嘿，今天靶场打了满分。"))
check("被截断 JSON", _parse_emotion_json('{"emotion": "shy", "reply": "指挥官你突然这样说我'),
      ("shy", "指挥官你突然这样说我"))
check("markdown 包裹", _parse_emotion_json('```json\n{"emotion": "sad", "reply": "你又熬夜了。"}\n```'),
      ("sad", "你又熬夜了。"))
check("非法 emotion 归零", _parse_emotion_json('{"emotion": "banana", "reply": "在的。"}'),
      ("idle", "在的。"))
check("空响应兜底", _parse_emotion_json("")[0], "idle")
check("事实数组抢救", len(salvage_json_objects(
    '[{"content": "a", "category": "偏好"}, {"content": "b", "categ')), 1)

print()
print("=" * 72)
print("4. 农历节日（原配置有错：除夕/中秋日期不对）")
print("=" * 72)
check("2026 春节 = 2/17", str(lunar_to_solar(2026, 1, 1)), "2026-02-17")
check("2026 中秋 = 9/25", str(lunar_to_solar(2026, 8, 15)), "2026-09-25")
check("2026 端午 = 6/19", str(lunar_to_solar(2026, 5, 5)), "2026-06-19")

# 除夕 = 春节前一天 = 2026-02-16（旧配置写成 2/17，与春节撞车）
ok("2026 除夕命中 2/16", matches(2026, 2, 16, 1, 1, offset_days=-1))
ok("2026 除夕不命中 2/17", not matches(2026, 2, 17, 1, 1, offset_days=-1))
ok("2026 中秋命中 9/25", matches(2026, 9, 25, 8, 15))
ok("2026 中秋不命中 10/6（旧配置的错误日期）", not matches(2026, 10, 6, 8, 15))

# 跨年容差：2027 春节 2027-02-06 属于农历丙午年
ok("2027 春节命中 2/6", matches(2027, 2, 6, 1, 1))
ok("表覆盖范围内无警告", table_coverage_warning() is None)

print()
print("=" * 72)
print("5. 原子写入 + 损坏文件隔离")
print("=" * 72)
tmpdir = tempfile.mkdtemp()
target = os.path.join(tmpdir, "t.json")
ok("原子写入成功", atomic_write_json(target, {"a": 1}))
check("内容可读回", load_json(target, None), {"a": 1})
ok("无残留 .tmp 文件", not os.path.exists(target + ".tmp"))

# 写入损坏内容 → load_json 应备份为 .corrupt 并返回默认值
with open(target, "w", encoding="utf-8") as f:
    f.write('{"a": 1, "b": ')          # 截断的 JSON
result = load_json(target, "DEFAULT")
check("损坏文件返回默认值", result, "DEFAULT")
ok("损坏文件已备份", os.path.exists(target + ".corrupt"), target + ".corrupt")

print()
print("=" * 72)
print("6. FactStore: contradicted_by 回填 + 无效 replace_id 校验")
print("=" * 72)
import src.memory.fact_store as fsmod

tmpdir2 = tempfile.mkdtemp()
fsmod.FACT_STORE_FILE = os.path.join(tmpdir2, "fact_store.json")

store = FactStore()

# 注入一条已有事实
store.facts = [{
    "id": "f_old0001", "content": "指挥官喜欢喝咖啡", "category": "偏好",
    "timestamp": "2026-01-01T00:00:00", "status": "active", "contradicted_by": None,
}]

# 模拟 LLM 判定为冲突
store._dedup_check = lambda content, window: {
    "action": "replace", "replace_id": "f_old0001", "reason": "test"
}
result = store.add_fact("指挥官不喜欢喝咖啡了", "偏好")
check("返回 replaced", result, "replaced")
old = next(f for f in store.facts if f["id"] == "f_old0001")
new = next(f for f in store.facts if f["status"] == "active")
check("旧事实标记 outdated", old["status"], "outdated")
check("contradicted_by 指向新事实（不再是 'pending'）", old["contradicted_by"], new["id"])
ok("contradicted_by 不是 pending", old["contradicted_by"] != "pending")

# 无效 replace_id 应被忽略，按新增处理
store2 = FactStore()
store2.facts = [{
    "id": "f_old0002", "content": "指挥官喜欢喝茶", "category": "偏好",
    "timestamp": "2026-01-01T00:00:00", "status": "active", "contradicted_by": None,
}]
store2._dedup_check = lambda content, window: {
    "action": "replace", "replace_id": "f_DOES_NOT_EXIST", "reason": "bad id"
}
r2 = store2.add_fact("指挥官改喝白开水了", "偏好")
check("无效 replace_id 按新增处理", r2, "added")
check("原事实仍为 active", store2.facts[0]["status"], "active")

# 本批重复应被本地拦截，不调用 LLM
store3 = FactStore()
calls = []
store3._dedup_check = lambda content, window: (calls.append(content) or {"action": "add"})
store3.add_fact("指挥官喜欢看星星", "偏好")
store3.add_fact("指挥官喜欢看星星", "偏好")   # 同一批内重复
check("同批重复只调用一次去重", len(calls), 1)

print()
print("=" * 72)
print("7. 事实提取进度标记")
print("=" * 72)
store4 = FactStore()
check("初始进度为 0", store4.last_processed_index, 0)
store4.mark_processed(126)
check("进度推进到 126", store4.last_processed_index, 126)
store4.mark_processed(50)   # 只前进不后退
check("进度不会回退", store4.last_processed_index, 126)

print()
print("=" * 72)
if failures:
    print(f"结果: {len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果: 全部通过 ✅")
