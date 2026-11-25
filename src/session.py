# -*- coding: utf-8 -*-
"""会话边界 — 让夏安分清「记得的事」和「正在发生的事」

## 要解决的问题

夏安每次启动都会继承上一次的摘要，但旧实现只把摘要丢进上下文，没有告诉模型：

1. **上次见面已经结束了** —— 摘要以叙述体记录"发生了什么"，
   读起来像一个仍在进行的场景
2. **中间隔了多久** —— 消息里原本没有时间戳，
   模型无从判断"准备睡觉"是 3 小时前还是 3 天前
3. **该用回忆的姿态，而不是接续的姿态** —— 旧提示词写的是
   "自然地融入这些信息"，等于在鼓励模型把旧场景当成当前场景继续演

结果就是用户遇到的那种割裂：半夜说过"要睡了"，中午启动时夏安
仍在那个场景里，于是出现"刚准备上床睡觉又要下床工作"。

## 解决思路

在**每次启动的第一次对话**里，注入一个显式的"会话边界"提示，明确告诉模型：

- 现在是哪个时刻（今天 / 几点）
- 上一次见面是什么时候、距今过去了多久
- 上一次是以什么场景收尾的
- **那件事已经结束了**，现在要当作"一段时间没见之后重新碰面"
- 记忆是背景知识（"你记得"），不是待续剧情（"你正在经历"）
- 指挥官**现在说的话优先于任何记忆里的状态**，
  记忆写"他要睡了"、他现在说"我要工作"，就以"要工作"为准

## 时间数据从哪来

`chat_history.json` 里加 `ts` 字段（ISO 格式）记录每条消息的真实时间。
老数据没有 `ts`，属于正常情况，会降级为"没有时间信息"并正常跳过。

另外把进程的启动/退出时间记在 `session.json`，
用来推算"上一次见面距今多久"。
"""
import json
import os
from datetime import datetime

from src.config_manager import BASE_DIR
from src.persistence import atomic_write_json, load_json

SESSION_FILE = os.path.join(BASE_DIR, "session.json")


def now_iso():
    """当前时间的 ISO 字符串（秒级精度）"""
    return datetime.now().isoformat(timespec="seconds")


def _parse_iso(value):
    """解析 ISO 时间字符串，失败返回 None（兼容老数据）"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def format_gap(delta_seconds):
    """把时间差转换成口语化的中文

    只保留一个量级（"大约 3 小时" 而不是 "3 小时 14 分 22 秒"），
    因为问候语里只需要一个模糊的"过了多久"的感觉。
    """
    if delta_seconds is None or delta_seconds < 0:
        return None

    minutes = delta_seconds / 60
    if minutes < 2:
        return "刚刚"
    if minutes < 60:
        return f"大约 {int(minutes)} 分钟"
    hours = minutes / 60
    if hours < 24:
        return f"大约 {int(hours)} 小时"
    days = hours / 24
    if days < 30:
        return f"大约 {int(days)} 天"
    months = days / 30
    if months < 12:
        return f"大约 {int(months)} 个月"
    return f"大约 {int(days / 365)} 年"


class SessionTracker:
    """记录"上一次见面是什么时候"，并生成会话边界提示"""

    def __init__(self):
        # session.json 被手工改成列表/字符串/数字时，
        # `self._data["last_start"] = ...` 会抛 TypeError（列表）或静默失败，
        # 而这个赋值发生在启动路径上（ChatDialog 构造 → QTimer 槽），
        # 未捕获异常会让 PyQt5 直接 abort 整个进程。所以强制归一化为 dict。
        data = load_json(SESSION_FILE, {})
        if not isinstance(data, dict):
            print(f"[Session] session.json 格式异常 "
                  f"({type(data).__name__})，已重置")
            data = {}
        self._data = data
        self._started_at = datetime.now()

    # ---------- 生命周期 ----------

    def mark_started(self):
        """记录本次启动时间

        注意：不覆盖 last_seen_end —— 它保存的是"上一次退出时间"，
        也就是本次会话用于计算"隔了多久"的基准。
        """
        self._data["last_start"] = now_iso()
        self._save()

    def mark_ended(self):
        """记录本次退出时间（供下次启动计算时间差）"""
        self._data["last_end"] = now_iso()
        self._save()

    # ---------- 时间差 ----------

    @property
    def previous_end(self):
        """上一次"还在运行"的时间（datetime 或 None）

        优先用正常退出时写入的 last_end。
        但如果程序被强杀（任务管理器结束进程、崩溃、直接关电源），
        last_end 不会写入 —— 这时退回用上一次的 last_start 近似：
        它至少给出了"上次是什么时候在用的"，比完全没有时间基准好得多。
        """
        return _parse_iso(self._data.get("last_end")) or _parse_iso(
            self._data.get("last_start")
        )

    def elapsed_since_last_seen(self, history=None):
        """距上次见面过了多少秒；无从判断时返回 None

        基准按可靠性排序：
          1. 历史里最后一条消息的时间戳 —— 最接近"上次真的说话"的时刻
          2. last_end（正常退出时写入）
          3. last_start（上次启动时间，仅在程序被强杀导致 last_end 缺失时兜底）

        程序被强杀时 last_end 不会写入。若只用 last_end，
        「隔了多久」会静默失效；用 last_start 兜底虽然略偏早，
        但比完全没有时间基准好得多。
        """
        reference = self.last_conversation_end(history) if history else None
        if reference is None:
            reference = self.previous_end
        if reference is None:
            return None
        return (self._started_at - reference).total_seconds()

    def last_conversation_end(self, history):
        """从历史里找出最后一条带时间戳的消息时间

        Args:
            history: chat_history.json 的消息列表

        Returns:
            datetime 或 None（老数据没有 ts 字段）
        """
        for msg in reversed(history or []):
            # 历史里可能混入非 dict 元素（手工编辑、早期版本写入异常）——
            # 直接 msg.get 会 AttributeError，而本方法在启动问候的
            # QTimer 槽里被调用，会导致整个进程 abort。
            if not isinstance(msg, dict):
                continue
            parsed = _parse_iso(msg.get("ts"))
            if parsed is not None:
                return parsed
        return None

    # ---------- 会话边界提示 ----------

    def build_boundary_block(self, history, now=None):
        """构建"会话边界"提示文本

        这段文本在每次启动的第一轮 LLM 交互里注入一次。

        两种变体只差"时间基准"那一句：
          - 有带时间戳的历史 → 能算出"上一次说话是何时、距今多久"
          - 老存档没有 ts    → 无法给出具体间隔，但**其余规则完全相同**

        注意：早期实现让老存档走一条极简分支，结果缺了"不要接着演"
        "当下优先"这些关键规则 —— 而恰好升级前的老存档都会走那条分支，
        等于修复对老用户完全失效。所以这两种变体必须共享同一套规则。

        Args:
            history: 完整对话历史（用于找上次对话的结束时间）
            now: 当前时间，默认取系统时间

        Returns:
            str 提示文本，或 None（完全没有历史的新用户不需要）
        """
        if not history:
            return None

        now = now or datetime.now()

        # 优先用历史里最后一条消息的真实时间；老数据没有就用上次退出时间
        last_talk = self.last_conversation_end(history) or self.previous_end

        if last_talk is not None:
            gap = format_gap((now - last_talk).total_seconds())
            if last_talk.date() == now.date():
                when = f"今天 {last_talk.strftime('%H:%M')}"
            else:
                when = last_talk.strftime("%Y年%m月%d日 %H:%M")
            anchor = f"你和指挥官上一次说话是在 {when}，距今{gap}。"
        else:
            # 老存档：没有时间戳可用，退化为"不知道具体隔了多久"
            anchor = (
                "你和指挥官之前聊过不少内容，但那些都是**过去某个时间**发生的事，"
                "具体隔了多久你已经记不清了。"
            )

        lines = [
            "【会话边界 — 非常重要，请仔细理解】",
            f"现在是 {now.strftime('%Y年%m月%d日 %H:%M')}。",
            anchor,
            "",
            "请注意以下几点：",
            "1. 上次的对话**已经结束了**。中间隔着刚才提到的那段时间，"
            "那段时间里你们各自在做自己的事，你不知道具体发生了什么。",
            "2. 下面是「你记得的事」，它是**背景知识**，不是**正在发生的剧情**。",
            "3. 绝对不要接着上次的场景往下演。"
            "例如上次结尾是「准备睡觉」，那件事早就结束了，"
            "不要表现成「刚躺下又被叫起来」。",
            "4. 指挥官**现在说的话，优先于记忆里的任何状态**。"
            "如果记忆里是「他要去睡了」，而他现在说「我要工作」，"
            "那就以「他现在要工作」为准 —— 这中间已经过去了一段时间。",
            "5. 你需要做的是：像一段时间没见到他的人那样自然地重新打招呼，"
            "可以顺口提一句你记得的事，但不要把它当成刚刚发生的事。",
        ]
        return "\n".join(lines)

    # ---------- 持久化 ----------

    def _save(self):
        atomic_write_json(SESSION_FILE, self._data)
