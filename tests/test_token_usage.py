# -*- coding: utf-8 -*-
"""验证 token 用量记录：不联网、不写数据文件

做法：注入假的 requests / yaml 模块，让 src.api 能导入；
再替换 get_api_config 与 requests.post，喂入带 usage 的响应，
检查打印内容与会话累计是否正确。
"""
import io
import json
import os
import sys
import types
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ---------- 1. 伪造 requests ----------
fake_requests = types.ModuleType("requests")


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            err = fake_requests.exceptions.HTTPError("boom")
            err.response = self
            raise err

    def json(self):
        return self._payload


class _Exc(types.ModuleType):
    pass


exceptions = types.ModuleType("requests.exceptions")


class RequestException(Exception):
    pass


class Timeout(RequestException):
    pass


class ConnectionError(RequestException):
    pass


class HTTPError(RequestException):
    response = None


exceptions.RequestException = RequestException
exceptions.Timeout = Timeout
exceptions.ConnectionError = ConnectionError
exceptions.HTTPError = HTTPError
fake_requests.exceptions = exceptions

STATE = {"payload": None}


def _post(*a, **k):
    return _Resp(STATE["payload"])


fake_requests.post = _post
sys.modules["requests"] = fake_requests
sys.modules["requests.exceptions"] = exceptions

# ---------- 2. 伪造 yaml（config_manager 在模块级 import 它）----------
fake_yaml = types.ModuleType("yaml")


class YAMLError(Exception):
    pass


fake_yaml.safe_load = lambda text: {}
fake_yaml.YAMLError = YAMLError
sys.modules["yaml"] = fake_yaml

# ---------- 3. 导入并打桩 ----------
import src.api as apimod

TEST_CFG = {
    "endpoint": "https://example.invalid/chat/completions",
    "api_key": "sk-test",
    "model": "deepseek-v4-pro",
    "thinking": False,
    "max_tokens": 400,
    "temperature": 0.8,
    "timeout": 30,
}
apimod.get_api_config = lambda: dict(TEST_CFG)

failures = []


def ok(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


def call(usage, content='{"emotion":"happy","reply":"好呀"}', finish="stop"):
    STATE["payload"] = {
        "choices": [{
            "message": {"content": content},
            "finish_reason": finish,
        }],
        "usage": usage,
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        text = apimod.post_chat([{"role": "user", "content": "测试"}])
    return text, buf.getvalue()


print("=" * 90)
print("1. 单次调用：应打印本次用量与会话累计")
print("=" * 90)
apimod.reset_usage_totals()

text, out = call({
    "prompt_tokens": 2253,
    "completion_tokens": 142,
    "total_tokens": 2395,
    "prompt_cache_hit_tokens": 2000,
    "prompt_cache_miss_tokens": 253,
})
print(out.strip())
ok("返回文本正常", text == '{"emotion":"happy","reply":"好呀"}')
ok("打印了 [Token]", "[Token]" in out)
ok("含本次输入/输出", "2,253" in out and "142" in out)
ok("含合计", "2,395" in out)
ok("含缓存命中率", "缓存命中" in out and "89%" in out)
ok("含累计与调用次数", "累计" in out and "1 次调用" in out)

print()
print("=" * 90)
print("2. 多次调用：累计应递增")
print("=" * 90)
text, out = call({
    "prompt_tokens": 1000,
    "completion_tokens": 50,
    "total_tokens": 1050,
})
print(out.strip())
ok("累计输入 = 2253+1000", "3,253" in out)
ok("累计输出 = 142+50", "192" in out)
ok("调用次数 = 2", "2 次调用" in out)

t = apimod.get_usage_totals()
ok("get_usage_totals 正确", t["prompt"] == 3253 and t["completion"] == 192
   and t["calls"] == 2, f"{t}")

print()
print("=" * 90)
print("3. 边界情况")
print("=" * 90)

# 3a 服务端没返回 usage
text, out = call(None)
print(out.strip())
ok("无 usage 时不崩溃且给出提示", "未返回 usage" in out)

# 3b finish_reason = length（被 max_tokens 截断）
text, out = call({"prompt_tokens": 100, "completion_tokens": 400,
                  "total_tokens": 500}, finish="length")
print(out.strip())
ok("截断时给出警告", "达到 max_tokens" in out)

# 3c usage 字段类型异常
STATE["payload"] = {
    "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": "不是数字", "completion_tokens": None},
}
buf = io.StringIO()
with redirect_stdout(buf):
    apimod.post_chat([{"role": "user", "content": "x"}])
ok("usage 类型异常不崩溃", True, buf.getvalue().strip()[:60])

# 3d 关闭日志
apimod.reset_usage_totals()
TEST_CFG["log_token_usage"] = False
STATE["payload"] = {
    "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}
buf = io.StringIO()
with redirect_stdout(buf):
    apimod.post_chat([{"role": "user", "content": "x"}])
ok("log_token_usage=false 时静默", "[Token]" not in buf.getvalue())
ok("关闭时也不累计", apimod.get_usage_totals()["calls"] == 0)
TEST_CFG.pop("log_token_usage", None)

print()
print("=" * 90)
print("4. 标签推断（区分主对话 / 摘要 / 事实提取）")
print("=" * 90)
for sys_prompt, expect in [
    ("你将扮演《少女前线2:追放》中的角色「夏安」。", "对话"),
    ("你是一个对话摘要助手。请将用户提供的对话记录压缩为简洁的摘要", "摘要"),
    ("你是一个信息提取助手。从对话记录中提取关于「指挥官」的关键事实。", "事实提取"),
    ("你是一个信息管理助手。判断新事实与已有事实的关系。", "事实去重"),
]:
    apimod.reset_usage_totals()
    STATE["payload"] = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }
    buf = io.StringIO()
    with redirect_stdout(buf):
        apimod.post_chat([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": "x"},
        ])
    got = f"[Token] {expect}:"
    ok(f"识别为「{expect}」", got in buf.getvalue(), buf.getvalue().strip()[:70])

print()
print("=" * 90)
if failures:
    print(f"结果: {len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果: token 用量记录工作正常 ✅")
