"""结构化事实存储 — 提取/去重/检索/持久化

事实格式:
{
  "id": "f_<uuid_short>",
  "content": "指挥官喜欢喝咖啡",
  "category": "偏好",
  "timestamp": "2026-05-31T22:00:00",
  "status": "active" | "outdated",
  "contradicted_by": null | "f_xxx"    # 取代它的那条新事实的 id
}

存储文件: fact_store.json
"""
import json
import os
import uuid
from datetime import datetime

from src.api import LLMRequestError, post_chat
from src.config_manager import BASE_DIR
from src.persistence import atomic_write_json, load_json, salvage_json_objects, strip_code_fence

FACT_STORE_FILE = os.path.join(BASE_DIR, "fact_store.json")

# 语义去重时最多比对多少条已有事实
# 全量比对是 O(n) 且随事实数量平方级增长（每条新事实都要重发全部旧事实），
# 这里取最近 N 条 —— 近因事实与当前对话的相关性最高，收益/成本比最好。
_DEDUP_WINDOW = 30

# 本地精确去重时最多回溯多少条历史事实（防文件损坏导致长列表卡顿）
_EXACT_WINDOW = 200

# 事实提取 LLM 提示词
_EXTRACT_SYSTEM = (
    "你是一个信息提取助手。从对话记录中提取关于「指挥官」的关键事实。"
    "只提取明确的信息，不要推测。如果没有值得记住的事实，返回空数组。"
)

_EXTRACT_USER = """从以下对话中提取关于「指挥官」的关键事实。

每条事实用一句话概括，从以下类别中选择一个：
- 偏好：指挥官喜欢/不喜欢什么
- 事件：指挥官经历了什么事
- 关系：指挥官与他人的关系
- 承诺：指挥官答应或承诺了什么
- 信息：指挥官的基本信息（职业、计划、状态等）

只提取值得长期记住的信息。忽略日常寒暄、情绪表达、角色扮演内容。
如果对话中没有值得提取的事实，返回空数组。

对话记录：
{messages_text}

请用 JSON 数组格式返回：
[{{"content": "一句话事实", "category": "类别"}}, ...]"""

# 去重/冲突检测 LLM 提示词
_DEDUP_SYSTEM = (
    "你是一个信息管理助手。判断新事实与已有事实的关系。"
)

_DEDUP_USER = """已有事实：
{existing_facts}

新事实：
{new_fact}

请判断新事实与已有事实的关系，返回 JSON：
- 如果新事实是全新信息：{{"action": "add", "reason": "..."}}
- 如果新事实与已有事实重复（语义相同）：{{"action": "skip", "duplicate_of": "已有事实内容", "reason": "..."}}
- 如果新事实与已有事实冲突（新事实更可信）：{{"action": "replace", "replace_id": "已有事实的id", "reason": "..."}}

注意：仅当新事实与已有事实**明显矛盾**时才判定为冲突。
同一主题的不同方面不算冲突（如"喜欢咖啡"和"喜欢喝茶"不冲突）。
replace_id 必须从上面「已有事实」列表中方括号里的 id 原样复制。
返回："""


class FactStore:
    """结构化事实的存储与检索"""

    def __init__(self):
        self.facts = []  # list of fact dicts
        # 本次会话中已出现过的事实内容（规范化后）。
        # 用于跳过 "提取→逐条 add" 流程里对同批事实的重复 LLM 去重调用：
        # 之前的实现会让同一批 N 条事实触发 N 次去重请求，
        # 且每次都把全部已有事实重发一遍，成本随事实数量平方级增长。
        self._seen_this_session = set()
        # 事实提取已经推进到完整对话历史的第几条消息。
        # MemoryManager 用它判断归档区间是否还需要补处理。
        self.last_processed_index = 0
        self._load()

    # ---------- 公开属性 ----------

    @property
    def active_facts(self):
        """所有活跃事实"""
        return [f for f in self.facts if f.get("status") == "active"]

    @property
    def fact_count(self):
        return len(self.active_facts)

    # ---------- 核心方法 ----------

    def extract_from_messages(self, messages):
        """从对话消息中提取结构化事实（LLM 调用）

        Returns:
            list of fact dicts (不含 id/timestamp，待 add 处理)
        """
        if not messages:
            return []

        text = self._format_messages(messages)
        try:
            result = post_chat(
                [
                    {"role": "system", "content": _EXTRACT_SYSTEM},
                    {"role": "user", "content": _EXTRACT_USER.format(messages_text=text)},
                ],
                max_tokens=800,  # 一条事实约占 20 token，800 足够输出 30+ 条
                temperature=0.2,  # 低温度，追求结构化准确性
            )
        except LLMRequestError as e:
            print(f"[FactStore] 事实提取失败: {e.message}")
            return []

        if not result:
            return []

        facts = self._parse_fact_json(result)
        if facts:
            print(f"[FactStore] 提取到 {len(facts)} 条事实")
        return facts

    def add_fact(self, content, category="信息"):
        """添加一条事实（自动去重与冲突检测）

        Returns:
            "added" | "skipped" | "replaced" | "invalid"
        """
        content = (content or "").strip()
        if not content:
            return "invalid"

        # ① 本次会话内的完全重复 — 直接跳过，不再打扰 LLM
        seen_key = self._normalize(content)
        if seen_key in self._seen_this_session:
            print(f"[FactStore] 本批重复，跳过: {content[:50]}")
            return "skipped"

        # ② 本地精确去重（只回溯最近若干条，防止异常数据导致长循环）
        recent = self.active_facts[-_EXACT_WINDOW:]
        for f in recent:
            if f.get("content", "").strip() == content:
                self._seen_this_session.add(seen_key)
                print(f"[FactStore] 跳过重复事实: {content[:50]}")
                return "skipped"

        # ③ 语义去重 / 冲突检测 — 只比对最近 _DEDUP_WINDOW 条
        window = self.active_facts[-_DEDUP_WINDOW:]
        if window:
            action = self._dedup_check(content, window)
        else:
            action = {"action": "add", "reason": "首个事实"}

        if action.get("action") == "skip":
            self._seen_this_session.add(seen_key)
            print(f"[FactStore] 语义重复，跳过: {content[:50]}")
            return "skipped"

        replaced = False
        replace_target = None
        if action.get("action") == "replace":
            # LLM 可能返回窗口外或根本不存在的 id — 必须校验，否则会出现
            # "新事实已入库但旧事实仍标记为 active" 的矛盾状态
            replace_id = str(action.get("replace_id", "")).strip()
            replace_target = next((f for f in window if f.get("id") == replace_id), None)
            if replace_target is None:
                print(
                    f"[FactStore] 忽略无效的 replace_id={replace_id!r}"
                    f"（不在比对窗口内），按新增处理"
                )

        # ④ 写入新事实
        new_id = f"f_{uuid.uuid4().hex[:8]}"
        if replace_target is not None:
            replace_target["status"] = "outdated"
            # 回填真正取代它的那条新事实 id
            # （旧实现写死 "pending"，永远不会被更新）
            replace_target["contradicted_by"] = new_id
            replaced = True

        self.facts.append(
            {
                "id": new_id,
                "content": content,
                "category": category,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "status": "active",
                "contradicted_by": None,
            }
        )
        self._seen_this_session.add(seen_key)
        self._save()

        if replaced:
            print(
                f"[FactStore] 取代旧事实 {replace_target['id']} "
                f"[{category}]: {content[:50]}"
            )
            return "replaced"

        print(f"[FactStore] 新增事实 [{category}]: {content[:50]}")
        return "added"

    def add_batch(self, raw_facts):
        """批量添加事实（从 extract_from_messages 的输出）

        单条事实处理失败不能拖垮整批 —— 之前 `rf.get("content","").strip()`
        遇到 `{"content": 42}` 会抛 AttributeError，后面那些**有效**事实
        全部丢失。所以这里先强制转字符串，再逐条兜住异常。

        Args:
            raw_facts: [{"content": "...", "category": "..."}, ...]

        Returns:
            int — 实际新增/取代的条数
        """
        added = 0
        for rf in raw_facts:
            if not isinstance(rf, dict):
                continue
            # content 可能是数字/None/嵌套结构，一律强制成字符串
            content = str(rf.get("content") or "").strip()
            if not content:
                continue
            category = str(rf.get("category") or "信息")
            try:
                result = self.add_fact(content, category)
            except Exception as e:
                print(f"[FactStore] 跳过异常事实 ({type(e).__name__}: {e}): "
                      f"{content[:40]}")
                continue
            if result in ("added", "replaced"):
                added += 1
        if added:
            print(f"[FactStore] 本批次共新增 {added} 条事实")
        return added

    def mark_processed(self, history_index):
        """记录事实提取已推进到完整历史的第几条消息

        只前进不后退，避免与工作记忆滑动窗口的重叠区间重复计数。
        """
        if history_index > self.last_processed_index:
            self.last_processed_index = history_index
            self._save()

    def reset_processed(self):
        """把处理进度归零

        清空历史时必须调用，否则会留下一个"超出新历史长度"的陈旧标记：
        启动归档的判断条件是 `last_processed_index < len(history) - 40`，
        一旦标记停留在旧的大值上，新对话的归档就被永久跳过 ——
        既不会进摘要、也不会提取事实，而且全程没有任何报错。
        """
        if self.last_processed_index != 0:
            self.last_processed_index = 0
            self._save()

    def search(self, query, top_k=5):
        """关键词检索：根据用户输入找到最相关的事实

        当前使用字符重叠评分（MVP）。
        后续可升级为 embedding 语义检索。

        Returns:
            list of fact dicts, sorted by relevance desc
        """
        if not query or not self.active_facts:
            return []

        scored = []
        query_chars = set(query)
        for fact in self.active_facts:
            fact_chars = set(fact.get("content", ""))
            overlap = len(query_chars & fact_chars)
            if overlap > 0:
                # 加分项：类别权重（偏好、承诺更重要）
                category_bonus = 0.2 if fact.get("category") in ("偏好", "承诺") else 0
                score = overlap / max(len(query_chars), 1) + category_bonus
                scored.append((score, fact))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [f for _, f in scored[:top_k]]

    def clear(self):
        """清空所有事实与处理进度

        必须连 last_processed_index 一起归零。否则清空历史后标记仍停留在
        旧的大值上，启动归档的条件 `last_processed_index < len(history)-40`
        永远不成立 —— 新对话既不进摘要也不提取事实，且没有任何报错。
        """
        self.facts.clear()
        self._seen_this_session.clear()
        self.last_processed_index = 0
        self._save()
        print("[FactStore] 所有事实已清空（处理进度已归零）")

    # ---------- 内部方法 ----------

    @staticmethod
    def _normalize(text):
        """规范化事实文本用于比较（去空白 + 去尾部标点）"""
        return "".join(text.split()).rstrip("。！？.!?")

    @staticmethod
    def _format_messages(messages):
        """将消息列表格式化为可读文本"""
        lines = []
        for msg in messages:
            role = "指挥官" if msg.get("role") == "user" else "夏安"
            content = msg.get("content", "")
            if content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _parse_fact_json(text):
        """解析 LLM 返回的事实 JSON 数组

        先走标准解析；失败时用括号匹配扫描器抢救出所有完整闭合的对象，
        避免因末尾对象被 max_tokens 截断而丢掉整批提取结果。
        """
        if not text or not text.strip():
            return []

        cleaned = strip_code_fence(text)

        try:
            data = json.loads(cleaned)
            if isinstance(data, list):
                return [
                    item
                    for item in data
                    if isinstance(item, dict) and str(item.get("content", "")).strip()
                ]
        except (ValueError, TypeError):
            pass

        # 兜底抢救：逐对象提取
        salvaged = [
            obj for obj in salvage_json_objects(cleaned) if str(obj.get("content", "")).strip()
        ]
        if salvaged:
            print(f"[FactStore] JSON 数组解析失败，已抢救出 {len(salvaged)} 条完整事实")
        return salvaged

    def _dedup_check(self, new_content, window):
        """LLM 语义去重/冲突检测（仅比对给定的窗口事实）"""
        existing_text = "\n".join(
            f"[{f.get('id', '?')}] {f.get('content', '')}" for f in window
        )
        try:
            result = post_chat(
                [
                    {"role": "system", "content": _DEDUP_SYSTEM},
                    {
                        "role": "user",
                        "content": _DEDUP_USER.format(
                            existing_facts=existing_text, new_fact=new_content
                        ),
                    },
                ],
                max_tokens=200,
                temperature=0.2,
            )
        except LLMRequestError as e:
            print(f"[FactStore] 去重检查失败: {e.message}")
            return {"action": "add", "reason": "去重检查失败，保守添加"}

        if not result:
            # 调用失败：保守处理，当作新事实添加
            return {"action": "add", "reason": "去重检查失败，保守添加"}

        try:
            parsed = json.loads(strip_code_fence(result))
            if isinstance(parsed, dict) and "action" in parsed:
                return parsed
        except (ValueError, TypeError):
            pass
        return {"action": "add", "reason": "去重解析失败，保守添加"}

    # ---------- 持久化 ----------

    def _load(self):
        """从磁盘加载事实

        兼容三种形态：
          {"facts": [...], "last_processed_index": N}   当前格式
          [...]                                          裸列表（更早期格式）
          其他/损坏                                      视为空

        并逐条校验：缺 id 或缺 content 的条目会让 _dedup_check 里的
        `f['id']` 抛 KeyError，从而拖垮整批归档 —— 这类条目直接丢弃。
        """
        data = load_json(FACT_STORE_FILE, None)

        raw_facts = None
        marker = 0
        if isinstance(data, dict) and isinstance(data.get("facts"), list):
            raw_facts = data["facts"]
            try:
                marker = int(data.get("last_processed_index", 0) or 0)
            except (TypeError, ValueError):
                marker = 0
        elif isinstance(data, list):
            # 更早期的裸列表格式：不能当成"空数据"丢掉，
            # 否则第一次 add_fact 就会把它整个覆盖掉
            raw_facts = data
            print("[FactStore] 检测到旧版裸列表格式，已兼容加载")

        if raw_facts is None:
            self.facts = []
            return

        valid = []
        dropped = 0
        for item in raw_facts:
            if not isinstance(item, dict):
                dropped += 1
                continue
            content = str(item.get("content") or "").strip()
            if not content or not item.get("id"):
                dropped += 1
                continue
            item["content"] = content
            item.setdefault("category", "信息")
            item.setdefault("status", "active")
            item.setdefault("contradicted_by", None)
            valid.append(item)

        self.facts = valid
        self.last_processed_index = max(0, marker)
        if dropped:
            print(f"[FactStore] 丢弃 {dropped} 条无效事实条目")
        print(
            f"[FactStore] 已加载 {len(self.facts)} 条事实 "
            f"(活跃: {len(self.active_facts)}, "
            f"处理进度: 第 {self.last_processed_index} 条)"
        )

    def _save(self):
        """将事实原子写入磁盘"""
        atomic_write_json(
            FACT_STORE_FILE,
            {
                "facts": self.facts,
                "last_processed_index": self.last_processed_index,
            },
        )
