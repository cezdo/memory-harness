"""shiori / 栞（用户 2026-11-13 定名；词典“shiori / 栞；夹栞”）：“我”在会话里夹的一枚栞。

- 标记就是调用本身：MCP 工具 `shiori` 的调用留在宿主的会话日志里（正本），采集照常把工具名收进 turn_text.tools；
  不另写库、不另写文件（11-12 内存形态：没有可供便签入住的中间存储层）。
- observe 的消费：预览把夹了栞的那一轮所在的组程序定成精读（preview.parse），不封窗、不插队，会话告一段落时照常预览。
- 未落库（pending）：夹了栞、还没产出陈述的轮次，供手记近况派生“未落库”一节（Synapse push 的 status）；
  出了陈述就自然不列，没有要清的东西。理由（why）从会话日志那几行里取（派生，不存）。
"""
import json
import re
from pathlib import Path

NAME = "shiori"
WHY = re.compile(r'"why"\s*:\s*("(?:[^"\\]|\\.)*")')


def marked(tools):
    """工具名列表（或它的 JSON）里有没有夹栞：Claude、dsh 写 mcp__mh__shiori，codex 采集时补成同样的名字。"""
    if isinstance(tools, str):
        try:
            tools = json.loads(tools)
        except ValueError:
            return False
    return any(t == NAME or str(t).endswith("__" + NAME) for t in tools or [])


def why(file, address):
    """从会话日志里这一轮的那几行取夹栞时写的理由；按行号定位的来源才取得到（Claude、codex），取不到返回空。"""
    lines = (address or {}).get("line")
    if not file or not lines:
        return ""
    out = []
    try:
        with open(file, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, 1):
                if i > lines[1]:
                    break
                if i >= lines[0] and NAME in line:
                    out += [json.loads(m) for m in WHY.findall(line) if _ok(m)]     # Claude 的 input、codex 的 arguments 都是对象，原样可读
    except OSError:
        return ""
    return next((w for w in out if w.strip()), "")


def _ok(s):
    try:
        json.loads(s)
        return True
    except ValueError:
        return False


def pending(conn, workspace=None):
    """夹了栞、还没产出陈述的轮次 → [{source, session, ordinal, started, user, why, file}]，按时间先后。
    workspace 给了就只看这个工作区的会话。关掉观察的会话不列（它们不会落库，列了也清不掉）。"""
    from ..store import workspace as ws_of
    from . import switch, unwatched
    rows = conn.execute(
        "SELECT d.occurrence, d.source, d.session, d.ordinal, d.address, o.started, x.user, x.tools, s.cwd, s.file "
        "FROM turn_text x JOIN turn_detail d ON d.text=x.id JOIN occurrence o ON o.id=d.occurrence "
        "JOIN session_detail s ON s.source=d.source AND s.session=d.session "
        "WHERE x.tools LIKE ? ORDER BY o.started", (f"%{NAME}%",)).fetchall()
    rows = [r for r in rows if marked(r["tools"]) and (workspace is None or ws_of(r["cwd"]) == workspace)]
    if not rows:
        return []
    states = switch.states(conn)
    off = {k for k, v in states.items() if v == "off"} | unwatched(conn, states)
    rows = [r for r in rows if (r["source"], r["session"]) not in off]
    turns = [r["occurrence"] for r in rows]
    events = {}
    for eid, t in conn.execute(f"SELECT id, turn FROM event WHERE turn IN ({','.join('?' * len(turns))})", turns):
        events[eid] = t
    landed = set()
    if events:
        for (eid,) in conn.execute(
                f"SELECT DISTINCT j.value FROM form_record f, json_each(f.refs, '$.event') j "
                f"WHERE j.value IN ({','.join('?' * len(events))})", list(events)):
            landed.add(events[eid])
    out = []
    for r in rows:
        if r["occurrence"] in landed:
            continue
        address = json.loads(r["address"]) if r["address"] else {}
        out.append({"source": r["source"], "session": r["session"], "ordinal": r["ordinal"], "started": r["started"],
                    "user": r["user"], "why": why(r["file"], address), "file": r["file"]})
    return out
