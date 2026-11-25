# -*- coding: utf-8 -*-
"""测试辅助：把数据落盘路径重定向到临时目录

## 为什么需要这个

测试会构造 ChatDialog / PetWindow，它们会**立刻写盘**：

- `ChatDialog.__init__` → `SessionTracker.mark_started()` → 写 `session.json`
- `LLMClient.__init__` → `MemoryManager()` → 读写 `memory_summary.json` / `fact_store.json`
- `FactStore` 加载与保存 → `fact_store.json`

所以任何构造 GUI 的测试，如果不重定向路径，都会改动用户的真实数据。

## 曾经的严重事故（务必保留这段说明）

早期版本的 `test_robustness.py` 先重定向了模块级常量，随后调用
`importlib.reload()` 重新执行模块代码 —— 新模块对象又把路径常量
绑定回**真实**路径，于是测试数据覆盖了用户的
`chat_history.json`（170 条真实对话）和 `memory_summary.json`。
测试是绿的，用户数据没了。

因此本模块：
1. **绝不 reload** 被重定向的模块
2. 重定向后立刻断言生效（失败就抛，不让测试带病跑）
3. 提供 `assert_real_data_untouched()` 做结束前核对

## 用法

    from tests.test_isolation import use_temp_data_dir, assert_real_data_untouched

    use_temp_data_dir()          # 必须在 import src.* 之前或之后、构造对象之前调用
    ...
    assert_real_data_untouched() # 测试结束时核对
"""
import hashlib
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 用户真实数据文件
REAL_FILES = ("chat_history.json", "memory_summary.json",
              "fact_store.json", "session.json")

_original_fingerprints = None
_temp_dir = None


def _fingerprint(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def snapshot_real_data():
    """记录用户真实数据文件的指纹（用临时路径，读真实文件）"""
    return {name: _fingerprint(os.path.join(ROOT, name)) for name in REAL_FILES}


def use_temp_data_dir(subdir="m200_test_data", fresh=True):
    """把所有数据落盘路径重定向到临时目录，并断言重定向生效

    Args:
        subdir: 临时子目录名
        fresh: True 时先清空该目录。默认开启 —— 否则上一个测试留下的
               历史/摘要会被下一个测试读到，造成互相干扰
               （例如残留的工作记忆会让"工作记忆应为空"的断言失败）

    Returns:
        str — 临时目录路径
    """
    global _original_fingerprints, _temp_dir

    if _original_fingerprints is None:
        _original_fingerprints = snapshot_real_data()

    _temp_dir = os.path.join(tempfile.gettempdir(), subdir)
    if fresh and os.path.isdir(_temp_dir):
        import shutil
        shutil.rmtree(_temp_dir, ignore_errors=True)
    os.makedirs(_temp_dir, exist_ok=True)

    # 这些模块必须已经被 import —— 本函数在对象构造前调用，
    # 调用方负责先 import src.*
    import src.llm_client as llmmod
    import src.memory.summarizer as smod
    import src.memory.fact_store as fmod
    import src.session as sessmod

    llmmod.HISTORY_FILE = os.path.join(_temp_dir, "chat_history.json")
    smod.MEMORY_SUMMARY_FILE = os.path.join(_temp_dir, "memory_summary.json")
    fmod.FACT_STORE_FILE = os.path.join(_temp_dir, "fact_store.json")
    sessmod.SESSION_FILE = os.path.join(_temp_dir, "session.json")

    # 断言生效 —— 重定向无效时测试会静默写坏用户数据，必须让这种情况响亮地失败
    for label, path in (
        ("HISTORY_FILE", llmmod.HISTORY_FILE),
        ("MEMORY_SUMMARY_FILE", smod.MEMORY_SUMMARY_FILE),
        ("FACT_STORE_FILE", fmod.FACT_STORE_FILE),
        ("SESSION_FILE", sessmod.SESSION_FILE),
    ):
        if _temp_dir not in path:
            raise RuntimeError(
                f"数据隔离失败：{label} 仍指向 {path}。"
                f"拒绝在未隔离的情况下继续运行测试。"
            )

    print(f"[isolation] 数据路径已重定向到 {_temp_dir}")
    return _temp_dir


def assert_real_data_untouched(label="测试结束"):
    """核对用户真实数据文件未被改动

    Returns:
        bool
    """
    if _original_fingerprints is None:
        return True

    changed = []
    for name, before in _original_fingerprints.items():
        now = _fingerprint(os.path.join(ROOT, name))
        if now != before:
            changed.append(name)

    if changed:
        print(f"[isolation] ⚠ {label}：用户数据文件被改动 -> {changed}")
        return False
    print(f"[isolation] {label}：用户数据文件未被改动 ✅")
    return True
