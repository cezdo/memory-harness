"""开关记录（数据目录的 observe-switch.jsonl）：Claude Code、codex 的 /no-observe、/do-observe 被 UserPromptSubmit 钩子拦下，
宿主的会话文件里没有这一轮，钩子写的这份记录是用户这次表达唯一的原始记录（switch/hook.py 写）。作为一个采集来源收成 turn
（2026-10-17，“我”提出，用户同意）：

- 每个宿主会话的开关记录收成一个会话：来源 switch，会话编号“宿主:宿主的会话编号”（钩子没拿到编号就用会话文件），
  parent 是宿主的会话编号；
- 每条记录一轮：原话是那条命令，没有回复，已结束；原文地址是记录文件的行号和宿主的会话文件（observe 用它对上库里的会话）；
- 来源是人，宿主类型是命令（collect 按来源定）；开关是有意的命令，不等 FRESH。
"""
import json

from ...store import HOME
from .turn import Session, Turn

LOG = HOME / "observe-switch.jsonl"
SOURCE = "switch"


def files():
    return [LOG] if LOG.exists() else []


def parse_all(path):
    """一个记录文件 → [(会话, 轮次)]，按每个宿主会话第一次开关的先后排。认不出的行跳过。"""
    by = {}
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("state") not in ("on", "off") or not e.get("prompt"):
                continue
            host, sid, file = e.get("source") or "", e.get("session") or "", e.get("file") or ""
            key = f"{host}:{sid or file}"
            if key not in by:
                by[key] = (Session(SOURCE, key, str(path), kind="user", parent=sid, title="observe 开关", started=e.get("time") or ""), [])
            by[key][1].append(Turn(user=e["prompt"], started=e.get("time") or "", closed=True,
                                   address={"line": n, "host": host, "file": file}))
    return list(by.values())
