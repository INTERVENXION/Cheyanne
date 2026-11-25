# -*- coding: utf-8 -*-
"""API Key 加密存储与迁移的回归测试

覆盖：
  - DPAPI 加密往返（保存后能读回原文）
  - 落盘文件里**不出现明文 key**（这是"安全"的硬指标）
  - 文件缺失 / 损坏 / 保护方式未知时不抛异常，按未配置处理
  - 优先级：加密存储 > config.yaml 明文
  - 保存后自动清空 config.yaml 里的明文（保留注释与缩进）
  - 启动迁移：老用户的明文 key 被搬进加密存储且只迁移一次

运行：python tests/test_api_key_store.py
"""
import base64
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 记录真实配置指纹 —— 本测试绝不允许改动它们
REAL_FILES = ("config.yaml", "api_key.dat")


def _fingerprint(paths):
    result = {}
    for name in paths:
        path = os.path.join(ROOT, name)
        if os.path.isfile(path):
            with open(path, "rb") as f:
                result[name] = f.read()
    return result


_real_before = _fingerprint(REAL_FILES)

import src.config_manager as cm
import src.secret_store as ss

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


TMP = tempfile.mkdtemp(prefix="m200_key_")

# 把所有路径重定向到临时目录
cm.BASE_DIR = TMP
cm.API_KEY_FILE = os.path.join(TMP, "api_key.dat")
KEY_FILE = cm.API_KEY_FILE
CFG = os.path.join(TMP, "config.yaml")


def write_cfg(text):
    with open(CFG, "w", encoding="utf-8") as f:
        f.write(text)


def reset():
    for p in (KEY_FILE, CFG):
        if os.path.isfile(p):
            os.remove(p)


try:
    print("=" * 74)
    print("1. DPAPI 可用性与加密往返")
    print("=" * 74)
    print(f"  平台: {sys.platform}   DPAPI 可用: {ss.dpapi_available()}")
    reset()

    SECRET = "sk-test-0123456789abcdefghijklmn"
    ok("保存成功", ss.save(SECRET, KEY_FILE) is True)
    ok("读回原文一致", ss.load(KEY_FILE) == SECRET, ss.mask(ss.load(KEY_FILE)))

    # ---- 安全硬指标：文件里不能出现明文
    with open(KEY_FILE, "rb") as f:
        raw = f.read()
    ok("落盘文件不含明文 key", SECRET.encode() not in raw,
       f"文件 {len(raw)} 字节")

    stored = json.loads(raw.decode("utf-8"))
    ok("保护方式为 dpapi", stored.get("protection") == "dpapi",
       str(stored.get("protection")))
    ok("data 是 base64 密文", isinstance(stored.get("data"), str)
       and SECRET not in stored.get("data", ""))

    print()
    print("=" * 74)
    print("2. 异常输入不崩溃（按未配置处理）")
    print("=" * 74)
    ok("文件不存在 -> 空串", ss.load(os.path.join(TMP, "nope.dat")) == "")

    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write("这不是 JSON{{{")
    ok("文件损坏 -> 空串", ss.load(KEY_FILE) == "")

    with open(KEY_FILE, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "protection": "dpapi",
                   "data": "!!!not-base64!!!"}, f)
    ok("base64 非法 -> 空串", ss.load(KEY_FILE) == "")

    with open(KEY_FILE, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "protection": "rot13", "data": "eA=="}, f)
    ok("未知 protection -> 空串", ss.load(KEY_FILE) == "")

    with open(KEY_FILE, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "protection": "plain",
                   "data": base64.b64encode(b"sk-plain-key").decode()}, f)
    ok("plain 降级可读回", ss.load(KEY_FILE) == "sk-plain-key")

    print()
    print("=" * 74)
    print("3. 优先级：加密存储 > config.yaml")
    print("=" * 74)
    reset()
    write_cfg('api:\n  endpoint: "https://x"\n  api_key: "sk-yaml"\n')

    ok("无加密存储时用 yaml 值", cm.resolve_api_key("sk-yaml") == "sk-yaml")
    ok("has_api_key 为真", cm.has_api_key() is True)

    ss.save("sk-stored", KEY_FILE)
    ok("有加密存储时覆盖 yaml 值",
       cm.resolve_api_key("sk-yaml") == "sk-stored")
    ok("get_api_config 返回生效的 key",
       cm.get_api_config().get("api_key") == "sk-stored")
    ok("resolve_api_key(None) 不崩", cm.resolve_api_key(None) == "sk-stored")

    print()
    print("=" * 74)
    print("4. 保存后清空 yaml 明文（保留注释与缩进）")
    print("=" * 74)
    reset()
    write_cfg(
        'api:\n'
        '  # API 密钥\n'
        '  api_key: "sk-very-secret"  # 行尾注释\n'
        '  model: "x"\n'
    )
    ok("保存成功", cm.save_api_key("sk-very-secret") is True)

    with open(CFG, encoding="utf-8") as f:
        text = f.read()
    ok("yaml 中已无明文", "sk-very-secret" not in text)
    ok("value 被清成空串", 'api_key: ""' in text, text.strip().splitlines()[2])
    ok("行尾注释保留", "# 行尾注释" in text)
    ok("其余配置未受影响", 'model: "x"' in text)

    print()
    print("=" * 74)
    print("5. 启动迁移（老用户：明文在 yaml 里）")
    print("=" * 74)
    reset()
    write_cfg('api:\n  endpoint: "https://x"\n  api_key: "sk-legacy-key"\n')

    ok("首次迁移返回 True", cm.migrate_api_key_from_yaml() is True)
    ok("key 已进加密存储", ss.load(KEY_FILE) == "sk-legacy-key")
    with open(CFG, encoding="utf-8") as f:
        text = f.read()
    ok("yaml 明文已清空", "sk-legacy-key" not in text)
    ok("迁移后 has_api_key 仍为真", cm.has_api_key() is True)
    ok("再次迁移返回 False（幂等）", cm.migrate_api_key_from_yaml() is False)

    print()
    print("=" * 74)
    print("6. 清除")
    print("=" * 74)
    ok("清除成功", cm.clear_api_key() is True)
    ok("文件已删除", not os.path.isfile(KEY_FILE))
    ok("清除后 has_api_key 为假", cm.has_api_key() is False)

    print()
    print("=" * 74)
    print("7. 打码显示")
    print("=" * 74)
    # 注意：这里必须用**假** key，不能把真实 key 写进测试
    masked = ss.mask("sk-0000aaaa1111bbbb2222cccc3333dddd")
    ok("长 key 打码", masked == "sk-0000…dddd", masked)
    ok("短 key 全打码", ss.mask("abc") == "•••", ss.mask("abc"))
    ok("空 key 返回空", ss.mask("") == "")

    print()
    print("=" * 74)
    print("8. 真实配置文件未被改动")
    print("=" * 74)
    real_after = _fingerprint(REAL_FILES)
    ok("config.yaml / api_key.dat 指纹一致", real_after == _real_before)

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("=" * 74)
if _failed:
    print(f"结果: {len(_failed)} 项失败 -> {_failed}")
    sys.exit(1)
print(f"结果: API Key 加密存储与迁移全部通过 ✅（{_passed} 项）")
