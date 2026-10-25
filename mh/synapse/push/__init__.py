"""push：手记推送（常态记忆）。重构 E1a（2026-10-19）起从旧的 mh/synapse/__init__.py 搬来常态记忆的编译和工作区判断，
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


def _core():
    """内核的源文件：实例配置的 [resources].core（旧名 standing）；开发仓库是 design/synapse/core.md；发行包读实例配置旁的
    core.md，没有就读旧的 standing.md（2026-10-24 改名前的叫法）。"""
    from ...engram.store import instance
    configured = instance._D.get("resources", {})
    if "core" not in configured and "standing" in configured:
        return resource("standing", Path(), "standing.md")
    got = resource("core", Path(__file__).resolve().parents[3] / "design/synapse/core.md", "core.md")
    old = instance.PATH.parent / "standing.md"
    return old if not got.exists() and old.exists() else got


CORE = _core()
STANDING = CORE          # 旧名：内核态（elevate）和测试还在用


def _text(path):
    """内核的文本（design/synapse/core.md，2026-10-24 前叫常态记忆）：源文件就是推送版；以前编译留下的第一行指纹照样跳过。"""
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


HOST_NAME = {"claude": "Claude Code", "codex": "codex", "dsh": "dsh"}


def environment(host, start="", model="", now=None):
    """环境段（放最后，2026-10-24）：现在几点、哪个宿主、哪个模型、怎么启动的（宿主给的原词：startup、resume、clear、compact）。
    内容只用 ASCII，标题和全篇一样用中文（用户 2026-10-24）。宿主没给的就不写。"""
    from datetime import datetime
    t = (now or datetime.now()).astimezone()
    bits = [f"{t:%Y-%m-%d %H:%M} {'Mon Tue Wed Thu Fri Sat Sun'.split()[t.weekday()]}", HOST_NAME.get(host, host)]
    bits += [x for x in (model, start) if x and x.isascii()]
    return "## 环境\n\n" + " | ".join(bits)


# ---- 新库上的手记推送（E1a）----

TOOL = "mcp__mh__"       # 宿主给 MCP 工具起的名字：mcp__<服务名>__<工具名>


def entry(L, cwd):
    """工具怎么用（2026-10-24：常用入口并进“最近停下的几件事”，每条带句柄；这里只留 roam 的提示）。L、cwd 留着给调用方。"""
    from ...engram.store import workspace as ws_of
    here = ws_of(cwd)
    return "\n".join(["### 工具",
                      f"- `{TOOL}evoke` 按词找（几个词一起给，带上换个说法的词），`{TOOL}relive` 按句柄读（必要时走到原话），"
                      f"`{TOOL}roam` 走地图（不知道用什么词时；这个工作区全部的线：roam {here}）；参数见工具说明。",
                      "- 句柄：线 L3、话题 L3:T5、陈述 L3:T5:S1（没挂上线写 L?）、骨架 k-、E、J。"])


def render(cwd, host="claude", session="", start="", model=""):
    """手记的正文（还没包 <memory-harness>）：内核 + 工具 → 项目现状 → 环境，稳定的在前、易变的在后。
    钩子和 `mh boot` 共用。项目现状是整理循环编好的，宿主自己给了的节去掉；读不到就不给（宿主不能报错）。"""
    from . import status
    text = standing(cwd, entry(None, cwd))
    now = status.read(workspace(cwd), host=host)
    if now:
        try:
            from . import recent
            now = recent.drop_self(now, host, session)      # 恢复的会话不列自己
        except Exception:
            pass
        text += "\n\n" + now
    return text + "\n\n" + environment(host, start, model)


def hook(conn, payload):
    """手记推送（Claude Code SessionStart、codex start、dsh 的 agent/created 用同一个入口）：内核 + 工具 + 项目现状 + 环境，
    稳定的在前、易变的在后；记一条 use（push / assist），再写进推送流水（pushes.py）。返回宿主要的 JSON。"""
    from .. import track
    from ..track import pushes
    from ..pull import VERSION
    host, session, cwd = payload.get("host") or "claude", payload.get("session_id", ""), payload.get("cwd", "")
    model = payload.get("model")
    text = render(cwd, host, session, str(payload.get("source") or ""), model if isinstance(model, str) else "")
    lines = sorted(set(re.findall(r"\bL(\d+):T\d+", text)))
    text = track.deliver(conn, VERSION, "assist", {"push": "standing"}, text,
              {"topic_line": [int(x) for x in lines]}, session=session, purpose="interactive",
              cwd=cwd, channel="push", meta={"host": host})
    try:                 # 流水出了任何错都不影响推送（pushes.py 的纪律）
        pushes.note("assist", host, session, cwd, True, summary=f"手记推送：{len(text)} 字",
                    version=VERSION, chars=len(text), workspace=workspace(cwd))
    except Exception:
        pass
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
