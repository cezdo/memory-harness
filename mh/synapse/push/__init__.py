"""push：开场推送（常态记忆）。重构 E1a（2026-10-19）起从旧的 mh/synapse/__init__.py 搬来常态记忆的编译和工作区判断，
地图入口换成新库的话题线（见 entry），推送的足迹记成新库的 use。联想（associate.py）还在旧库上，见 runbook E1a。
"""
import re
from pathlib import Path

from ...engram.store.instance import PROJECTS, SYNAPSE_DIRS as WORKSPACES    # 改名、删除、别名的目录，放项目的目录：实例配置（重构 H）
from ...engram.store.instance import resource

HOME = Path.home()


def workspace(path):
    """工作区的键。先查 WORKSPACES；否则取所在 git 仓库根目录的名字（跳过家目录本身的仓库）；
    再否则取放项目的目录（PROJECTS）下的第一层目录；都不是就归 home。纯程序判断，目录不必仍然存在。"""
    p = Path(path or "")
    for d in sorted(WORKSPACES, key=lambda d: -len(d.parts)):
        if p == d or d in p.parents:
            return WORKSPACES[d]
    for d in [p, *p.parents]:
        if d != HOME and HOME in d.parents and (d / ".git").exists():
            return d.name
    if PROJECTS and PROJECTS in p.parents:
        return p.relative_to(PROJECTS).parts[0]
    return "home"


STANDING = resource("standing", Path(__file__).resolve().parents[3] / "design/synapse/常态记忆.md", "standing.md")


def _text(path):
    """常态记忆的文本（design/synapse/常态记忆.md）。2026-10-20 起它就是源文件，不再从 design/基准.md 编译（用户：基准只用于推送，
    和 AGENTS.md 重复；自我的部分留在这里，执行的部分进 AGENTS.md，项目的部分进项目现状）；以前编译留下的第一行指纹照样跳过。"""
    lines = path.read_text().splitlines() if path.exists() else []
    return "\n".join(l for l in lines if not l.startswith(("<!--", ">"))).strip()     # 开头的引用说明是给维护的人看的，不推


MEMORY_SECTION = "## 记忆与认知"


def _sections(text):
    """按二级标题切开 → (标题前的部分, [[二级标题, 内容行]])，内容行去掉空行。"""
    head, secs = [], []
    for line in text.splitlines():
        if line.startswith("## "):
            secs.append([line, []])
        elif secs:
            if line.strip():
                secs[-1][1].append(line)
        elif not line.startswith("# "):
            head.append(line)
    return head, secs


def standing(cwd="", memory=""):
    """常态记忆：design/synapse/常态记忆.md，memory（工具和常用入口）接在「记忆与认知」末尾。哪个工作区都是同一份
    （2026-10-20 起各工作区自己的那份撤掉：执行的部分进各仓库的 AGENTS.md，项目的部分进项目现状）；cwd 留着给调用方定工作区。"""
    head, secs = _sections(_text(STANDING))
    if memory:
        if MEMORY_SECTION not in [x[0] for x in secs]:
            secs.append([MEMORY_SECTION, []])
        secs[[x[0] for x in secs].index(MEMORY_SECTION)][1] += ["", memory]
    parts = ["# 常态记忆", "\n".join(head).strip()] + [h + "\n\n" + "\n".join(lines).strip() for h, lines in secs]
    return "\n\n".join(x for x in parts if x)


# ---- 新库上的开场推送（E1a）----

TOOL = "mcp__mh__"       # 宿主给 MCP 工具起的名字：mcp__<服务名>__<工具名>
ENTRY_LINES = 8          # 常用入口列本工作区最近的几条话题线


def entry(L, cwd):
    """工具怎么用，加上常用入口：本工作区最近有会话的话题线（新的在前）。工作区里还没有线就只给工具。"""
    from ...engram.store import workspace as ws_of
    here = ws_of(cwd)
    how = [f"`{TOOL}evoke` 按词找，`{TOOL}relive` 按句柄读，`{TOOL}roam` 走地图（参数见工具说明）。"
           f"句柄：线 L3、话题 L3:T5、陈述 L3:T5:S1（没挂上线写 L?）、骨架 k-、E、J。"]
    from ..pull import _latest, _node_line
    lines = sorted((nid for nid, n in L.nodes.items() if n["kind"] == "line" and n["ws"] == here),
                   key=lambda k: -_latest(L, k))[:ENTRY_LINES]
    common = [f"- {_node_line(L, k)}" for k in lines]
    return "\n".join(["### 工具", *how]
                     + (["", f"### 常用入口（{here} 最近的话题线；全部：roam {here}）", *common] if common else []))


def hook(conn, payload):
    """开场推送（Claude Code SessionStart、codex start、dsh 的 agent/created 用同一个入口）：常态记忆 + 工具 + 常用入口；
    记一条 use（push / assist），再写进推送流水（pushes.py）。返回宿主要的 JSON。"""
    from .. import track
    from ..track import pushes
    from ..pull import VERSION, layers
    host, session, cwd = payload.get("host") or "claude", payload.get("session_id", ""), payload.get("cwd", "")
    L = layers(conn)
    text = standing(cwd, entry(L, cwd))
    from . import status
    now = status.read(workspace(cwd), host=host)   # 项目现状：整理循环编好的，宿主自己给了的节去掉；没有就不给（读不到不影响推送）
    if now:
        text += "\n\n" + now
    lines = re.findall(r"^- L(\d+) ", text, re.M)
    text = track.deliver(conn, VERSION, "assist", {"push": "standing"}, text,
              {"topic_line": [int(x) for x in lines]}, session=session, purpose="interactive",
              cwd=cwd, channel="push", meta={"host": host})
    try:                 # 流水出了任何错都不影响推送（pushes.py 的纪律）
        pushes.note("assist", host, session, cwd, True, summary=f"开场推送：常态记忆 {len(text)} 字（新库）",
                    version=VERSION, chars=len(text), workspace=workspace(cwd))
    except Exception:
        pass
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
