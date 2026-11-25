# -*- coding: utf-8 -*-
"""回归测试：聊天气泡不得裁剪正文

## 背景

用户报告：UI 里只显示「好，指挥官先忙，我」，
而 LLM 返回的是完整的「好，指挥官先忙，我等你。别太晚哦。」

排查结论：数据链路完全正常（JSON 解析 → 术语过滤 → 截断 都保留了全文），
问题出在**渲染**：

  QLabel + setWordWrap(True) 在本项目的布局栈里
  （ScrollArea → chat_widget → QVBoxLayout → 气泡 → QVBoxLayout → QLabel）
  无法正确上报"给定宽度下的换行高度" —— 文本要折成两行，
  heightForWidth(400) 却返回一行的高度，第二行被裁掉。

修复：用 _WrapLabel 自行用 QFontMetrics + Qt.TextWordWrap 精确计算高度，
并让气泡按实际可用宽度约束自身，同时在几何变化后自校正高度。

## 本测试要守住什么

1. 正文控件必须完整落在气泡内（不溢出右边界）
2. 正文控件高度必须 ≥ 该宽度下换行所需高度
3. 各种长度/中英混排/含标点的文本都不能被裁
4. 窗口尺寸变化、连续缩放、边加边缩放、字体变化后依然成立

注意：必须用真实平台（不要设置 QT_QPA_PLATFORM=offscreen），
并设置与 main.py 一致的字体，否则字体度量与真机不同、无法复现该类问题。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QFont, QFontMetrics
from PyQt5.QtCore import Qt, QRect

app = QApplication(sys.argv)
# 与 main.py 一致，否则字体度量与真机不同
app.setFont(QFont("Microsoft YaHei", 9))

from src.chat_dialog import ChatDialog, ChatBubble

# 数据隔离：ChatDialog 构造时会写 session.json 等文件，
# 必须重定向到临时目录，否则测试会改动用户的真实数据
from tests.test_isolation import use_temp_data_dir, assert_real_data_untouched
use_temp_data_dir()

failures = []


def ok(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


TEXTS = [
    ("截图里的原句", "好，指挥官先忙，我等你。别太晚哦。", False),
    ("用户长句", "嗯，我还有点工作要确认一下，没有问题的话就去睡觉了", True),
    ("夏安长句", "指挥官，这么晚还没睡呀？我刚好也没困意，要不要一起坐会儿？", False),
    ("极短", "好", False),
    ("单行", "晚安。", False),
    ("超长（必然换行）",
     "指挥官你今天又加班到这么晚，我其实一直在旁边等着，"
     "想着等你忙完就给你热一杯牛奶，再陪你说说话。"
     "你要是实在累的话就先靠一会儿，我把灯调暗一点。", False),
    ("含英文数字", "我今天把 M200 校准了一遍，弹道偏差只有 0.3 MOA。", False),
    ("纯中文无标点", "指挥官我先去准备晚饭了你忙完记得过来吃", True),
    ("多标点", "嗯……那个，我、我只是想问问，你还、还好吗？", False),
]


def audit(dialog, ctx):
    """核对所有气泡：文本完整 / 不溢出 / 高度足够"""
    problems = []
    for b in dialog.chat_widget.findChildren(ChatBubble):
        body = b._body
        txt = body.text()
        fm = QFontMetrics(body.font())
        need = fm.boundingRect(
            QRect(0, 0, max(1, body.width()), 10000),
            int(Qt.TextWordWrap) | int(body.alignment()), txt)
        if body.width() > b.width():
            problems.append(f"溢出: body={body.width()} bubble={b.width()} {txt[:14]!r}")
        elif body.height() < need.height():
            problems.append(f"高度不足: h={body.height()} need={need.height()} {txt[:14]!r}")
    if problems:
        print(f"    [{ctx}] {len(problems)} 处问题:")
        for p in problems[:5]:
            print(f"      - {p}")
    return problems


print("=" * 92)
print("各尺寸窗口下逐个气泡核对")
print("=" * 92)

for win_w in (260, 300, 320, 340, 356, 380, 420, 520, 700):
    cd = ChatDialog()
    cd.resize(win_w, 480)
    cd.show()
    app.processEvents()
    for _label, txt, is_user in TEXTS:
        cd._add_bubble(txt, is_user=is_user)
    for _ in range(15):
        app.processEvents()
    probs = audit(cd, f"w={win_w}")
    ok(f"窗口宽 {win_w}px：{len(TEXTS)} 个气泡全部完整", not probs,
       "" if not probs else f"{len(probs)} 处问题")
    cd.shutdown()

print()
print("=" * 92)
print("真实使用节奏：连续缩放")
print("=" * 92)
cd = ChatDialog()
cd.resize(420, 460)
cd.show()
app.processEvents()
for _label, txt, is_user in TEXTS:
    cd._add_bubble(txt, is_user=is_user)
for _ in range(15):
    app.processEvents()

bad = []
for w in (400, 360, 320, 300, 340, 420, 520, 380, 300, 500, 356, 260):
    cd.resize(w, 460)
    for _ in range(12):
        app.processEvents()
    if audit(cd, f"w={w}"):
        bad.append(w)
ok("连续缩放 12 次后仍完整", not bad, f"失败宽度={bad}")
cd.shutdown()

print()
print("=" * 92)
print("真实使用节奏：边加消息边缩放")
print("=" * 92)
cd = ChatDialog()
cd.resize(360, 460)
cd.show()
app.processEvents()
bad = []
for i, (_l, txt, is_user) in enumerate(TEXTS):
    cd._add_bubble(txt, is_user=is_user)
    cd.resize(320 + (i * 30) % 220, 460)
    for _ in range(12):
        app.processEvents()
    if audit(cd, f"第{i+1}条后"):
        bad.append(_l)
ok("交替加消息/缩放全程完整", not bad, f"失败={bad}")
cd.shutdown()

print()
print("=" * 92)
print("字体变化（模拟 DPI / 主题切换）")
print("=" * 92)
cd = ChatDialog()
cd.resize(356, 460)
cd.show()
app.processEvents()
for _label, txt, is_user in TEXTS:
    cd._add_bubble(txt, is_user=is_user)
for _ in range(15):
    app.processEvents()

bad = []
for size in (8, 9, 11, 14, 10, 9):
    for b in cd.chat_widget.findChildren(ChatBubble):
        b._body.setFont(QFont("Microsoft YaHei", size + 1))
    for _ in range(15):
        app.processEvents()
    if audit(cd, f"字体{size}pt"):
        bad.append(size)
ok("字体变化后仍完整", not bad, f"失败字号={bad}")
cd.shutdown()

print()
print("=" * 92)
if failures:
    print(f"结果: {len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果: 气泡正文渲染完整，无裁剪 ✅")

# 数据隔离核对
assert_real_data_untouched("test_bubble_render.py")
