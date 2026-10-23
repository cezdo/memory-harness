"""dsh 会话（~/.dsh/sessions/<workspace>/<session>/session*.jsonl.zstd）。"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .turn import Session, Turn

ROOT = Path.home() / ".dsh/sessions"
_RANK = {"session.v4.jsonl.zstd": 4, "session.v3.jsonl.zstd": 3, "session.jsonl.zstd": 1}
SWITCHES = ("no-observe", "do-observe")


def files():
    """每个会话目录只取最高版本的日志；旧版本是迁移前的副本。"""
    for d in sorted(p for p in ROOT.glob("*/*") if p.is_dir()):
        logs = [p for p in d.iterdir() if p.name in _RANK]
        if logs:
            yield max(logs, key=lambda p: _RANK[p.name])


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else ""


def _events(path):
    # 正在写入的文件尾帧可能不完整：zstd 返回非零，但已解出的完整行照常使用
    out = subprocess.run(["zstd", "-d", "-c", "--", str(path)], capture_output=True).stdout
    for line in out.decode("utf-8", errors="replace").splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _texts(content, kind):
    return [b["text"] for b in content or []
            if isinstance(b, dict) and b.get("type") == kind and isinstance(b.get("text"), str) and b["text"].strip()]


def parse(path):
    session = Session("dsh", path.parent.name, str(path))
    turns, cur, texts, waiting = [], None, [], []

    def close(closed):
        if cur is None:
            return
        cur.closed = closed
        if texts:
            cur.assistant = texts[-1]
            cur.commentary = texts[:-1]
        turns.append(cur)
        turns.extend(waiting)
        waiting.clear()

    for e in _events(path):
        t, d = e.get("type"), e.get("data") or {}
        seq = e.get("seq")
        if t == "session":
            session.session = e.get("id") or session.session
            session.cwd = e.get("cwd", "")
            session.started = _iso(e.get("createdAt"))
        elif t == "subagent/descriptor":
            session.kind = "subagent"
            session.model = d.get("agentModel", session.model)
        elif t in ("request/context", "model/selection") and not session.model:
            session.model = d.get("model", "")
        elif t == "session/title":
            session.title = d.get("title", session.title)
        elif t == "command/run" and d.get("name") in SWITCHES and not (d.get("args") or "").strip():
            # observe 开关（mh/engram/observe/switch/）：不经过模型，记成原话为 /no-observe、没有回复的一轮；
            # 带参数的插件会报用法错误，不算；正在进行的一轮先收完再记
            switch = Turn(user="/" + d["name"], started=_iso(e.get("time")), address={"seq": [seq, seq]}, closed=True)
            if cur is None:
                turns.append(switch)
            else:
                waiting.append(switch)
            continue
        elif t == "user/message" and (d.get("source") or {}).get("kind") == "user":
            text = "\n".join(_texts(d.get("content"), "text")).strip()
            if not text:
                continue
            if cur is not None and not texts:
                cur.user += "\n" + text      # 同一轮里排队的多条用户输入
                continue
            close(False)
            cur, texts = Turn(user=text, started=_iso(e.get("time")), address={"seq": [seq, seq]}), []
        elif t == "assistant/message" and cur is not None:
            msg = d.get("message") or {}
            text = "\n".join(_texts(msg.get("content"), "text")).strip()
            if text:
                texts.append(text)
            cur.reasoning += _texts(msg.get("content"), "reasoning")
            cur.tools += [b.get("name", "?") for b in msg.get("content") or []
                          if isinstance(b, dict) and b.get("type") == "tool-call"]
        elif t == "turn/end" and cur is not None:
            cur.address["seq"][1] = seq
            # 中止、出错也写 turn/end：照样算结束（不必等下一次输入），但记下怎么结束的，
            # 否则最后一句过程话会被当成最终回答（2026-10-11，e7c5f17c 第 8 轮 goal 跑了 9.5 小时被用户中止）
            r = d.get("reason") or {}
            if r.get("kind") == "aborted":
                cur.ending = "aborted:" + ((r.get("reason") or {}).get("kind") or "")
            elif r.get("kind") == "error":
                cur.ending = "error:" + ((r.get("error") or {}).get("message") or "")[:200]
            elif r.get("kind") not in (None, "completed"):
                cur.ending = r["kind"]
            close(True)
            cur, texts = None, []
            continue
        if cur is not None and seq is not None:
            cur.address["seq"][1] = seq
    close(False)
    turns.extend(waiting)
    return session, turns
