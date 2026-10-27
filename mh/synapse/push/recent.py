"""手记推送的“最近停下的几件事”（2026-10-24，runbook I6）：某个工作区最近停下的几段会话，每段一行，纯程序、不调模型。
交互会话不猜接哪件事（对用户是一句话），这里只列出来，用户点名了再载入。按会话列，宿主 fork、恢复出来的几段会话
共用开头那一轮（轮次的内容哈希相同），并成一条、留最近的。整理循环随项目现状一起编（status.compile），手记推送读编好的。
最后提交借 observe 的 written()（读宿主原始记录和 git），是 observe 给 Synapse 的接口函数。
"""
import re
from datetime import datetime

from . import workspace

N = 3                  # 列几条
SAID = 60              # “你最后说”最多几个字
HOST = {"claude": "Claude Code", "codex": "codex", "dsh": "dsh"}


def _t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if (s or "")[:1].isdigit() else None


def _one(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "……"


def sessions(conn, ws, foreign=False):
    """这个工作区的用户会话：[{source, session, file, cwd, title, turns: [{origin, started, text, user}]}]，最近停下的在前。
    foreign：反过来取在别的工作区开的会话（是否碰过这个工作区由 crossed() 判断）。"""
    out = []
    for s in conn.execute("SELECT source, session, file, cwd, title FROM session_detail "
                          "WHERE kind='user' AND source IN ('claude','codex','dsh')"):
        if (workspace(s[3]) == ws) == foreign:
            continue
        turns = [dict(zip(("origin", "started", "text", "user"), r)) for r in conn.execute(
            """SELECT d.origin, o.started, d.text, x.user FROM turn_detail d JOIN occurrence o ON o.id=d.occurrence
               JOIN turn_text x ON x.id=d.text WHERE d.source=? AND d.session=? ORDER BY d.ordinal""", (s[0], s[1]))]
        human = [t for t in turns if t["origin"] == "human" and _t(t["started"])]
        if not human:
            continue
        out.append({"source": s[0], "session": s[1], "file": s[2], "cwd": s[3], "title": s[4], "turns": turns,
                    "human": human, "last": max(_t(t["started"]) for t in turns if _t(t["started"]))})
    return sorted(out, key=lambda x: x["last"], reverse=True)


def crossed(x, ws):
    """在别的工作区开的会话，工具调用里碰过这个工作区的路径（不分读写）。"""
    try:
        from ...engram.observe.written_files import touched
        return any(workspace(p) == ws for p in touched(x["source"], x["file"], x.get("cwd")))
    except Exception:
        return False


def candidates(conn, ws, n=N):
    """本工作区的会话，加上比第 n+1 条还近、碰过这个工作区的别处会话（2026-10-26：在别的项目的目录开、改本仓的会话
    只列在那个项目那边）。只查够近的别处会话，免得每次把全部原始记录读一遍。"""
    own = dedupe(sessions(conn, ws))
    edge = own[n]["last"] if len(own) > n else None
    far = [x for x in sessions(conn, ws, foreign=True) if (edge is None or x["last"] > edge) and crossed(x, ws)]
    return dedupe(sorted(own + far, key=lambda x: x["last"], reverse=True))


DAYS = 3               # 没提交的改动往回找几天的会话
TOUCHED = "碰过"


def active(conn, since):
    """since（UTC ISO）以后有过轮次的用户会话，不分工作区：[{source, session, file, cwd, title, last}]。"""
    rows = conn.execute("""SELECT s.source, s.session, s.file, s.cwd, s.title, MAX(o.started) FROM session_detail s
                           JOIN turn_detail d ON d.source=s.source AND d.session=s.session JOIN occurrence o ON o.id=d.occurrence
                           WHERE s.kind='user' AND s.source IN ('claude','codex','dsh') GROUP BY s.source, s.session
                           HAVING MAX(o.started) >= ?""", (since,)).fetchall()
    return [dict(zip(("source", "session", "file", "cwd", "title"), r[:5]), last=_t(r[5])) for r in rows]


def dirty_sources(conn, repo, files, ws, days=DAYS):
    """没提交的改动是哪段会话碰的（2026-10-26：别的项目的目录开的会话改了 runbook 没提交，开工时没人认领）。每个文件归给改动时刻
    （mtime）之前最后碰过它的会话（工具调用里出现过它或它所在的目录，不分读写）；按会话归组 → 一行一行的说明。"""
    from ...engram.observe.written_files import touches, _t as iso
    since = iso((datetime.now().timestamp() - days * 86400) * 1000)
    seen = [(x, touches(x["source"], x["file"], x["cwd"])) for x in active(conn, since)]
    by, lost = {}, []
    for f in files:
        p = repo / f.rstrip("/")
        try:
            at = iso((p.stat().st_mtime + 60) * 1000)
        except OSError:
            at = iso(datetime.now().timestamp() * 1000)         # 删掉的文件：不限时刻
        best = None
        for near in (lambda q: q == str(p) or q.startswith(str(p) + "/"),                 # 碰过这个文件（或这个新目录里的）
                     lambda q: str(p).startswith(q.rstrip("/") + "/") and q.rstrip("/") != str(repo)):   # 只碰过所在的目录
            for x, t in seen:
                hit = [ts for q, ts in t.items() if ts <= at and near(q)]
                if hit and (best is None or max(hit) > best[1]):
                    best = (x, max(hit))
            if best:
                break
        if best:
            k = (best[0]["source"], best[0]["session"])
            by.setdefault(k, [best[0], best[1], []])[2].append(f)
            by[k][1] = max(by[k][1], best[1])
        else:
            lost.append(f)
    out = []
    for x, ts, fs in sorted(by.values(), key=lambda v: v[1], reverse=True):
        where = workspace(x["cwd"])
        host = HOST[x["source"]] + (f"（在 {where} 开的）" if where != ws else "")
        title = x["title"] if (x["title"] or "?")[:1] != "?" else x["session"][:8]
        when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        out.append(f"- {'、'.join(fs)}：{host}「{title}」（{handle(conn, x)}）{when:%m-%d %H:%M} 最后{TOUCHED}")
    if lost:
        out.append(f"- {'、'.join(lost)}：近 {days} 天的会话都没{TOUCHED}")
    return out


def dedupe(items):
    """共用开头那一轮的几段会话（fork、恢复、重试）只留最近的一段；items 已按最近在前排好。"""
    seen, out = set(), []
    for x in items:
        first = x["human"][0]["text"]
        if first in seen:
            continue
        seen.add(first)
        out.append(x)
    return out


def handle(conn, x):
    """已经形成的给话题句柄（L:T，relive 读得到）；还没形成的给宿主的会话号。"""
    got = []
    for (e,) in conn.execute("SELECT id FROM session_entry WHERE source=? AND session=? ORDER BY id", (x["source"], x["session"])):
        line = conn.execute("SELECT line FROM attachment WHERE entry=? ORDER BY id DESC LIMIT 1", (e,)).fetchone()
        got.append(f"L{line[0]}:T{e}" if line else f"L?:T{e}")
    return "、".join(got) if got else f"{x['source']}:{x['session'][:8]}，还没整理"


def line(x, handle, last_commit=None):
    title = x["title"] if (x["title"] or "?")[:1] != "?" else _one(x["human"][0]["user"], 30)
    where = workspace(x.get("cwd"))
    host = HOST[x["source"]] + (f"（在 {where} 开的）" if x.get("ws") and where != x["ws"] else "")
    head = f"- {x['last'].astimezone():%m-%d %H:%M} · {host} · {title}（{len(x['human'])} 轮，{handle}）"
    tail = [f"最后提交：{_one(last_commit, 50)}"] if last_commit else []
    tail.append(f"你最后说：{_one(x['human'][-1]['user'], SAID)}")
    return head + "\n  - " + "；".join(tail)


def _last_commit(x):
    try:
        from ...engram.observe.written_files import written
        w = written(x["source"], x["file"], x["cwd"], [t["started"] for t in x["turns"]])
    except Exception:
        return None
    got = [l for k in sorted(w) for l in w[k] if l.startswith("提交 ")]
    return got[-1].split("「", 1)[1].split("」", 1)[0] if got else None


TITLE = "### 最近停下的几件事（程序列的；接哪件说一句就行）"


def compile(conn, ws, n=N):
    """→ 这一节的文本（放在项目现状的最后）；没有就 None。多列一条，留给送手记时去掉当前会话自己。
    每条第一行末尾带 <!-- 来源:会话号 -->，送手记时按它认出当前会话。"""
    items = [dict(x, ws=ws) for x in candidates(conn, ws, n)[:n + 1]]
    if not items:
        return None
    return "\n".join([TITLE, *(line(x, handle(conn, x), _last_commit(x)) + f" <!-- {x['source']}:{x['session']} -->"
                                for x in items)])


def drop_self(text, source, session, n=N):
    """送手记时：去掉当前会话自己那一条（恢复的会话），截到 n 条，去掉认会话用的注释。这一节必须在最后。"""
    if TITLE not in text:
        return text
    head, body = text.split(TITLE, 1)
    items = [b for b in body.strip("\n").split("\n- ") if b.strip()]
    items = [b if b.startswith("- ") else "- " + b for b in items]
    me = f"<!-- {source}:{session} -->"
    kept = [b for b in items if not (session and me in b)][:n]
    kept = [re.sub(r" <!-- \S+ -->", "", b) for b in kept]
    return head + TITLE + "\n" + "\n".join(kept)
