"""摘要生成器 — 将旧对话压缩为叙述性摘要，支持增量合并"""
import os
import re
from datetime import datetime

from src.api import LLMRequestError, post_chat
from src.config_manager import BASE_DIR
from src.persistence import atomic_write_json, load_json

MEMORY_SUMMARY_FILE = os.path.join(BASE_DIR, "memory_summary.json")

# 摘要 LLM 的系统提示词（中性摘要助手，非夏安角色）
_SUMMARIZE_SYSTEM = (
    "你是一个对话摘要助手。请将用户提供的对话记录压缩为简洁的摘要，"
    "保留关键信息，忽略日常寒暄。"
)

_SUMMARIZE_USER = """请将以下对话记录压缩为一段简洁的摘要（中文，400字以内）。
保留关键信息：提到的人名、日期、事件、承诺、喜好、决定等。
忽略日常寒暄和重复内容。以第三人称叙述。

【时间要求 — 很重要】
对话记录里带有时间标记（例如「指挥官 [2026-09-28 00:15]」）。
请在摘要开头注明这段对话发生的时间（例如「9月28日深夜」），
让读的人能判断这些事是"什么时候"发生的，
而不是一串没有时间感、容易被误当成"正在发生"的叙述。

对话记录：
{messages_text}

摘要："""

_MERGE_SYSTEM = (
    "你是一个对话摘要助手。请将两段摘要合并为一段简洁的摘要。"
)

_MERGE_USER = """请将以下两段对话摘要合并为一段（中文，800字以内）。
保留所有关键信息，去除重复内容，按时间顺序组织。
如果两段讲的是同一件事，合并成一次叙述，不要重复两遍。

旧摘要：
{old_summary}

新摘要：
{new_summary}

合并后的摘要："""

# 摘要长度上限（字符）
#
# 注意这里必须留足余量，否则会**静默吃掉用户已有的记忆**：
#   - 提示词要求摘要 400 字、合并后 800 字
#   - 但用户长期使用后，摘要会以 "---" 分段累积到 1000+ 字符
#   - 早期把上限设成 300，结果任何一次合并/加载都会把
#     用户 565 字符的摘要裁成 300 字符，而且不报错
# 现在按"正常路径"与"兜底路径"分别给上限：
#   正常合并（LLM 参与）→ 按提示词目标收紧
#   兜底拼接（LLM 失败）→ 宽松，宁可长一点也不能丢
#
# 另外：摘要现在要覆盖**全部历史**（不再只覆盖窗口之外的部分），
# 所以上限必须跟着放大，否则早期记忆会被后来的内容挤掉。
_SUMMARY_CHAR_LIMIT = 1500

# 合并失败时的兜底拼接上限（比正常上限更宽松，优先保证不丢信息）
_MERGE_FALLBACK_LIMIT = 2000

# 机械/战术术语 — 摘要里出现的这些词对后续上下文毫无价值
# （模型偶尔会把夏安的机械设定写进摘要），做中性替换而非角色扮演式改写。
# 这是代码层兜底，正常情况不该命中。
_NEUTRAL_TERM_MAP = [
    (re.compile(r"战术板|战术终端|战术面板|战术数据"), "工作记录"),
    (re.compile(r"传感器(?:模块|数据|反馈|读数)?"), ""),
    (re.compile(r"减震(?:系统|装置|器|模块)?"), ""),
    (re.compile(r"恒温(?:系统|模式|设定|功能)?"), ""),
    (re.compile(r"核心温度"), "体温"),
    (re.compile(r"散热口"), ""),
    (re.compile(r"素体"), "身体"),
]


def _friendly_time(value):
    """把 ISO 时间串转成便于阅读的 "2026-09-28 00:15"，失败返回空串"""
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ""


def _neutralize(text):
    """把摘要中残留的机械/战术术语替换为中性表达

    摘要不是给用户看的，但会被注入后续对话上下文。
    这里只做中性的术语消解，不套用夏安的人类化话术。
    """
    if not text:
        return text
    for pattern, replacement in _NEUTRAL_TERM_MAP:
        text = pattern.sub(replacement, text)
    return text


def _trim_summary(text, limit=_SUMMARY_CHAR_LIMIT):
    """把摘要裁剪到 limit 字符以内，优先在句末截断"""
    if not text or len(text) <= limit:
        return text

    for delim in ("。", "！", "？"):
        idx = text.rfind(delim, limit // 2, limit)
        if idx != -1:
            return text[:idx + 1]
    return text[:limit]


class Summarizer:
    """摘要生成器 — 负责将对话压缩、合并、持久化"""

    def __init__(self):
        self._summary = None
        # 摘要已经覆盖到完整历史的第几条（绝对下标）。
        #
        # 为什么需要它：旧实现用"工作记忆窗口"来决定归档区间
        # （只处理 history[0 : total - window]），于是**最近 window 条永远
        # 进不了摘要**。窗口设成 200 条时，166 条历史全部落在"不归档"区间里，
        # 摘要四天没更新，用户每次重启都像失忆。
        # 现在改成独立游标：只要 covered_index < 历史长度，就继续补摘要，
        # 与窗口大小彻底解耦。
        self._covered_index = 0
        self._load()

    # ---------- 公开属性 ----------

    @property
    def summary(self):
        """当前摘要文本，无摘要时返回 None"""
        return self._summary

    @property
    def has_summary(self):
        return bool(self._summary and self._summary.strip())

    @property
    def covered_index(self):
        """摘要已覆盖到的历史下标（0 表示还没有摘要覆盖任何内容）"""
        return self._covered_index

    def set_covered_index(self, index):
        """记录摘要覆盖进度并落盘"""
        try:
            index = int(index)
        except (TypeError, ValueError):
            return
        if index < 0:
            index = 0
        if index == self._covered_index:
            return
        self._covered_index = index
        self._save()
        print(f"[Summarizer] 摘要覆盖进度 → 第 {index} 条")

    # ---------- 核心方法 ----------

    def summarize(self, messages):
        """将一组消息压缩为摘要文本

        Args:
            messages: [{"role": "user"|"assistant", "content": "..."}, ...]

        Returns:
            摘要字符串，失败时返回 None
        """
        if not messages:
            return None

        text = self._format_messages(messages)
        try:
            result = post_chat(
                [
                    {"role": "system", "content": _SUMMARIZE_SYSTEM},
                    {"role": "user", "content": _SUMMARIZE_USER.format(messages_text=text)},
                ],
                max_tokens=900,
                temperature=0.3,  # 低温度，追求事实准确
            )
        except LLMRequestError as e:
            print(f"[Summarizer] 摘要生成失败: {e.message}")
            return None

        if result:
            result = _neutralize(result)
            print(f"[Summarizer] 生成了新摘要 ({len(result)} 字符)")
            return result
        return None

    def merge(self, old_summary, new_summary):
        """合并两段摘要，避免无限膨胀

        Returns:
            合并后的摘要，失败时尽力保留新信息（不再硬切掉旧摘要尾部）
        """
        if not old_summary:
            return new_summary
        if not new_summary:
            return old_summary

        try:
            result = post_chat(
                [
                    {"role": "system", "content": _MERGE_SYSTEM},
                    {
                        "role": "user",
                        "content": _MERGE_USER.format(
                            old_summary=old_summary, new_summary=new_summary
                        ),
                    },
                ],
                max_tokens=900,
                temperature=0.3,
            )
            if result:
                merged = _trim_summary(_neutralize(result))
                print(f"[Summarizer] 摘要合并完成 ({len(merged)} 字符)")
                return merged
        except LLMRequestError as e:
            print(f"[Summarizer] 摘要合并失败: {e.message}")

        # 合并失败时的兜底拼接。
        #
        # 这里曾经有个会**丢掉用户全部旧摘要**的 bug：
        #   room = 600 - len(new_summary)
        #   if room >= 100: f"{old_summary[:room]}…\n---\n{new_summary}"
        #   else:           new_summary          ← 旧摘要直接没了
        # 旧摘要一旦长到 500+ 字符（长期使用必然如此），room 就小于 100，
        # 于是每次都走 else 分支把旧摘要整个丢弃 —— 而兜底分支正是为
        # "网络抖动导致合并失败"准备的，所以这不是极端情况而是常态。
        # 结果是用户最早期、最基础的记忆被优先丢掉。
        #
        # 正确做法：两段都保留，超出上限时对**拼接结果整体**裁剪，
        # 而不是按"谁新谁留"做取舍。
        combined = f"{old_summary}\n---\n{new_summary}"
        if len(combined) > _MERGE_FALLBACK_LIMIT:
            combined = _trim_summary(combined, _MERGE_FALLBACK_LIMIT)
        print(f"[Summarizer] 合并失败，使用兜底拼接 ({len(combined)} 字符)")
        return combined

    def add_batch(self, messages):
        """将一批消息生成摘要并合并到现有摘要

        Returns:
            True 如果摘要已更新，False 如果无需更新或失败
        """
        if not messages:
            return False

        new_summary = self.summarize(messages)
        if not new_summary:
            return False

        if self._summary:
            self._summary = self.merge(self._summary, new_summary)
        else:
            self._summary = _trim_summary(new_summary)

        self._save()
        return True

    def clear(self):
        """清空摘要（同时把覆盖进度归零，否则新对话永远不会进摘要）"""
        self._summary = None
        self._covered_index = 0
        self._save()

    # ---------- 内部方法 ----------

    @staticmethod
    def _format_messages(messages):
        """将消息列表格式化为可读文本（带时间标记）

        消息里的 ts 字段（若有）会渲染成 [2026-09-28 00:15]，
        让摘要模型能够写出"这段对话发生在什么时间"。
        老消息没有 ts，则退化为不带时间的旧格式。
        """
        lines = []
        for msg in messages:
            role = "指挥官" if msg.get("role") == "user" else "夏安"
            content = msg.get("content", "")
            if not content:
                continue

            stamp = _friendly_time(msg.get("ts"))
            if stamp:
                lines.append(f"{role} [{stamp}]: {content}")
            else:
                lines.append(f"{role}: {content}")
        return "\n".join(lines)

    # ---------- 持久化 ----------

    def _load(self):
        """从磁盘加载摘要

        必须校验类型：用户被鼓励直接编辑 exe 旁边的 JSON/YAML，
        手工把 summary 写成列表或数字是完全可能的事。
        旧实现直接 `data["summary"] or None`，随后 has_summary 里调
        `.strip()` 就会抛 AttributeError，**启动即崩溃**。
        """
        data = load_json(MEMORY_SUMMARY_FILE, None)
        self._covered_index = 0
        if isinstance(data, dict) and "summary" in data:
            raw = data["summary"]
            if raw is None:
                self._summary = None
            elif isinstance(raw, str):
                self._summary = raw or None
            else:
                print(
                    f"[Summarizer] 摘要字段类型异常 "
                    f"({type(raw).__name__})，已忽略并重置为无摘要"
                )
                self._summary = None
            if self._summary:
                print(f"[Summarizer] 已加载摘要 ({len(self._summary)} 字符)")

            # 覆盖进度：老文件没有这个字段，按 0 处理。
            #
            # 0 意味着"摘要还没覆盖任何内容"，于是首次启动会把整段历史
            # 重新摘要一遍。这看起来浪费，但比另一个选择安全：
            # 若默认成"已覆盖全部"，那么现有 166 条历史里未被摘要的部分
            # （9/29 以后的所有对话）就再也不会被补上 —— 正是要修的病。
            # 重复的部分交给 merge 提示词去重（它被要求"同一件事合并成
            # 一次叙述，不要重复两遍"）。
            cov = data.get("covered_index")
            try:
                cov = int(cov)
            except (TypeError, ValueError):
                cov = 0
            self._covered_index = max(0, cov)
            if self._covered_index:
                print(f"[Summarizer] 摘要已覆盖到第 {self._covered_index} 条")
        else:
            self._summary = None

    def _save(self):
        """将摘要原子写入磁盘（含覆盖进度）"""
        return atomic_write_json(
            MEMORY_SUMMARY_FILE,
            {"summary": self._summary, "covered_index": self._covered_index},
        )
