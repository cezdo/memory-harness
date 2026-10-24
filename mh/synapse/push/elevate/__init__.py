"""内核态（/mhcore，2026-10-24）：内核（core.md）默认只读（用户态），用户用命令临时提权，这一轮是内核态。

- 提权：UserPromptSubmit 认出整条消息以 /mhcore（codex $mhcore）开头、后面跟着要求，开窗口、记日志、补一句上下文，不拦输入；
- 窗口：这一轮结束（Stop）关；下一次输入、超过 WINDOW 秒也关；
- 拦：PreToolUse 看编辑类工具，目标是源文件又不在窗口里就拒绝；Bash 这类不解析，交给比对；
- 记：PostToolUse 每次比对源文件和快照，变了就记 edit（窗口里）或 unlogged（窗口外，谁发现的记在 found_by）。
日志是数据目录的 kernel-log.jsonl，只追加。钩子要轻（每次调工具都跑），只用标准库；出错放行，见 hook.py。
设计见 design/synapse/设计.md“内核态”。
"""
import difflib
import fcntl
import hashlib
import json
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
WINDOW = 30 * 60                     # 窗口最长多久（秒）：Stop 丢了的兜底
EDIT_TOOLS = re.compile(r"(Edit|Write|MultiEdit|NotebookEdit|apply_patch)")
_RAW = re.compile(r"[/$]mhcore(?:\s+(.*))?", re.S)
_CLAUDE = re.compile(r"<command-name>\s*/mhcore\s*</command-name>.*?(?:<command-args>(.*?)</command-args>|$)", re.S)

GRANTED = ("〔内核态〕这一轮由用户用 /mhcore 临时提权：可以改内核（core.md）{path}。"
           "只改要求涉及的句子，改完告诉用户改了哪句（改前、改后）；这一轮结束回到用户态，改动由程序记进内核日志。")
USAGE = "memory-harness：/mhcore 后面要写要求，如“/mhcore 接下来优先把称呼改成示例用户”。"
DENIED = ("内核（core.md）在用户态下只读（{path}）。要改请用户用 /mhcore <要求> 临时提权；"
          "或者先把想改的句子写给用户看。")


class Kernel:
    """一份源文件和它在数据目录里的状态：日志、快照、各会话的窗口。"""

    def __init__(self, path, home):
        self.path, self.home = Path(path), Path(home)
        self.log = self.home / "kernel-log.jsonl"
        self.state = self.home / "kernel"
        self.last = self.state / "last.md"

    # —— 记录 ——
    def note(self, kind, host="", session="", **more):
        self.home.mkdir(parents=True, exist_ok=True)
        rec = {"time": datetime.now(timezone.utc).isoformat(), "kind": kind, "host": host, "session": session, **more}
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    @contextmanager
    def lock(self):
        self.state.mkdir(parents=True, exist_ok=True)
        with open(self.state / ".lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    # —— 窗口 ——
    def _window(self, host, session):
        return self.state / "open" / f"{host}-{re.sub(r'[^A-Za-z0-9_.-]', '_', session or 'unknown')}.json"

    def opened(self, host, session, now=None):
        w = self._window(host, session)
        try:
            d = json.loads(w.read_text())
        except (OSError, ValueError):
            return None
        if (now or time.time()) - d.get("at", 0) > WINDOW:
            return None
        return d

    def open(self, host, session, prompt, cwd=""):
        w = self._window(host, session)
        w.parent.mkdir(parents=True, exist_ok=True)
        w.write_text(json.dumps({"at": time.time(), "prompt": prompt}, ensure_ascii=False))
        self.note("grant", host, session, cwd=cwd, prompt=prompt, file=str(self.path))

    def close(self, host, session, why):
        w = self._window(host, session)
        if w.exists():
            w.unlink()
            self.note("close", host, session, why=why)

    # —— 比对 ——
    def _text(self):
        try:
            return self.path.read_text()
        except OSError:
            return None

    def compare(self, host="", session="", tool="", cwd=""):
        """源文件和快照不一样就记一条、更新快照；返回记下的那条或 None。"""
        with self.lock():
            now = self._text()
            if now is None:
                return None
            try:
                before = self.last.read_text()
            except OSError:
                before = None
            if before == now:
                return None
            shutil.copyfile(self.path, self.last)
            sha = lambda s: hashlib.sha256(s.encode()).hexdigest()[:16]
            if before is None:
                return self.note("baseline", host, session, file=str(self.path), after=sha(now))
            diff = "".join(difflib.unified_diff(before.splitlines(True), now.splitlines(True), "改前", "改后", n=1))
            change = dict(file=str(self.path), before=sha(before), after=sha(now), diff=diff)
            if self.opened(host, session):
                return self.note("edit", host, session, cwd=cwd, tool=tool, **change)
            # 窗口外的改动不知道是谁改的（Bash、编辑器、别的宿主）；顶层不写宿主、会话，免得读成发现者改的
            return self.note("unlogged", found_by=dict(host=host, session=session, tool=tool, cwd=cwd), **change)

    def targets(self, tool_input, cwd=""):
        """编辑类工具的参数里有没有指向源文件的路径（Claude Code 的 file_path、codex apply_patch 补丁里的文件行）。"""
        text = tool_input if isinstance(tool_input, str) else json.dumps(tool_input, ensure_ascii=False)
        me = self.path.resolve()
        for m in re.findall(r"[^\s\"'\\:]*" + re.escape(self.path.name), text):
            p = Path(m).expanduser()
            if not p.is_absolute():
                p = Path(cwd or ".") / p
            try:
                if p.resolve() == me:
                    return True
            except OSError:
                continue
        return False


def request(prompt):
    """这条输入是不是 /mhcore：是就返回要求（可能是空串），不是返回 None。只认整条消息以命令开头的写法。"""
    u = (prompt or "").strip()
    if u.startswith("<command-"):
        m = _CLAUDE.search(u)
        return None if not m else (m.group(1) or "").strip()
    m = _RAW.fullmatch(u)
    return None if not m else (m.group(1) or "").strip()


def handle(kernel, host, event, d):
    """一个钩子事件 → (宿主要的 JSON 或 None, 退出码)。"""
    session, cwd = d.get("session_id") or "", d.get("cwd") or ""
    if event == "prompt":
        want = request(d.get("prompt"))
        if want is None:
            kernel.close(host, session, "下一次输入")
            return None, 0
        if not want:
            return {"decision": "block", "reason": USAGE}, 0
        kernel.compare(host, session, cwd=cwd)          # 提权前先把手改的认出来，免得记到这一轮头上
        kernel.open(host, session, want, cwd)
        return {"systemMessage": "memory-harness：本轮内核态（/mhcore）",
                "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": GRANTED.format(path=kernel.path)}}, 0
    if event == "pre":
        tool = d.get("tool_name") or ""
        if EDIT_TOOLS.search(tool) and kernel.targets(d.get("tool_input") or {}, cwd) and not kernel.opened(host, session):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": DENIED.format(path=kernel.path)}}, 0
        return None, 0
    if event == "post":
        kernel.compare(host, session, d.get("tool_name") or "", cwd)
        return None, 0
    if event == "stop":
        kernel.compare(host, session, cwd=cwd)
        kernel.close(host, session, "这一轮结束")
        return None, 0
    raise ValueError(f"未知事件 {event}")


# —— 安装 ——
EVENTS = {"UserPromptSubmit": ("prompt", None), "PreToolUse": ("pre", {"claude": "Edit|Write|MultiEdit|NotebookEdit"}),
          "PostToolUse": ("post", None), "Stop": ("stop", None)}


def _link(src, dst, apply):
    if dst.is_symlink() and dst.resolve() == src.resolve():
        return None
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"{dst} 已存在且不是指向 {src} 的链接，先看清再处理")
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
    return f"{dst} → {src}"


def _hooks(path, host, python, apply):
    """把四个事件的钩子加进宿主配置（已经有就不动）；改之前留一份备份。"""
    data = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    script = HERE / "hook.py"
    added = []
    for name, (event, matchers) in EVENTS.items():
        groups = hooks.setdefault(name, [])
        if any(f"{script} {host} {event}" in h.get("command", "") for g in groups for h in g.get("hooks", [])):
            continue
        g = {"hooks": [{"type": "command", "command": f"{python} -I -S {script} {host} {event}", "timeout": 5}]}
        if matchers and matchers.get(host):
            g["matcher"] = matchers[host]
        groups.append(g)
        added.append(name)
    if not added:
        return None
    if apply:
        if path.exists():
            shutil.copy2(path, path.with_name(f"{path.name}.bak-mhcore-{datetime.now():%Y%m%d-%H%M%S}"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return f"{path}：加 {'、'.join(added)}"


def install(home=Path.home(), python="/usr/bin/python3", apply=False):
    """装 Claude Code 命令、codex skill（符号链接到仓库）和两边的四个钩子；apply=False 只列要做什么。
    codex 的新钩子要用户在 codex 的 /hooks 里信任一次。dsh 还没接。"""
    done = [_link(HERE / "claude/mhcore.md", home / ".claude/commands/mhcore.md", apply),
            _link(HERE / "codex/mhcore", home / ".codex/skills/mhcore", apply),
            _hooks(home / ".claude/settings.json", "claude", python, apply),
            _hooks(home / ".codex/hooks.json", "codex", python, apply)]
    return [x for x in done if x]
