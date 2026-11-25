"""服务商设置的运行时存储（provider.json）

## 为什么单独存一个文件，而不是直接改 config.yaml

config.yaml 是**带大量注释的手写配置**：用户会读它、改它、按注释理解行为。
程序如果为了"记住用户在界面上选的模型"而去重写它，会遇到两个麻烦：

1. 用 yaml.safe_dump 重写会**丢失全部注释**（这个项目里注释就是文档）
2. 逐行替换只能改固定字段，加一个字段就要再写一套正则

所以把"界面上的选择"放进独立的 provider.json，优先级高于 config.yaml：

    provider.json（界面选择）  >  config.yaml（手写默认值）

删掉 provider.json 就回到 config.yaml 的配置，行为可预期。

文件内容示例：

    {"version": 1, "provider": "gemini",
     "endpoint": "https://.../chat/completions", "model": "gemini-2.0-flash"}

注意：**API Key 不放这里**。key 是凭据，走 secret_store 的 DPAPI 加密存储。
"""
from src.persistence import atomic_write_json, load_json

_VERSION = 1


def load(path):
    """读取服务商设置

    Returns:
        dict — 至少包含 provider/endpoint/model 三个键（缺失为空串）；
               文件不存在或损坏时返回空 dict（表示"没有覆盖，用 config.yaml"）
    """
    data = load_json(path, None)
    if not isinstance(data, dict):
        return {}

    result = {}
    for key in ("provider", "endpoint", "model"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value.strip()
    return result


def save(path, provider, endpoint, model):
    """保存服务商设置

    Returns:
        True 写入成功
    """
    payload = {
        "version": _VERSION,
        "provider": (provider or "").strip(),
        "endpoint": (endpoint or "").strip(),
        "model": (model or "").strip(),
    }
    return atomic_write_json(path, payload)


def clear(path):
    """删除覆盖设置（回到 config.yaml 的值）"""
    import os

    try:
        if path and os.path.isfile(path):
            os.remove(path)
            return True
    except OSError as e:
        print(f"[ProviderConfig] 删除失败: {e}")
    return False
