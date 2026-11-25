"""API Key 的安全存储 — Windows DPAPI 加密

## 为什么不能放在 config.yaml 里

config.yaml 是**给人看的**配置文件：用户会打开它、截图发人、
整体备份到网盘、或者把 dist 目录打包发给朋友。
API Key 明文放在那里，等于把账号余额的钥匙一起发出去 ——
拿到 key 的人可以直接盗刷，而且你很难第一时间发现。

## 为什么用 DPAPI

Windows 自带 DPAPI（CryptProtectData / CryptUnprotectData）：
- 用**当前登录用户**的凭据派生密钥，密文只有同一个 Windows 用户能解开
- 不需要用户记密码，也不需要额外的密钥文件（没有"密钥和密文放在一起"的问题）
- 换个 Windows 账号或换台机器就解不开 —— 这正是我们要的隔离

代价：如果用户把整个目录拷到另一台电脑，key 需要重新填一次。
这是有意的取舍：宁可让用户重填，也不让 key 跟着文件夹到处跑。

## 落盘格式（api_key.dat）

    {"version": 1, "protection": "dpapi", "data": "<base64 密文>"}

- `data` 用 base64 是为了在 JSON 里安全承载二进制密文
- `protection` 标记实际使用的保护方式：
    dpapi — 已用 Windows 加密（正常情况）
    plain — DPAPI 不可用时的降级（仍是 base64，但**没有真正的加密**）
- 无论哪种方式，文件里都**不会出现可读的 key 明文**

文件读写复用 persistence 的原子写入与多编码容错，
避免"写到一半断电 → 文件损坏 → key 丢失"。
"""
import base64
import ctypes
import os
import sys
from ctypes import wintypes

from src.persistence import atomic_write_json, load_json

# ---------------------------------------------------------------- DPAPI 绑定

_IS_WINDOWS = sys.platform == "win32"

# CryptProtectData 的 UI 标志：禁止弹任何窗口。
# 必须带上 —— 否则在服务/无桌面会话里调用会阻塞。
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

_dpapi_ready = False
_crypt32 = None
_kernel32 = None


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _init_dpapi():
    """延迟初始化 crypt32 绑定；失败则保持不可用（走降级路径）"""
    global _dpapi_ready, _crypt32, _kernel32
    if not _IS_WINDOWS:
        return False
    if _dpapi_ready:
        return True
    try:
        _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        _crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),      # pDataIn
            wintypes.LPCWSTR,                # szDataDescr
            ctypes.POINTER(_DATA_BLOB),      # pOptionalEntropy
            ctypes.c_void_p,                 # pvReserved
            ctypes.c_void_p,                 # pPromptStruct
            wintypes.DWORD,                  # dwFlags
            ctypes.POINTER(_DATA_BLOB),      # pDataOut
        ]
        _crypt32.CryptProtectData.restype = wintypes.BOOL

        _crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),      # pDataIn
            ctypes.POINTER(wintypes.LPWSTR),  # ppszDataDescr
            ctypes.POINTER(_DATA_BLOB),      # pOptionalEntropy
            ctypes.c_void_p,                 # pvReserved
            ctypes.c_void_p,                 # pPromptStruct
            wintypes.DWORD,                  # dwFlags
            ctypes.POINTER(_DATA_BLOB),      # pDataOut
        ]
        _crypt32.CryptUnprotectData.restype = wintypes.BOOL

        _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        _kernel32.LocalFree.restype = wintypes.HLOCAL

        _dpapi_ready = True
        return True
    except Exception as e:
        print(f"[SecretStore] DPAPI 初始化失败，将降级保存: {e}")
        return False


def _make_blob(data):
    """把 bytes 包成 DATA_BLOB（同时返回缓冲区，调用期间必须保活）"""
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(
        len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
    )
    return blob, buffer


def dpapi_available():
    """当前环境是否真的能用 Windows DPAPI"""
    return _init_dpapi()


def _encrypt(data):
    """用 DPAPI 加密；失败返回 None"""
    if not _init_dpapi():
        return None
    try:
        in_blob, _keep = _make_blob(data)
        out_blob = _DATA_BLOB()
        ok = _crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "XiaAn API Key",
            None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            print(f"[SecretStore] 加密失败 (WinError {ctypes.get_last_error()})")
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            _kernel32.LocalFree(out_blob.pbData)
    except Exception as e:
        print(f"[SecretStore] 加密异常: {e}")
        return None


def _decrypt(blob):
    """用 DPAPI 解密；失败返回 None"""
    if not _init_dpapi():
        return None
    try:
        in_blob, _keep = _make_blob(blob)
        out_blob = _DATA_BLOB()
        descr = wintypes.LPWSTR()
        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            ctypes.byref(descr),
            None, None, None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            # 换了 Windows 账号 / 换机器时必然走到这里：
            # 不是损坏，而是"不属于你"——按未配置处理即可，不要报错吓人
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            _kernel32.LocalFree(out_blob.pbData)
            if descr:
                _kernel32.LocalFree(descr)
    except Exception as e:
        print(f"[SecretStore] 解密异常: {e}")
        return None


# ---------------------------------------------------------------- 对外接口

def save(key, path):
    """把 API Key 加密写入 path

    Returns:
        True 表示写入成功（保护方式见 protection_of）
    """
    key = (key or "").strip()
    if not key:
        return False

    raw = key.encode("utf-8")
    encrypted = _encrypt(raw)
    if encrypted is not None:
        payload = {
            "version": 1,
            "protection": "dpapi",
            "data": base64.b64encode(encrypted).decode("ascii"),
        }
    else:
        # 降级：仍然不写明文，但必须让用户知道这不是真正加密
        print("[SecretStore] 警告: DPAPI 不可用，API Key 仅做了编码保存")
        payload = {
            "version": 1,
            "protection": "plain",
            "data": base64.b64encode(raw).decode("ascii"),
        }

    return atomic_write_json(path, payload)


def load(path):
    """读取并解密 API Key

    Returns:
        str — 明文 key；不存在 / 损坏 / 无法解密时返回空字符串
              （绝不抛异常：它在启动路径上，抛了会打断整个启动）
    """
    data = load_json(path, None)
    if not isinstance(data, dict):
        return ""

    encoded = data.get("data")
    if not isinstance(encoded, str) or not encoded:
        return ""

    try:
        blob = base64.b64decode(encoded.encode("ascii"), validate=True)
    except Exception:
        return ""

    protection = data.get("protection")
    if protection == "dpapi":
        plain = _decrypt(blob)
    elif protection == "plain":
        plain = blob
    else:
        return ""

    if not plain:
        return ""
    try:
        return plain.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def clear(path):
    """删除已保存的 API Key"""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
            return True
    except OSError as e:
        print(f"[SecretStore] 删除失败: {e}")
    return False


def protection_of(path):
    """查询当前文件使用的保护方式（供设置界面显示）"""
    data = load_json(path, None)
    if not isinstance(data, dict):
        return ""
    value = data.get("protection")
    return value if isinstance(value, str) else ""


def mask(key, head=7, tail=4):
    """把 key 打码成 sk-xxxx…abcd 形式，用于日志/提示

    设置界面里用户要求"能看到自己的 key"，那里不调用本函数；
    本函数只用于可能被截图或贴到日志的场合。
    """
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= head + tail:
        return "•" * len(key)
    return f"{key[:head]}…{key[-tail:]}"
