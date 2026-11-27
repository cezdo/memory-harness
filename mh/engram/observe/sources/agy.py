"""agy（~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript_full.jsonl，元数据在 conversation_summaries.db）。"""

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import unquote

from .turn import Session, Turn
from ...store import HOME

SELF_ROOT = HOME / "agy-self-home"
ROOTS = [Path.home() / ".gemini/antigravity-cli", Path.home() / ".gemini/antigravity-acp",
         Path.home() / ".gemini/antigravity", SELF_ROOT]
_USER_REQUEST = re.compile(r"<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>", re.S)
_META_TAGS = re.compile(r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>.*?</\1>\s*", re.S)
_INJECTED = re.compile(r"<memory-harness>.*?</memory-harness>\s*", re.S)   # ACP 下 Zeron 加在用户输入前的联想（synapse/hosts/agy acp-prompt）
# Zeron 让 agy 给会话起标题的内部调用：不是用户的对话，整段不收
_HELPER = ("You generate session titles.",)


def _user_text(raw):
    s = raw or ""
    m = _USER_REQUEST.search(s)
    s = m.group(1) if m else _META_TAGS.sub("", s)
    return _INJECTED.sub("", s).strip()


def _conv_id(path):
    """.../<brain>/<conv_id>/.system_generated/logs/transcript_full.jsonl → <conv_id>。"""
    p = Path(path)
    if p.parent.name == "logs" and p.parent.parent.name == ".system_generated":
        return p.parent.parent.parent.name
    return p.stem


def _root_of(path):
    """.../brain/<conv_id>/.system_generated/logs/transcript_full.jsonl → ..."""
    p = Path(path)
    if p.parent.name == "logs" and p.parent.parent.name == ".system_generated":
        brain = p.parent.parent.parent.parent
        if brain.name == "brain":
            return brain.parent
    return None


def _meta(path, conv_id):
    """读 CLI 的会话摘要库，或 ACP 的 .meta 与子代理登记目录 → {title, cwd, parent, kind}。"""
    root = _root_of(path)
    if not root:
        return {}
    out = {}
    db = root / "conversation_summaries.db"
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT title, workspace_uris, parent_conversation_id, nesting_depth "
                    "FROM conversation_summaries WHERE conversation_id=?",
                    (conv_id,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            row = None
        if row:
            title, uris_raw, parent, depth = row
            cwd = ""
            try:
                uris = json.loads(uris_raw or "[]")
                if uris and isinstance(uris[0], str):
                    u = uris[0]
                    cwd = unquote(u.removeprefix("file://")) if u.startswith("file://") else u
            except ValueError:
                pass
            parent = parent or ""
            kind = "subagent" if (parent or (depth or 0) > 0) else "user"
            out = {"title": (title or "").strip(), "cwd": cwd, "parent": parent, "kind": kind}
    records = root / "conversations"
    child = next((records / "subagents").glob(f"*/{conv_id}.json"), None)
    if child is not None:
        out.update(kind="subagent", parent=child.parent.name)
    meta_file = records / f"{conv_id}.meta"
    if meta_file.exists() and (not out or not out.get("cwd")):
        try:
            m = json.loads(meta_file.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            m = None
        if isinstance(m, dict):
            cwd = m.get("cwd") if isinstance(m.get("cwd"), str) else ""
            title = m.get("title").strip() if isinstance(m.get("title"), str) else ""
            if cwd or title:
                out.setdefault("kind", "user")
                out.setdefault("parent", "")
                out["cwd"] = out.get("cwd") or cwd
                out["title"] = out.get("title") or title
    return out


def signature(path):
    """元数据（title、cwd、parent、kind）存在外层 SQLite 或 .meta 里，变了也要重新采集。"""
    m = _meta(path, _conv_id(path))
    raw = f"v2|{m.get('cwd', '')}|{m.get('title', '')}|{m.get('kind', '')}|{m.get('parent', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def files():
    seen = set()
    for root in ROOTS:
        brain = root / "brain"
        if not brain.is_dir():
            continue
        for conv in sorted(brain.iterdir()):
            logs = conv / ".system_generated/logs"
            full = logs / "transcript_full.jsonl"
            short = logs / "transcript.jsonl"
            target = full if full.exists() else (short if short.exists() else None)
            if target is not None and target not in seen:
                seen.add(target)
                yield target


def _tool_name(tc):
    name = tc.get("name") or ""
    if name == "call_mcp_tool":
        args = tc.get("args")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if isinstance(args, dict):
            srv = args.get("ServerName") or args.get("server_name") or ""
            tool = args.get("ToolName") or args.get("tool_name") or ""
            if srv and tool:
                return f"mcp_{srv}_{tool}"
    return name


def _workspace_dir(path_str):
    """绝对路径 → 所属项目工作区根目录；不是具体项目路径返回 ""。"""
    from ... import store as st
    if not st.project_workspace(path_str):
        return ""
    c = st.unworktree(path_str)
    for d in sorted(st.WORKSPACE_DIRS, key=len, reverse=True):
        if c == d or c.startswith(d + "/"):
            return d
    root = st._git_root(c, str(Path.home()))
    if root:
        main = st.worktree_main(Path(root))
        return main or root
    p = Path(c)
    return str(p if p.is_dir() else p.parent)


def _infer_cwd(steps):
    """DB 和 .meta 里都没查到 cwd 时，从 run_command/view_file/read_file/edit_file/write_file 等绝对路径兜底推断工作区目录。"""
    fallback = ""
    for o in steps:
        for tc in o.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            args = tc.get("args") or {}
            if not isinstance(args, dict):
                continue
            for key in ("Cwd", "Path", "AbsolutePath", "TargetFile", "DirectoryPath"):
                p = args.get(key) or ""
                if isinstance(p, str) and p.startswith('"'):
                    try:
                        p = json.loads(p)
                    except ValueError:
                        pass
                if isinstance(p, str) and p.startswith("/"):
                    if key == "Cwd" and not fallback:
                        fallback = p
                    d = _workspace_dir(p)
                    if d:
                        return d
    return fallback


def parse(path):
    conv_id = _conv_id(path)
    meta = _meta(path, conv_id)
    session = Session(
        "agy",
        conv_id,
        str(path),
        kind=meta.get("kind") or "user",
        parent=meta.get("parent") or "",
        cwd=meta.get("cwd") or "",
        title=meta.get("title") or "",
    )
    if session.kind == "subagent":
        return session, []
    turns, cur = [], None
    steps = []

    def close(done=False):
        if cur is None:
            return
        texts = cur.pop("_texts")
        if texts:
            cur["turn"].assistant = texts[-1]
            cur["turn"].commentary = texts[:-1]
        if done:
            cur["turn"].closed = True
        turns.append(cur["turn"])

    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(o, dict):
                continue
            steps.append(o)
            t = o.get("type")
            ts = o.get("created_at") or ""
            step_idx = o.get("step_index")
            if t == "USER_INPUT":
                text = _user_text(o.get("content"))
                if not text:
                    continue
                if not turns and cur is None and text.startswith(_HELPER):
                    session.kind = "title"
                    return session, []
                if cur is not None:
                    close(done=True)
                if not session.started and ts:
                    session.started = ts
                addr = {"line": [lineno, lineno]}
                if step_idx is not None:
                    addr["step"] = [step_idx, step_idx]
                cur = {"turn": Turn(user=text, started=ts, address=addr), "_texts": [], "_last": None}
            elif t == "PLANNER_RESPONSE" and cur is not None:
                model = o.get("model") or ""
                if model:
                    session.model = model
                    cur["turn"].note_model(model)
                thinking = (o.get("thinking") or "").strip()
                if thinking:
                    cur["turn"].reasoning.append(thinking)
                tcs = o.get("tool_calls") or []
                for tc in tcs:
                    if isinstance(tc, dict):
                        tn = _tool_name(tc)
                        if tn:
                            cur["turn"].tools.append(tn)
                content = (o.get("content") or "").strip()
                if content:
                    cur["_texts"].append(content)
                cur["_last"] = o
            elif cur is not None:
                cur["_last"] = o
            if cur is not None:
                cur["turn"].address["line"][1] = lineno
                if step_idx is not None and "step" in cur["turn"].address:
                    cur["turn"].address["step"][1] = step_idx

    if cur is not None:
        last = cur.get("_last") or {}
        done = (
            last.get("type") == "PLANNER_RESPONSE"
            and last.get("status") == "DONE"
            and not last.get("tool_calls")
            and bool(cur["_texts"])
        )
        close(done=done)
    if not session.cwd:
        session.cwd = _infer_cwd(steps)
    return session, turns
