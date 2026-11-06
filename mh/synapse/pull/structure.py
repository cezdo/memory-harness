"""relive 话题超窗时先给会话结构（D4，用户 2026-11-05：格式化的更好读；design/synapse/设计.md“relive”）。纯程序。

结构是 form 的产物（engram/form/structure.py，act_detail 里 step = structure）：问题下面排回应它的方向、判断、遗留，证据缩进一层；
节点后面写谁提的、来自哪几条陈述（S 序号，就是这个话题的句柄）、和别的节点的关系；判断带“因为”，问题带原问。
只用和现在的主列一致的那份（陈述编号、顺序都对得上）；对不上（重做了、续上了，form 还没重做结构）就当没有。"""
import json

TYPE = {"Q": "问题", "D": "方向", "E": "证据", "J": "判断", "R": "遗留"}
MARK = {"推翻": "✗ 推翻", "接替": "↻ 接替", "支持": "← 支持", "引出": "→ 引出"}


def latest(conn, session, items):
    """这段会话最后一份成功的结构，且输入就是现在的主列 → (act, 结构)；没有返回 None。"""
    r = conn.execute("SELECT o.id, a.output, a.meta FROM act_detail a JOIN occurrence o ON o.id=a.occurrence "
                     "WHERE a.process='form' AND a.step='structure' AND o.outcome='ok' "
                     "AND json_extract(a.window, '$.unit[0]')=? AND json_extract(a.window, '$.unit[1]')=? ORDER BY o.id DESC LIMIT 1",
                     session).fetchone()
    if r is None or json.loads(r["meta"] or "{}").get("items") != items:
        return None
    try:
        return r["id"], json.loads(r["output"])
    except ValueError:
        return None


def render(d, seq):
    """seq(n)：陈述编号（从 1 起）→ 显示的序号（S5）。→ 行的列表。"""
    nodes = {n["id"]: n for n in d["nodes"]}
    src = lambda ids: "、".join(seq(i) for i in ids)
    rel = lambda nid: "；".join(f"{MARK[e['type']]} {e['to']}" for e in d["edges"] if e["from"] == nid and e["type"] in MARK)

    def line(n, ind=""):
        r = rel(n["id"])
        s = [f"{ind}- {n['id']} {TYPE.get(n['id'][0], '')}（{n.get('by', '')}；{src(n.get('from', []))}）{n['text']}" + (f"〔{r}〕" if r else "")]
        if n.get("because"):
            s.append(f"{ind}  因为：{n['because']}")
        for a in n.get("asked") or []:
            s.append(f"{ind}  原问（{src(a.get('from', []))}）：{a['text']}")
        return s

    adj, owner = {}, {}
    for e in d["edges"]:
        adj.setdefault(e["from"], set()).add(e["to"])
        adj.setdefault(e["to"], set()).add(e["from"])
        if e["type"] == "回应":
            owner[e["from"]] = e["to"]                 # 直接回应的问题，取最后一个（后来的问题更具体）
    first = lambda n: (min(n.get("from") or [999]), n["id"])
    out, placed = [], set()
    for q in [n for n in d["nodes"] if n["id"].startswith("Q")]:
        out += line(q)
        placed.add(q["id"])
        comp, todo = set(), [q["id"]]
        while todo:                                    # 和这个问题连着的（不经过别的问题）
            for y in adj.get(todo.pop(), ()):
                if y not in comp and y not in placed and not y.startswith("Q") and owner.get(y, q["id"]) == q["id"]:
                    comp.add(y)
                    todo.append(y)
        for n in sorted((nodes[x] for x in comp), key=first):
            out += line(n, "    " if n["id"][0] == "E" else "  ")
            placed.add(n["id"])
    rest = [n for n in d["nodes"] if n["id"] not in placed]
    if rest:
        out.append("没挂到问题下的：")
        for n in sorted(rest, key=first):
            out += line(n, "  ")
    return out
