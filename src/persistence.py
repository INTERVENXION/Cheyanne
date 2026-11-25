"""落盘工具 — 原子写入 + 并发安全 + 编码容错 + 损坏文件隔离

原来的三处 save（chat_history / memory_summary / fact_store）都是直接
open(path, "w") 覆写。写入过程中崩溃或断电会留下一个被截断的 JSON，
而各处的 _load 又会把 JSONDecodeError 静默当成"空数据"，
结果是用户的全部记忆被无声清空。

本模块提供：
- atomic_write_json(): 唯一临时文件名 + 每路径锁 → fsync → os.replace()
- load_json(): 多编码容错读取；解析失败时把损坏文件另存为 .corrupt 备份

## 曾经踩过的三个坑（都有回归测试守着）

1. **固定临时文件名导致并发写互相破坏**
   原实现用 `f"{path}.tmp"`。后台 SummaryWorker 与 GUI 线程
   （用户点"清空对话历史"）可能同时写同一个文件，两个线程写同一个
   临时文件再互相 replace —— 实测 200 轮并发里有 15% 留下无法解析的
   文件（内容被交错拼接）。随后 load_json 把它当损坏文件隔离，
   用户的整个聊天记录就"凭空消失"了。
   现在临时文件名带 pid + 线程 id + 随机串，并对同一路径加进程内锁。

2. **只捕获 OSError**
   `json.dump` 遇到孤立代理字符（模型被 max_tokens 截断在 \\uXXXX 中间
   时会产出）会抛 UnicodeEncodeError，那是 ValueError 不是 OSError，
   于是异常穿透到 QThread.run —— PyQt5 对槽函数里的未捕获异常会直接
   调用 qFatal()，**整个进程无提示地消失**。
   现在捕获 (OSError, ValueError, TypeError) 并在 finally 里清理临时文件。

3. **GBK / BOM 文件导致启动失败**
   旧版本用裸 open(path, "w") 写过数据，在中文 Windows 上编码是 GBK。
   本模块声称要隔离这类损坏文件，却因为 open() 先抛 UnicodeDecodeError
   （ValueError 的子类）而根本没走到隔离逻辑 —— 升级后程序直接起不来。
   Notepad 保存的 UTF-8 BOM 也会被误判为损坏。
   现在按 utf-8-sig → utf-8 → gbk → mbcs 依次尝试，并真正走隔离分支。
"""
import json
import os
import re
import threading
import uuid

# 损坏文件的备份后缀
_CORRUPT_SUFFIX = ".corrupt"

# 读取时依次尝试的编码
#   utf-8-sig 同时兼容"带 BOM"与"不带 BOM"的 UTF-8
#   gbk / mbcs 兼容旧版本在中文 Windows 上用裸 open() 写出的文件
_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "mbcs")

# 每个目标路径一把锁，避免同进程内多线程同时替换同一文件
_path_locks = {}
_locks_guard = threading.Lock()

# markdown 代码块围栏
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n?|\n?```\s*$")


def _lock_for(path):
    """取得（或创建）某个路径的锁"""
    key = os.path.abspath(path).lower()
    with _locks_guard:
        lock = _path_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _path_locks[key] = lock
        return lock


def strip_surrogates(text):
    """移除无法用 UTF-8 编码的孤立代理字符

    LLM 被 max_tokens 截断在 `\\uXXXX` 转义中间时会返回半截转义，
    json.loads 解析出孤立代理（如 '\\ud83d'），后续用 utf-8 写盘就会抛
    UnicodeEncodeError。这里在写入/存储前把它们替换掉。
    """
    if not isinstance(text, str):
        return text
    for ch in text:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            return text.encode("utf-8", "replace").decode("utf-8", "replace")
    return text


def _sanitize(obj):
    """递归清理结构里的孤立代理字符

    不用 `ensure_ascii=True` 是因为项目要保持中文可读，
    所以必须在写入前把无法编码的字符清理掉。
    """
    if isinstance(obj, str):
        return strip_surrogates(obj)
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def atomic_write_json(path, data, indent=2):
    """原子写入 JSON 文件（并发安全）

    写临时文件 → flush + fsync → os.replace 原子替换。
    临时文件名唯一（pid + 线程 id + 随机串），同一路径加锁，
    因此多个线程同时写同一文件不会互相破坏。

    Returns:
        True 写入成功；False 写入失败（失败信息打印到控制台）
    """
    tmp_path = "{}.{}.{}.{}.tmp".format(
        path, os.getpid(), threading.get_ident(), uuid.uuid4().hex[:8]
    )
    lock = _lock_for(path)
    try:
        with lock:
            payload = _sanitize(data)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=indent)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            return True
    except (OSError, ValueError, TypeError) as e:
        print(f"[Persistence] 写入 {os.path.basename(path)} 失败: "
              f"{type(e).__name__}: {e}")
        return False
    finally:
        # 无论成功失败都清理临时文件（成功时它已被 replace 掉）
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _read_text(path):
    """按多种编码尝试读取文本，返回 (文本, 使用的编码) 或 (None, None)"""
    last_error = None
    for encoding in _ENCODINGS:
        try:
            with open(path, "r", encoding=encoding) as f:
                return f.read(), encoding
        except UnicodeDecodeError as e:
            last_error = e
            continue
        except OSError as e:
            print(f"[Persistence] 读取 {os.path.basename(path)} 失败: {e}")
            return None, None
    if last_error is not None:
        print(f"[Persistence] {os.path.basename(path)} 编码无法识别: {last_error}")
    return None, None


def load_json(path, default):
    """读取 JSON 文件，失败时返回 default

    与旧实现的区别：
    - 支持 utf-8 / utf-8-sig(BOM) / gbk / mbcs 多种编码
    - 解析失败（文件损坏）时不再静默返回空数据，
      而是把损坏内容备份为 <path>.corrupt，让用户有机会人工抢救

    Args:
        path: 文件路径
        default: 文件不存在或无法解析时返回的默认值

    Returns:
        解析后的对象，或 default
    """
    if not os.path.exists(path):
        return default

    text, _encoding = _read_text(path)
    if text is None:
        # 连编码都识别不了 —— 这才是真正的"损坏"，走隔离
        _backup_corrupt_file(path, "无法识别的编码")
        return default

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        _backup_corrupt_file(path, e)
        return default


def _backup_corrupt_file(path, error):
    """把损坏的 JSON 文件另存为 .corrupt 备份，避免静默丢数据"""
    backup_path = path + _CORRUPT_SUFFIX
    try:
        with _lock_for(path):
            os.replace(path, backup_path)
        print(
            f"[Persistence] ⚠ {os.path.basename(path)} 已损坏 ({error})，"
            f"原文件已备份为 {os.path.basename(backup_path)}"
        )
    except OSError as e:
        print(f"[Persistence] ⚠ {os.path.basename(path)} 已损坏，且备份失败: {e}")


def strip_code_fence(text):
    """剥离 LLM 输出外面可能套着的 ``` 或 ```json 围栏"""
    if not text:
        return ""
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned

    lines = cleaned.split("\n")
    lines = lines[1:]  # 去掉开头的 ``` / ```json
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def salvage_json_objects(text):
    """从被截断的 JSON 数组中抢救出所有完整的对象

    LLM 返回事实数组时，若 max_tokens 用尽，末尾对象会缺少闭合花括号，
    导致 json.loads 整体失败、这一批提取成果全部作废。

    这里手写一个括号匹配扫描器（同样正确处理字符串内的花括号和转义符），
    逐个提取顶层数组中完整闭合的 {...} 对象。

    Args:
        text: 原始文本

    Returns:
        list of dict — 成功解析出的对象（失败的对象被跳过）
    """
    if not text:
        return []

    objects = []
    depth = 0
    in_string = False
    escaped = False
    start = None

    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    chunk = text[start:i + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            objects.append(obj)
                    except (json.JSONDecodeError, TypeError):
                        pass
                    start = None

    return objects
