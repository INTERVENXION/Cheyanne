"""共享 LLM API 层 — 统一 DeepSeek / OpenAI 兼容请求

此前有三份几乎相同的 HTTP 调用代码（LLMClient.chat / FactStore._call_llm /
Summarizer._call_llm），各自的 headers、payload、异常处理都不一致。
本模块把它们收敛到一处。

## 思考模式（重要）

DeepSeek 新模型（deepseek-flash / deepseek-v4-pro）默认开启思考模式，
effort 为 high。对桌宠来说有两个致命影响：

1. **temperature 完全失效** — 官方文档：思考模式不支持 temperature、
   presence_penalty、frequency_penalty，"设置这些参数不会报错，但也不会生效"。
   角色扮演依赖 temperature 0.8 的多样性，不关掉思考模式这个参数就是摆设。
2. **延迟大幅增加** — 每轮回复前先生成一大段思维链，桌宠对话会明显变卡。

因此这里通过 `thinking: {"type": "disabled"}` 显式关闭思考模式（由 config.yaml
的 api.thinking 控制，默认 false）。关闭后才发送 temperature。
"""
import threading

import requests

from src import providers
from src.config_manager import get_api_config


class LLMRequestError(Exception):
    """API 调用失败 — 携带可直接展示给用户的中文说明"""

    def __init__(self, message, kind="unknown"):
        super().__init__(message)
        self.message = message
        self.kind = kind

    def __str__(self):
        return self.message


def _positive_int(value, default):
    """把配置值转成正整数，非法/None 时返回默认值"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _number(value, default):
    """把配置值转成数字，非法/None 时返回默认值"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _build_payload(model, messages, max_tokens, temperature, thinking):
    """（已废弃）请求体现在由 src.providers.build_request 统一组装

    保留这个函数只为兼容可能存在的旧调用；新代码不要再用它 ——
    它不区分服务商，会把 DeepSeek 的 thinking 字段发给所有厂商。
    """
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }

    if thinking:
        payload["thinking"] = {"type": "enabled"}
    else:
        payload["thinking"] = {"type": "disabled"}
        payload["temperature"] = temperature

    return payload


def post_chat(messages, max_tokens=None, temperature=None, timeout=None,
              log_usage=True, label=None):
    """发送一次聊天补全请求，返回助手回复的纯文本

    Args:
        messages: OpenAI 格式的 messages 列表
        max_tokens: 覆盖配置中的 max_tokens（摘要/事实提取等辅助任务用）
        temperature: 覆盖配置中的 temperature
        timeout: 覆盖配置中的 timeout
        log_usage: 是否打印并累计 token 用量（默认 True）
        label: 调用类型标签，用于区分主对话 / 摘要 / 事实提取。
            为 None 时按 system prompt 自动推断。

    Returns:
        str: 助手回复文本（可能为空字符串，由调用方决定如何处理）

    Raises:
        LLMRequestError: 配置缺失、网络异常、HTTP 错误或响应格式异常
    """
    api_config = get_api_config()

    endpoint = api_config.get("endpoint") or ""
    api_key = api_config.get("api_key") or ""

    if not endpoint:
        raise LLMRequestError(
            "未配置接口地址 —— 请右击桌宠，在「设置 API Key 与模型」中选择服务商并填写接口地址。",
            kind="config",
        )
    if not api_key:
        raise LLMRequestError(
            "还没有设置 API Key —— 请右击桌宠，在「设置 API Key 与模型」中填写。",
            kind="config",
        )

    provider_id = providers.resolve_id(api_config.get("provider"))

    if max_tokens is None:
        max_tokens = _positive_int(api_config.get("max_tokens"), 512)
    if temperature is None:
        temperature = _number(api_config.get("temperature"), 0.8)
    if timeout is None:
        timeout = _positive_int(api_config.get("timeout"), 60)

    # 配置里可以关掉用量日志（例如嫌输出太吵）
    if api_config.get("log_token_usage") is False:
        log_usage = False

    # 各家请求格式不同（OpenAI 兼容 / Anthropic），统一由 providers 组装
    request = providers.build_request(
        provider_id, api_config, messages, max_tokens, temperature
    )

    try:
        resp = requests.post(
            request["url"],
            headers=request["headers"],
            json=request["json"],
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        raise LLMRequestError("请求超时，请检查网络或 API 服务状态", kind="timeout")
    except requests.exceptions.ConnectionError:
        raise LLMRequestError(
            "无法连接到 API 服务器，请检查接口地址配置", kind="connection"
        )
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        detail = ""
        if e.response is not None:
            try:
                detail = providers.extract_error(provider_id, e.response.json())
            except ValueError:
                detail = providers.extract_error(provider_id, e.response.text)
        if status == 401:
            raise LLMRequestError(
                f"API Key 无效或已失效 ({status})，请在设置中检查", kind="auth"
            )
        if status == 403:
            raise LLMRequestError(
                f"没有权限访问该模型 ({status})：{detail}", kind="auth"
            )
        if status == 404:
            raise LLMRequestError(
                f"接口地址或模型不存在 ({status})：{detail}", kind="model"
            )
        if status == 400 and "model" in str(detail).lower():
            raise LLMRequestError(
                f"模型名可能无效 ({status})，请在设置中检查 model：{detail}",
                kind="model",
            )
        if status == 429:
            raise LLMRequestError(
                f"请求过于频繁或额度不足 ({status})：{detail}", kind="quota"
            )
        raise LLMRequestError(f"API 请求失败 ({status}): {detail}", kind="http")
    except requests.exceptions.RequestException as e:
        raise LLMRequestError(f"请求异常: {e}", kind="request")

    try:
        data = resp.json()
    except ValueError:
        raise LLMRequestError("API 返回的不是合法 JSON", kind="format")

    if not providers.looks_valid(provider_id, data):
        detail = providers.extract_error(provider_id, data)
        raise LLMRequestError(
            f"API 返回格式异常（与所选服务商不匹配）：{detail[:160]}", kind="format"
        )

    text = providers.parse_response(provider_id, data)
    finish_reason = providers.finish_reason(provider_id, data)

    if not text:
        # 空 content 是需要调用方感知的情况：可能被内容过滤，也可能是服务端抖动
        print(f"[API] 警告: 返回空 content (finish_reason={finish_reason})")

    # 记录本次调用的 token 用量（增量 + 会话累计）
    if log_usage:
        record_usage(data.get("usage"), label=label or _infer_label(messages),
                     finish_reason=finish_reason)

    return text


# ============================================================
# Token 用量统计
#
# 为什么要做这个：DeepSeek 的返回体里有 usage 字段，但之前我们只取
# content 就把整个响应丢了。于是"这次花了多少 token"只能靠字符数估算
# （官方换算：中文 0.6 token/字、英文 0.3 token/字符），而估算会受
# 模型分词方式影响。直接用服务端返回的 usage 才是权威数字。
#
# 打印分两部分：
#   本次 —— 这一句消耗多少（增量）
#   累计 —— 进程启动以来一共多少（含调用次数，便于算成本）
# ============================================================

# 进程内累计（线程安全）
_usage_totals = {
    "calls": 0,
    "prompt": 0,
    "completion": 0,
    "cache_hit": 0,
    "cache_miss": 0,
}
_usage_lock = threading.Lock()


def _infer_label(messages):
    """从 messages 推断调用类型，便于区分主对话与摘要/事实提取"""
    try:
        sys_msgs = [m for m in messages if m.get("role") == "system"]
        first = (sys_msgs[0].get("content") or "") if sys_msgs else ""
    except (AttributeError, IndexError, TypeError):
        first = ""
    if "对话摘要助手" in first:
        return "摘要"
    if "信息提取助手" in first:
        return "事实提取"
    if "信息管理助手" in first:
        return "事实去重"
    return "对话"


def _int_field(usage, *names):
    """从 usage 里按多个可能的键名取整数，缺失返回 0"""
    for name in names:
        v = usage.get(name)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def record_usage(usage, label="对话", finish_reason=None):
    """打印并累计一次调用的 token 用量

    Args:
        usage: API 返回的 usage 字典（可能为 None）
        label: 调用类型（对话 / 摘要 / 事实提取 / 事实去重）
        finish_reason: 结束原因，length 表示被 max_tokens 截断
    """
    usage = usage if isinstance(usage, dict) else {}

    prompt = _int_field(usage, "prompt_tokens", "input_tokens")
    completion = _int_field(usage, "completion_tokens", "output_tokens")
    total = _int_field(usage, "total_tokens") or (prompt + completion)
    cache_hit = _int_field(usage, "prompt_cache_hit_tokens", "cache_hit_tokens")
    cache_miss = _int_field(usage, "prompt_cache_miss_tokens", "cache_miss_tokens")

    with _usage_lock:
        _usage_totals["calls"] += 1
        _usage_totals["prompt"] += prompt
        _usage_totals["completion"] += completion
        _usage_totals["cache_hit"] += cache_hit
        _usage_totals["cache_miss"] += cache_miss
        acc = dict(_usage_totals)

    if not usage:
        # 服务端没返回 usage（极少见）—— 至少给出调用计数
        print(f"[Token] {label}: 服务端未返回 usage  |  累计 {acc['calls']} 次调用")
        return

    # 缓存命中能显著降低输入费用，单列出来
    cache_part = ""
    if cache_hit or cache_miss:
        rate = cache_hit / (cache_hit + cache_miss) * 100 if (cache_hit + cache_miss) else 0
        cache_part = f"  |  缓存命中 {cache_hit:,} ({rate:.0f}%)"

    truncated = "  |  ⚠ 达到 max_tokens，回复被截断" if finish_reason == "length" else ""

    print(
        f"[Token] {label}: 输入 {prompt:,} + 输出 {completion:,} = {total:,}"
        f"{cache_part}{truncated}  |  "
        f"累计 {acc['prompt']:,} + {acc['completion']:,} = "
        f"{acc['prompt'] + acc['completion']:,} ({acc['calls']} 次调用)"
    )


def get_usage_totals():
    """取当前进程的累计用量（供 UI/退出时展示）"""
    with _usage_lock:
        return dict(_usage_totals)


def reset_usage_totals():
    """清零累计用量"""
    with _usage_lock:
        for k in _usage_totals:
            _usage_totals[k] = 0
