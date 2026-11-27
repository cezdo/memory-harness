"""宿主入口转给 mh 服务那台机器（X7 服务化试跑，2026-11-25）：宿主这边的钩子、MCP 照旧起 `python3 …`，
入口一开头调 maybe()；本机有转发配置就原样 exec 成 `ssh <服务机> python3 …`，标准输入输出、退出码都穿过去。

- 转发配置：`~/.config/memory-harness/forward.toml`（不在实例配置里：服务机自己的实例配置不能有它，免得转个圈）。
  ssh = "server"（ssh 别名）、repo = "memory-harness"（服务机上的检出，相对它的家目录；默认就是这个）、
  python = "/opt/homebrew/bin/python3"、device = "laptop"（这台叫什么）。删掉文件就回到本机跑。
  服务机用它自己的家目录（数据目录、实例配置、原文都在那边的 ~ 下，design/deploy）；本机命令行里指向本仓库的路径
  换成相对检出的路径，不要求两边同路径（11-25 改：之前靠同路径的 HOME）。
- 只用标准库，钩子脚本在 -I -S 下按路径载入它。
- 服务机要知道的本机状态由环境变量带过去：MH_HOST_PID（往上找到的 claude 进程，带设备名，MCP 和 SessionStart
  靠它认会话，hosts/claude.py）、MH_DEVICE，和宿主给的几个 CLAUDE_CODE_* 变量。
- 连不上：ssh 自己报错退出（255），宿主照常显示钩子出错、不挡用户（不静默）。"""
import os
import shlex
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]       # 本机的仓库根
CONFIG = Path.home() / ".config/memory-harness/forward.toml"
PASS_ENV = ("CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "PYTHONPATH")
SSH_OPTS = ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "-o", "ServerAliveInterval=15",
            "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/cm-mh-%C", "-o", "ControlPersist=600"]
RELAY_PATHS = ('$HOME/local/mh-tools/relay/mh-relay', '/usr/local/mh-tools/relay/mh-relay')


def config(path=CONFIG):
    if os.environ.get("MH_NO_FORWARD"):
        return None
    try:
        import tomllib
        return tomllib.loads(path.read_text())
    except (OSError, ValueError, ImportError):
        return None


def _claude_pid():
    """往上找第一个叫 claude 的进程（同 hosts/claude.host_pid，这里不 import mh）；只看 /proc，找不到给 0。"""
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            return 0
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            name, rest = stat[stat.index("(") + 1:stat.rindex(")")], stat[stat.rindex(")") + 2:].split()
        except (OSError, ValueError, IndexError):
            return 0
        if name == "claude":
            return pid
        pid = int(rest[1])
    return 0


def _remote_arg(a, repo=REPO):
    """本机仓库里的路径 → 相对服务机检出的路径（远端先 cd 进检出）；别的原样。"""
    root = str(repo)
    if a == root:
        return "."
    return a[len(root) + 1:] if a.startswith(root + "/") else a


def _mcp_host(argv):
    """是不是 MCP 入口 → 宿主名（codex / agy / "" 表示默认 claude 等），不是返回 None。"""
    for i, a in enumerate(argv[1:], 1):
        if a == "-m" and i + 1 < len(argv):
            mod, rest = argv[i + 1], argv[i + 2:]
            if mod == "mh.synapse.pull.mcp":
                return ""
            if mod.startswith("mh.synapse.hosts.") and rest == ["mcp"]:
                return mod.rsplit(".", 1)[-1]
    return None


def _hook_action(argv):
    """是不是可交常驻服务代跑的钩子 → action 名，不是返回 None。"""
    for i, a in enumerate(argv[1:], 1):
        if a == "-m" and i + 1 < len(argv):
            mod, rest = argv[i + 1], argv[i + 2:]
            if mod == "mh" and rest == ["inject"]:
                return "inject"
            if mod == "mh.synapse.push.associate" and not rest:
                return "associate"
            if mod == "mh.synapse.hosts.codex" and rest in (["start"], ["associate"]):
                return f"codex-{rest[0]}"
    return None


def command(cfg, argv, env=os.environ, repo=REPO):
    """→ ssh 的参数表。argv 是本机 python 的完整命令行（sys.orig_argv）：解释器换成服务机的，指向本仓库的路径换成相对检出的。
    MCP 和高频钩子（inject / associate）在服务机有 mh-relay 时直接经 relay 连常驻服务（X10），不在服务机另起 Python；没装 relay 照旧 exec 原命令。"""
    device = cfg.get("device", "client")
    pid = _claude_pid()
    sets = {"MH_DEVICE": device, "MH_CLIENT_HOME": str(Path.home()), **({"MH_HOST_PID": f"{device}-{pid}"} if pid else {}),
            **{k: env[k] for k in PASS_ENV if env.get(k)}}
    sets.pop("PYTHONPATH", None) if env.get("PYTHONPATH") == str(repo) else None      # 指向本机仓库的不带过去（远端 cd 进检出就有）
    host = _mcp_host(argv)
    hook = None if host is not None else _hook_action(argv)
    if host:
        sets["MH_HOST"] = host
        sets.pop("CLAUDE_CODE_SESSION_ID", None)          # 启动非 Claude 宿主的背景会话号不带过去（同 codex/agy 入口）
    env_str = " ".join(f"{k}={shlex.quote(v)}" for k, v in sets.items())
    py_cmd = shlex.join([cfg.get("python", "python3"), *(_remote_arg(a, repo) for a in argv[1:])])
    cd = f"cd {shlex.quote(cfg.get('repo', 'memory-harness'))}"
    if host is not None or hook is not None:
        if cfg.get("relay"):
            probe = f'r={shlex.quote(cfg["relay"])}'
        else:
            probe = f'r="{RELAY_PATHS[0]}"; [ -x "$r" ] || r={RELAY_PATHS[1]}'
        flag = f"--hook {shlex.quote(hook)} " if hook is not None else ""
        remote = f'{cd} && {probe}; if [ -x "$r" ]; then exec env {env_str} "$r" {flag}-- {py_cmd}; else exec env {env_str} {py_cmd}; fi'
    else:
        remote = f"{cd} && exec env {env_str} {py_cmd}"
    return ["ssh", *SSH_OPTS, cfg["ssh"], remote]


def maybe():
    """有转发配置就把这次调用整个换成 ssh（不返回）；没有就什么都不做。"""
    cfg = config()
    if not cfg or not cfg.get("ssh"):
        return
    sys.stdout.flush()
    os.execvp("ssh", command(cfg, getattr(sys, "orig_argv", [sys.executable, *sys.argv])))
