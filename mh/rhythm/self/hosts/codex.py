"""self 的 Codex 接入：独立 CODEX_HOME，沿用 Synapse 的 hooks 和 MCP。"""
import importlib.util
import json
import os
import shutil
import shlex
import subprocess
from pathlib import Path

from ....engram import store
from .. import confine
from ....engram.invoke import NO_UPDATE, OFFLINE_FEATURES
from ....synapse.hosts.codex.install import configuration

NAME = "codex"
REPO = Path(__file__).resolve().parents[4]
MODEL = "gpt-6.1-sol"
RESUME_BYTES = 3_000_000


def hooks():
    out = configuration()
    py = f"/usr/bin/python3 -I -S {REPO}"
    for event, command in (
        ("UserPromptSubmit", f"{py}/mh/engram/observe/switch/hook.py codex"),
        ("UserPromptSubmit", f"{py}/mh/engram/elevate/hook.py codex prompt"),
        ("PreToolUse", f"{py}/mh/engram/elevate/hook.py codex pre"),
        ("PostToolUse", f"{py}/mh/engram/elevate/hook.py codex post"),
        ("Stop", f"{py}/mh/engram/elevate/hook.py codex stop"),
        ("Stop", f"{py}/mh/engram/observe/switch/hook.py codex stop"),
    ):
        out["hooks"].setdefault(event, []).append({"hooks": [
            {"type": "command", "command": command, "timeout": 5}]})
    return out


def home(base=None, root=store.SELF_ROOT):
    h = Path(base or store.HOME / "codex-self-home")
    h.mkdir(parents=True, exist_ok=True)
    auth = h / "auth.json"
    if not auth.exists() and not auth.is_symlink():
        auth.symlink_to(Path.home() / ".codex/auth.json")
    # 只生成 self 自己的配置，不复制交互配置（通知、插件、全局说明等）。
    q = lambda s: json.dumps(str(s))
    (h / "config.toml").write_text(
        'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
        'check_for_update_on_startup = false\n'
        '[features]\nmemories = false\nplugins = false\napps = false\nremote_plugin = false\n'
        '[memories]\ngenerate_memories = false\nuse_memories = false\n'
        '[analytics]\nenabled = false\n'
        f'[projects.{q(Path(root).resolve())}]\ntrust_level = "trusted"\n'
        '[mcp_servers.mh]\ncommand = "/usr/bin/python3"\n'
        'args = ["-m", "mh.synapse.hosts.codex", "mcp"]\n'
        'default_tools_approval_mode = "approve"\n'
        f'[mcp_servers.mh.env]\nPYTHONPATH = {q(REPO)}\n')
    (h / "hooks.json").write_text(json.dumps(hooks(), ensure_ascii=False, indent=2))
    return h


def ready():
    if not shutil.which("codex"):
        return "Codex 没装"
    if not (Path.home() / ".codex/auth.json").exists():
        return "Codex 未登录"
    if importlib.util.find_spec("mh.rhythm.quota") is None:
        return ""
    from ... import BUSY_5H, quota
    q = quota.read([NAME])[0]
    if "error" in q:
        return "Codex 额度读不到：" + q["error"]
    used = {w["name"]: w["used"] for w in q["windows"]}
    return "Codex 额度吃紧" if not q.get("ok", True) or used.get("5h", 0) >= BUSY_5H else ""


def new_session():
    return ""                         # Codex 自己分配 thread id


def _files(h):
    for name in ("sessions", "archived_sessions"):
        yield from sorted((h / name).rglob("*.jsonl"))


def resumable(session, h=None):
    if not session:
        return False
    return any(p.stat().st_size < RESUME_BYTES for p in _files(Path(h or store.HOME / "codex-self-home"))
               if p.stem.endswith("-" + session))


def launch(root, text, session, resumed, logs):
    h = home(root=root)
    logs = Path(logs)
    # 保存每个记录的起点，续接核查不重复统计上一轮；子代理的新文件从 0 读。
    (logs / "codex.json").write_text(json.dumps({"home": str(h), "root": str(Path(root).resolve()),
        "offsets": {str(p): p.stat().st_size for p in _files(h)}}))
    args = ["codex", "exec"] + (["resume", session] if resumed else [])
    args += ["-m", MODEL, "--json", "--strict-config", "--skip-git-repo-check",
             "--dangerously-bypass-hook-trust", *OFFLINE_FEATURES, *NO_UPDATE, "-"]
    env = dict(os.environ, CODEX_HOME=str(h))
    # 外层会话/终端身份不传入后台会话，避免终端包装器改当前 pane 或错误绑定足迹。
    for key in ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "TMUX", "TMUX_PANE"):
        env.pop(key, None)
    with open(text) as stdin, (logs / "run.jsonl").open("w") as out, (logs / "run.err").open("w") as err:
        return subprocess.Popen(confine.wrap(args, root, logs, [h, Path.home() / ".codex/auth.json"]), stdin=stdin, stdout=out, stderr=err, cwd=root,
                                env=env, start_new_session=True)


def _events(path, offset=0):
    if not path.exists():
        return
    with path.open("rb") as f:
        f.seek(offset)
        for line in f:
            try:
                yield json.loads(line)
            except ValueError:
                continue


def outcome(logs):
    ok, said, session = False, "", ""
    for e in _events(Path(logs) / "run.jsonl"):
        t = e.get("type")
        if t == "thread.started":
            session = e.get("thread_id") or session
        elif t == "turn.completed":
            ok = True
        elif t in ("turn.failed", "error", "turn.started"):
            ok = False
        elif t == "item.completed" and e.get("item", {}).get("type") == "agent_message":
            said = e["item"].get("text", "").strip()
    return ok, said, session


def _actions(item, root):
    kind = item.get("type")
    if kind in ("command_execution", "CommandExecution") and item.get("command"):
        command = item["command"]
        if isinstance(command, list):
            command = shlex.join(command)
        return [("run", command)]
    if kind in ("file_change", "FileChange"):
        changes = item.get("changes") or []
        paths = changes if isinstance(changes, dict) else [c["path"] for c in changes if c.get("path")]
        return [("write", str((Path(root) / p).resolve())) for p in paths]
    return []


def actions(logs):
    """读实际执行事件（包括 exec 内部调用），不从 JavaScript 文字猜动作。

    子代理按 session_meta 父会话递归读取；续接只读本次新增记录。
    脚本内部任意写入仍只能列命令，不能还原程序的文件副作用。
    """
    logs = Path(logs)
    meta = json.loads((logs / "codex.json").read_text())
    out = []
    for e in _events(logs / "run.jsonl"):
        if e.get("type") in ("item.started", "item.completed"):
            out.extend(_actions(e.get("item") or {}, meta["root"]))
    session = outcome(logs)[2]
    records, selected = [], {session} if session else set()
    for p in _files(Path(meta["home"])):
        first = next(_events(p), {})
        m = first.get("payload") or {}
        records.append((p, m.get("id"), m.get("forked_from_id") or m.get("parent_thread_id"), m.get("cwd")))
    while True:
        added = {sid for _, sid, parent, _ in records if parent in selected} - selected
        if not added:
            break
        selected.update(added)
    # 记录以后还会续写：以这次主轮的完成时刻封口，回看旧核查也不混入后来的动作。
    end = None
    for p, sid, _, _ in records:
        if sid == session:
            for e in _events(p, meta["offsets"].get(str(p), 0)):
                if e.get("type") == "event_msg" and (e.get("payload") or {}).get("type") == "task_complete":
                    end = e.get("timestamp")
                    break
    for p, sid, _, cwd in records:
        if sid not in selected:
            continue
        for e in _events(p, meta["offsets"].get(str(p), 0)):
            if end and e.get("timestamp", "") > end:
                continue
            payload = e.get("payload") or {}
            if e.get("type") == "event_msg" and payload.get("type") in ("item_started", "item_completed"):
                if payload.get("thread_id", sid) != sid:
                    continue             # fork 复制进来的父会话事件不属于子代理
                out.extend(_actions(payload.get("item") or {}, cwd or meta["root"]))
    return list(dict.fromkeys(out))
