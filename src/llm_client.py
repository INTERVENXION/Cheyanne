"""LLM API 客户端 — 通用 OpenAI 兼容接口"""
import json
import os
import re

from src.api import LLMRequestError, post_chat
from src.config_manager import BASE_DIR, get_prompt_config
from src.memory.memory_manager import MemoryManager
from src.persistence import atomic_write_json, load_json, strip_code_fence
from src.session import now_iso as session_now_iso

HISTORY_FILE = os.path.join(BASE_DIR, "chat_history.json")

# 合法情绪值（与 sprite_manager.EMOTION_FILES 对齐）
_VALID_EMOTIONS = {"happy", "sad", "angry", "surprised", "shy", "neutral", "idle"}

# 兜底回复（当 LLM 返回空内容时使用，避免空白气泡和级联污染）
_FALLBACK_REPLY = "抱歉指挥官，信号好像有点问题…能再说一遍吗？"

# 从 prompt.yaml 解析字数上限用的正则
# 匹配形如「…控制在 200 字以内」「…50-80 字」的表述
_CHAR_LIMIT_RE = re.compile(r"(\d{2,4})\s*字")

# 亲密场景判定关键词。
# prompt.yaml 对两类场景给了不同的字数预算：
#   普通聊天  50-80 字
#   亲密场景  200 字以内
# 代码层若只用一个全局上限，要么砍掉亲密场景、要么放任日常闲聊变长，
# 所以这里按场景分别取上限。
_INTIMATE_MARKERS = (
    "做爱", "亲热", "上床", "脱衣", "脱掉", "脱了", "衣服", "吻", "亲我", "亲一下",
    "抱我", "抱抱", "搂", "摸", "胸", "腿", "腰", "臀", "舔", "含", "进入",
    "插入", "顶", "夹", "高潮", "呻吟", "娇喘", "湿", "硬", "裸", "身体",
    "床上", "一起睡", "要我", "想要", "继续", "深一点", "快一点", "舒服",
)


def _infer_emotion_from_text(text):
    """从纯文本回复中推断情绪 — 最后一道防线

    当 LLM 不输出 JSON 格式、也无法匹配旧版 [标签] 格式时调用。
    通过中文情绪关键词分析，推断最可能的情绪值。

    匹配优先级：shy > happy > sad > angry > surprised > idle
    （害羞优先，因为夏安的角色设定核心就是害羞）
    """
    if not text or not text.strip():
        return "idle"

    # 情绪关键词库（按优先级排序，先匹配到的优先）
    # 注意：不再包含"传感器/减震"等机械词 —— 角色设定已改为纯人类表达，
    # 命中这些词说明回复跑偏了，不应据此判定为"害羞"。
    _EMOTION_KEYWORDS = [
        ("shy", (
            "不好意思", "害羞", "讨厌", "别说了", "脸红", "别笑", "别盯着",
            "太突然", "不要这样", "怎么能", "别说出去", "当没听到", "当没看见",
            "心跳", "手抖", "偷偷", "忍不住", "请不要笑", "请别", "不准看",
            "这、", "那、", "指、", "不、不",
        )),
        ("happy", (
            "哈哈", "嘿嘿", "嘻嘻", "开心", "太好了", "真棒", "喜欢", "高兴",
            "没问题", "交给我", "放心", "很棒", "真好",
            "笑着", "当然", "我很高兴", "棒", "耶", "~",
        )),
        ("sad", (
            "难过", "伤心", "对不起", "抱歉", "担心", "失眠", "熬夜",
            "累", "疲惫", "压力", "受伤", "失去", "困难",
        )),
        ("angry", (
            "生气", "过分", "不行", "不许", "不准", "不好好",
            "又不", "怎么又", "别想", "绝不",
        )),
        ("surprised", (
            "诶", "居然", "难道", "原来", "不会吧", "真的假的",
            "天哪", "天啊", "什么？！", "！？",
        )),
    ]

    for emotion, keywords in _EMOTION_KEYWORDS:
        for kw in keywords:
            if kw in text:
                print(f"[LLM] 纯文本情绪推断: 命中关键词 '{kw}' → {emotion}")
                return emotion

    return "idle"


def _parse_emotion(text):
    """从回复中解析情绪标签，返回 (emotion, cleaned_text)"""
    match = re.match(r"^\[(开心|伤心|生气|惊讶|害羞|默认)\]\s*", text)
    if match:
        emotion_map = {
            "开心": "happy",
            "伤心": "sad",
            "生气": "angry",
            "惊讶": "surprised",
            "害羞": "shy",
            "默认": "idle",
        }
        emotion = emotion_map[match.group(1)]
        cleaned = text[match.end():].strip()
        return emotion, cleaned
    # 如果文本以 { 开头，说明是 JSON 解析失败后的残留，不应展示给用户
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("```"):
        print(
            f"[LLM] 警告: 标签正则未匹配，且文本疑似残缺 JSON，使用兜底回复。"
            f"原始前100字符: {stripped[:100]}"
        )
        return "idle", _FALLBACK_REPLY
    # 纯文本回复：推断情绪而非总是返回 idle
    emotion = _infer_emotion_from_text(text)
    return emotion, text


def _try_parse_json_object(text):
    """尝试把文本解析成 {"emotion": ..., "reply": ...}

    Returns:
        (emotion, reply) 或 None（无法解析 / reply 为空）
    """
    if not text:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None

    if not isinstance(data, dict):
        return None

    emotion = data.get("emotion", "idle")
    reply = data.get("reply", "")
    if emotion not in _VALID_EMOTIONS:
        emotion = "idle"
    if isinstance(reply, str) and reply.strip():
        return emotion, reply.strip()
    return None


def _parse_emotion_json(text):
    """从 JSON 格式回复中解析情绪和正文，失败时逐级 fallback"""
    # 0) 空响应直接返回兜底
    if not text or not text.strip():
        print("[LLM] 警告: API 返回了空内容，使用兜底回复")
        return "idle", _FALLBACK_REPLY

    # 1) 直接解析 JSON
    parsed = _try_parse_json_object(text)
    if parsed:
        return parsed

    # 2) 剥离 markdown 代码块后解析
    cleaned = strip_code_fence(text)
    parsed = _try_parse_json_object(cleaned)
    if parsed:
        return parsed

    # 3) 正则从残缺 JSON 中挽救 reply 字段
    #    LLM 有时会生成截断/嵌套的 JSON（如 reply 内容中出现第二个 {）
    #    此时 json.loads 失败，但正则仍可能提取到有效内容
    for pattern, label in (
        (r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', "残缺"),
        (r'"reply"\s*:\s*"((?:[^"\\]|\\.)+)', "被截断(token限制)"),
    ):
        reply_m = re.search(pattern, text)
        if not reply_m:
            continue
        reply_text = reply_m.group(1).strip().rstrip("{").strip()
        if not reply_text:
            continue
        emo_m = re.search(r'"emotion"\s*:\s*"([^"]*)"', text)
        emotion = emo_m.group(1) if emo_m else "idle"
        if emotion not in _VALID_EMOTIONS:
            emotion = "idle"
        print(f"[LLM] JSON {label}，正则挽救成功: emotion={emotion}, reply={reply_text[:50]}...")
        return emotion, reply_text

    # 4) fallback 到旧版标签正则
    emotion, reply = _parse_emotion(text)
    if not reply or not reply.strip():
        print(f"[LLM] 警告: 标签正则也未匹配到有效回复，使用兜底回复。原始文本: {text[:200]}")
        return emotion, _FALLBACK_REPLY
    return emotion, reply


# ═══════════════════════════════════════════════════════════════
# 机械术语后处理过滤 — 代码层硬兜底
# 即使 LLM 偶尔产出机械/战术术语，也会在此被拦截替换
# ═══════════════════════════════════════════════════════════════

# 自纠正模式：匹配 "xxx...不对，我不说那个，" 并移除
# 例："核心温度还行...不对，我不说那个，就是肚子饿" → 移除纠正部分
# 匹配 ASCII "..." 和 Unicode "…" "⋯" 省略号
_SELF_CORRECT_RE = re.compile(
    r'(?:，|。|！|？|但|不过)?[^。！？\.…⋯，、]{0,20}(?:\.{2,}|[⋯…])+[^。！？]{0,25}?不对[，。]?(?:我)?不说那[个些]，?'
)

# 机械/战术术语 → 人类表达映射表（按顺序应用）
_MECHANICAL_TERM_MAP = [
    # ── 身体感受 ──
    (re.compile(r'核心过热'), '心跳加速'),
    (re.compile(r'核心温度'), '体温'),
    (re.compile(r'散热口'), '身上'),
    (re.compile(r'关节(?:调整|角度|模块)'), '调整'),
    (re.compile(r'关节'), ''),
    (re.compile(r'素体'), '身体'),
    (re.compile(r'(?:引擎|核心)转速'), '心跳'),
    # ── 传感器/电子 ──
    (re.compile(r'传感器(?:模块|数据|反馈|读数)?'), ''),
    (re.compile(r'减震(?:系统|装置|器|模块)?'), ''),
    (re.compile(r'恒温(?:系统|模式|设定|功能)?'), ''),
    # ── 战术装备 ──
    (re.compile(r'战术(?:数据|面板|分析|指令|终端|背心|马甲|装备|套件|肩带|手套)'), ''),
    (re.compile(r'(?:作战|战术)装备'), ''),
    (re.compile(r'护目镜'), ''),
    (re.compile(r'护膝|膝盖护垫'), ''),
    (re.compile(r'(?:战术)?肩带'), ''),
    (re.compile(r'遮阳帽'), ''),
]

# 中文标点 —— 术语被删掉后，用它们清理残留的悬空标点
_CJK_PUNCT = "，。！？、；：…～~"


def _collapse_spaces(text):
    """折叠空格，但不删除换行

    旧实现用 re.sub(r'\\s+', '', text) 把所有空白（含换行）全部删掉，
    连行首缩进都会被吃掉。中文正文里换行和缩进是有意义的结构。
    """
    if not text:
        return text
    # 只折叠空格与制表符，保留 \n
    text = re.sub(r'[ \t]{2,}', ' ', text)
    # 删除 CJK 字符之间的单个空格（"肚子 饿" → "肚子饿"）
    cjk = r'[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]'
    text = re.sub(rf'(?<={cjk}) (?={cjk})', '', text)
    # 删除中文字符与中文标点之间的空格
    text = re.sub(rf'(?<={cjk}) (?=[{re.escape(_CJK_PUNCT)}])', '', text)
    text = re.sub(rf'(?<=[{re.escape(_CJK_PUNCT)}]) (?={cjk})', '', text)
    return text


def _cleanup_punctuation(text):
    """清理术语删除后残留的标点异常

    例如："我想用传感器测一下，然后" → 删掉"传感器"后可能留下"我想用测一下，然后"
    这类问题无法完全自动修复，这里只处理最刺眼的几种：
    连续标点、句首标点、标点两侧的空格。
    """
    if not text:
        return text
    # 连续的中文标点合并
    text = re.sub(r'([，。！？、；：])\1+', r'\1', text)
    # 逗号/顿号紧跟句末标点时，去掉逗号："。，" → "。"
    text = re.sub(r'([。！？])\s*[，、]\s*', r'\1', text)
    # 标点前不留空格
    text = re.sub(rf'\s+([{re.escape(_CJK_PUNCT)}])', r'\1', text)
    # 句首多余的标点
    text = re.sub(rf'^[{re.escape(_CJK_PUNCT)}]+', '', text)
    return text


def _sanitize_reply(text):
    """后处理过滤：移除机械术语和自纠正模式

    这是 prompt 约束之外的硬兜底——即使 LLM 偶尔产出机械术语，
    也会在此被拦截替换。用户永远看不到这些词。

    处理顺序：
    1. 移除自纠正模式（"xxx...不对，我不说那个"）
    2. 逐条替换机械/战术术语
    3. 清理残留标点与空格（保留换行）
    """
    if not text:
        return text

    # 第一步：移除自纠正模式
    prev = None
    while prev != text:
        prev = text
        text = _SELF_CORRECT_RE.sub('', text)

    text = _cleanup_punctuation(text)

    # 第二步：逐条替换机械/战术术语
    for pattern, replacement in _MECHANICAL_TERM_MAP:
        new_text = pattern.sub(replacement, text)
        if new_text != text:
            print("[LLM] 术语过滤: 命中并已替换")
            text = new_text

    # 第三步：清理残留空格与标点（保留换行）
    text = _collapse_spaces(text)
    text = _cleanup_punctuation(text)

    return text.strip()


# 截断时使用的句子分隔符（按优先级排序）
_TRUNCATE_DELIMITERS = ["。", "！", "？", "~", "…", "，", "、"]


def _prompt_char_limits():
    """从 prompt.yaml 解析出 (普通场景上限, 亲密场景上限)

    解析失败时退回 (80, 200) —— 即 prompt.yaml 里的原始约定。
    """
    default = (80, 200)
    try:
        system_prompt = get_prompt_config().get("system_prompt", "") or ""
    except Exception:
        return default

    numbers = sorted({int(m) for m in _CHAR_LIMIT_RE.findall(system_prompt)})
    limits = [n for n in numbers if 20 <= n <= 2000]
    if not limits:
        return default

    # 提示词里同时出现 50/80/200 三类数字：
    # 最小的一批是普通对话预算，最大的是亲密场景预算
    normal = max([n for n in limits if n <= 120], default=default[0])
    intimate = max(limits)
    return normal, intimate


def _is_intimate_scene(context_text):
    """粗略判断当前是否处于亲密场景（用于选择字数上限）"""
    if not context_text:
        return False
    return any(marker in context_text for marker in _INTIMATE_MARKERS)


def _reply_char_limit(user_message=None):
    """获取本次回复适用的字数上限

    Args:
        user_message: 当前用户输入，用于判断场景；None 时返回普通场景上限
    """
    normal, intimate = _prompt_char_limits()
    if user_message and _is_intimate_scene(user_message):
        return intimate
    return normal


def _truncate_reply(text, max_chars=None):
    """将回复截断到 max_chars 以内，优先在句末截断

    截断策略：
    1. 如果文本本身 ≤ max_chars，直接返回
    2. 在 max_chars 范围内找最后一个句末标点（。！？）截断
    3. 找不到句末标点时，退而找逗号/顿号
    4. 实在找不到合适的标点，硬截断并加省略号
    """
    if max_chars is None:
        max_chars = _reply_char_limit()

    if not text or len(text) <= max_chars:
        return text

    # 在限定范围内找最佳截断点
    search_start = max_chars // 2
    for delim in _TRUNCATE_DELIMITERS:
        idx = text.rfind(delim, search_start, max_chars)
        if idx != -1:
            truncated = text[:idx + len(delim)]
            if len(truncated) >= search_start:  # 不能太短
                print(f"[LLM] 回复截断: {len(text)}字 → {len(truncated)}字 (分隔符: '{delim}')")
                return truncated

    # 兜底：硬截断
    truncated = text[:max_chars - 1] + "…"
    print(f"[LLM] 回复硬截断: {len(text)}字 → {len(truncated)}字")
    return truncated


class LLMClient:
    """LLM API 客户端，支持 OpenAI 兼容格式，对话历史持久化到 JSON

    记忆架构：
    - chat_history.json: 完整对话历史（追加式，不截断）
    - memory_summary.json: 早期对话的摘要（由 MemoryManager 管理）
    - 工作记忆: 最近 N 轮完整对话（滑动窗口）
    """

    def __init__(self):
        self._full_history = []          # 完整对话历史（所有消息）
        # 待异步摘要的溢出消息队列。
        # 旧实现是单个槽位，连续两次溢出时前一批会被直接覆盖丢失 ——
        # 那些消息既不在工作记忆里，也没被摘要，等于永久失忆。
        self._pending_batches = []
        self.memory_manager = MemoryManager()
        full_history = self._load_history()
        self.memory_manager.init_from_history(full_history)

    def _load_history(self):
        """从 JSON 文件加载完整对话历史（不再截断）

        逐条校验元素必须是含 content 的 dict：
        历史文件被手工编辑或早期版本写坏时，可能混入字符串/数字/None，
        而下游（会话边界、摘要、事实提取）都在这些元素上调 .get()。
        其中 `last_conversation_end` 是在启动问候的 QTimer 槽里跑的，
        未捕获的 AttributeError 会让 PyQt5 直接 abort 整个进程 ——
        表现为"程序启动约 1 秒后凭空消失"。
        """
        data = load_json(HISTORY_FILE, None)
        if not isinstance(data, list):
            self._full_history = []
            return []

        valid = []
        dropped = 0
        for item in data:
            if not isinstance(item, dict):
                dropped += 1
                continue
            content = item.get("content")
            if not isinstance(content, str) or not content:
                dropped += 1
                continue
            role = item.get("role")
            if role not in ("user", "assistant", "system"):
                dropped += 1
                continue
            valid.append(item)

        self._full_history = valid
        print(f"[LLM] 已加载 {len(valid)} 条历史消息"
              + (f"（丢弃 {dropped} 条无效条目）" if dropped else ""))
        return valid

    def _save_full_history(self):
        """原子保存完整对话历史

        Returns:
            bool — 是否真的写盘成功
        """
        return atomic_write_json(HISTORY_FILE, self._full_history)

    def _build_messages(self, user_message, session_block=None, force_memory=False):
        """构建请求 messages — 委托给 MemoryManager"""
        return self.memory_manager.build_context(
            user_message, session_block=session_block, force_memory=force_memory
        )

    def chat(self, user_message, save_history=True, session_block=None,
             force_memory=False):
        """发送消息并获取回复，返回 (emotion, reply_text) 或 (None, error_msg)

        Args:
            user_message: 用户输入
            save_history: False 时不写入历史与持久化文件
                （适用于系统自动触发的消息，如启动问候）
            session_block: 会话边界提示文本，仅本次启动第一次交互传入。
                它让模型知道"上次已经结束、中间隔了多久"，
                避免把上一次的场景当成当前场景继续演。
            force_memory: 是否无条件注入摘要记忆。
                启动问候没有用户输入，靠关键词门控是取不到记忆的，
                所以需要显式要求注入。
        """
        messages = self._build_messages(
            user_message, session_block=session_block, force_memory=force_memory
        )

        try:
            raw_reply = post_chat(messages)
        except LLMRequestError as e:
            return None, e.message

        print(f"[LLM] 原始返回({len(raw_reply)}字符): {raw_reply[:300]}")

        emotion, clean_reply = _parse_emotion_json(raw_reply)

        # 后处理过滤：移除机械/战术术语（prompt 之外的硬兜底）
        if clean_reply and clean_reply.strip():
            clean_reply = _sanitize_reply(clean_reply.strip())

        # 软长度约束：上限与 prompt.yaml 的约定保持一致
        # （按场景区分：普通对话 80 字，亲密场景 200 字）
        if clean_reply and clean_reply.strip():
            clean_reply = _truncate_reply(
                clean_reply.strip(),
                max_chars=_reply_char_limit(user_message),
            )

        if save_history:
            # 防护：绝不将空回复存入历史，避免级联污染
            if clean_reply and clean_reply.strip():
                # 带真实时间戳 —— 下次启动时用它在"会话边界"里算出隔了多久。
                # 老消息没有 ts 字段，读取方会优雅降级。
                stamp = session_now_iso()
                user_msg = {"role": "user", "content": user_message, "ts": stamp}
                assistant_msg = {"role": "assistant", "content": clean_reply, "ts": stamp}

                # 完整历史始终追加（chat_history.json 保留所有记录）
                self._full_history.append(user_msg)
                self._full_history.append(assistant_msg)
                if not self._save_full_history():
                    # 写盘失败（磁盘满/权限/被占用）：不要把消息留在内存里
                    # 假装记得 —— 下次启动就没了，会让用户困惑。
                    # 回滚内存状态，并在控制台明确报错。
                    del self._full_history[-2:]
                    print("[LLM] 警告: 对话历史写入失败，本轮未记录")
                    return emotion, clean_reply

                # 工作记忆由 MemoryManager 管理（滑动窗口 + 溢出摘要）
                overflow_job = self.memory_manager.add_exchange(user_msg, assistant_msg)
                if overflow_job:
                    # 入队，由 ChatDialog 在后台异步摘要
                    self._pending_batches.append(overflow_job)
            else:
                print("[LLM] 警告: clean_reply 为空，跳过存入历史，避免级联污染")

        return emotion, clean_reply

    def clear_history(self):
        """清空对话历史（包括完整历史 + 工作记忆 + 摘要 + 事实）"""
        self._full_history.clear()
        self.memory_manager.clear()
        self._pending_batches.clear()
        self._save_full_history()

    # ---------- 异步摘要接口（由 ChatDialog 调用）----------

    @property
    def has_pending_summary(self):
        """是否有待处理的摘要任务"""
        if self.memory_manager.peek_pending_job():
            return True
        return bool(self._pending_batches)

    def peek_pending_summary(self):
        """查看待摘要任务但不出队（优先启动归档，其次运行时溢出）

        与旧 consume_pending_summary 的区别：旧实现一取出就 pop，
        处理中一旦抛异常或程序退出，这批消息就永久消失。
        现在只有处理成功后才 ack 出队，失败则保留重试。
        """
        # 优先处理启动时的历史归档
        job = self.memory_manager.peek_pending_job()
        if job:
            return job

        # 其次按 FIFO 处理运行时的溢出消息
        if self._pending_batches:
            return self._pending_batches[0]

        return None

    def ack_pending_summary(self, job=None):
        """确认任务处理成功，出队

        传入 job 时按身份确认：归档批次处理完会自动排下一批，
        此时队列头部已经不是刚处理的这个任务，不能再 pop。
        """
        if self.memory_manager.peek_pending_job() is not None:
            return self.memory_manager.ack_pending_job(job)
        if self._pending_batches and (
            job is None or self._pending_batches[0] is job
        ):
            return self._pending_batches.pop(0)
        return None

    def run_summarization(self, job):
        """执行摘要处理（在后台线程中调用）"""
        self.memory_manager.process_overflow(job)
