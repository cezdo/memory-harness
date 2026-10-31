"""开关记录：钩子和 MCP 工具在数据目录写的几份 jsonl，作为一个采集来源收成 turn（2026-10-17，“我”提出，用户同意；
2026-10-30 起联想开关、yorimichi 也收进来，runbook N3）。这几种表达在宿主的会话文件里没有原话（钩子拦下了命令，
工具调用只剩参数），记录是唯一的原始记录：

- observe-switch.jsonl：用户的 /no-observe、/do-observe（observe/switch/hook.py 写）；
- souhatsu-switch.jsonl：用户的 /no-souhatsu、/do-souhatsu（synapse/push/switch 写）；
- yorimichi.jsonl：“我”调 yorimichi 工具记下的条数和理由（synapse/push/yorimichi 写）。

收法：
- 每个宿主会话、每一种记录收成一个会话：来源 switch，会话编号“宿主:宿主的会话编号”（钩子没拿到编号就用会话文件），
  联想开关、yorimichi 在后面加 #souhatsu、#yorimichi；parent 是宿主的会话编号；
- 每条记录一轮：原话是那条命令（yorimichi 写成“yorimichi 2：理由”），没有回复，已结束；原文地址是记录文件的行号和
  宿主的会话文件（observe 用它对上库里的会话）；
- 谁做的记在 turn 的 origin（origin()）：用户的命令是 human、宿主类型是命令；yorimichi 是 main（主会话，“我”）。
  没有回复的轮次 observe 不读（no_answer）；开关是有意的表达，不等 FRESH。
判状态的读法各归各的模块：observe 开关只看库（observe/switch），联想每轮要最新的，直接读记录（和库里是同一份）。
"""
import json
from pathlib import Path

from ...store import HOME, NA
from .turn import Session, Turn

LOG = HOME / "observe-switch.jsonl"
SOUHATSU = HOME / "souhatsu-switch.jsonl"
YORIMICHI = HOME / "yorimichi.jsonl"
SOURCE = "switch"
TITLES = {"observe": "observe 开关", "souhatsu": "联想开关", "yorimichi": "yorimichi"}


def files():
    return [p for p in (LOG, SOUHATSU, YORIMICHI) if p.exists()]


def kind(path):
    """记录文件 → 哪一种：observe | souhatsu | yorimichi（按文件对上，认不出的当 observe 开关）。"""
    path = Path(path)
    return "souhatsu" if path == SOUHATSU else "yorimichi" if path == YORIMICHI else "observe"


def origin(session):
    """开关记录的一轮是谁做的 → (origin, host_kind)：yorimichi 是主会话，其余是用户的命令。"""
    return ("main", NA) if session.session.endswith("#yorimichi") else ("human", "command")


def _said(k, e):
    """一条记录的原话；认不出的返回 None。"""
    if k == "yorimichi":
        w = e.get("width")
        if not isinstance(w, int) or isinstance(w, bool):
            return None
        return f"yorimichi {w}" + (f"：{e['why']}" if e.get("why") else "")
    return e["prompt"] if e.get("state") in ("on", "off") and e.get("prompt") else None


def parse_all(path):
    """一个记录文件 → [(会话, 轮次)]，按每个宿主会话第一条记录的先后排。认不出的行跳过。"""
    k, by = kind(path), {}
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            said = _said(k, e) if isinstance(e, dict) else None
            if not said:
                continue
            host, sid, file = e.get("source") or "", e.get("session") or "", e.get("file") or ""
            key = f"{host}:{sid or file}" + ("" if k == "observe" else f"#{k}")
            if key not in by:
                by[key] = (Session(SOURCE, key, str(path), kind="user", parent=sid, title=TITLES[k], started=e.get("time") or ""), [])
            by[key][1].append(Turn(user=said, started=e.get("time") or "", closed=True,
                                   address={"line": n, "host": host, "file": file}))
    return list(by.values())
