"""世界的现状和核查（rhythm/self）：醒来那一轮“此刻”里程序编的世界现状（world），开始时和跑完的快照（snapshot）、
两者比出来的变化（audit），醒着的进程还在不在（alive），Claude 的额度紧不紧（busy）。
2026-11-08 从 explore 搬来：self 公开、explore 还冻结着，explore 反过来从这里引用。"""
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _run(args, cwd=REPO):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True).stdout.rstrip()


def alive(pid):
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    try:                                    # 自己的子进程：收掉已经退出的，免得僵尸算活着
        return os.waitpid(pid, os.WNOHANG) == (0, 0)
    except ChildProcessError:               # 整理循环重启过：不是自己的子进程了，按 /proc 认，pid 被复用的不算
        try:
            return (b"claude" in Path(f"/proc/{pid}/cmdline").read_bytes()
                    and "zombie" not in Path(f"/proc/{pid}/status").read_text().lower())
        except OSError:
            return False


def busy():
    """Claude 的额度紧不紧。没有 quota（公开包不带）就当作不紧：真用完了宿主自己报错，这次醒来没正常结束，隔一天再叫。"""
    import importlib.util
    if importlib.util.find_spec("mh.rhythm.quota") is None:
        return False
    from .. import BUSY_5H, quota
    q = quota.read(["claude"])[0]
    if "error" in q:
        return True
    used = {w["name"]: w["used"] for w in q["windows"]}
    return not q.get("ok", True) or used.get("5h", 0) >= BUSY_5H


def _services():
    names = _run(["systemctl", "--user", "list-units", "--plain", "--no-legend", "--all", "memory-harness-*"], None).split("\n")
    out = {}
    for line in names:
        name = line.split()[0] if line.strip() else ""
        if name.endswith(".service"):
            out[name] = _run(["systemctl", "--user", "show", name, "-p", "ActiveState", "-p", "ActiveEnterTimestamp", "--value"],
                             None).replace("\n", " ")
    return out


def _processes():
    """mh 的进程和宿主进程：[(pid, 起始时刻, 命令行摘要)]。"""
    out = []
    for line in _run(["ps", "-eo", "pid=,lstart=,args="], None).splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        cmd = parts[6]
        if re.search(r"python3 -m mh\b|mh\.synapse\.pull\.mcp|dsh web|(^|/)codex( |$)|(^|/)claude( |$)", cmd):
            when = datetime.strptime(" ".join(parts[1:6]), "%a %b %d %H:%M:%S %Y")
            out.append((int(parts[0]), when, cmd[:120]))
    return out


def snapshot():
    """核查要比的：HEAD、已跟踪文件的改动、服务。"""
    return {"head": _run(["git", "rev-parse", "HEAD"]),
            "tracked": _run(["git", "-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=no"]),
            "branches": _run(["git", "branch", "--format=%(refname:short)"]),
            "services": _services()}


def world(now=None):
    """这一轮的“世界的现状”（程序编，提示词的槽位）。"""
    head = _run(["git", "log", "-1", "--format=%h %cd %s", "--date=format-local:%m-%d %H:%M"])
    dirty = _run(["git", "-c", "core.quotepath=false", "status", "--porcelain", "--untracked-files=no"]).splitlines()
    lines = [f"- 仓库 `{REPO}`，main 在 `{head}`。",
             "- 分支：" + "、".join(_run(["git", "branch", "--format=%(refname:short)"]).split()),
             "- worktree：" + "；".join(" ".join(x.split()) for x in _run(["git", "worktree", "list"]).splitlines()),
             f"- 已跟踪文件没提交的改动：{len(dirty)} 个" + ("（" + "、".join(x[3:] for x in dirty[:10]) + "）" if dirty else "")]
    lines.append("- 常驻服务（systemd 用户级）：")
    lines += [f"  - {n}：{v}" for n, v in _services().items()]
    lines.append("- 进程（mh、宿主；宿主持有的 MCP 吃的是它起来那一刻的代码）：")
    lines += [f"  - {pid} 起于 {when:%m-%d %H:%M}：`{cmd}`" for pid, when, cmd in _processes()]
    tz = os.path.realpath("/etc/localtime").split("zoneinfo/")[-1]
    lines.append(f"- 机器时区 {tz}；mh 的一天从 UTC 0 点切。")
    return "\n".join(lines)


def audit(before, after):
    """开始时和跑完的快照比 → 越界的说明（空 = 没越界）。"""
    out = []
    if after["head"] != before["head"]:
        out.append(f"HEAD 变了：{before['head'][:8]} → {after['head'][:8]}")
    if after["tracked"] != before["tracked"]:
        out.append("已跟踪文件的改动变了：\n```\n" + after["tracked"] + "\n```")
    if after.get("branches") != before.get("branches"):
        out.append(f"分支变了：{before.get('branches')} → {after.get('branches')}")
    for name, v in before["services"].items():
        if after["services"].get(name) != v:
            out.append(f"服务 {name}：{v} → {after['services'].get(name)}")
    return out
