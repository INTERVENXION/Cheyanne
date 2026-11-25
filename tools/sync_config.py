# -*- coding: utf-8 -*-
"""把根目录的配置文件同步到 dist\\

## 为什么需要这个工具

- 源码运行（`python main.py`）读取根目录的 `config.yaml`
- `dist\\XiaAnPet.exe` 以自身所在目录为根目录，读取 `dist\\config.yaml`

两份配置不同步时，会出现"改了配置但行为没变"的困惑。
`main.py` 启动时也会检测到不一致并给出提示。

## 用法

    python tools/sync_config.py          # 同步
    python tools/sync_config.py --check  # 只检查，不写入
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")

# 需要同步的配置文件
FILES = ("config.yaml", "prompt.yaml", "holidays.yaml")

# config.yaml 中值得在同步前高亮提示的设置
# key 为 yaml 路径，如 "api.model" / "memory.restore_working_memory"
CHECK_KEYS = (
    "api.model",
    "api.thinking",
    "api.max_tokens",
    "api.temperature",
    "api.timeout",
    "memory.restore_working_memory",
)


def _dig(data, dotted):
    """按 "a.b.c" 路径取值"""
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def read_config(path):
    """读取整个 config.yaml（失败时返回空 dict）"""
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, ValueError, ImportError, AttributeError):
        return {}


def _files_differ(name):
    """比较根目录与 dist 下的同名文件内容"""
    src = os.path.join(ROOT, name)
    dst = os.path.join(DIST, name)
    if not os.path.isfile(src) or not os.path.isfile(dst):
        return True
    try:
        with open(src, "rb") as f1, open(dst, "rb") as f2:
            return f1.read() != f2.read()
    except OSError:
        return True


def main():
    check_only = "--check" in sys.argv

    if not os.path.isfile(os.path.join(DIST, "XiaAnPet.exe")):
        print("没有找到 dist\\XiaAnPet.exe，无需同步。")
        return 0

    print(f"源目录 : {ROOT}")
    print(f"目标   : {DIST}")
    print()

    # 同步前先报告关键项差异
    root_cfg = read_config(os.path.join(ROOT, "config.yaml"))
    dist_cfg = read_config(os.path.join(DIST, "config.yaml"))
    diffs = [
        (key, _dig(root_cfg, key), _dig(dist_cfg, key))
        for key in CHECK_KEYS
        if _dig(root_cfg, key) != _dig(dist_cfg, key)
    ]

    # 除逐项比对外，也直接比对文件内容 ——
    # 否则新增的配置段（例如 memory:）会因为不在 CHECK_KEYS 里而被漏掉
    changed_files = [name for name in FILES if _files_differ(name)]

    if diffs:
        print("检测到以下设置不一致：")
        for key, root_val, dist_val in diffs:
            print(f"  {key:34} 根目录={root_val!r}  dist={dist_val!r}")
        print()
    elif not changed_files:
        print("关键设置与文件内容均已一致，无需同步。")
        return 0
    else:
        print("检测到配置文件内容有差异（可能新增了配置段）：")
        for name in changed_files:
            dst = os.path.join(DIST, name)
            reason = "dist 中缺失" if not os.path.isfile(dst) else "内容不同"
            print(f"  {name}: {reason}")
        print()

    if check_only:
        print("（--check 模式，未写入任何文件）")
        return 1

    copied = 0
    for name in FILES:
        src = os.path.join(ROOT, name)
        dst = os.path.join(DIST, name)
        if not os.path.isfile(src):
            print(f"  [跳过] {name} 不存在")
            continue
        shutil.copy2(src, dst)
        print(f"  [同步] {name}")
        copied += 1

    print()
    print(f"完成，已同步 {copied} 个文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
