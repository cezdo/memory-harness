"""self 的 Claude Code 接入（design/rhythm/self/接入.md“Claude Code”，“我”在 Claude Code 里写，2026-11-08）。

接口照接入标准：NAME、ready、new_session、resumable、launch、outcome、actions。
"""
import json
import os
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

from ....engram import store
from .. import confine

NAME = "claude"
REPO = Path(__file__).resolve().parents[4]
MODEL = "claude-opus-5-5"
TOOLS = ["Read", "Grep", "Glob", "Bash", "Write", "Edit", "Agent"]
RESUME_BYTES = 3_000_000           # 上一段会话的记录超过这么大就另开一段（续接要把整段重新读进来；没调过，按真实成本再定）
WRITES = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def hooks(repo=REPO):
    """配置目录里装的钩子：只装 mh 自己的（手记、联想、开关、内核态），不装用户级的 tui 信号、主题钩子（不往用户的终端发信号）。"""
    py = f"/usr/bin/python3 -I -S {repo}"
    cmd = lambda c: {"type": "command", "command": c}
    return {
        "SessionStart": [{"hooks": [cmd(f"cd {repo} && python3 -m mh inject")]}],
        "UserPromptSubmit": [{"hooks": [cmd(f"cd {repo} && python3 -m mh.synapse.push.associate"),
                                        cmd(f"{py}/mh/engram/observe/switch/hook.py claude"),
                                        cmd(f"{py}/mh/engram/elevate/hook.py claude prompt")]}],
        "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                        "hooks": [cmd(f"{py}/mh/engram/elevate/hook.py claude pre")]}],
        "PostToolUse": [{"hooks": [cmd(f"{py}/mh/engram/elevate/hook.py claude post")]}],
        "Stop": [{"hooks": [cmd(f"{py}/mh/engram/elevate/hook.py claude stop"),
                            cmd(f"{py}/mh/engram/observe/switch/hook.py claude stop")]}]}


def home(base=None):
    """配置目录（数据目录下 claude-self-home）：认证指向原来的，设置和 MCP 每次照代码重写，不开自动记忆。
    采集读它的 projects/（observe sources/claude.SELF_ROOT），会话因此进记忆，工作区按 cwd 认成 self。"""
    h = Path(base or store.HOME / "claude-self-home")
    h.mkdir(parents=True, exist_ok=True)
    cred = h / ".credentials.json"
    if not cred.is_symlink():
        if cred.exists():
            cred.rename(cred.with_name(f".credentials.json.stale-{int(datetime.now().timestamp())}"))
        cred.symlink_to(Path.home() / ".claude/.credentials.json")
    (h / "settings.json").write_text(json.dumps({"autoMemoryEnabled": False, "hooks": hooks()}, ensure_ascii=False, indent=1))
    (h / "mcp.json").write_text(json.dumps({"mcpServers": {"mh": {
        "type": "stdio", "command": "env", "args": [f"PYTHONPATH={REPO}", "python3", "-m", "mh.synapse.pull.mcp"], "env": {}}}}))
    return h


def ready():
    """能不能叫：Claude 的 5 小时额度吃紧就不叫。返回空串 = 能；否则是为什么不能。"""
    from .. import world
    return "Claude 额度吃紧" if world.busy() else ""


def new_session():
    """新会话的编号：Claude Code 能自己指定（--session-id）。"""
    return str(uuid.uuid4())


def _file(session, h=None):
    return next(iter(sorted((Path(h or store.HOME / "claude-self-home") / "projects").glob(f"*/{session}.jsonl"))), None)


def resumable(session, h=None):
    """这段会话能不能接着：记录还在、没超过 RESUME_BYTES。"""
    f = _file(session, h)
    return bool(f) and f.stat().st_size < RESUME_BYTES


def launch(root, text, session, resumed, logs):
    """在后台起 claude -p：cwd 是 self，输入是 text 文件，输出进 logs/run.jsonl。返回进程（新进程组，超时整组停）。
    --setting-sources user,project：只读配置目录里的设置，外加 self 的项目说明（只给 user 时 AGENTS.md 读不进来，11-08 冒烟）。"""
    h = home()
    args = ["claude", "-p", "--model", MODEL, "--permission-mode", "dontAsk", "--tools", ",".join(TOOLS),
            "--allowedTools", *TOOLS, "mcp__mh", "--setting-sources", "user,project",
            "--mcp-config", str(h / "mcp.json"), "--strict-mcp-config", "--add-dir", str(Path.home()),
            "--output-format", "stream-json", "--verbose"]
    args += ["--resume", session] if resumed else ["--session-id", session]
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(h))
    with open(text) as stdin, open(Path(logs) / "run.jsonl", "w") as out, open(Path(logs) / "run.err", "w") as err:
        return subprocess.Popen(confine.wrap(args, root, logs, [h, Path.home() / ".claude/.credentials.json"]), stdin=stdin, stdout=out, stderr=err, cwd=root, env=env, start_new_session=True)


def _events(logs):
    p = Path(logs) / "run.jsonl"
    for line in p.read_text().splitlines() if p.exists() else []:
        try:
            yield json.loads(line)
        except ValueError:
            continue


def outcome(logs):
    """(正常结束没有, 最后一句, 会话编号)：读最后的 result 事件。"""
    last = None
    for e in _events(logs):
        if e.get("type") == "result":
            last = e
    if not last:
        return False, "", ""
    ok = not last.get("is_error") and last.get("subtype") == "success"
    return ok, (last.get("result") or "").strip(), last.get("session_id") or ""


def actions(logs):
    """这次会话（含子代理）自己做的：[("write", 路径) | ("run", 命令)]。"""
    out = []
    for e in _events(logs):
        if e.get("type") != "assistant":
            continue
        for b in (e.get("message") or {}).get("content") or []:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            inp = b.get("input") or {}
            if b.get("name") in WRITES and (inp.get("file_path") or inp.get("notebook_path")):
                out.append(("write", inp.get("file_path") or inp.get("notebook_path")))
            elif b.get("name") == "Bash" and inp.get("command"):
                out.append(("run", inp["command"]))
    return out
