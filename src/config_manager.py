"""配置管理器 — 读写 YAML 配置文件"""
import os
import re
import sys
import uuid

import yaml

from src import provider_config, providers, secret_store


def _get_base_dir():
    """获取应用根目录（兼容 PyInstaller 打包模式）"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _get_base_dir()

# API Key 的加密存储文件（Windows DPAPI，见 src/secret_store.py）
#
# 故意与 config.yaml 分开：config.yaml 是人会打开、截图、备份、转发的文件，
# 而 key 是可以直接盗刷余额的凭据，两者不该放在一起。
API_KEY_FILE = os.path.join(BASE_DIR, "api_key.dat")

# 界面里选择的"服务商 / 接口地址 / 模型"覆盖文件（见 src/provider_config.py）
PROVIDER_FILE = os.path.join(BASE_DIR, "provider.json")

# 匹配 config.yaml 里 `api_key: "sk-xxx"  # 注释` 这一行，
# 用于把明文 key 清空（保留缩进、引号位置与行尾注释）
_API_KEY_LINE = re.compile(
    r'^(?P<indent>\s*api_key\s*:\s*)'
    r'(?P<value>"[^"]*"|\'[^\']*\'|[^\s#]+)'
    r'(?P<tail>\s*(?:#.*)?)$'
)


def load_yaml(filename):
    """加载 YAML 文件，返回 dict

    容错要点（每一条都是踩过的坑）：
    - **编码**：用户被明确告知"改 prompt.yaml 就能改性格"，
      而记事本在中文 Windows 上默认存成 ANSI(GBK)，另存 UTF-8 时又会带 BOM。
      旧实现只按 utf-8 打开，遇到这类文件直接抛 UnicodeDecodeError ——
      而它是在 QThread.run / QTimer 槽里被调用的，
      PyQt5 对槽函数里的未捕获异常会 qFatal() 整个进程。
      所以这里按 utf-8-sig → utf-8 → gbk → mbcs 依次尝试。
    - **类型**：YAML 合法但根节点不是 dict（例如整份文件是一行列表或标量）
      时，`.get()` 会 AttributeError，同样会打穿到槽函数。
      统一归一化为 dict。
    """
    path = os.path.join(BASE_DIR, filename)
    last_error = None

    for encoding in ("utf-8-sig", "utf-8", "gbk", "mbcs"):
        try:
            with open(path, "r", encoding=encoding) as f:
                data = yaml.safe_load(f)
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError as e:
            last_error = e
            continue
        except yaml.YAMLError as e:
            print(f"[ConfigManager] 解析 {filename} 失败: {e}")
            return {}
        except OSError as e:
            print(f"[ConfigManager] 读取 {filename} 失败: {e}")
            return {}

        if data is None:
            return {}
        if isinstance(data, dict):
            return data
        print(f"[ConfigManager] {filename} 根节点不是字典 "
              f"({type(data).__name__})，已忽略")
        return {}

    print(f"[ConfigManager] {filename} 编码无法识别: {last_error}")
    return {}


def _as_dict(value):
    """把可能是 None / 非 dict 的配置段归一化成 dict"""
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------- API Key

def resolve_api_key(yaml_value=""):
    """确定实际使用的 API Key

    优先级：
      1. api_key.dat（DPAPI 加密存储）—— 用户在设置界面填的
      2. config.yaml 的 api.api_key —— 兼容旧配置/手工填写
      3. 空字符串 —— 视为未配置

    加密存储优先，这样用户在设置界面改过 key 之后，
    即使 config.yaml 里还留着旧值也不会被顶回去。
    """
    stored = secret_store.load(API_KEY_FILE)
    if stored:
        return stored
    return yaml_value.strip() if isinstance(yaml_value, str) else ""


def get_api_config():
    """获取 API 配置（保证返回 dict，已合并界面设置与已解析的 api_key）

    优先级：
      provider.json（界面里选的）  >  config.yaml（手写默认值）
      api_key.dat（加密存储）      >  config.yaml 的 api_key
    """
    config = _as_dict(load_yaml("config.yaml").get("api"))
    config["api_key"] = resolve_api_key(config.get("api_key"))

    override = provider_config.load(PROVIDER_FILE)
    for key in ("provider", "endpoint", "model"):
        value = override.get(key)
        if value:
            config[key] = value

    # 归一化服务商：显式指定就用它，否则按 endpoint/model 推断。
    # 这样老配置文件（只有 endpoint、没有 provider）也能正确识别。
    declared = config.get("provider")
    if isinstance(declared, str) and declared in providers.PROVIDERS:
        config["provider"] = declared
    else:
        config["provider"] = providers.detect(config)

    return config


def get_provider_settings():
    """读取界面里保存的服务商设置（可能为空 dict）"""
    return provider_config.load(PROVIDER_FILE)


def save_provider_settings(provider, endpoint, model):
    """保存界面里选择的服务商 / 接口地址 / 模型"""
    return provider_config.save(PROVIDER_FILE, provider, endpoint, model)


def clear_provider_settings():
    """清除界面设置（回到 config.yaml 的值）"""
    return provider_config.clear(PROVIDER_FILE)


def has_api_key():
    """当前是否已配置可用的 API Key"""
    return bool(resolve_api_key(_as_dict(load_yaml("config.yaml").get("api"))
                                .get("api_key")))


def api_key_storage_info():
    """返回存储位置与保护方式，供设置界面展示"""
    return {
        "path": API_KEY_FILE,
        "protection": secret_store.protection_of(API_KEY_FILE),
        "exists": os.path.isfile(API_KEY_FILE),
    }


def save_api_key(key):
    """保存 API Key（加密），并清空 config.yaml 里的明文

    Returns:
        True 保存成功
    """
    key = (key or "").strip()
    if not key:
        return False
    ok = secret_store.save(key, API_KEY_FILE)
    if ok:
        _blank_api_key_in_yaml()
    return ok


def clear_api_key():
    """清除已保存的 API Key"""
    return secret_store.clear(API_KEY_FILE)


def migrate_api_key_from_yaml():
    """把 config.yaml 里的明文 key 搬进加密存储，然后清空明文

    只在"加密存储还没有内容 + yaml 里有 key"时执行一次。
    这一步是为了让老用户（key 写在 yaml 里）也享受到加密存储，
    否则"确保 key 安全"只对新填的 key 生效，老 key 仍然是明文。

    Returns:
        True 表示本次确实做了迁移
    """
    # 已经迁移过（或用户已在设置里填过）就不要再覆盖
    if secret_store.protection_of(API_KEY_FILE):
        return False

    api = _as_dict(load_yaml("config.yaml").get("api"))
    key = api.get("api_key")
    if not isinstance(key, str) or not key.strip():
        return False

    if not secret_store.save(key.strip(), API_KEY_FILE):
        return False

    _blank_api_key_in_yaml()
    print("[Config] 已将 API Key 迁移到加密存储 api_key.dat，"
          "config.yaml 中的明文已清空")
    return True


def _atomic_write_text(path, text):
    """原子写入文本（唯一临时名 + fsync + replace）"""
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except (OSError, ValueError, TypeError) as e:
        print(f"[Config] 写入 {os.path.basename(path)} 失败: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _blank_api_key_in_yaml(filename="config.yaml"):
    """把 config.yaml 里 api_key 的值清空（保留缩进与行尾注释）

    为什么逐行改而不是 yaml.safe_dump 重写整份文件：
    config.yaml 里大量注释是给用户看的说明，dump 之后会全部丢失。
    这里只动匹配到的那一行，且写入是原子的。
    """
    path = os.path.join(BASE_DIR, filename)
    if not os.path.isfile(path):
        return False

    for encoding in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=encoding) as f:
                text = f.read()
            break
        except UnicodeDecodeError:
            continue
        except OSError:
            return False
    else:
        return False

    lines = text.splitlines(keepends=True)
    changed = False
    out = []
    for line in lines:
        ending = line[len(line.rstrip("\r\n")):]
        body = line[:len(line) - len(ending)] if ending else line
        m = _API_KEY_LINE.match(body)
        if m and m.group("value") not in ('""', "''"):
            out.append(f'{m.group("indent")}""{m.group("tail")}{ending}')
            changed = True
        else:
            out.append(line)

    if not changed:
        return False
    return _atomic_write_text(path, "".join(out))


# 私人提示词覆盖文件（不进版本库）
#
# 为什么需要它：仓库里放的是面向所有人的通用设定（prompt.yaml），
# 而作者自己往往有更私人的版本。如果直接改 prompt.yaml，
# 每次提交都会把私人内容推到仓库里。
#
# 约定：同目录存在 prompt.local.yaml 时优先读它，否则回退 prompt.yaml。
# 该文件名已写入 .gitignore。
LOCAL_PROMPT_FILE = "prompt.local.yaml"


def get_prompt_config():
    """获取提示词配置（每次都重新读取，实现热更新）

    优先级：
      1. prompt.local.yaml — 个人版（不进版本库）
      2. prompt.yaml       — 仓库里的通用版

    任何一份解析失败或为空时自动回退到下一份，避免"文件在但内容坏了"
    导致角色设定整个丢失。
    """
    for name in (LOCAL_PROMPT_FILE, "prompt.yaml"):
        if not os.path.isfile(os.path.join(BASE_DIR, name)):
            continue
        config = load_yaml(name)
        if config:
            return config
    return {}


def get_memory_config():
    """获取记忆/会话配置

    Returns:
        dict，至少包含 restore_working_memory 键（默认 False）
    """
    config = load_yaml("config.yaml")
    memory = dict(_as_dict(config.get("memory")))
    memory.setdefault("restore_working_memory", False)
    memory.setdefault("restore_tail_summary", False)
    # 单次会话内保留的对话轮数；缺省由 MemoryManager 决定（100 轮）
    memory.setdefault("working_memory_turns", None)
    # 是否每轮都注入摘要 + 事实。
    # 默认 True：关掉会让长期记忆只在会话第一句生效，第二句起就失忆。
    memory.setdefault("always_inject", True)
    return memory


def get_holidays_config():
    """获取节日配置（保证 special_dates 一定是列表）

    holidays.yaml 里 `special_dates:` 留空会解析成 None，
    旧代码 `for entry in None` 抛 TypeError —— 而它是在启动问候的
    QTimer 槽里被调用的，PyQt5 会直接把整个进程 abort。
    """
    config = dict(load_yaml("holidays.yaml"))
    dates = config.get("special_dates")
    config["special_dates"] = dates if isinstance(dates, list) else []
    return config
