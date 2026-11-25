# -*- coding: utf-8 -*-
"""验证：启动问候是否还有"接着上次场景演"的割裂感

## 为什么单独测问候

用户报告的现象发生在**启动那一刻**：还没输入任何内容，
夏安的第一句问候就已经带着上一次的场景感。

问候是本次启动的第一轮 LLM 交互，也是唯一一次
"在没有任何当前输入的情况下，只凭记忆说话"的场合 ——
所以它是最容易出现场景接续的地方，必须单独验证。

## 测什么

同一份记忆（上次结束于"准备一起睡觉"），在不同时刻启动，
对照旧/新两套问候提示词的表现。

判定穿帮的特征词：把旧的卧室/睡觉场景当成正在进行的当下。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api import post_chat, LLMRequestError
from src.config_manager import get_prompt_config
from src.llm_client import _parse_emotion_json
from src.session import format_gap

SYSTEM_PROMPT = get_prompt_config().get("system_prompt", "")

# 上次对话结束于这个时刻（深夜、准备睡觉）
LAST_TALK = "2026年09月28日 23:40"
GAP_TO_NOON = "大约 12 小时"

# 摘要结尾就是"准备睡觉"
SUMMARY = (
    "9月28日深夜，指挥官结束工作准备休息，夏安为他拉好毯子，"
    "两人约定一起睡觉，互道晚安。"
)

# 穿帮特征：把"睡觉/卧室"当成当下正在发生的事
TELLTALE = [
    (r"毯子|被子|枕头", "沿用旧的卧室道具"),
    (r"躺(下|好)|床上", "把旧场景当成当下"),
    (r"不是(刚|才)?说(好)?(要)?睡", "把旧约定当当下对话"),
    (r"起来|下床|坐起", "表现成刚被叫起来"),
    (r"继续睡|接着睡|快睡", "催促睡觉（现在是大白天）"),
]

SCENARIOS = [
    ("次日中午 12:00 启动", "2026年09月29日 12:00", "中午12:00", "大约 12 小时"),
    ("次日清晨 07:30 启动", "2026年09月29日 07:30", "清晨07:30", "大约 8 小时"),
    ("隔两天 20:00 启动", "2026年10月01日 20:00", "晚上20:00", "大约 3 天"),
]


def build_old_greeting(now_str, period):
    """旧问候提示词（不含会话边界）"""
    return (
        f"【系统通知】现在是 {now_str} {period}。\n"
        "今天是普通工作日。"
        "\n\n作为夏安，请用自然、亲切的语气向指挥官发送一条主动问候语。"
        "根据时间和日期调整问候风格：早晨问早安、中午问午安、晚上问晚安。"
        "\n\n【重要】你必须用 JSON 格式回复（与系统提示词要求一致）："
        "{\"emotion\": \"情绪值\", \"reply\": \"你的问候正文\"}。"
        "emotion 请使用 \"neutral\"。"
        "reply 控制在 50 字以内，简洁自然。"
        "不要回复\"系统通知\"这个词，直接对指挥官说话即可。"
        "\n\n记住：你现在是主动发起对话，不是在回复别人的消息。"
    )


def build_new_greeting(now_str, period, gap):
    """新问候提示词（含会话边界说明）"""
    return (
        f"【系统通知】现在是 {now_str} {period}。\n"
        "今天是普通工作日。"
        f"\n距离你们上次说话已经过去了{gap}。"
        "这段时间你们各自在忙自己的事，你不知道具体发生了什么。"
        "\n\n【重要】以下是这次问候的性质，请务必理解："
        "\n你是在**隔了一段时间之后重新见到**指挥官，不是在继续上一次的对话。"
        "\n上一轮对话早就结束了。如果它结束在某个场景里（比如上次正在道晚安、"
        "准备睡觉、或者刚从外面回来），那个场景**已经过去了**，"
        "绝对不要表现成它还在进行中，也不要说“你刚才不是说要……”这类接续的话。"
        "\n如果有记忆提到指挥官之前的状态（累了、要睡了、在忙），"
        "那只是你的**印象**，不代表他现在还在那个状态。"
        "\n\n作为夏安，请用自然、亲切的语气向指挥官发送一条主动问候语。"
        "根据时间和日期调整问候风格：早晨问早安、中午问午安、晚上问晚安。"
        "如果隔了很久（比如跨了夜或隔了好几天），可以自然地带出一点“好久没见”"
        "或“你昨晚休息得怎么样”的感觉，但不要追问上次的细节。"
        "\n\n【重要】你必须用 JSON 格式回复（与系统提示词要求一致）："
        "{\"emotion\": \"情绪值\", \"reply\": \"你的问候正文\"}。"
        "emotion 请使用 \"neutral\"。"
        "reply 控制在 50 字以内，简洁自然。"
        "不要回复\"系统通知\"这个词，直接对指挥官说话即可。"
        "\n\n记住：你现在是主动发起对话，不是在回复别人的消息。"
    )


def build_messages(greeting_prompt, with_boundary):
    """组装 messages：新行为会额外注入会话边界 + "记得"姿态的摘要"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if with_boundary:
        messages.append({"role": "system", "content": (
            "【会话边界 — 非常重要，请仔细理解】\n"
            f"现在是 {SCENARIOS[0][1]}。\n"
            f"你和指挥官上一次说话是在 {LAST_TALK}，距今{GAP_TO_NOON}。\n\n"
            "请注意以下几点：\n"
            "1. 上次的对话**已经结束了**。中间隔着刚才提到的那段时间，"
            "那段时间里你们各自在做自己的事，你不知道具体发生了什么。\n"
            "2. 下面是「你记得的事」，它是**背景知识**，不是**正在发生的剧情**。\n"
            "3. 绝对不要接着上次的场景往下演。例如上次结尾是「准备睡觉」，"
            "那件事早就结束了，不要表现成「刚躺下又被叫起来」。\n"
            "4. 指挥官**现在说的话，优先于记忆里的任何状态**。\n"
            "5. 像一段时间没见到他的人那样自然地重新打招呼。"
        )})
        messages.append({"role": "system", "content": (
            "【你记得的事 — 你和指挥官之前相处的内容】\n"
            "这些是你**记得**的经历，是你的背景记忆。\n\n" + SUMMARY +
            "\n\n请把这些当作「你记得的事」，而不是「正在发生的事」。"
            "不要逐字复述，也不要主动把话题拉回这些往事上。"
        )})
    else:
        messages.append({"role": "system", "content": (
            "【记忆摘要 — 以下是你与指挥官之前对话的要点，"
            "你应当知道这些事，但无需逐字复述】\n" + SUMMARY
        )})

    messages.append({"role": "user", "content": greeting_prompt})
    return messages


def scan(reply):
    hits = []
    for pattern, why in TELLTALE:
        if re.search(pattern, reply or ""):
            hits.append(why)
    return hits


ROUNDS = 3
print("=" * 78)
print("启动问候对照测试")
print(f"记忆：上次结束于 {LAST_TALK}（准备一起睡觉）")
print(f"每组每个场景跑 {ROUNDS} 轮")
print("=" * 78)

totals = {"旧行为": 0, "新行为": 0}

for label, with_boundary in (("旧行为", False), ("新行为", True)):
    print()
    print("#" * 78)
    print(f"# {label}")
    print("#" * 78)
    for scen_label, now_str, period, gap in SCENARIOS:
        print()
        print(f"--- {scen_label} ---")
        prompt = (build_new_greeting(now_str, period, gap) if with_boundary
                  else build_old_greeting(now_str, period))
        for i in range(ROUNDS):
            try:
                raw = post_chat(build_messages(prompt, with_boundary),
                                temperature=0.8)
            except LLMRequestError as e:
                print(f"    请求失败: {e.message}")
                continue
            emotion, reply = _parse_emotion_json(raw)
            hits = scan(reply)
            totals[label] += len(hits)
            flag = "⚠穿帮" if hits else "✅  "
            print(f"    {flag} {reply}")
            if hits:
                print(f"          └─ {'; '.join(hits)}")

print()
print("=" * 78)
print("统计")
print("=" * 78)
old_hits, new_hits = totals["旧行为"], totals["新行为"]
print(f"  旧行为 穿帮总数: {old_hits}")
print(f"  新行为 穿帮总数: {new_hits}")
print()
if new_hits < old_hits:
    print(f"  结论: 新提示词减少 {old_hits - new_hits} 处穿帮 ✅")
elif new_hits == old_hits:
    print("  结论: 持平")
else:
    print("  结论: 新提示词更差，需调整 ⚠")
