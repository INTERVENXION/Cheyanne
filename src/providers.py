"""模型服务商适配层

## 为什么要单独一层

不同厂商虽然都提供"聊天补全"，但请求格式并不统一：

| 服务商 | 认证方式 | 请求体 | 返回体 |
|---|---|---|---|
| OpenAI / DeepSeek / Grok / Kimi / GLM / Gemini(兼容模式) | `Authorization: Bearer` | `messages` 里可以带 system | `choices[0].message.content` |
| Anthropic (Claude) | `x-api-key` + `anthropic-version` | system 必须**单独提出来**，`max_tokens` 必填 | `content[]` 数组 |

另外还有只属于某一家的小字段，例如 DeepSeek 的 `thinking`。
如果把这些差异散落在 `api.py` 里用 if/else 拼，改一家就要动一次主流程。

所以这里收敛成三个纯函数：

    build_request(provider, config, messages, max_tokens, temperature)
        -> {"url": ..., "headers": {...}, "json": {...}}
    parse_response(provider, data) -> str
    finish_reason(provider, data) -> str

它们是**纯函数**（不联网、不读文件），所以可以用单元测试把每家都钉住，
不用真的花钱调 API。

## 关于 Gemini

Google 同时提供两种入口：
  - 原生接口：`/v1beta/models/<model>:generateContent`（请求体完全不同）
  - OpenAI 兼容接口：`/v1beta/openai/chat/completions`

这里用的是**兼容接口**，因为项目其余代码都按 OpenAI 格式组装。
用户在设置里选 "Google Gemini" 时会自动填入兼容接口地址。
如果用户手动填了原生地址，会收到 404/400 —— 这是预期内的反馈。
"""
import re

# 各服务商的默认配置。
#
# model 只是"可用的起点"，不保证长期有效 —— 各家模型名字变动很频繁，
# 所以设置界面里 model 是**可编辑**的，用户随时可以改成本账号实际可用的名字。
PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "style": "openai",
        "supports_thinking": True,
        "hint": "DeepSeek 官方接口。thinking 开关只对本服务商生效。",
    },
    "openai": {
        "name": "OpenAI / ChatGPT",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
        "style": "openai",
        "supports_thinking": False,
        "hint": "也可用于任何 OpenAI 兼容的第三方中转。",
    },
    "anthropic": {
        "name": "Anthropic / Claude",
        "endpoint": "https://api.anthropic.com/v1/messages",
        "model": "claude-3-5-sonnet-latest",
        "style": "anthropic",
        "supports_thinking": False,
        "hint": "Claude 用 x-api-key 认证，请求格式与 OpenAI 不同（已自动适配）。",
    },
    "gemini": {
        "name": "Google Gemini",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "model": "gemini-2.0-flash",
        "style": "openai",
        "supports_thinking": False,
        "hint": "使用 Gemini 的 OpenAI 兼容接口，不要填原生 generateContent 地址。",
    },
    "grok": {
        "name": "xAI / Grok",
        "endpoint": "https://api.x.ai/v1/chat/completions",
        "model": "grok-2-latest",
        "style": "openai",
        "supports_thinking": False,
        "hint": "xAI 官方接口，OpenAI 兼容。",
    },
    "kimi": {
        "name": "Moonshot / Kimi",
        "endpoint": "https://api.moonshot.cn/v1/chat/completions",
        "model": "moonshot-v1-8k",
        "style": "openai",
        "supports_thinking": False,
        "hint": "Moonshot 国内站。海外站把域名换成 api.moonshot.ai。",
    },
    "zhipu": {
        "name": "智谱 GLM",
        "endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4-flash",
        "style": "openai",
        "supports_thinking": False,
        "hint": "智谱 AI 开放平台，OpenAI 兼容。",
    },
    "custom": {
        "name": "自定义 / 中转",
        "endpoint": "",
        "model": "",
        "style": "openai",
        "supports_thinking": False,
        "hint": "任何 OpenAI 兼容服务：填完整的 chat/completions 地址。",
    },
}

# 界面里的显示顺序
ORDER = ("deepseek", "openai", "anthropic", "gemini", "grok", "kimi",
         "zhipu", "custom")

_ANTHROPIC_VERSION = "2023-06-01"


def list_providers():
    """[(id, 显示名)]，按界面顺序"""
    return [(pid, PROVIDERS[pid]["name"]) for pid in ORDER if pid in PROVIDERS]


def get(provider_id):
    """取服务商定义，未知 id 归到 custom"""
    return PROVIDERS.get(provider_id) or PROVIDERS["custom"]


def resolve_id(provider_id):
    """把任意输入归一化成合法 id"""
    if isinstance(provider_id, str) and provider_id in PROVIDERS:
        return provider_id
    return "custom"


def detect(api_config):
    """根据 endpoint / model 猜服务商

    用户没显式指定 provider 时用它兜底（比如老配置文件里只有 endpoint）。
    """
    declared = api_config.get("provider")
    if isinstance(declared, str) and declared in PROVIDERS:
        return declared

    endpoint = str(api_config.get("endpoint") or "").lower()
    model = str(api_config.get("model") or "").lower()

    rules = (
        ("anthropic", ("api.anthropic.com", "claude")),
        ("deepseek", ("api.deepseek.com", "deepseek")),
        ("gemini", ("generativelanguage.googleapis.com", "gemini")),
        ("grok", ("api.x.ai", "grok")),
        ("kimi", ("moonshot", "kimi")),
        ("zhipu", ("bigmodel.cn", "zhipu", "glm")),
        ("openai", ("api.openai.com", "gpt-4", "gpt-3.5", "gpt-5", "o1-", "o3-")),
    )
    for pid, needles in rules:
        if any(n in endpoint or n in model for n in needles):
            return pid
    return "custom"


def should_send_thinking(provider_id, api_config):
    """是否在请求体里附带 DeepSeek 专有的 thinking 字段

    config: api.send_thinking = auto | true | false
      auto  —— 只有 DeepSeek 才发（默认，保证切到别家不会 400）
      true  —— 强制发送（用于兼容 DeepSeek 的中转）
      false —— 从不发送
    """
    mode = api_config.get("send_thinking", "auto")
    if isinstance(mode, bool):
        return mode
    text = str(mode).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    return provider_id == "deepseek"


# ---------------------------------------------------------------- 请求构建

def _split_system(messages):
    """把 system 消息拆出来，其余按顺序保留

    Anthropic 要求 system 作为顶层参数，messages 里只能是 user/assistant。
    本项目的上下文里有 3~4 条 system（角色设定 / 会话边界 / 摘要 / 事实），
    这里合并成一段。
    """
    system_parts = []
    conversation = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if not isinstance(content, str) or not content:
            continue
        if role == "system":
            system_parts.append(content)
        else:
            conversation.append({"role": role, "content": content})
    return "\n\n".join(system_parts), conversation


def _merge_adjacent(conversation):
    """合并相邻的同角色消息

    Anthropic 对 messages 的交替要求更严格；本项目正常情况下本来就是交替的，
    但摘要/事实注入等路径可能产生连续 user，这里统一兜底。
    """
    merged = []
    for msg in conversation:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n\n" + msg["content"]
        else:
            merged.append({"role": msg["role"], "content": msg["content"]})
    return merged


def _openai_like(provider_id, endpoint, model, api_key, messages,
                 max_tokens, temperature, api_config):
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }

    thinking_on = (bool(api_config.get("thinking", False))
                   and should_send_thinking(provider_id, api_config))
    if should_send_thinking(provider_id, api_config):
        payload["thinking"] = {"type": "enabled" if thinking_on else "disabled"}
    if not thinking_on:
        payload["temperature"] = temperature

    return {
        "url": endpoint,
        "headers": {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        "json": payload,
    }


def _anthropic(endpoint, model, api_key, messages, max_tokens, temperature):
    system_text, conversation = _split_system(messages)
    conversation = _merge_adjacent(conversation)

    # Anthropic 要求 messages 非空且以 user 开头。
    # 本项目的上下文一定以 user（当前输入）结尾；万一为空，补一句占位。
    if not conversation:
        conversation = [{"role": "user", "content": "你好"}]

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": conversation,
        "temperature": temperature,
    }
    if system_text:
        payload["system"] = system_text

    return {
        "url": endpoint,
        "headers": {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        },
        "json": payload,
    }


def build_request(provider_id, api_config, messages, max_tokens, temperature):
    """按服务商组装一次请求

    Returns:
        {"url": str, "headers": dict, "json": dict}
    """
    pid = resolve_id(provider_id)
    spec = get(pid)

    endpoint = str(api_config.get("endpoint") or spec["endpoint"] or "").strip()
    model = str(api_config.get("model") or spec["model"] or "").strip()
    api_key = str(api_config.get("api_key") or "").strip()

    if spec["style"] == "anthropic":
        return _anthropic(endpoint, model, api_key, messages,
                          max_tokens, temperature)
    return _openai_like(pid, endpoint, model, api_key, messages,
                        max_tokens, temperature, api_config)


# ---------------------------------------------------------------- 响应解析

def _content_to_text(content):
    """content 可能是字符串，也可能是 [{type:text,text:...}] 数组"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def parse_response(provider_id, data):
    """从响应体里取出助手回复文本（失败返回空串，由调用方兜底）"""
    if not isinstance(data, dict):
        return ""
    pid = resolve_id(provider_id)

    if get(pid)["style"] == "anthropic":
        # {"content": [{"type": "text", "text": "..."}], ...}
        return _content_to_text(data.get("content")).strip()

    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ""
    return _content_to_text(message.get("content")).strip()


def finish_reason(provider_id, data):
    """统一成 finish_reason 语义（"length" 表示被 max_tokens 截断）"""
    if not isinstance(data, dict):
        return "N/A"
    pid = resolve_id(provider_id)

    if get(pid)["style"] == "anthropic":
        reason = data.get("stop_reason")
    else:
        try:
            reason = data["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError):
            reason = None

    if not reason:
        return "N/A"
    # Anthropic 用 max_tokens 表示截断，与 OpenAI 的 length 同义
    return "length" if reason in ("max_tokens", "length") else reason


def extract_error(provider_id, body):
    """从错误响应里挖出可读信息

    body 可能是已解析的 dict，也可能是原始文本。
    """
    pid = resolve_id(provider_id)

    if isinstance(body, dict):
        if get(pid)["style"] == "anthropic":
            error = body.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if error is None and body.get("message"):
            return str(body["message"])
        # 有些服务商把信息放在 base_resp / detail 里
        for key in ("base_resp", "detail", "msg"):
            value = body.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, dict) and value.get("status_msg"):
                return str(value["status_msg"])

    if isinstance(body, str):
        text = body.strip()
        # 截断超长的 HTML 错误页
        text = re.sub(r"<[^>]+>", " ", text)
        return text[:300]
    return ""


def looks_valid(provider_id, data):
    """响应体是否符合该服务商的预期结构

    用途：用户选错服务商（例如 Gemini 地址却选了 Claude）时，
    返回的 JSON 结构对不上，这里能给出明确错误，而不是静默返回空回复。
    """
    if not isinstance(data, dict):
        return False
    pid = resolve_id(provider_id)

    if get(pid)["style"] == "anthropic":
        return isinstance(data.get("content"), (list, str))

    choices = data.get("choices")
    return isinstance(choices, list) and bool(choices)


def default_endpoint(provider_id):
    """设置界面用：把 {model} 之类的占位替换掉"""
    return get(provider_id)["endpoint"]


def default_model(provider_id):
    return get(provider_id)["model"]
