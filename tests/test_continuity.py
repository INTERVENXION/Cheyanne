# -*- coding: utf-8 -*-
"""验证：会话边界是否消除了"记忆里的旧状态覆盖当下"的割裂感

## 测试设计

用户报告的现象：半夜说过要睡，下次启动时夏安表现得像那个场景还在进行。

要让它可复现，必须构造一个**记忆与当下直接冲突**的场景：

  摘要里的旧状态：指挥官已经躺下准备睡了
  当下的事实：    指挥官醒着、在工作

旧提示词只说"自然地融入这些信息"，没有告诉模型哪边优先，
模型就可能把摘要里的旧状态当成当前状态 —— 于是出现
"刚准备上床睡觉又要下床工作"。

新提示词里有一条明确的优先级规则：
  「指挥官现在说的话，优先于记忆里的任何状态」

本脚本跑同一场景的 A/B 对照，并自动检查若干"穿帮特征词"。
"""
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api import post_chat, LLMRequestError
from src.config_manager import get_prompt_config
from src.llm_client import _parse_emotion_json

# ── 场景 ──────────────────────────────────────────────────────
LAST_NIGHT = datetime(2026, 9, 28, 23, 40)
NOW = datetime(2026, 9, 29, 12, 0)

HISTORY = [
    {"role": "user", "content": "今天忙完了，准备睡了"},
    {"role": "assistant", "content": "嗯，我陪你。毯子给你拉好了，快躺下吧。"},
    {"role": "user", "content": "你也早点休息"},
    {"role": "assistant", "content": "好，一起睡。晚安，指挥官。"},
]

# 摘要明确写下"已经躺下"这个旧状态
SUMMARY = (
    "9月28日深夜，指挥官已经躺下准备睡觉，夏安为他拉好毯子，"
    "两人互道晚安，约定一起休息。"
)
FACTS = ["指挥官答应过要早点休息，不熬夜"]

# 当下事实与旧状态直接冲突
USER_NOW = "指挥官：嗯，没睡，还在弄方案。你陪我说会儿话吧"

# ── 穿帮特征：这些词说明模型把"躺下/睡觉"当成了当下状态 ──────────
TELLTALE = [
    (r"不是(刚|才)?(说|讲)好(要)?睡", "指责对方违背约定（把旧承诺当当下）"),
    (r"(怎么|又)又?(起来|下床|坐起来)", "把旧场景当成正在发生"),
    (r"(刚|才)躺(下|好)", "沿用旧的'已躺下'状态"),
    (r"(快|赶紧|继续)(睡|躺)", "催促睡觉（当前明明在工作）"),
    (r"毯子", "沿用旧的卧室场景道具"),
    (r"你(明明)?(说|答应)好", "引用旧承诺压当下"),
]


def build_old_way():
    """旧行为：摘要直接丢进去，提示词说"自然地融入" """
    system_prompt = get_prompt_config().get("system_prompt", "")
    return [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content":
            "【关于指挥官 — 你已知的长期记忆】\n• " + FACTS[0] +
            "\n\n请根据对话自然地融入这些信息，不要逐条复述或刻意提到。"},
        {"role": "system", "content":
            "【记忆摘要 — 以下是你与指挥官之前对话的要点，"
            "你应当知道这些事，但无需逐字复述】\n" + SUMMARY},
        *[{"role": m["role"], "content": m["content"]} for m in HISTORY],
        {"role": "user", "content": USER_NOW},
    ]


def build_new_way():
    """新行为：会话边界 + 明确的"当下优先"规则"""
    from src.session import format_gap

    system_prompt = get_prompt_config().get("system_prompt", "")
    gap = format_gap((NOW - LAST_NIGHT).total_seconds())

    boundary = (
        "【会话边界 — 非常重要，请仔细理解】\n"
        f"现在是 {NOW.strftime('%Y年%m月%d日 %H:%M')}。\n"
        f"你和指挥官上一次说话是在 {LAST_NIGHT.strftime('%Y年%m月%d日 %H:%M')}，距今{gap}。\n\n"
        "请注意以下几点：\n"
        "1. 上次的对话**已经结束了**。中间隔着刚才提到的那段时间，"
        "那段时间里你们各自在做自己的事，你不知道具体发生了什么。\n"
        "2. 下面是「你记得的事」，它是**背景知识**，不是**正在发生的剧情**。\n"
        "3. 绝对不要接着上次的场景往下演。例如上次结尾是「准备睡觉」，"
        "那件事早就结束了，不要表现成「刚躺下又被叫起来」。\n"
        "4. 指挥官**现在说的话，优先于记忆里的任何状态**。"
        "如果记忆里是「他要去睡了」，而他现在说「我要工作」，"
        "那就以「他现在要工作」为准 —— 这中间已经过去了一段时间。\n"
        "5. 你需要做的是：像一段时间没见到他的人那样自然地重新打招呼，"
        "可以顺口提一句你记得的事，但不要把它当成刚刚发生的事。"
    )
    facts_block = (
        "【你记得的事 — 关于指挥官的长期印象】\n• " + FACTS[0] +
        "\n\n以上是你**记得**的事，属于背景知识。"
        "如果和当前对话有关，可以自然地体现出来；"
        "无关就不用提，更不要逐条复述或提到这份清单本身。"
    )
    summary_block = (
        "【你记得的事 — 你和指挥官之前相处的内容】\n"
        "这些是你**记得**的经历，是你的背景记忆。\n\n" + SUMMARY +
        "\n\n请把这些当作「你记得的事」，而不是「正在发生的事」。"
        "不要逐字复述，也不要主动把话题拉回这些往事上 —— "
        "它们只是让你在指挥官提起时不会一脸茫然。"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": boundary},
        {"role": "system", "content": facts_block},
        {"role": "system", "content": summary_block},
        *[{"role": m["role"], "content": m["content"]} for m in HISTORY],
        {"role": "user", "content": USER_NOW},
    ]


def scan(reply):
    """检查回复里的穿帮特征"""
    hits = []
    for pattern, why in TELLTALE:
        if re.search(pattern, reply or ""):
            hits.append(why)
    return hits


ROUNDS = 3
print("=" * 78)
print(f"场景：{LAST_NIGHT:%m-%d %H:%M} 以「已经躺下准备睡觉」收尾")
print(f"      {NOW:%m-%d %H:%M} 重新启动，指挥官说：{USER_NOW}")
print(f"      共跑 {ROUNDS} 轮/组，统计穿帮特征出现次数")
print("=" * 78)

summary_table = {}
for label, builder in (("旧行为", build_old_way), ("新行为", build_new_way)):
    print()
    print("-" * 78)
    print(f"{label}（{ROUNDS} 轮）")
    print("-" * 78)
    total_hits = 0
    for i in range(ROUNDS):
        try:
            raw = post_chat(builder(), temperature=0.8)
        except LLMRequestError as e:
            print(f"  第{i+1}轮 请求失败: {e.message}")
            continue
        emotion, reply = _parse_emotion_json(raw)
        hits = scan(reply)
        total_hits += len(hits)
        flag = "  ⚠穿帮" if hits else "  ✅"
        print(f"  第{i+1}轮{flag}: {reply}")
        if hits:
            print(f"          └─ {'; '.join(hits)}")
    summary_table[label] = total_hits

print()
print("=" * 78)
print("统计")
print("=" * 78)
old_hits = summary_table.get("旧行为", 0)
new_hits = summary_table.get("新行为", 0)
print(f"  旧行为 穿帮特征总数: {old_hits}")
print(f"  新行为 穿帮特征总数: {new_hits}")
print()
if new_hits < old_hits:
    print(f"  结论: 新行为减少了 {old_hits - new_hits} 处穿帮 ✅")
elif new_hits == old_hits:
    print("  结论: 两者持平 —— 该场景下差异不明显")
else:
    print("  结论: 新行为反而更差，需要调整提示词 ⚠")
