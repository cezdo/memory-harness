"""push：手记推送（常态记忆）。重构 E1a（2026-10-19）起从旧的 mh/synapse/__init__.py 搬来常态记忆的编译和工作区判断，
地图入口换成新库的话题线（见 entry），推送的足迹记成新库的 use。联想（associate.py）还在旧库上，见 runbook E1a。
"""
import re
from pathlib import Path

from ...engram.store.instance import resource, unplugged

def workspace(path):
    """工作区的键（2026-11-19 与 store.workspace 统一）：不再另走一套映射或兜底到 home。"""
    from ...engram.store import workspace as ws_of
    return ws_of(path)


def _core():
    """内核的源文件：实例配置的 [resources].core（旧名 standing）；开发仓库是 design/engram/core.md（2026-11-13 从 design/synapse/ 搬来：正本归 Engram）；发行包读实例配置旁的
    core.md，没有就读旧的 standing.md（2026-10-24 改名前的叫法）。"""
    from ...engram.store import instance
    configured = instance._D.get("resources", {})
    if "core" not in configured and "standing" in configured:
        return resource("standing", Path(), "standing.md")
    got = resource("core", Path(__file__).resolve().parents[3] / "design/engram/core.md", "core.md")
    old = instance.PATH.parent / "standing.md"
    return old if not got.exists() and old.exists() else got


CORE = _core()
STANDING = CORE          # 旧名：内核态（elevate）和测试还在用


def _text(path):
    """内核的文本（design/engram/core.md，2026-10-24 前叫常态记忆）：源文件就是推送版；以前编译留下的第一行指纹照样跳过。"""
    lines = path.read_text().splitlines() if path.exists() else []
    return "\n".join(l for l in lines if not l.startswith(("<!--", ">"))).strip()     # 开头的引用说明是给维护的人看的，不推


MEMORY_SECTION = "## 记忆与认知"
TITLE = "# 手记"          # 整份推送（用户 2026-10-25：不用“开场”“开机”这类会话开关的说法，目标是无感切换会话）
FLYLEAF = "## 扉页"       # core.md：写在扉页上给自己的话，几乎不改；它的二级标题推出去降一级，和近况、环境分开


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
    """内核（core.md），memory（工具）接在「记忆与认知」末尾。哪个工作区都是同一份；cwd 留着给调用方定工作区。"""
    head, secs = _sections(_text(CORE))
    if memory:
        if MEMORY_SECTION not in [x[0] for x in secs]:
            secs.append([MEMORY_SECTION, []])
        secs[[x[0] for x in secs].index(MEMORY_SECTION)][1] += ["", memory]
    down = lambda s: re.sub(r"^(#+ )", r"#\1", s, flags=re.M)       # 扉页里的标题都降一级
    parts = [TITLE, FLYLEAF, "\n".join(head).strip()] + [down(h + "\n\n" + "\n".join(lines).strip()) for h, lines in secs]
    return "\n\n".join(x for x in parts if x)


HOST_NAME = {"claude": "Claude Code", "codex": "codex", "dsh": "dsh", "agy": "agy"}


def environment(host, start="", model="", now=None):
    """环境段（放最后，2026-10-24）：现在几点、哪个宿主、哪个模型、怎么启动的（宿主给的原词：startup、resume、clear、compact）。
    内容只用 ASCII，标题和全篇一样用中文（用户 2026-10-24）。宿主没给的就不写。"""
    from datetime import datetime
    t = (now or datetime.now()).astimezone()
    bits = [f"{t:%Y-%m-%d %H:%M} {'Mon Tue Wed Thu Fri Sat Sun'.split()[t.weekday()]}", HOST_NAME.get(host, host)]
    bits += [x for x in (model, start) if x and x.isascii()]
    return "## 环境\n\n" + " | ".join(bits)


# ---- 新库上的手记推送（E1a）----

TOOL = "mcp__mh__"       # 宿主给 MCP 工具起的名字：mcp__<服务名>__<工具名>（agy 是单下划线 mcp_mh_）


def entry(L, cwd, host="claude"):
    """工具怎么用（2026-10-24：常用入口并进“最近停下的几件事”，每条带句柄；2026-10-28 改成介绍的口吻，和内核“记忆与认知”一致，加上 yorimichi）。L、cwd 留着给调用方。"""
    from ...engram.store import workspace as ws_of
    here = ws_of(cwd)
    tool = "mcp_mh_" if host == "agy" else TOOL
    return "\n".join(["### 工具",
                      f"- `{tool}evoke`（words）：按词回忆，几个词一起给，带上换个说法的词；",
                      f"- `{tool}relive`（handles）：按句柄读下去，需要时一直走到原话；",
                      f"- `{tool}roam`（node）：不知道用什么词的时候，在地图上一层层走（这个工作区全部的线：`roam {here}`）；",
                      f"- `{tool}yorimichi`（width）：告诉记忆现在给联想留几条；",
                      "- 句柄：线 L3、话题 L3:T5、陈述 L3:T5:S1（没挂上线写 L?）、骨架 k-、E、J、认知 C8（也认名字）。括号里是主参数，其余参数见各工具的说明。"])


def render(cwd, host="claude", session="", start="", model="", conn=None):
    """手记的正文（还没包 <memory-harness>）：内核 + 工具 → 项目现状 → 环境，稳定的在前、易变的在后。
    钩子和 `mh boot` 共用。项目现状是整理循环编好的，宿主自己给了的节去掉；没有手写近况源的工作区也送达环境级反射和未落库栞。"""
    from . import status
    text = standing(cwd, entry(None, cwd, host))
    ws = workspace(cwd)
    now = status.read(ws, host=host, conn=conn) or status.fallback(ws, conn=conn)
    if now:
        try:
            from . import recent
            now = recent.drop_self(now, host, session)      # 恢复的会话不列自己
        except Exception:
            pass
        text += "\n\n" + now
    return text + "\n\n" + environment(host, start, model)


def hook(conn, payload):
    """手记推送（Claude Code SessionStart、codex start、dsh 的 agent/created、agy start 用同一个入口）：内核 + 工具 + 项目现状 + 环境，
    稳定的在前、易变的在后；记一条 use（push / assist），再写进推送流水（pushes.py）。返回宿主要的 JSON。"""
    from .. import track
    from ..track import pushes
    from ..pull import VERSION
    host, session, cwd = payload.get("host") or "claude", payload.get("session_id", ""), payload.get("cwd", "")
    if unplugged(cwd):                       # 不接 mh 的目录（[connect].unplugged）：不推、不记
        return {}
    if host == "claude":
        try:                                 # /clear 以后 MCP 按这个认会话；出错不影响推送
            from ..hosts import claude
            claude.bind(session)
        except Exception:
            pass
    model = payload.get("model")
    text = render(cwd, host, session, str(payload.get("source") or ""), model if isinstance(model, str) else "", conn=conn)
    pairs = re.findall(r"(?<![A-Za-z0-9_:])L(\d+|\?):T(\d+)(?![0-9])", text.replace(entry(None, cwd, host), ""))  # 写出的话题和线算送达，工具节的示例不算
    got = {"topic_line": sorted({int(x) for x, _ in pairs if x != "?"}), "session_entry": sorted({int(t) for _, t in pairs})}
    text = track.deliver(conn, VERSION, "assist", {"push": "standing"}, text,
              {k: v for k, v in got.items() if v}, session=session, purpose="interactive",
              cwd=cwd, channel="push", meta={"host": host})
    try:                 # 流水出了任何错都不影响推送（pushes.py 的纪律）
        pushes.note("assist", host, session, cwd, True, summary=f"手记推送：{len(text)} 字",
                    version=VERSION, chars=len(text), workspace=workspace(cwd))
    except Exception:
        pass
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
