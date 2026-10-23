"""Codex rollout（~/.codex/sessions、archived_sessions）。"""
import json
import re
from pathlib import Path

from .turn import Session, Turn

ROOTS = [Path.home() / ".codex/sessions", Path.home() / ".codex/archived_sessions"]
KINDS = {"user": "user", "subagent": "subagent", "guardian_review": "review"}

# 宿主注入而非用户输入的块：整块包在一个 <tag>…</tag> 里，或是 AGENTS.md 注入
_WRAPPED = re.compile(r"^\s*<([A-Za-z_][\w-]*)[\s>].*</\1>\s*$", re.S)


def files():
    for root in ROOTS:
        yield from sorted(root.rglob("*.jsonl"))


def _user_text(content):
    parts = []
    for block in content or []:
        text = block.get("text") if isinstance(block, dict) else None
        if not isinstance(text, str):
            continue
        if _WRAPPED.match(text) or text.startswith("# AGENTS.md instructions"):
            continue
        parts.append(text)
    return "\n".join(parts).strip()


def _out_text(content):
    return "\n".join(b.get("text", "") for b in content or []
                     if isinstance(b, dict) and b.get("type") == "output_text").strip()


def parse(path):
    session = Session("codex", path.stem, str(path))
    turns, cur = [], None

    def close():
        if cur is None:
            return
        finals = cur.pop("_final")
        if finals:
            cur["turn"].assistant = finals[-1]
            cur["turn"].commentary += finals[:-1]
        elif cur["turn"].commentary:
            cur["turn"].assistant = cur["turn"].commentary.pop()
        turns.append(cur["turn"])

    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t, p = o.get("type"), o.get("payload") or {}
            if t == "session_meta":
                if session.started:      # fork 文件随后还会带上父会话的 meta，只认第一条
                    continue
                session.session = p.get("id") or session.session
                session.parent = p.get("forked_from_id") or p.get("parent_thread_id") or ""
                session.kind = KINDS.get(p.get("thread_source"), p.get("thread_source") or "user")
                session.cwd = p.get("cwd", "")
                session.started = p.get("timestamp", "")
            elif t == "turn_context" and not session.model:
                session.model = p.get("model", "")
            elif t == "event_msg" and p.get("type") == "thread_settings_applied":
                session.model = (p.get("thread_settings") or {}).get("model", session.model)
            elif t == "event_msg" and p.get("type") == "task_complete" and cur is not None:
                cur["turn"].closed = True
            elif t == "response_item" and p.get("type") == "message":
                role = p.get("role")
                if role == "user":
                    text = _user_text(p.get("content"))
                    if not text:
                        continue
                    if cur is not None:
                        cur["turn"].closed = True
                        close()
                    cur = {"turn": Turn(user=text, started=o.get("timestamp", ""),
                                        address={"line": [lineno, lineno]}), "_final": []}
                elif role == "assistant" and cur is not None:
                    text = _out_text(p.get("content"))
                    if text:
                        target = cur["_final"] if p.get("phase") == "final_answer" else cur["turn"].commentary
                        target.append(text)
            elif t == "response_item" and cur is not None and p.get("type") in (
                    "function_call", "custom_tool_call"):
                cur["turn"].tools.append(p.get("name", "?"))
            if cur is not None:
                cur["turn"].address["line"][1] = lineno
    close()
    return session, turns
