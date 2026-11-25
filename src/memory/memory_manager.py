"""记忆管理器 — 编排工作记忆 + 摘要记忆 + 事实记忆，构建 LLM 上下文"""
from src.config_manager import get_memory_config, get_prompt_config
from src.memory.summarizer import Summarizer
from src.memory.fact_store import FactStore

# 工作记忆默认保留的**轮数**（1 轮 = 1 问 + 1 答 = 2 条消息）
#
# 实际值可在 config.yaml 的 memory.working_memory_turns 覆盖。
# 为什么敢放到 100 轮：DeepSeek 上下文窗口 1M token，实测每轮问答
# 平均约 74 token，100 轮只占约 0.74%。之前固定 20 轮，等于让
# 99.5% 的窗口空着，却把更早的内容压成一份有损摘要。
DEFAULT_WORKING_MEMORY_TURNS = 100

# 每次溢出处理的默认轮数（10 轮 = 20 条）
# 与窗口同比例放大：窗口越大，越没必要每两条消息就触发一次摘要。
DEFAULT_OVERFLOW_TURNS = 10

# 兜底：轮数配置异常时的最小/最大值，防止填错把窗口搞成 0 或离谱的大数
_MIN_TURNS = 1
_MAX_TURNS = 2000

# 上下文注入的最大事实条数
MAX_FACTS_IN_CONTEXT = 8

# 单批归档最多处理多少条消息。
#
# 为什么要有上限：启动补齐历史时可能积压几百上千条未摘要的消息，
# 一次性塞给摘要模型会超出上下文/成本，也会让单次启动卡很久。
# 60 条约 2200 token，响应 400 字摘要，是一次合理的请求规模。
# 超出的部分会自动续批（process_overflow 处理完一批后接着排下一批）。
SUMMARY_BATCH = 60



# 事实检索触发词 — 只有用户消息中包含以下短语时，才从事实库检索关联记忆
# 目的：避免每次对话都自动注入旧记忆，仅在用户主动提起过往内容时触发
_MEMORY_FACT_TRIGGERS = (
    "记得吗", "你还记得", "记不记得", "不记得",
    "之前你说过", "之前你说", "之前说过", "之前提到",
    "上次聊到", "上次说的", "上次你说", "上一次",
    "以前说过", "以前提到", "以前聊过",
    "回忆", "回想", "想起来",
    "你说过", "你提到过", "你讲过",
    "答应过我", "答应过", "承诺过",
    "告诉过我", "跟我说过",
)


def _memory_cfg():
    """读取记忆配置（失败时返回空 dict，绝不抛异常）

    MemoryManager 在启动路径上被构造，这里抛异常会连带
    打断整个启动流程（PyQt5 对槽函数未捕获异常会直接 abort）。
    """
    try:
        cfg = get_memory_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _positive_int_or(value, default):
    """配置值转正整数；None/非法/<=0 时用默认值"""
    if isinstance(value, bool):      # bool 是 int 子类，单独排除
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


class MemoryManager:
    """管理对话记忆系统

    三层结构：
    1. 工作记忆 — 最近 N 条完整消息，直接注入上下文
    2. 摘要记忆 — 早期对话的压缩文本，注入 system prompt 之后
    3. 事实记忆 — 结构化关键信息，按需检索注入
    """

    def __init__(self):
        self.working_memory = []  # list of {"role": ..., "content": ...}
        self.summarizer = Summarizer()
        self.fact_store = FactStore()

        # 工作记忆容量从配置读取（config.yaml: memory.working_memory_turns）
        turns = _positive_int_or(_memory_cfg().get("working_memory_turns"),
                                 DEFAULT_WORKING_MEMORY_TURNS)
        turns = max(_MIN_TURNS, min(turns, _MAX_TURNS))
        self.working_memory_turns = turns
        # 每轮 = 1 问 + 1 答 = 2 条消息
        self.working_memory_size = turns * 2
        # 一次溢出移出多少条：默认 10 轮（20 条），
        # 但不超过窗口的一半，避免窗口设得很小时一溢出就清空。
        self.overflow_batch = max(
            2, min(DEFAULT_OVERFLOW_TURNS * 2, self.working_memory_size // 2)
        )

        # 待异步处理的任务。
        #
        # 结构固定为 {"messages": [...], "is_archive": bool, "end_index": int}。
        # 旧实现把这三件事拆成三个实例属性
        # （_pending_overflow / _pending_is_archive / _pending_archive_end），
        # 只要批次被取出后没走到 process_overflow（异常、退出、被 terminate），
        # 标记就会残留，下一批运行时溢出被误当成归档批次、
        # 用别人的结束下标去推进进度 —— 进度虚高，那段内容以后不再补处理。
        # 现在整包传递，没有隐藏状态可以残留。
        self._pending_job = None

        # 供"归档续批"使用的历史引用（由 init_from_history 注入）。
        # 只读切片用，不持有副本以免内存翻倍。
        self._history_ref = []

    # ---------- 触发词检测 ----------

    @staticmethod
    def _has_memory_reference(text):
        """检测用户消息是否包含记忆触发词

        只有当用户消息包含触发词（如"记得吗"、"上次说过"等），
        才从事实库检索相关记忆。避免日常闲聊中自动注入旧记忆。
        """
        for trigger in _MEMORY_FACT_TRIGGERS:
            if trigger in text:
                return True
        return False

    # ---------- 初始化 ----------

    def init_from_history(self, full_history):
        """从完整历史加载，自动拆分为摘要 + 工作记忆

        Args:
            full_history: 完整的消息列表 (从 chat_history.json 加载)
        """
        if not full_history:
            return

        # 供归档续批使用（引用而非副本）
        self._history_ref = full_history

        total = len(full_history)

        # 自愈：处理进度标记必须落在历史长度范围内。
        # 它可能因为"清空历史"（旧版本不归零）或手工替换数据文件
        # 而停留在超出当前历史的大值上 —— 那时归档条件永远不成立，
        # 新对话既不进摘要也不提取事实，且全程无报错。
        if self.fact_store.last_processed_index > total:
            print(f"[Memory] 处理进度标记 {self.fact_store.last_processed_index} "
                  f"超出历史长度 {total}，已校正")
            self.fact_store.reset_processed()
        if self.summarizer.covered_index > total:
            print(f"[Memory] 摘要覆盖进度 {self.summarizer.covered_index} "
                  f"超出历史长度 {total}，已校正")
            self.summarizer.set_covered_index(total)

        restore = bool(get_memory_config().get("restore_working_memory", False))

        if not restore:
            # 默认路径：不恢复上次的原始对话记录。
            #
            # 为什么这是默认值：
            #   旧实现把上次最近 20 轮**原文**放进上下文。那些原文没有时间戳，
            #   而且上一轮的结尾就贴在当前输入前面 —— 模型会自然地顺着它往下演。
            #   用户遇到的现象就是这样来的：
            #     上次结尾「指挥官...你刚才答应要一起睡的。现在又要去书房？」
            #     下次启动时这句原文仍在上下文里，夏安于是继续那个场景，
            #     哪怕已经过去了十几个小时。
            #   只保留"记得"层面的长期记忆（摘要 + 事实），
            #   会让每次启动都是一次干净的重新见面。
            print(
                f"[Memory] 未恢复上次工作记忆（restore_working_memory=false），"
                f"仅保留摘要与事实记忆；历史共 {total} 条"
            )
            # 注意：这里**不再依赖窗口大小**。只要还有没被摘要覆盖的历史，
            # 就排一批去补 —— 否则"历史条数 ≤ 窗口"时什么都不做，
            # 那段时间的对话就永久停留在无人区（既不在工作记忆，也不在摘要）。
            self._schedule_archive(total)
            return

        if total <= self.working_memory_size:
            # 历史不长，全部放入工作记忆
            self.working_memory = list(full_history)
            print(f"[Memory] 加载工作记忆: {len(self.working_memory)} 条消息")
            # 即使恢复了工作记忆，仍要把更早的部分补进摘要
            self._schedule_archive(total)
            return

        self._schedule_archive(total)

    def _schedule_archive(self, total):
        """把"还没进摘要"的历史区间排成一批待处理任务

        与旧实现的区别（这是本次修复的核心）：

          旧：归档区间 = history[0 : total - 工作记忆窗口]
              窗口是 200 条时，166 条历史一条都不满足条件，
              摘要从 6 月起再没更新过 —— 用户每次重启都像失忆。

          新：归档区间 = history[摘要覆盖进度 : 摘要覆盖进度 + 每批上限]
              只要游标没到历史末尾，就继续补，与窗口大小彻底解耦。
              每批最多 SUMMARY_BATCH 条，避免一次请求塞进上千条消息。

        分批处理完后会自动续下一批（见 process_overflow）。
        """
        if not self._history_ref:
            return

        covered = max(0, min(self.summarizer.covered_index, total))
        # 事实提取有独立的进度标记，取两者中更落后者，保证都不漏
        fact_done = max(0, min(self.fact_store.last_processed_index, total))
        cursor = min(covered, fact_done)

        if cursor >= total:
            if total:
                print("[Memory] 摘要与事实已覆盖全部历史，无需处理")
            return

        end = min(cursor + SUMMARY_BATCH, total)
        batch = self._history_ref[cursor:end]
        if not batch:
            return

        self._pending_job = {
            "messages": batch,
            "is_archive": True,
            "end_index": end,
        }
        remaining = total - end
        extra = f"，剩余 {remaining} 条待后续批次" if remaining else ""
        print(f"[Memory] {len(batch)} 条历史待摘要+事实提取 "
              f"（第 {cursor}→{end} 条，共 {total} 条{extra}）")

    # ---------- 上下文构建 ----------

    def build_context(self, user_message, session_block=None, force_memory=False):
        """构建发给 LLM 的完整 messages 数组

        上下文结构:
        1. 角色 system prompt
        2. 会话边界（仅每次启动后的第一轮，由调用方传入）
        3. 记忆（摘要 + 事实，默认每轮都注入）
        4. 工作记忆（最近 N 轮原文，始终注入）
        5. 当前用户输入

        Args:
            user_message: 当前用户输入
            session_block: 会话边界提示文本，仅本次启动第一次交互时传入
            force_memory: 无条件注入摘要记忆（启动问候用）

        Returns:
            list of {"role": ..., "content": ...}
        """
        prompt_config = get_prompt_config()
        system_prompt = prompt_config.get("system_prompt", "")

        messages = [{"role": "system", "content": system_prompt}]

        # ② 会话边界 —— 告诉模型"上次已经结束、中间隔了多久、现在是重新见面"
        #    必须在记忆之前给出，否则模型会先把摘要当成当前场景
        if session_block:
            messages.append({"role": "system", "content": session_block})

        # ③ 记忆注入
        #
        # 这里曾经是本次 bug 的第二层原因：只有带 session_block（会话第一句）
        # 或 force_memory（启动问候）时才注入摘要。而 session_block 是一次性的，
        # 第一句用掉就没了 —— 于是从第二句起，请求里再也看不到任何长期记忆，
        # 用户体感就是"她每句话都失忆一次"。
        #
        # 现在：默认每轮都注入摘要 + 事实（约 460 token/次，占 1M 窗口的 0.05%）。
        # 措辞里已经写明"这是你记得的事、不是正在发生的事、不要主动提起"，
        # 所以常驻不会导致她反复翻旧账。
        #
        # 命中记忆触发词时切换到"按关键词检索相关事实"的路径，
        # 这样即使事实库很大，也只挑与当前话题相关的几条。
        triggered = self._has_memory_reference(user_message)
        if triggered:
            self._inject_memory(messages, query=user_message, force_summary=True)
        elif session_block or force_memory or self._always_inject():
            self._inject_memory(messages, force_summary=True, force_facts=True)

        # ④ 注入工作记忆（最近的完整对话）
        messages.extend(self.working_memory)

        # ⑤ 当前用户输入
        messages.append({"role": "user", "content": user_message})

        return messages

    def _always_inject(self):
        """是否每轮都注入长期记忆（config: memory.always_inject，默认开）"""
        try:
            value = get_memory_config().get("always_inject", True)
        except Exception:
            return True
        return bool(value)

    def _inject_memory(self, messages, query=None, force_summary=False,
                       force_facts=False):
        """把摘要记忆和事实记忆追加进 messages

        两条路径共用同一套"回忆姿态"措辞，区别只在触发的时机：

        - force_summary=True（会话开始 / 每轮常驻）：主动给出摘要，
          但强调那都是过去的事，不要接着演
        - query 不为空（对话中提起过往）：按关键词检索相关事实 + 摘要

        Args:
            messages: 待追加的 messages 列表（原地修改）
            query: 检索用的用户输入
            force_summary: 是否无条件注入摘要
            force_facts: 是否无条件注入事实（未命中关键词时用）
        """
        # 事实记忆
        #
        # 两条来源：
        #   1. 有 query（命中"记得"类触发词）→ 按相关性检索
        #   2. force_facts → 取最近的几条常驻
        # 旧实现只有第 1 条路径，于是那 15 条事实实际上永远进不了上下文
        # （用户几乎不会刻意说"你还记得吗"），而注释却写着"会话开始：摘要 + 事实"。
        if query:
            relevant_facts = self.fact_store.search(query, top_k=MAX_FACTS_IN_CONTEXT)
        elif force_facts:
            relevant_facts = self.fact_store.active_facts[-MAX_FACTS_IN_CONTEXT:]
        else:
            relevant_facts = []

        if relevant_facts:
            facts_text = (
                "【你记得的事 — 关于指挥官的长期印象】\n"
                + "\n".join(f"• {f['content']}" for f in relevant_facts)
                + "\n\n以上是你**记得**的事，属于背景知识。"
                "如果和当前对话有关，可以自然地体现出来；"
                "无关就不用提，更不要逐条复述或提到这份清单本身。"
            )
            messages.append({"role": "system", "content": facts_text})

        # 摘要记忆
        if self.summarizer.has_summary and (query or force_summary):
            if query:
                intro = (
                    "【你记得的事 — 你和指挥官之前聊过的内容】\n"
                    "你主动回想起了这些，用来回答指挥官刚刚提起的话题。\n\n"
                )
                outro = (
                    "\n\n请只把其中与当前话题相关的部分自然地用上。"
                    "这些是**已经发生过的往事**，不是正在进行的场景 —— "
                    "不要顺着它往下演，也不要逐字复述。"
                )
            else:
                intro = (
                    "【你记得的事 — 你和指挥官之前相处的内容】\n"
                    "这些是你**记得**的经历，是你的背景记忆。\n\n"
                )
                outro = (
                    "\n\n请把这些当作「你记得的事」，而不是「正在发生的事」。"
                    "不要逐字复述，也不要主动把话题拉回这些往事上 —— "
                    "它们只是让你在指挥官提起时不会一脸茫然。"
                )
            messages.append(
                {"role": "system", "content": intro + self.summarizer.summary + outro}
            )

    # ---------- 对话更新 ----------

    def add_exchange(self, user_msg, assistant_msg):
        """添加一轮对话到工作记忆，返回溢出消息（如有）

        Args:
            user_msg: {"role": "user", "content": "..."}
            assistant_msg: {"role": "assistant", "content": "..."}

        Returns:
            dict or None — 待处理任务（无溢出时返回 None）
        """
        self.working_memory.append(user_msg)
        self.working_memory.append(assistant_msg)

        if len(self.working_memory) > self.working_memory_size:
            # 取最旧的 overflow_batch 条移出工作记忆
            overflow = self.working_memory[:self.overflow_batch]
            self.working_memory = self.working_memory[self.overflow_batch:]
            print(
                f"[Memory] 工作记忆溢出 {len(overflow)} 条 → 待摘要+事实提取 "
                f"(工作记忆保留 {len(self.working_memory)} 条)"
            )
            # 运行时溢出不是"归档"：消息还在完整历史里，进度游标不要动。
            return {"messages": overflow, "is_archive": False, "end_index": 0}

        return None

    # ---------- 异步处理（摘要 + 事实提取）----------

    @property
    def pending_job(self):
        """当前待处理任务（由 ChatDialog 在后台线程中消费）"""
        return self._pending_job

    def peek_pending_job(self):
        """查看待处理任务但不取出

        为什么要 peek/ack 分离：旧实现"先出队再处理"，
        处理过程中抛异常或程序退出，这批消息就永久消失
        （既不在工作记忆，也不在队列，没进摘要也没提取事实，全程无提示）。
        """
        return self._pending_job

    def ack_pending_job(self, job=None):
        """确认任务处理成功，出队

        必须按"身份"确认：归档批次处理完会**自动排下一批**，
        此时 _pending_job 已经指向新批次。若无条件清空，
        刚排好的下一批会被这一步误删 —— 表现为"只处理了第一批就停了"。
        """
        current = self._pending_job
        if job is not None and current is not job:
            # 当前挂着的已经是新批次，不能清
            return None
        self._pending_job = None
        return current

    def nack_pending_job(self):
        """处理失败：保留任务，下次再试（由 SummaryWorker 记录）"""
        return self._pending_job

    def process_overflow(self, job):
        """对一个任务执行摘要 + 事实提取（在后台线程中调用）

        摘要和事实提取共用同一批消息，
        但使用独立的 LLM 调用和提示词。

        Args:
            job: {"messages": [...], "is_archive": bool, "end_index": int}
                 为兼容旧调用也接受裸的消息列表。
        """
        if isinstance(job, dict):
            overflow_messages = job.get("messages") or []
            is_archive = bool(job.get("is_archive"))
            archive_end = int(job.get("end_index") or 0)
        else:
            overflow_messages = job or []
            is_archive = False
            archive_end = 0

        if not overflow_messages:
            return

        print(f"[Memory] 开始后台处理 {len(overflow_messages)} 条消息...")

        # 摘要（方案 B）
        summary_ok = self.summarizer.add_batch(overflow_messages)
        if summary_ok:
            print("[Memory] ✓ 后台摘要完成")
        else:
            # 可能是 API 超时、返回空、或无需更新——非致命
            print("[Memory] - 后台摘要跳过 (API 未响应或无需更新)")

        # 事实提取（方案 C）
        raw_facts = self.fact_store.extract_from_messages(overflow_messages)
        facts_ok = bool(raw_facts)
        if facts_ok:
            added = self.fact_store.add_batch(raw_facts)
            print(f"[Memory] ✓ 后台事实提取完成 (新增 {added} 条)")
        else:
            # 对话中无可提取的事实，或 API 未响应——正常情况
            print("[Memory] - 本批次无可提取的新事实")

        # 推进进度游标。
        #
        # 只有"归档"批次才推进 —— 运行时溢出批次的消息仍在完整历史末尾，
        # 推进游标会让进度虚高，那段内容以后不再补处理。
        #
        # 而且**只在真正成功时**推进。旧实现在 API 超时/断网时也照样推进，
        # 于是游标声称"已处理到第 N 条"而实际一条都没提取到，
        # 配合"从游标续处理"的逻辑，这段内容就永久漏掉了。
        if is_archive:
            if summary_ok or facts_ok:
                if archive_end > 0:
                    # 摘要覆盖进度与事实提取进度一起推进
                    self.summarizer.set_covered_index(archive_end)
                    self.fact_store.mark_processed(archive_end)
                    # 若还有没覆盖的历史，接着排下一批
                    self._schedule_archive(len(self._history_ref))
                else:
                    # 兜底：没有记录绝对下标时，至少不要写入一个错误的值
                    print("[Memory] 归档批次缺少结束下标，跳过进度推进")
            else:
                print("[Memory] 本批次摘要与事实均未成功，进度保持不变，"
                      "下次启动会重试")

    # ---------- 清空 ----------

    def clear(self):
        """清空所有记忆（工作记忆 + 摘要 + 事实）"""
        self.working_memory.clear()
        self._pending_job = None
        self._history_ref = []
        self.summarizer.clear()
        # FactStore.clear() 会一并把 last_processed_index 归零 ——
        # 否则清空后归档条件永远不成立，新对话既不进摘要也不提取事实
        self.fact_store.clear()
        print("[Memory] 所有记忆已清空（工作记忆 + 摘要 + 事实）")
