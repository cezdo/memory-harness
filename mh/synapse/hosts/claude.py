"""Claude Code 的会话记录（transcript），以及 MCP 怎么知道现在是哪段会话。

/clear、/resume 以后宿主换了会话，MCP 子进程不重启，环境变量 CLAUDE_CODE_SESSION_ID 还是启动时的那段（2026-10-27，
tui 4bfb59ad → ea43d745）。SessionStart 钩子（新会话、恢复、clear、压缩后都触发）按宿主进程记下当前会话：
钩子往上找到 claude 进程，写 <HOME>/run/claude/<pid>；MCP 是 claude 的直接子进程，按父进程读。读不到照旧用环境变量。"""
import json
import os
from pathlib import Path

RUN = Path(os.environ.get("MH_HOME", Path.home() / ".local/share/memory-harness")) / "run" / "claude"   # 同 store.HOME


def _parent(pid):
    """/proc 里 pid 的 (父进程, 名字)；读不到给 (0, "")。"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        name, rest = stat[stat.index("(") + 1:stat.rindex(")")], stat[stat.rindex(")") + 2:].split()
        return int(rest[1]), name
    except (OSError, ValueError, IndexError):
        return 0, ""


def host_pid(start=None):
    """往上找第一个叫 claude 的进程（钩子经 sh 起，MCP 直接是它的子进程）；找不到给 0。"""
    pid = start or os.getppid()
    for _ in range(8):
        if pid <= 1:
            return 0
        ppid, name = _parent(pid)
        if name == "claude":
            return pid
        pid = ppid
    return 0


def bind(session, pid=None):
    """钩子：记下这个 claude 进程现在的会话。顺手删掉进程已经不在的记录。出任何错都不影响钩子。"""
    try:
        pid = pid or host_pid()
        if not pid or not session:
            return
        RUN.mkdir(parents=True, exist_ok=True)
        tmp = RUN / f".{pid}.tmp"
        tmp.write_text(session)
        tmp.replace(RUN / str(pid))
        for f in RUN.iterdir():
            if f.name.isdigit() and not Path(f"/proc/{f.name}").exists():
                f.unlink(missing_ok=True)
    except OSError:
        pass


def current(pid=None):
    """MCP：父进程（claude）现在的会话；没记过给空串。"""
    try:
        return (RUN / str(pid or os.getppid())).read_text().strip()
    except OSError:
        return ""


def read_context(path, prompt):
    """transcript 里最后一次压缩之后的内容（模型现在看得到的），以及这一句是不是宿主注入的。
    返回 (上下文文本, 宿主注入的类型或 None, transcript 里有没有找到这一句)。"""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return "", None, False
    start = max((i for i, l in enumerate(lines) if '"compact_boundary"' in l), default=0)
    texts, host, found, last = [], None, False, None
    want = prompt.strip()

    def strings(x, skip=("thinking", "signature")):
        if isinstance(x, str):
            yield x
        elif isinstance(x, list):
            for y in x:
                yield from strings(y)
        elif isinstance(x, dict):
            if x.get("type") in skip:
                return
            for k, y in x.items():
                if k not in skip:
                    yield from strings(y)

    for l in lines[start:]:
        try:
            d = json.loads(l)
        except ValueError:
            continue
        t = d.get("type")
        if t == "system":
            last = d.get("subtype")
        elif t == "user":
            c = d.get("message", {}).get("content")
            said = c if isinstance(c, str) else "".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
            if said.strip() == want:
                found = True
                kind = (d.get("origin") or {}).get("kind")
                host = ("宿主（isMeta）" if d.get("isMeta") else
                        f"宿主（{kind}）" if kind is not None and kind != "human" else None)
                continue                                 # 这一句本身不算上下文
            last = "user"
            texts.extend(strings(d.get("message", {}).get("content")))
        elif t == "assistant":
            texts.extend(strings(d.get("message", {}).get("content")))
        elif t == "attachment":
            a = d.get("attachment") or {}
            if a.get("type") != "hook_system_message":    # 联想那一行只给用户看，模型看不到
                texts.extend(strings(a))
    if not found and last == "scheduled_task_fire":
        host = "定时任务"
    return "\n".join(texts), host, found
