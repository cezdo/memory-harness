"""醒着的“我”自言自语的想法只记在脑海中（design/rhythm/self/设计.md）：宿主命令包进沙箱，整个文件系统只能读，
只有这几处可写：self、这次醒来的草稿纸（日志由外面的节律写，里面改不了）、宿主自己的配置目录和登录、记忆自己的记录（钩子和 MCP 写的足迹、事件）。
网络照常（要调模型）。

沙箱按平台（2026-11-26：不需要很强的沙箱，只限制写入就够）：
- Linux：bwrap，根目录只读绑定，可写的几处绑回来；/tmp、/var/tmp、~/.cache 各给一块用完就扔的 tmpfs。
- macOS：sandbox-exec（Seatbelt），默认全允许、禁止写文件，可写的几处放行；Seatbelt 给不了用完就扔的 /tmp，
  这次醒来的 TMPDIR 指到草稿纸下的 tmp/，系统的临时目录（/private/tmp、/private/var/tmp、用户的 /var/folders 那两处、~/.cache）照常可写。
可写的清单两边是同一份（writable）。

- wrap(args, root, logs, extra) → 包好的命令；extra 是宿主自己要写的（配置目录、登录凭据），符号链接放行它指向的那份
- env(base, logs) → 起宿主时的环境（macOS 上 TMPDIR 指到草稿纸）
- ready() → 空串 = 能包；否则是为什么不能（没有沙箱就不叫醒）
- DENIED：写别处时看到的报错，“此刻”里先说一句
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ...engram import store

# 记忆在形成，也算在脑海中：钩子、MCP 写的（11-09 用 strace 跑一段短会话实测，加上读代码）
MIND = ("engram.db", "engram.db-wal", "engram.db-shm", "events", "kernel", "run", "assoc-cache-engram",
        "yorimichi.jsonl", "observe-switch.jsonl", "observe.wake")
SCRATCHPAD = "草稿"                  # 这次醒来日志目录里的草稿纸（start 建好）
SCRATCH = ("/tmp", "/var/tmp", str(Path.home() / ".cache"))
MAC = sys.platform == "darwin"
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
DENIED = "Operation not permitted" if MAC else "Read-only file system"


def writable(root, logs, extra=(), home=None):
    """可写的路径（解开符号链接、去重；不存在的跳过，bwrap 绑不了不存在的）。"""
    home = Path(home or store.HOME)
    out = []
    for p in [Path(root), Path(logs) / SCRATCHPAD, *map(Path, extra), *(home / m for m in MIND)]:
        p = p.expanduser().resolve()
        if p.exists() and p not in out:
            out.append(p)
    return out


def _darwin_dirs():
    """macOS 用户自己的临时、缓存目录（/var/folders/…/T、/C）：系统框架和 node 不看 TMPDIR 也会写这里。"""
    out = []
    for name in ("DARWIN_USER_TEMP_DIR", "DARWIN_USER_CACHE_DIR"):
        try:
            v = subprocess.run(["getconf", name], capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            v = ""
        if v:
            out.append(Path(v).resolve())
    return out


def _q(p):
    return '"' + str(p).replace("\\", "\\\\").replace('"', '\\"') + '"'


def profile(paths, scratch=()):
    """Seatbelt 配置：默认全允许，禁止写文件，放行 paths（子树）和 scratch（系统临时目录）、/dev。"""
    allow = [f"(subpath {_q(p)})" for p in [*paths, *scratch]]
    return "\n".join(["(version 1)", "(allow default)", "(deny file-write*)",
                      f"(allow file-write* (subpath \"/dev\") {' '.join(allow)})"])


def wrap(args, root, logs, extra=(), home=None):
    paths = writable(root, logs, extra, home)
    if MAC:
        scratch = [Path(t).resolve() for t in SCRATCH if Path(t).is_dir()] + _darwin_dirs()
        return [SANDBOX_EXEC, "-p", profile(paths, scratch), *args]
    pre = ["bwrap", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev"]
    for t in SCRATCH:
        if Path(t).is_dir():
            pre += ["--tmpfs", t]
    for p in paths:                                # 放在 tmpfs 后面：落在 /tmp 下的（测试）也绑得回来
        pre += ["--bind", str(p), str(p)]
    return pre + ["--chdir", str(Path(root).resolve()), "--", *args]


def env(base, logs):
    """起宿主的环境：macOS 上 TMPDIR 指到草稿纸下的 tmp/（Seatbelt 没有用完就扔的 /tmp）。"""
    if not MAC:
        return base
    tmp = Path(logs) / SCRATCHPAD / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    return {**base, "TMPDIR": str(tmp) + "/"}


def ready():
    if MAC:
        return "" if Path(SANDBOX_EXEC).exists() else "没有 sandbox-exec（醒来要在只能写几处的沙箱里）"
    return "" if shutil.which("bwrap") else "没有 bwrap（醒来要在只读沙箱里）"
