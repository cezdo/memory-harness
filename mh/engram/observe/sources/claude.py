"""Claude Code 会话（~/.claude/projects/<project>/<session>.jsonl）。

别的会话发来的消息（Claude Code 2.1.29x 起，isMeta、origin.kind = peer，形状见 participants）不是用户的话，
也不成轮（切轮方式不变）：解析时认出来，记在 Turn.asides（谁、正文），由采集存进库，读的时候不回原文件（原文件
不保证一直在）。夹在一轮中间的记在这一轮；落在两轮之间（上一轮已经结束）的记在前一轮，标 after，“我”接着对它的
回答记在 reply（2026-11-01）。
"""
import json
from pathlib import Path

from .turn import Session, Turn
from .. import participants

ROOT = Path.home() / ".claude/projects"


def files():
    yield from sorted(ROOT.glob("*/*.jsonl"))


def _text(o):
    """一条 user 条目的文字；工具结果、旁支不算，返回空串。"""
    if o.get("isSidechain"):
        return ""
    content = (o.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return ""
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return ""
    return content.strip()


def _peer(o):
    """别的会话发来的消息：返回原话，不是返回空串。宿主版本不带 isMeta 的也认，不让它被当成用户的话。"""
    text = _text(o)
    return text if text and ((o.get("origin") or {}).get("kind") == "peer" or participants.is_peer(text)) else ""


def _human_text(o):
    """只取人实际输入的 prompt；工具结果、meta、斜杠命令回显和别的会话发来的消息不算。"""
    return "" if o.get("isMeta") or _peer(o) else _text(o)


def parse(path):
    """一轮从用户的一次输入开始。结束：见到 system/turn_duration，或者这一轮最后一条 assistant 消息的
    stop_reason 是 end_turn（Claude Code 的 VSCode 扩展不写 turn_duration，2026-10-09）。切轮方式不变：
    后台通知、打断标记、命令回显仍各自算一轮，它们是什么由观察时的程序标注。
    回退改写（同一文件里的分支）：序号仍按写入顺序，不拆会话；新输入的 parentUuid 指回的不是上一轮时，
    记下它接在哪一轮之后（Turn.prev），被放弃的那条分支照常观察（2026-10-10）。"""
    session = Session("claude", path.stem, str(path))
    turns, cur, texts, stop = [], None, {}, None
    owner = {}       # 消息 uuid → 它所在的轮次序号（-1 = 第一轮之前）
    pending, replies = None, {}   # 两轮之间别的会话发来的消息（记在前一轮的 asides）和“我”对它的回答（消息 ID → 文字）

    def close(closed):
        if cur is None:
            return
        cur.closed = closed
        ordered = [t for t in texts.values() if t]
        if ordered:
            cur.assistant = ordered[-1]
            cur.commentary = ordered[:-1]
        turns.append(cur)

    with open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if o.get("cwd") and not session.cwd:
                session.cwd = o["cwd"]
                session.started = o.get("timestamp", "")
            if t == "ai-title":
                session.title = o.get("aiTitle", session.title)
            elif t == "user":
                peer = _peer(o)
                if peer and (cur is not None or turns):
                    aside = {"who": participants.label(peer), "text": participants.body(peer)}
                    if cur is not None:
                        cur.asides.append(aside)
                    else:
                        aside.update(after=True, reply="")
                        turns[-1].asides.append(aside)
                        pending, replies = aside, {}
                text = _human_text(o)
                if text:
                    pending = None
                    after = owner.get(o.get("parentUuid") or o.get("logicalParentUuid"))
                    close(stop == "end_turn")
                    stop = None
                    cur, texts = Turn(user=text, started=o.get("timestamp", ""),
                                      address={"line": [lineno, lineno]}), {}
                    if after is not None and after != len(turns) - 1:
                        cur.prev = after
            elif t == "assistant" and cur is None and pending is not None and not o.get("isSidechain"):
                msg = o.get("message") or {}
                for b in msg.get("content") or []:
                    if b.get("type") == "text":
                        replies[msg.get("id")] = (replies.get(msg.get("id"), "") + b.get("text", "")).strip()
                pending["reply"] = "\n\n".join(v for v in replies.values() if v)
            elif t == "assistant" and cur is not None and not o.get("isSidechain"):
                msg = o.get("message") or {}
                session.model = msg.get("model", session.model)
                if msg.get("stop_reason"):
                    stop = msg["stop_reason"]
                # 同一条消息按内容块分多行写入，用消息 ID 拼回
                for b in msg.get("content") or []:
                    if b.get("type") == "text":
                        texts[msg.get("id")] = (texts.get(msg.get("id"), "") + b.get("text", "")).strip()
                    elif b.get("type") == "thinking" and b.get("thinking"):
                        cur.reasoning.append(b["thinking"])
                    elif b.get("type") == "tool_use":
                        cur.tools.append(b.get("name", "?"))
            elif t == "system" and o.get("subtype") == "turn_duration" and cur is not None:
                cur.address["line"][1] = lineno
                owner[o.get("uuid")] = len(turns)
                close(True)
                cur, texts, stop = None, {}, None
                continue
            if o.get("uuid"):
                owner[o["uuid"]] = len(turns) - (cur is None)
            if cur is not None:
                cur.address["line"][1] = lineno
    close(stop == "end_turn")
    return session, turns
