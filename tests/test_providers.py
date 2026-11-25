# -*- coding: utf-8 -*-
"""多服务商适配层的回归测试

覆盖七家主流服务商 + 自定义：
  DeepSeek / OpenAI / Anthropic(Claude) / Gemini / Grok / Kimi / GLM

重点验证三件容易出错的事：
  1. **请求格式差异** —— Claude 的 system 必须提出来、认证用 x-api-key
  2. **thinking 字段只发给 DeepSeek** —— 否则别家可能 400
  3. **响应解析差异** —— Claude 是 content 数组，其余是 choices[0]

这些函数都不联网，所以测试是确定性的、不花钱。

运行：python tests/test_providers.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src.providers as P

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


MESSAGES = [
    {"role": "system", "content": "你是夏安。"},
    {"role": "system", "content": "【会话边界】4 天没见。"},
    {"role": "user", "content": "我回来了"},
    {"role": "assistant", "content": "欢迎回来。"},
    {"role": "user", "content": "今天有点累"},
]

print("=" * 78)
print("1. 服务商注册表")
print("=" * 78)
ok("七家 + 自定义都在", len(P.PROVIDERS) == 8, f"{len(P.PROVIDERS)} 个")
for pid in ("deepseek", "openai", "anthropic", "gemini", "grok", "kimi",
            "zhipu", "custom"):
    spec = P.PROVIDERS.get(pid) or {}
    ok(f"{pid} 元数据完整",
       bool(spec.get("name")) and "style" in spec and "endpoint" in spec)

ok("列表顺序稳定", [p for p, _ in P.list_providers()] == list(P.ORDER))
ok("未知 id 归到 custom", P.resolve_id("不存在的厂商") == "custom")
ok("合法 id 原样返回", P.resolve_id("anthropic") == "anthropic")

print()
print("=" * 78)
print("2. 服务商自动识别（老配置没有 provider 字段时靠它）")
print("=" * 78)
CASES = [
    ({"endpoint": "https://api.deepseek.com/chat/completions"}, "deepseek"),
    ({"model": "deepseek-v4-pro"}, "deepseek"),
    ({"endpoint": "https://api.openai.com/v1/chat/completions"}, "openai"),
    ({"model": "gpt-4o-mini"}, "openai"),
    ({"endpoint": "https://api.anthropic.com/v1/messages"}, "anthropic"),
    ({"model": "claude-3-5-sonnet-latest"}, "anthropic"),
    ({"endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"}, "gemini"),
    ({"model": "gemini-2.0-flash"}, "gemini"),
    ({"endpoint": "https://api.x.ai/v1/chat/completions"}, "grok"),
    ({"endpoint": "https://api.moonshot.cn/v1/chat/completions"}, "kimi"),
    ({"endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions"}, "zhipu"),
    ({"model": "glm-4-flash"}, "zhipu"),
    ({"endpoint": "https://my-proxy.example.com/v1/chat/completions"}, "custom"),
]
for cfg, expected in CASES:
    got = P.detect(cfg)
    ok(f"识别 {expected}", got == expected, f"{cfg} -> {got}")

ok("显式 provider 优先于推断",
   P.detect({"provider": "anthropic",
             "endpoint": "https://api.deepseek.com/x"}) == "anthropic")

print()
print("=" * 78)
print("3. thinking 字段只发给 DeepSeek")
print("=" * 78)
BASE = {"endpoint": "https://x/y", "model": "m", "api_key": "k"}

for pid in ("deepseek", "openai", "anthropic", "gemini", "grok", "kimi",
            "zhipu", "custom"):
    cfg = dict(BASE, provider=pid)
    req = P.build_request(pid, cfg, MESSAGES, 400, 0.8)
    body = req["json"]
    has_thinking = "thinking" in body
    expect = (pid == "deepseek")
    ok(f"{pid}: thinking 存在={has_thinking}（期望 {expect}）",
       has_thinking == expect)

ok("send_thinking=true 强制发送",
   "thinking" in P.build_request(
       "openai", dict(BASE, provider="openai", send_thinking=True),
       MESSAGES, 400, 0.8)["json"])
ok("send_thinking=false 从不发送",
   "thinking" not in P.build_request(
       "deepseek", dict(BASE, provider="deepseek", send_thinking=False),
       MESSAGES, 400, 0.8)["json"])
ok("auto 模式下 DeepSeek 关闭思考时发送 disabled",
   P.build_request("deepseek", dict(BASE, provider="deepseek", thinking=False),
                   MESSAGES, 400, 0.8)["json"]["thinking"]["type"] == "disabled")
ok("DeepSeek 开启思考时不发 temperature（官方不支持）",
   "temperature" not in P.build_request(
       "deepseek", dict(BASE, provider="deepseek", thinking=True),
       MESSAGES, 400, 0.8)["json"])
ok("其它服务商始终发 temperature",
   P.build_request("openai", dict(BASE, provider="openai", thinking=True),
                   MESSAGES, 400, 0.8)["json"].get("temperature") == 0.8)

print()
print("=" * 78)
print("4. OpenAI 兼容族的请求形状")
print("=" * 78)
for pid in ("deepseek", "openai", "gemini", "grok", "kimi", "zhipu"):
    cfg = dict(BASE, provider=pid, endpoint=f"https://{pid}.example/v1/chat",
               model=f"{pid}-model", api_key="sk-test")
    req = P.build_request(pid, cfg, MESSAGES, 400, 0.8)
    body = req["json"]
    ok(f"{pid}: url 用配置值", req["url"] == cfg["endpoint"], req["url"])
    ok(f"{pid}: Bearer 认证",
       req["headers"].get("Authorization") == "Bearer sk-test")
    ok(f"{pid}: model/max_tokens 正确",
       body["model"] == f"{pid}-model" and body["max_tokens"] == 400)
    ok(f"{pid}: messages 原样传入（含 system）",
       body["messages"] == MESSAGES)

print()
print("=" * 78)
print("5. Anthropic(Claude) 的请求形状")
print("=" * 78)
cfg = {"provider": "anthropic",
       "endpoint": "https://api.anthropic.com/v1/messages",
       "model": "claude-3-5-sonnet-latest",
       "api_key": "sk-ant-test"}
req = P.build_request("anthropic", cfg, MESSAGES, 400, 0.8)
body = req["json"]

ok("url 正确", req["url"] == cfg["endpoint"])
ok("用 x-api-key 认证", req["headers"].get("x-api-key") == "sk-ant-test")
ok("不带 Bearer", "Authorization" not in req["headers"])
ok("带 anthropic-version",
   req["headers"].get("anthropic-version") == "2023-06-01")
ok("system 被提为顶层参数",
   body.get("system") == "你是夏安。\n\n【会话边界】4 天没见。",
   repr(body.get("system"))[:60])
ok("messages 里不含 system",
   all(m["role"] in ("user", "assistant") for m in body["messages"]))
ok("max_tokens 必填且正确", body.get("max_tokens") == 400)
ok("带 temperature", body.get("temperature") == 0.8)
ok("不带 thinking", "thinking" not in body)
ok("对话顺序保持",
   [m["role"] for m in body["messages"]] == ["user", "assistant", "user"])

print()
print("=" * 78)
print("6. Anthropic 相邻同角色合并")
print("=" * 78)
weird = [
    {"role": "system", "content": "S"},
    {"role": "user", "content": "第一句"},
    {"role": "user", "content": "第二句"},
    {"role": "assistant", "content": "回复"},
]
body = P.build_request("anthropic", cfg, weird, 100, 0.5)["json"]
ok("连续 user 被合并成一条", len(body["messages"]) == 2, str(body["messages"]))
ok("合并内容保留两段",
   body["messages"][0]["content"] == "第一句\n\n第二句")
ok("空对话时补占位 user",
   P.build_request("anthropic", cfg, [], 100, 0.5)["json"]["messages"][0]["role"]
   == "user")

print()
print("=" * 78)
print("7. 响应解析")
print("=" * 78)
openai_resp = {
    "choices": [{"message": {"content": "你好呀"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
}
ok("OpenAI 字符串 content",
   P.parse_response("openai", openai_resp) == "你好呀")
ok("OpenAI finish_reason",
   P.finish_reason("openai", openai_resp) == "stop")
ok("OpenAI length 原样",
   P.finish_reason("openai", {"choices": [{"finish_reason": "length"}]}) == "length")

list_resp = {"choices": [{"message": {"content": [
    {"type": "text", "text": "第一段"},
    {"type": "text", "text": "第二段"},
]}, "finish_reason": "stop"}]}
ok("OpenAI 数组 content 也能拼",
   P.parse_response("openai", list_resp) == "第一段第二段")

anthropic_resp = {
    "content": [{"type": "text", "text": "在的，"}, {"type": "text", "text": "指挥官。"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 5},
}
ok("Anthropic content 数组拼接",
   P.parse_response("anthropic", anthropic_resp) == "在的，指挥官。")
ok("Anthropic stop_reason",
   P.finish_reason("anthropic", anthropic_resp) == "end_turn")
ok("Anthropic max_tokens 归一成 length",
   P.finish_reason("anthropic", {"stop_reason": "max_tokens"}) == "length")

ok("非法响应返回空串", P.parse_response("openai", {"foo": 1}) == "")
ok("None 不崩", P.parse_response("openai", None) == "")
ok("finish_reason 缺失为 N/A", P.finish_reason("openai", {}) == "N/A")

print()
print("=" * 78)
print("8. 结构校验（选错服务商时能报警）")
print("=" * 78)
ok("OpenAI 响应对 openai 有效", P.looks_valid("openai", openai_resp))
ok("Claude 响应对 openai 无效",
   not P.looks_valid("openai", anthropic_resp))
ok("Claude 响应对 anthropic 有效", P.looks_valid("anthropic", anthropic_resp))
ok("OpenAI 响应对 anthropic 无效",
   not P.looks_valid("anthropic", openai_resp))
ok("空 dict 无效", not P.looks_valid("openai", {}))

print()
print("=" * 78)
print("9. 错误信息提取")
print("=" * 78)
ok("OpenAI 风格",
   P.extract_error("openai", {"error": {"message": "invalid api key"}})
   == "invalid api key")
ok("Anthropic 风格",
   P.extract_error("anthropic",
                   {"type": "error", "error": {"message": "credit balance low"}})
   == "credit balance low")
ok("顶层 message",
   P.extract_error("openai", {"message": "quota exceeded"}) == "quota exceeded")
ok("纯文本会去掉 HTML 标签",
   "<html>" not in P.extract_error("openai", "<html><body>502 Bad Gateway</body></html>"))
ok("未知结构返回空串", P.extract_error("openai", {}) == "")

print()
print("=" * 78)
print("10. post_chat 端到端（用假 requests，不联网）")
print("=" * 78)


class _FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = str(data)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise NotImplementedError

    def json(self):
        return self._data


class _FakeExceptions:
    class RequestException(Exception):
        pass

    class Timeout(RequestException):
        pass

    class ConnectionError(RequestException):
        pass

    class HTTPError(RequestException):
        response = None


class _FakeRequests:
    exceptions = _FakeExceptions

    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


import src.api as apimod

_real_requests = apimod.requests
_real_get_config = apimod.get_api_config

try:
    # --- DeepSeek ---
    fake = _FakeRequests(_FakeResponse(openai_resp))
    apimod.requests = fake
    apimod.get_api_config = lambda: {
        "provider": "deepseek",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-v4-pro",
        "api_key": "sk-ds",
        "max_tokens": 400, "temperature": 0.8, "timeout": 30,
        "log_token_usage": False,
    }
    text = apimod.post_chat([{"role": "user", "content": "hi"}])
    ok("post_chat 返回解析后的文本", text == "你好呀", repr(text))
    sent = fake.calls[0]
    ok("发到了配置的地址", sent["url"].endswith("/chat/completions"))
    ok("带 Bearer", sent["headers"]["Authorization"] == "Bearer sk-ds")
    ok("DeepSeek 请求带 thinking", "thinking" in sent["json"])

    # --- Claude ---
    fake2 = _FakeRequests(_FakeResponse(anthropic_resp))
    apimod.requests = fake2
    apimod.get_api_config = lambda: {
        "provider": "anthropic",
        "endpoint": "https://api.anthropic.com/v1/messages",
        "model": "claude-3-5-sonnet-latest",
        "api_key": "sk-ant",
        "max_tokens": 400, "temperature": 0.8, "timeout": 30,
        "log_token_usage": False,
    }
    text = apimod.post_chat(MESSAGES)
    ok("Claude 回复解析正确", text == "在的，指挥官。", repr(text))
    sent2 = fake2.calls[0]
    ok("Claude 用 x-api-key", sent2["headers"].get("x-api-key") == "sk-ant")
    ok("Claude 请求无 thinking", "thinking" not in sent2["json"])
    ok("Claude system 已提出", "system" in sent2["json"])
finally:
    apimod.requests = _real_requests
    apimod.get_api_config = _real_get_config

print()
print("=" * 78)
if _failed:
    print(f"结果: {len(_failed)} 项失败 -> {_failed}")
    sys.exit(1)
print(f"结果: 多服务商适配全部通过 ✅（{_passed} 项）")
