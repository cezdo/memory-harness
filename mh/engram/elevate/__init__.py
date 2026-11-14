"""内核态（/mhcore，2026-10-24）：内核（core.md）默认只读（用户态），用户用命令临时提权，这一轮是内核态。

- 提权：UserPromptSubmit 认出整条消息以 /mhcore（codex $mhcore）开头、后面跟着要求，开窗口、记日志、补一句上下文，不拦输入；
- 窗口：这一轮结束（Stop）关；下一次输入、超过 WINDOW 秒也关；
- 拦：PreToolUse 看编辑类工具，目标是源文件又不在窗口里就拒绝；Bash 这类不解析，交给比对；
- 记：PostToolUse 每次比对源文件和快照，变了就记 edit（窗口里）或 unlogged（窗口外，谁发现的记在 found_by）。
日志是数据目录的 kernel-log.jsonl，只追加。钩子要轻（每次调工具都跑），只用标准库；出错放行，见 hook.py。
读日志照 Linux 的内核日志（dmesg，2026-10-30）：一条一行、前缀固定（编号、时刻、级别、谁、种类、会话、摘要），按级别筛，
连着的同类折成一行；diff、哈希留在原件里，按编号展开（view、detail）。grant 的原话、edit 那一轮本来就在宿主的会话记录里，
采集已经收进库，日志不再收一遍（runbook N3）。
设计见 design/engram/elevate/设计.md（2026-11-13 从 Synapse 搬来：core.md 是正本，改它的门归 Engram）。
"""
import difflib
import fcntl
import hashlib
import json
import os
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


LEVELS = ("info", "notice", "warn")
LEVEL = {"baseline": "info", "close": "info", "grant": "notice", "edit": "notice", "unlogged": "warn"}
WHO = {"grant": "用户", "edit": "我"}          # 其余是程序记的：基线、关窗，窗口外的改动（不知道是谁改的）
SUMMARY = 40                                  # 原话、理由在一行里最多几个字


def records(log):
    """日志 → [(编号, 记录)]，编号是行号（从 1 起），坏行跳过。读不到就是空。"""
    out = []
    try:
        with open(log, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("kind") in LEVEL:
                    out.append((n, d))
    except OSError:
        pass
    return out


def _local(t):
    try:
        return datetime.fromisoformat(t).astimezone()
    except (TypeError, ValueError):
        return None


def _churn(diff):
    lines = (diff or "").splitlines()
    add = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
    rm = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
    return f"+{add}-{rm}"


def _cut(text, n=SUMMARY):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def _key(d):
    """折叠用：种类、宿主、会话都一样的连着几条折成一行。窗口外的改动按发现它的会话算。"""
    by = d.get("found_by") or d
    return d["kind"], by.get("host", ""), by.get("session", "")


def _summary(d, folded=False):
    """一行的摘要。折起来的几条改动各改各的，行数加起来没有意义，只写工具。"""
    k = d["kind"]
    churn = "" if folded else _churn(d.get("diff")) + " "
    if k == "grant":
        return _cut(d.get("prompt"))
    if k == "edit":
        return f"{churn}{d.get('tool') or ''}".strip()
    if k == "unlogged":           # 10-24 早先几条把发现者写在顶层
        by = d.get("found_by") or d
        return f"{churn}{by.get('tool') or '比对'} 之后发现"
    if k == "close":
        return _cut(d.get("why"))
    return f"快照 {d.get('after', '')}"


def view(log, level="notice", since=None):
    """dmesg 式的行：只给 level 及以上的，since（带时区的 datetime）以后的；连着的同类折成一行，写 ×N 和时段。"""
    floor = LEVELS.index(level)
    got = [(n, d) for n, d in records(log) if LEVELS.index(LEVEL[d["kind"]]) >= floor
           and (since is None or (_local(d.get("time")) or since) >= since)]
    runs = []
    for n, d in got:
        if runs and _key(runs[-1][-1][1]) == _key(d):
            runs[-1].append((n, d))
        else:
            runs.append([(n, d)])
    out = []
    for run in runs:
        (n, d), last = run[0], run[-1][1]
        t, t2 = _local(d.get("time")), _local(last.get("time"))
        when = f"{t:%m-%d %H:%M}" if t else "?"
        if len(run) > 1 and t2:
            when += f"–{t2:%H:%M}" if t and t2.date() == t.date() else f"–{t2:%m-%d %H:%M}"
        _, host, ses = _key(d)
        where = f"{host}:{ses[:8]}" if host or ses else "-"
        more = f" ×{len(run)}（#{n}–#{run[-1][0]}）" if len(run) > 1 else ""
        out.append(f"#{n} {when} {LEVEL[d['kind']]} {WHO.get(d['kind'], '程序')} {d['kind']} {where} {_summary(d, len(run) > 1)}{more}")
    return out


def detail(log, n):
    """按编号展开一条：原件全部字段，diff 原样。"""
    for i, d in records(log):
        if i == n:
            diff = d.pop("diff", None)
            head = json.dumps(d, ensure_ascii=False, indent=1)
            return head + ("\n" + diff if diff else "")
    raise ValueError(f"内核日志里没有 #{n}")


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


SKILL = HERE.parents[1] / "skills/mhcore"      # 命令和 skill 的源文件（2026-11-13 归 mh/skills/）
OLD = ("mh/synapse/push/elevate/",)             # 搬家前的位置：宿主里指向它的链接、钩子就地换成新的，不并存（11-13）


def _link(src, dst, apply):
    if dst.is_symlink() and dst.resolve() == src.resolve():
        return None
    if dst.is_symlink() and any(o in str(Path(os.readlink(dst))) for o in OLD):    # 指向搬家前的位置：换掉
        if apply:
            dst.unlink()
            dst.symlink_to(src)
        return f"{dst} → {src}（原来指向旧位置）"
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
        for g in groups:                       # 搬家前的路径：就地换成新的
            for h in g.get("hooks", []):
                cmd = h.get("command", "")
                for o in OLD:
                    if f"{o}hook.py {host} {event}" in cmd:
                        h["command"] = re.sub(r"\S*" + re.escape(o) + r"hook\.py", str(script), cmd)
                        added.append(f"{name}（换路径）")
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
    done = [_link(SKILL / "claude/mhcore.md", home / ".claude/commands/mhcore.md", apply),
            _link(SKILL / "codex/mhcore", home / ".codex/skills/mhcore", apply),
            _hooks(home / ".claude/settings.json", "claude", python, apply),
            _hooks(home / ".codex/hooks.json", "codex", python, apply)]
    return [x for x in done if x]
