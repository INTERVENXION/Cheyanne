# -*- coding: utf-8 -*-
"""提示词覆盖机制 + 开源版提示词纯净度测试

两个目的：

1. **prompt.local.yaml 优先于 prompt.yaml**
   仓库里放公共版，作者本地放私人版。这条保证私人版不会被误改回去，
   也保证公共版损坏时能优雅回退。

2. **开源版 prompt.yaml 不含成人内容**
   这是发布前的硬门槛：一旦漏了，仓库和账号都可能被处理。
   这里用关键词黑名单做最后一道检查。

运行：python tests/test_prompt_override.py
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src.config_manager as cm

_passed = 0
_failed = []


def ok(name, cond, detail=""):
    global _passed
    if cond:
        _passed += 1
        print(f"[PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        _failed.append(name)
        print(f"[FAIL] {name}" + (f"  ({detail})" if detail else ""))


TMP = tempfile.mkdtemp(prefix="m200_prompt_")
_real_base = cm.BASE_DIR

PUBLIC = "character:\n  name: \"公共版\"\nsystem_prompt: |\n  你是公共版角色。\n"
LOCAL = "character:\n  name: \"私人版\"\nsystem_prompt: |\n  你是私人版角色。\n"

try:
    cm.BASE_DIR = TMP

    print("=" * 74)
    print("1. 只有公共版时用它")
    print("=" * 74)
    with open(os.path.join(TMP, "prompt.yaml"), "w", encoding="utf-8") as f:
        f.write(PUBLIC)
    cfg = cm.get_prompt_config()
    ok("读到公共版", cfg.get("character", {}).get("name") == "公共版",
       str(cfg.get("character")))
    ok("含 system_prompt", "你是公共版角色" in cfg.get("system_prompt", ""))

    print()
    print("=" * 74)
    print("2. 出现 prompt.local.yaml 后优先它")
    print("=" * 74)
    with open(os.path.join(TMP, "prompt.local.yaml"), "w", encoding="utf-8") as f:
        f.write(LOCAL)
    cfg = cm.get_prompt_config()
    ok("读到私人版", cfg.get("character", {}).get("name") == "私人版",
       str(cfg.get("character")))

    print()
    print("=" * 74)
    print("3. 私人版损坏 / 为空时回退公共版")
    print("=" * 74)
    with open(os.path.join(TMP, "prompt.local.yaml"), "w", encoding="utf-8") as f:
        f.write("")
    cfg = cm.get_prompt_config()
    ok("空文件回退公共版", cfg.get("character", {}).get("name") == "公共版",
       str(cfg.get("character")))

    with open(os.path.join(TMP, "prompt.local.yaml"), "w", encoding="utf-8") as f:
        f.write("这: [不是合法\n  yaml")
    cfg = cm.get_prompt_config()
    ok("非法 YAML 回退公共版", cfg.get("character", {}).get("name") == "公共版",
       str(cfg.get("character")))

    print()
    print("=" * 74)
    print("4. 删掉私人版后回到公共版")
    print("=" * 74)
    os.remove(os.path.join(TMP, "prompt.local.yaml"))
    cfg = cm.get_prompt_config()
    ok("恢复公共版", cfg.get("character", {}).get("name") == "公共版")
finally:
    cm.BASE_DIR = _real_base
    shutil.rmtree(TMP, ignore_errors=True)


print()
print("=" * 74)
print("5. 仓库里的 prompt.yaml 是否干净（发布前硬门槛）")
print("=" * 74)
repo_prompt = os.path.join(ROOT, "prompt.yaml")
with open(repo_prompt, encoding="utf-8") as f:
    text = f.read()

# 成人向关键词黑名单（命中任何一条都不允许发布）
BANNED = [
    "性爱", "淫语", "娇喘", "淫水", "阴道", "阴蒂", "肉棒", "鸡巴",
    "小穴", "骚穴", "乳头", "奶子", "口交", "抽插", "插入", "高潮",
    "潮吹", "精液", "揉胸", "前戏", "放荡", "下流", "射精",
]
hits = [w for w in BANNED if w in text]
ok("不含成人向关键词", not hits, ("命中: " + ", ".join(hits)) if hits else "")

print()
print("=" * 74)
print("6. 人设与输出协议必须保留（删成人内容不能把角色删没了）")
print("=" * 74)
MUST_KEEP = [
    ("角色名", "夏安"),
    ("作品", "少女前线2"),
    ("代号 M200", "M200"),
    ("狙击", "狙击"),
    ("摄影", "摄影"),
    ("吉他", "吉他"),
    ("成年声明", "成年"),
    ("称呼", "指挥官"),
    ("JSON 协议", "\"emotion\""),
    ("reply 字段", "\"reply\""),
    ("情绪 happy", "\"happy\""),
    ("情绪 shy", "\"shy\""),
    ("情绪 surprised", "\"surprised\""),
    ("情绪 sad", "\"sad\""),
    ("情绪 angry", "\"angry\""),
    ("情绪 neutral", "\"neutral\""),
]
for label, needle in MUST_KEEP:
    ok(f"保留 {label}", needle in text)

import yaml
parsed = yaml.safe_load(text)
ok("YAML 可解析", isinstance(parsed, dict))
ok("有 character 段", isinstance(parsed.get("character"), dict))
ok("有 system_prompt", isinstance(parsed.get("system_prompt"), str)
   and len(parsed.get("system_prompt", "")) > 500,
   f"{len(parsed.get('system_prompt', ''))} 字符")

print()
print("=" * 74)
print("7. prompt.local.yaml 必须在 .gitignore 里")
print("=" * 74)
with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
    ignore = f.read()
ok("已忽略 prompt.local.yaml", "prompt.local.yaml" in ignore)
ok("已忽略 api_key.dat", "api_key.dat" in ignore)
ok("已忽略 chat_history.json", "chat_history.json" in ignore)
ok("已忽略 dist/", "dist/" in ignore)

print()
print("=" * 74)
if _failed:
    print(f"结果: {len(_failed)} 项失败 -> {_failed}")
    sys.exit(1)
print(f"结果: 提示词覆盖与开源纯净度全部通过 ✅（{_passed} 项）")
