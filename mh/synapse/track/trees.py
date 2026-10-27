"""访问树（design/synapse/设计.md 足迹一节，2026-10-20）：从 use 推出“我”在一段会话里怎样一步步取记忆。只记曝光和路径，不判用没用。

- 一步 = 一条 use（手记推送、送达的联想、查词、get、map）。这一步给看到了什么，按 use 里记下的（inputs 的陈述、E、J，meta 的 handles 的
  话题、线、骨架），不从文本里猜；补进来的旧足迹 inputs 空着的，从 meta 的 results 读。
- 边按连通定（2026-10-20：比“任务”精准）：get、map 要的句柄若是此前某一步给看到过的，就挂到最近给看到它的那一步下面；
  查词、手记推送、联想，和要的句柄此前没人给过的 get（“自带”），各是一个根。一步要了几个句柄、来自几步，就有几个上一步；
  连在一起的步是一份访问记录（连通分量），按第一个上一步排成树，其余的记作交叉边。
- 节点最小到 E，可以往上合并：E → 引它的陈述 → 话题 → 线 → 骨架。
- 只看新版本（2026-10-20：J 本身是扁平的，读 J 的基本没用，只有新版本的层级才可能有用）：换库前补进来的旧足迹不算，
  新足迹里给看到、要了的旧 J 也不进树。
"""
import bisect
import json
import re
from collections import defaultdict

from . import legacy_results

_TOKEN = re.compile(r"(?<![A-Za-z0-9_\-])(?:"
                    r"(?P<s>(?:L(?:\d+|\?):)?T\d+:S\d+(?:-S?\d+)?|L\d+:S\d+(?:-S?\d+)?)"
                    r"|(?:L(?:\d+|\?):)?T(?P<t>\d+)"
                    r"|L(?P<l>\d+)"
                    r"|(?P<k>k-\w[\w\-]*\w)"
                    r"|E(?P<e>\d+)"
                    r"|[JK](?P<j>\d+)"
                    r")(?![A-Za-z0-9_]|:[LTS][\d?])")      # 后面紧跟的冒号只有接着句柄时才算同一个句柄
KIND_ZH = {"assist": "手记推送", "associate": "联想", "search": "查词", "get": "get", "recall": "relive", "map": "地图",
           "evoke": "查词", "relive": "relive", "roam": "地图"}     # v0.13 改名 evoke、relive、roam，旧 mode 照认
PURPOSES = ("interactive", "prefetch")
LEVELS = ("E", "S", "T", "L")               # 往上合并的层级


def tokens(L, text):
    """句柄文字 → [认成的东西]：("S", 陈述) ("T", 话题) ("L", 线) ("k", 骨架) ("E", …) ("J", …)。按 pull 的解析认，对不上的不要。"""
    from ..pull import resolve
    out = []
    for m in _TOKEN.finditer(text or ""):
        h = m.group(0)
        if m.group("s"):
            kind, got = resolve(L, h)
            out += [("S", rid) for _, rid in (got if kind == "stmts" else []) if rid is not None]
        elif m.group("t"):
            if int(m.group("t")) in L.t_session:
                out.append(("T", int(m.group("t"))))
        elif m.group("l"):
            if f"L{m.group('l')}" in L.nodes:
                out.append(("L", int(m.group("l"))))
        elif m.group("k"):
            if h in L.nodes:
                out.append(("k", h))
        elif m.group("e"):
            out.append(("E", int(m.group("e"))))
        elif int(m.group("j")) in L.j:
            out.append(("J", int(m.group("j"))))
    return out


def shows(inputs, meta):
    """一条 use 给看到了什么。"""
    out = {("S", x) for x in inputs.get("form_record", [])} | {("E", x) for x in inputs.get("event", [])} \
        | {("J", x) for x in inputs.get("judgment", [])} | {("J", j) for j in legacy_results(meta.get("results"))}
    hs = meta.get("handles") or {}
    return out | {("T", x) for x in hs.get("session_entry", [])} | {("L", x) for x in hs.get("topic_line", [])} \
        | {("k", x) for x in hs.get("skeleton", [])}


def sessions(conn):
    """有新版本真实取用的会话（会话编号对上采集里唯一一段）→ [(来源, 会话)]，按第一次取用排。"""
    from . import rebound
    moved, first = rebound(conn), {}
    for oid, sid in conn.execute(f"""SELECT d.occurrence, d.session FROM use_detail d
                                     JOIN occurrence o ON o.id=d.occurrence JOIN operator p ON p.id=o.operator
                                     WHERE d.purpose IN ({','.join('?' * len(PURPOSES))}) AND p.name NOT LIKE 'legacy/%'
                                     AND d.channel!='track' ORDER BY d.occurrence""", PURPOSES):
        first.setdefault(moved.get(oid, sid), oid)       # /clear 后记错会话的，按更正算
    out = []
    for sid in sorted(first, key=first.get):
        srcs = {r[0] for r in conn.execute("SELECT source FROM session_detail WHERE session=?", (sid,))}
        if len(srcs) == 1:
            out.append((srcs.pop(), sid))
    return out


def steps(conn, L, source, sid):
    """一段会话里的每一步，按时刻排（backfill 补进来的旧足迹编号更大，不能按编号排）。只记录没送出去的联想不算（“我”没看到）。"""
    starts = [r[0][:19] for r in conn.execute(
        "SELECT o.started FROM turn_detail d JOIN occurrence o ON o.id=d.occurrence WHERE d.source=? AND d.session=? "
        "ORDER BY d.ordinal", (source, sid))]
    from . import by_session
    out = []
    where, args = by_session(conn, sid)
    for oid, started, inputs, mode, query, meta, op in conn.execute(
            f"""SELECT o.id, o.started, o.inputs, d.mode, d.query, d.meta, p.name FROM use_detail d
                JOIN occurrence o ON o.id=d.occurrence JOIN operator p ON p.id=o.operator
                WHERE {where} AND d.purpose IN ({','.join('?' * len(PURPOSES))}) AND d.channel IN ('push','pull')
                ORDER BY substr(o.started, 1, 19), o.id""", (*args, *PURPOSES)):     # 读取与送达构成路径；引用回填和呈现反馈另留记录
        q, m = json.loads(query), json.loads(meta)
        if op.startswith("legacy/"):                       # 换库前补进来的旧足迹：只有旧 J，没有层级
            continue
        st = {"id": oid, "at": started, "turn": bisect.bisect_right(starts, started[:19]) - 1, "mode": mode,
              "kind": KIND_ZH.get(mode, mode), "shows": {c for c in shows(json.loads(inputs), m) if c[0] != "J"},
              "wants": [], "desc": ""}
        if mode == "associate":
            a = q.get("associate") or {}
            if a.get("skip") or a.get("shadow", True):
                continue
        elif mode in ("get", "relive", "recall"):
            g = q.get("relive") or q.get("get") or {}
            hs = g.get("handles") or ([f"K{q['k']}"] if q.get("k") else [])
            st["desc"] = " ".join(hs)
            st["wants"] = list(dict.fromkeys(c for h in hs for c in tokens(L, h) if c[0] != "J"))
        elif mode in ("map", "roam"):
            node = (q.get("roam") or q.get("map") or {}).get("node")
            st["desc"] = node or "入口"
            st["wants"] = [c for c in tokens(L, node) if c[0] != "J"] if node else []
        elif mode in ("search", "evoke"):
            st["desc"] = " ".join(q.get("words") or [])
        if st["shows"] or st["wants"] or mode in ("search", "evoke"):
            out.append(st)
    return out


def build(steps_):
    """→ {"parents": {步: [(上一步, 要的句柄)]}, "records": [[步……]]}。每个要的句柄各记来路：此前最近给看到它的那一步；
    都没有来路的步是根。"""
    parents = {}
    for i, s in enumerate(steps_):
        ps = []
        for c in s["wants"]:
            p = next((t["id"] for t in reversed(steps_[:i]) if c in t["shows"]), None)
            if p is not None:
                ps.append((p, c))
        parents[s["id"]] = ps
    root = {s["id"]: s["id"] for s in steps_}             # 并查集：连在一起的步是一份访问记录

    def find(x):
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x
    for x, ps in parents.items():
        for p, _ in ps:
            root[find(x)] = find(p)
    groups = defaultdict(list)
    for s in steps_:
        groups[find(s["id"])].append(s["id"])
    return {"parents": parents, "records": sorted(groups.values(), key=lambda g: g[0])}


def up(L, c, level):
    """把一个节点合并到某一层：E → 陈述 → 话题 → 线 → 骨架。合并不上的（比 level 高，或找不到归属）返回空。"""
    k, v = c
    order = {"E": 0, "J": 1, "S": 1, "T": 2, "L": 3, "k": 4}
    want = LEVELS.index(level) if level in LEVELS else 4
    if order[k] > want:
        return set()
    if order[k] == want or (k == "J" and level == "S"):
        return {c}
    if k == "E":
        return set().union(*(up(L, ("S", s), level) for s in L.cites.get(v, []))) if L.cites.get(v) else set()
    if k in ("S", "J"):
        if k == "S":
            nid = L.stmts.get(v, {}).get("entry")
            if nid is None:
                ses = L.ssess.get(v)
                nid = L.by_session.get(ses) if ses else None
        else:
            ses = L.j.get(v, {}).get("session")
            nid = L.by_session.get(ses) if ses else None
        return up(L, ("T", int(nid[1:])), level) if nid else set()
    if k == "T":
        line = (L.nodes.get(f"T{v}") or {}).get("parent")
        return {("L", int(line[1:]))} if line and level == "L" else set()
    return set()


def features(steps_, built, record):
    """一份访问记录的数据特征：只数事实，不打分。"""
    by_id = {s["id"]: s for s in steps_}
    ss = [by_id[x] for x in record]
    depth = {}
    for x in record:                                       # 按时刻排的，上一步一定已经算过
        ps = built["parents"][x]
        depth[x] = 1 + max((depth.get(p, 0) for p, _ in ps), default=0)
    seen = [c for s in ss for c in s["shows"]]
    wanted = {c for s in ss for c in s["wants"]}
    return {"steps": len(ss), "depth": max(depth.values()), "turns": sorted({s["turn"] for s in ss}),
            "kinds": [s["kind"] for s in ss], "shown": len(set(seen)), "shown_again": len(seen) - len(set(seen)),
            "followed": len(wanted & set(seen)), "cross": sum(max(0, len({p for p, _ in built["parents"][x]}) - 1) for x in record),
            "own": sum(1 for s in ss for c in s["wants"] if c not in {cc for _, cc in built["parents"][s["id"]]})}
