"""醒着的“我”自言自语的想法只记在脑海中（design/rhythm/self/设计.md）：宿主命令包进 bwrap，整个文件系统只读，
只有这几处可写：self、这次醒来的草稿纸（日志由外面的节律写，里面改不了）、宿主自己的配置目录和登录、记忆自己的记录（钩子和 MCP 写的足迹、事件）。
/tmp、/var/tmp、~/.cache 各给一块用完就扔的；网络照常（要调模型）。

- wrap(args, root, logs, extra) → 包好的命令；extra 是宿主自己要写的（配置目录、登录凭据），符号链接绑它指向的那份
- ready() → 空串 = 能包；否则是为什么不能（没有 bwrap 就不叫醒）
"""
import shutil
from pathlib import Path

from ...engram import store

# 记忆在形成，也算在脑海中：钩子、MCP 写的（11-09 用 strace 跑一段短会话实测，加上读代码）
MIND = ("engram.db", "engram.db-wal", "engram.db-shm", "events", "kernel", "run", "assoc-cache-engram",
        "yorimichi.jsonl", "observe-switch.jsonl", "observe.wake")
SCRATCHPAD = "草稿"                  # 这次醒来日志目录里的草稿纸（start 建好）
SCRATCH = ("/tmp", "/var/tmp", str(Path.home() / ".cache"))


def writable(root, logs, extra=(), home=None):
    """可写的路径（解开符号链接、去重；不存在的跳过，bwrap 绑不了不存在的）。"""
    home = Path(home or store.HOME)
    out = []
    for p in [Path(root), Path(logs) / SCRATCHPAD, *map(Path, extra), *(home / m for m in MIND)]:
        p = p.expanduser().resolve()
        if p.exists() and p not in out:
            out.append(p)
    return out


def wrap(args, root, logs, extra=(), home=None):
    pre = ["bwrap", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev"]
    for t in SCRATCH:
        if Path(t).is_dir():
            pre += ["--tmpfs", t]
    for p in writable(root, logs, extra, home):        # 放在 tmpfs 后面：落在 /tmp 下的（测试）也绑得回来
        pre += ["--bind", str(p), str(p)]
    return pre + ["--chdir", str(Path(root).resolve()), "--", *args]


def ready():
    return "" if shutil.which("bwrap") else "没有 bwrap（醒来要在只读沙箱里）"

