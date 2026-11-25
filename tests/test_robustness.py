# -*- coding: utf-8 -*-
"""回归测试：健壮性与并发安全

覆盖审计发现并修复的问题，以及之前踩过的坑：

1. **并发请求** —— 请求进行中再次发送会打乱对话顺序（已修复：显式 in-flight 守卫）
2. **问候竞态** —— 用户抢先发消息会抢走会话边界（已修复：问候不消费额度）
3. **退化数据启动** —— 手工编辑过的 JSON 曾导致启动崩溃
4. **中途退出** —— 请求在飞时退出不应崩溃或破坏数据
5. **并发写入** —— 固定 .tmp 名曾导致 15% 文件损坏
6. **编码容错** —— GBK / BOM 曾导致启动失败或误隔离
7. **清空历史后归档失效** —— 陈旧进度标记让新对话永不入摘要
8. **归档区间** —— 旧实现每次从索引 0 重跑整段历史

## ⚠️ 数据安全（这条是血的教训）

本测试会写入 JSON 文件，**必须**把所有落盘路径重定向到临时目录。
`isolate()` 会在重定向前后做断言校验 —— 因为曾经出过这样的事故：
测试先重定向了模块级常量，随后 `importlib.reload()` 重新执行模块代码，
新模块对象又绑定了**真实**路径常量，于是测试数据覆盖了用户的
真实 `chat_history.json` 与 `memory_summary.json`。

所以这里：
  - 重定向后立即断言"生效了"，不生效就中止测试
  - 绝不 reload 被重定向的模块
  - 结束后再次断言路径仍指向临时目录

运行：python tests/test_robustness.py
"""
import json
import os
import sys
import tempfile
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont

app = QApplication(sys.argv)
app.setFont(QFont("Microsoft YaHei", 9))

import src.llm_client as llmmod
import src.memory.summarizer as smod
import src.memory.fact_store as fmod
import src.session as sessmod

# 用户真实数据文件 —— 任何情况下都不允许被本测试写入
REAL_FILES = ("chat_history.json", "memory_summary.json",
              "fact_store.json", "session.json")
_real_fingerprints = {}
for _name in REAL_FILES:
    _p = os.path.join(ROOT, _name)
    if os.path.isfile(_p):
        with open(_p, "rb") as _f:
            _real_fingerprints[_name] = hash(_f.read())

failures = []


def ok(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def isolate(tmpdir):
    """把所有落盘路径重定向到临时目录，并**断言**重定向确实生效

    断言很关键：重定向无效时测试会写坏用户数据，
    而那种失败是静默的（测试全绿，用户数据没了）。
    """
    llmmod.HISTORY_FILE = os.path.join(tmpdir, "hist.json")
    smod.MEMORY_SUMMARY_FILE = os.path.join(tmpdir, "sum.json")
    fmod.FACT_STORE_FILE = os.path.join(tmpdir, "facts.json")
    # session.json 也必须重定向：ChatDialog 构造 SessionTracker 时
    # 会立刻调用 mark_started() 写盘
    sessmod.SESSION_FILE = os.path.join(tmpdir, "session.json")

    assert str(tmpdir) in llmmod.HISTORY_FILE, "HISTORY_FILE 重定向失败"
    assert str(tmpdir) in smod.MEMORY_SUMMARY_FILE, "MEMORY_SUMMARY_FILE 重定向失败"
    assert str(tmpdir) in fmod.FACT_STORE_FILE, "FACT_STORE_FILE 重定向失败"
    assert str(tmpdir) in sessmod.SESSION_FILE, "SESSION_FILE 重定向失败"


def assert_real_files_untouched(ctx):
    """核对用户真实数据文件未被写入"""
    changed = []
    for name, before in _real_fingerprints.items():
        p = os.path.join(ROOT, name)
        if not os.path.isfile(p):
            changed.append(f"{name}(消失)")
            continue
        with open(p, "rb") as f:
            if hash(f.read()) != before:
                changed.append(name)
    ok(f"{ctx}：用户数据文件未被改动", not changed,
       f"被改动的文件={changed}" if changed else "")


print("=" * 92)
print("1. 并发请求守卫（请求进行中不得再发）")
print("=" * 92)

tmpdir = tempfile.mkdtemp()
isolate(tmpdir)
llmmod.post_chat = lambda m, **kw: '{"emotion": "happy", "reply": "好的。"}'

from src.chat_dialog import ChatDialog, LLMWorker, SummaryWorker

started = []
for cls in (LLMWorker, SummaryWorker):
    cls.start = lambda self, _cls=cls: started.append(type(self).__name__)

cd = ChatDialog()
cd.resize(360, 460)
cd.show()
app.processEvents()

cd.input_field.setText("第一条")
cd._send_message()
n1 = len(started)
ok("首次发送启动 worker", n1 == 1)

# 三种"绕过禁用"的路径都不能再启动 worker
cd._send_message()
ok("直接调用被拦截", len(started) == n1)

cd.input_field.setText("第二条")
cd.input_field.returnPressed.emit()
ok("回车键被拦截", len(started) == n1)

cd.send_btn.setEnabled(True)
cd.send_btn.click()
ok("按钮被强制启用后点击也被拦截", len(started) == n1)

# 请求结束后应恢复可发送
cd._on_reply("happy", "回复")
ok("请求结束后状态复位", not cd._is_requesting)
ok("输入框恢复可用", cd.input_field.isEnabled())
cd.input_field.setText("第三条")
cd._send_message()
ok("结束后可再次发送", len(started) == n1 + 1)

cd.shutdown()
started.clear()

print()
print("=" * 92)
print("2. 问候竞态（用户抢先时问候仍须带会话边界）")
print("=" * 92)

captured = []
for cls in (LLMWorker, SummaryWorker):
    cls.start = lambda self, _cls=cls: captured.append(self)

cd = ChatDialog()
# 隔离目录里历史为空，而空历史时 build_boundary_block 会返回 None
# （没有历史就谈不上"重新见面"）。这里给一个最小历史，
# 模拟"有过去对话"的真实场景。
cd.llm_client._full_history = [
    {"role": "user", "content": "以前的对话", "ts": "2026-09-28T23:40:00"},
    {"role": "assistant", "content": "以前回复", "ts": "2026-09-28T23:40:01"},
]
cd.input_field.setText("我在你之前就发了")
cd._send_message()                    # 用户抢先，消费掉边界额度
ok("用户抢先已消费额度", cd._session_block_used)
greeting = cd._greeting_session_block()
ok("问候仍拿到会话边界", greeting is not None,
   "（否则问候会在没有时间锚点的情况下读旧记忆）")

cd.shutdown()
captured.clear()

print()
print("=" * 92)
print("3. 退化数据启动（手工编辑/损坏的 JSON）")
print("=" * 92)

DEGRADED = [
    ("历史截断", '[{"role": "user", "content": "A"', "{}", '{"facts": []}'),
    ("历史非列表", '{"role": "user"}', "{}", '{"facts": []}'),
    ("历史含 null 元素", '[null, null]', "{}", '{"facts": []}'),
    ("摘要为 null", '[]', '{"summary": null}', '{"facts": []}'),
    ("摘要是列表", '[]', '{"summary": ["x"]}', '{"facts": []}'),
    ("摘要是数字", '[]', '{"summary": 42}', '{"facts": []}'),
    ("摘要截断", '[]', '{"summary": "半截', '{"facts": []}'),
    ("facts 为 null", '[]', "{}", '{"facts": null}'),
    ("facts 非字典", '[]', "{}", '[{"id": "f_1"}]'),
    ("fact 缺字段", '[]', "{}", '{"facts": [{"id": "f_1"}]}'),
    ("ts 非法", '[{"role": "user", "content": "A", "ts": "bad"}]', "{}", '{"facts": []}'),
    ("ts 为数字", '[{"role": "user", "content": "A", "ts": 123}]', "{}", '{"facts": []}'),
]

for label, h, s, f in DEGRADED:
    d = tempfile.mkdtemp()
    hp, sp, fp = (os.path.join(d, n) for n in ("h.json", "s.json", "f.json"))
    for path, content in ((hp, h), (sp, s), (fp, f)):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    llmmod.HISTORY_FILE, smod.MEMORY_SUMMARY_FILE, fmod.FACT_STORE_FILE = hp, sp, fp

    try:
        client = llmmod.LLMClient()
        client.memory_manager.build_context("测试")
        client.memory_manager.build_context("", session_block="【会话边界】", force_memory=True)
        client.chat("测试消息")
        ok(f"退化数据：{label}", True)
    except Exception as e:
        ok(f"退化数据：{label}", False, f"{type(e).__name__}: {e}")

print()
print("=" * 92)
print("4. 中途退出（请求在飞时 shutdown）")
print("=" * 92)

tmpdir = tempfile.mkdtemp()
isolate(tmpdir)


def slow_post_chat(messages, max_tokens=None, temperature=None, timeout=None):
    time.sleep(15)
    return '{"emotion": "happy", "reply": "迟到"}'


llmmod.post_chat = slow_post_chat

# 恢复真实的 start()：本节要真的把线程跑起来，
# 才能验证"请求在飞时退出"这条路径。
del LLMWorker.start
del SummaryWorker.start

cd = ChatDialog()
cd.resize(360, 460)
cd.show()
app.processEvents()
cd.input_field.setText("会卡住的消息")
cd._send_message()
app.processEvents()
ok("请求在飞", any(w.isRunning() for w in cd._workers),
   f"{len(cd._workers)} 个 worker")

t0 = time.time()
cd.shutdown()
elapsed = time.time() - t0
ok("shutdown 及时返回", elapsed < 10, f"{elapsed:.2f}s")
ok("无运行中线程", not any(w.isRunning() for w in cd._workers))
ok("已置位 shutting_down", cd._is_shutting_down)

# 退出后不得再接受新请求
before = len(cd._workers)
cd.input_field.setText("退出后的消息")
cd._send_message()
ok("退出后拒绝新请求", len(cd._workers) == before)

# 数据文件要么不存在，要么仍是合法 JSON
for name in ("hist.json", "sum.json", "facts.json"):
    p = os.path.join(tmpdir, name)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                json.load(fh)
            ok(f"{name} 未被破坏", True)
        except Exception as e:
            ok(f"{name} 未被破坏", False, f"{type(e).__name__}: {e}")
    else:
        ok(f"{name} 未写入（可接受）", True)

leftover = [x for x in os.listdir(tmpdir) if x.endswith(".tmp")]
ok("无 .tmp 残留", not leftover, f"{leftover}")

try:
    cd.shutdown()
    ok("重复 shutdown 安全", True)
except Exception as e:
    ok("重复 shutdown 安全", False, f"{type(e).__name__}: {e}")

print()
print("=" * 92)
print("5. 并发写入安全（固定 .tmp 名曾导致 15% 文件损坏）")
print("=" * 92)

from src.persistence import atomic_write_json, load_json
import threading

ctmp = tempfile.mkdtemp()
ctarget = os.path.join(ctmp, "concurrent.json")
corrupt = 0
ROUNDS = 150
for _ in range(ROUNDS):
    atomic_write_json(ctarget, [])

    def _big():
        atomic_write_json(ctarget, [{"role": "user", "content": "x" * 2000}] * 20)

    def _small():
        atomic_write_json(ctarget, [])

    t1 = threading.Thread(target=_big)
    t2 = threading.Thread(target=_small)
    t1.start(); t2.start(); t1.join(); t2.join()

    try:
        with open(ctarget, encoding="utf-8") as fh:
            json.load(fh)
    except Exception:
        corrupt += 1

ok(f"并发写 {ROUNDS} 轮不损坏文件", corrupt == 0,
   f"损坏 {corrupt} 次 ({corrupt / ROUNDS * 100:.0f}%)")

print()
print("=" * 92)
print("6. 编码容错（GBK / BOM 曾导致启动失败或误隔离）")
print("=" * 92)

etmp = tempfile.mkdtemp()

gbk_json = os.path.join(etmp, "gbk.json")
with open(gbk_json, "wb") as fh:
    fh.write('{"汇总": "中文内容"}'.encode("gbk"))
ok("GBK JSON 能正确读取", load_json(gbk_json, None) == {"汇总": "中文内容"})

bom_json = os.path.join(etmp, "bom.json")
with open(bom_json, "w", encoding="utf-8-sig") as fh:
    json.dump({"a": 1}, fh)
ok("UTF-8 BOM JSON 能正确读取", load_json(bom_json, None) == {"a": 1})

import src.config_manager as cmod
_old_base = cmod.BASE_DIR
cmod.BASE_DIR = etmp
gbk_yaml = os.path.join(etmp, "gbk.yaml")
with open(gbk_yaml, "wb") as fh:
    fh.write('api:\n  endpoint: "x"  # 中文\n'.encode("gbk"))
try:
    loaded = cmod.load_yaml("gbk.yaml")
    ok("GBK YAML 能正确读取", loaded.get("api", {}).get("endpoint") == "x")
except Exception as e:
    ok("GBK YAML 能正确读取", False, f"{type(e).__name__}: {e}")

# 坏 YAML 也不能抛异常（它在 QThread.run / QTimer 槽里，逃出去会让进程 abort）
import yaml as _yaml
with open(os.path.join(etmp, "broken.yaml"), "w", encoding="utf-8") as fh:
    fh.write("a: [1, 2\n  b: : :\n")
ok("损坏 YAML 安全降级为 {}", cmod.load_yaml("broken.yaml") == {})
cmod.BASE_DIR = _old_base

print()
print("=" * 92)
print("7. 清空历史后归档不能失效（陈旧标记曾让新对话永不入摘要）")
print("=" * 92)

import src.memory.fact_store as fs_mod
import src.memory.summarizer as sm_mod

mtmp = tempfile.mkdtemp()
fs_mod.FACT_STORE_FILE = os.path.join(mtmp, "facts.json")
sm_mod.MEMORY_SUMMARY_FILE = os.path.join(mtmp, "sum.json")
llmmod.HISTORY_FILE = os.path.join(mtmp, "hist.json")

store = fs_mod.FactStore()
store.mark_processed(128)
ok("可设置进度标记", store.last_processed_index == 128)

store.clear()
ok("清空后标记归零", store.last_processed_index == 0,
   f"实际 {store.last_processed_index}")

disk = json.load(open(fs_mod.FACT_STORE_FILE, encoding="utf-8"))
ok("磁盘上的标记也已归零", disk.get("last_processed_index") == 0)

# 自愈：标记超出历史长度时应被校正
store2 = fs_mod.FactStore()
store2.mark_processed(9999)
from src.memory.memory_manager import MemoryManager
mm = MemoryManager()
mm.fact_store = store2
mm.init_from_history([{"role": "user", "content": "A"}])
ok("超范围标记被自愈校正", mm.fact_store.last_processed_index <= 1,
   f"实际 {mm.fact_store.last_processed_index}")

print()
print("=" * 92)
print("8. 归档只处理未处理过的区间（旧实现每次从 0 重跑）")
print("=" * 92)

mtmp2 = tempfile.mkdtemp()
fs_mod.FACT_STORE_FILE = os.path.join(mtmp2, "facts.json")
sm_mod.MEMORY_SUMMARY_FILE = os.path.join(mtmp2, "sum.json")
llmmod.HISTORY_FILE = os.path.join(mtmp2, "hist.json")

# ⚠️ 这里**绝对不能** importlib.reload(模块)。
# reload 会重新执行模块顶层代码，把上面刚改掉的路径常量重新绑定回
# 真实路径，后续测试就会写进用户的 chat_history.json / memory_summary.json。
# （这正是之前造成一次真实数据被覆盖的原因。）
# 需要新实例时直接构造即可 —— 构造器每次都从模块常量取路径。
assert str(mtmp2) in fs_mod.FACT_STORE_FILE
assert str(mtmp2) in sm_mod.MEMORY_SUMMARY_FILE

# 有摘要 + 已有进度 128 + 历史 400 条
#
# 注意：归档区间现在由「摘要覆盖进度」决定，与工作记忆窗口无关
# （本次修复的核心之一：旧实现用 total - 窗口 作归档上界，
#  导致最近 N 条永远进不了摘要）。
# 这里取 400 条历史、事实进度 128、摘要无覆盖进度，期望从第 128 条开始
# 续处理，一批 60 条 → [128, 188)。
sum_path = sm_mod.MEMORY_SUMMARY_FILE
with open(sum_path, "w", encoding="utf-8") as fh:
    json.dump({"summary": "已存在的摘要", "covered_index": 128}, fh,
              ensure_ascii=False)

mm2 = MemoryManager()
total = 400
mm2.fact_store.last_processed_index = 128
history = [{"role": "user" if i % 2 == 0 else "assistant",
            "content": f"m{i}", "ts": "2026-09-29T12:00:00"} for i in range(total)]
mm2.init_from_history(history)

job = mm2.peek_pending_job()
if job:
    batch = job["messages"]
    ok("归档批次不含已处理部分", batch[0]["content"] == "m128",
       f"批次首条={batch[0]['content']} 大小={len(batch)}")
    ok("归档结束下标为绝对值", job["end_index"] == 128 + len(batch),
       f"实际 {job['end_index']} 期望 {128 + len(batch)}")
else:
    ok("归档应被调度", False,
       f"pending_job 为空（历史={total} 进度=128）")

# 历史短于一批时也要整体归档（旧实现会因为 total <= 窗口 直接 return）
mm_short = MemoryManager()
mm_short.summarizer._covered_index = 0
mm_short.fact_store.reset_processed()
mm_short.init_from_history([
    {"role": "user" if i % 2 == 0 else "assistant", "content": f"x{i}"}
    for i in range(40)
])
job_s = mm_short.peek_pending_job()
ok("历史短于窗口时仍会归档", job_s is not None
   and len(job_s["messages"]) == 40,
   f"{len(job_s['messages']) if job_s else 0} 条")
d2 = tempfile.mkdtemp()
fs_mod.FACT_STORE_FILE = os.path.join(d2, "facts.json")
sm_mod.MEMORY_SUMMARY_FILE = os.path.join(d2, "sum.json")
mm3 = MemoryManager()
mm3.init_from_history([
    {"role": "user" if i % 2 == 0 else "assistant", "content": f"x{i}"}
    for i in range(5)
])
# 处理掉启动排的这一批，之后不应再排
if mm3.peek_pending_job():
    _j = mm3.peek_pending_job()
    mm3.process_overflow(_j)
    mm3.ack_pending_job(_j)
ok("全部覆盖后不再排队", mm3.peek_pending_job() is None)

print()
print("=" * 92)
print("最终安全检查")
print("=" * 92)
assert_real_files_untouched("测试结束")

print()
print("=" * 92)
if failures:
    print(f"结果: {len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果: 健壮性与并发安全全部通过 ✅")
