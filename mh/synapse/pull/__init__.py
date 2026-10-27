"""pull：在新库上拉取（重构 E1a，2026-10-19；design/synapse/设计.md“pull”）。按方向分（v0.13，2026-10-22）：
evoke（唤起）横着找，按相关度排，命中即给陈述全文；relive（重温）按位置读：一条陈述给它在会话里前后几条，一个话题按顺序分页，
带 source、said 往下走到依据和原话；roam（漫游）按结构一层层走。相关度只归 evoke，relive 只看位置和结构。
v0.12 以前叫 search、get、map，足迹里的旧 mode 照认。
只读 store 和 form 给出的产物（form.pool、form.entries），每次拉取经 track 追加一条 use。

层，从上往下：工作区 → 骨架节点 k-… → 话题线 L… → 会话入口（话题）T… → 陈述 S（会话记录里的一条）。
陈述是最小单位（文件），线和话题是装着它们的文件夹；E、原话、历史是陈述的属性（source、said、recall 另取），不再往下分（2026-10-19）。
句柄（2026-10-19）：话题线 L（line）加编号，L3；话题 T（topic）加编号，带上所在的线写成 L3:T5；陈述带上线和话题写成 L3:T5:S1，
一眼看出是文件还是文件夹、在哪个文件夹里；还没挂上线的写 L?:T5、L?:T5:S1。编号是库里 topic_line、session_entry 的编号；
form 也这样写（10-19 以前写 l-3、s-5，这里照认）。
陈述的序号按会话编（v0.11）：同一段会话写过的陈述按写出的先后编，只增不减、不重用；续上写出新的话题 T 以后序号照旧，
重新整理换掉的陈述照样取得到（标明已不是现在的）。取的时候 T5:S1、L?:T5:S1、L3:T5:S1 都认（线只是位置，不参与定位），
同一段会话以前的 T 按这段会话取。v0.9–v0.10 按线编的 L3:S1（挂线的先后）照认，只用来接旧句柄。
每次最多给 SHOW 条（2026-10-19：之前的实验，一次展示最多六到八条）：线下面的会话入口、骨架下面的节点、检索的结果都截在这里；
陈述多的会话入口先用“停在”分组（一个停在指着几条陈述，可以重叠），按 6–8 条怎么分组要等 Synapse 攒了数据再定。
旧系统导进来的 J（旧 K 编号原样，K123 就是 J123）在旁边：依据是旧的陈述；同一段会话已经有会话入口的，读的时候标出来，
不改写旧数据（2026-10-19：新库在重建，旧库没有的都已覆盖，和旧库有一部分重叠）。
relive 只给一层（2026-10-19）：这个节点本身、它的直接下一层。
"""
import contextvars
import functools
import json
import math
import re
from collections import Counter, defaultdict

from ...engram import form, show
from ...engram.store import absent
from .. import track
from . import retrieve

VERSION = "v0.24"        # v0.8 换库：读新库，get 只给一层，一次最多 SHOW 条；v0.9 句柄定成话题线 L3、话题 T5、陈述 L3:S1；
                         # v0.10 只给主要内容（来源、原话、历史另取），新的优先，旧 J 只进联想；
                         # v0.11 句柄带归属（话题 L3:T5、陈述 L3:T5:S1，陈述按会话编号），陈述是最小单位：带 source、said、recall 时
                         # 不重复正文，陈述的 recall 给写出的时间和现在在不在；
                         # v0.12 search 的 BM25 几路不拿几乎处处都有的词查（retrieve.UBIQUITOUS），全是这种词又没碰上就说没有命中（2026-10-20）
                         # v0.13 改名 evoke、relive、roam，按方向分：话题按顺序分页、一条陈述给前后几条（2026-10-22）
                         # v0.14 三个工具都有 limit、page（条数、翻页），evoke 加 grain（最小到 L、T、S）（2026-10-22）
                         # v0.16 旧 J 退役（2026-10-23，全量重读后由陈述代替）：evoke 不再给，relive 旧句柄标明已退役
                         # v0.17 evoke 去掉 limit、page：一页 6–8 条按字数定，同一轮前面给过的不再列，再 evoke 就是接着往下（2026-10-27）
                         # v0.18 陈述的来源按 form 写下的顺序给，不按编号（2026-10-27：相关的被挤出第一页，足迹记的那页也对不上）
                         # v0.19 话题超窗先给目录（按 observe 预览的分组），陈述带 said 只给原话的位置和开头（2026-10-27：查看窗口 6–8 条）
                         # v0.20 陈述的来源页对齐分节：两页要同一预览组，否则按组、轮分；同一轮再执行给下一节，分了节才提示（2026-10-27）
                         # v0.21 来源分节先按 form 写下的结构：直接来源、各『引用』、各『文件』各一节（2026-10-27）
                         # v0.22 『文件』指向和来源分开，默认不给，relive 带 files 才给（2026-10-27）
                         # v0.23 话题头下提示“挂线待审视”（中间插入留下的标记，form 设计“挂线是粗筛”；J6，2026-10-27）
                         # v0.24 relive、roam 也去掉 limit，一页固定 SHOW 条，只能翻页（2026-10-27：查看窗口 6–8 条，不让调大绕过去）
SHOW = 8                 # 一次最多展示几条（2026-10-19：实验里最多六到八条）
CONTEXT = 3              # relive 一条陈述：前后各给几条（v0.13）
EVOKE_PAGE = (6, 8)      # evoke 一页至少、至多几条（v0.17，2026-10-27）
EVOKE_CHARS = 800        # 超过 6 条时一页最多多少字：近期 8 条满页的中位约 800 字（10-27 量的）
GRAINS = {"L": ("line", "node"), "T": ("line", "node", "entry"), "S": ("line", "node", "entry")}   # evoke 最小给到哪一层（v0.14）
K1, B = 1.5, 0.75
OLD_NODE = re.compile(r"(D\d+G\d+|D\d+|O:.+|M\d{4}-\d{2}(-\d{2})?)")   # 旧地图的节点编号（旧的主题 T47 和新的话题同形，按话题认）
KIND_ZH = {"node": "骨架", "line": "话题线", "entry": "话题"}
REL_ZH = show.REL_ZH
INPUT_KIND = {"node": "skeleton", "line": "topic_line", "entry": "session_entry"}


class Index:
    """BM25：ASCII 词加汉字二元组（和检索的 BM25 一路同一种分词）。"""

    def __init__(self, docs):
        self.docs = {k: Counter(retrieve.tokens(t)) for k, t in docs.items()}
        self.lens = {k: sum(c.values()) for k, c in self.docs.items()}
        self.avg = sum(self.lens.values()) / max(1, len(self.docs))
        self.df = Counter(t for c in self.docs.values() for t in c)
        self.post = defaultdict(list)
        for k, c in self.docs.items():
            for t in c:
                self.post[t].append(k)

    def top(self, query, n):
        q, total, out = set(retrieve.tokens(query)), len(self.docs), Counter()
        for t in q:
            idf = math.log(1 + (total - self.df[t] + 0.5) / (self.df[t] + 0.5))
            for k in self.post.get(t, ()):
                tf = self.docs[k][t]
                out[k] += idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * self.lens[k] / self.avg))
        return out.most_common(n)


class Layers:
    """新库的读视图：按库里最新的 Occurrence 编号缓存，库变了重建（建一次约一两秒）。"""

    def __init__(self, conn):
        self.stamp = stamp(conn)
        pool = form.pool(conn)
        self.nodes = dict(pool)
        for sid, e in form.entries(conn).items():
            self.nodes.setdefault(sid, e)                      # 还没挂上线的会话入口也能取到
        self.kids = defaultdict(list)
        for nid, n in self.nodes.items():
            if n["parent"] and not n.get("retired"):            # 清空了的线不列（旧句柄照样能打开）
                self.kids[n["parent"]].append(nid)
        self.by_session = {n["session"]: nid for nid, n in self.nodes.items() if n["kind"] == "entry"}
        self.number(conn)
        self.number_sessions(conn)
        self.t_session = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, source, session FROM session_entry")}
        self.stmts = {}                                        # 陈述编号 → {"text", "entry", "h"}：现在的陈述
        for nid, n in self.nodes.items():
            if n["kind"] != "entry":
                continue
            for i, it in enumerate(n.get("items", []), 1):
                h = f"{self.where(nid)}:S{self.sseq.get(it['id'], i)}"
                self.stmts[it["id"]] = {"text": it["text"], "entry": nid, "h": h, "id": it["id"]}
        self.by_h = {s["h"]: sid for sid, s in self.stmts.items()}
        self.conn = conn
        self.j = {}
        for r in conn.execute(J_ROWS):
            self.j[r["id"]] = {"text": r["text"], "holders": json.loads(r["holders"]), "relations": json.loads(r["relations"]),
                               "basis": json.loads(r["basis"]).get("form_record", []), "date": r["started"] or "",
                               "session": (r["source"], r["session"]) if r["source"] else None,
                               "ws": _ws(r["cwd"]), "title": r["title"] or ""}
        self.replaced = {}                                     # 旧 J → 取代它的那条（和怎么说）
        for jid, j in self.j.items():
            for rel in j["relations"]:
                if rel["kind"] in ("supersedes", "split_from", "merged_from"):
                    self.replaced.setdefault(rel["to"], (rel["kind"], jid))

    def number(self, conn):
        """陈述在线里的序号：按挂线（attachment，只追加）的先后，把每次挂上的会话入口那份会话记录的陈述接着往后编。
        seq {(线, 陈述): 序号}；at {(线, 序号): 陈述}。只由只追加的表算出来，所以序号不会变。"""
        self.seq, self.at, nxt = {}, {}, Counter()
        rows = conn.execute("SELECT a.line, e.record FROM attachment a JOIN session_entry e ON e.id=a.entry ORDER BY a.id")
        for line, record in rows.fetchall():
            for (rid,) in conn.execute("SELECT id FROM form_record WHERE born=? AND kind='session' ORDER BY idx", (record,)):
                if (line, rid) not in self.seq:
                    nxt[line] += 1
                    self.seq[(line, rid)], self.at[(line, nxt[line])] = nxt[line], rid
        for old, new in form.replacements(conn).items():     # 修订提示改写的陈述沿用原来那条的序号（2026-10-21）
            for (line, rid), n in list(self.seq.items()):
                if rid == old and new is not None:
                    self.seq[(line, new)], self.at[(line, n)] = n, new

    def number_sessions(self, conn):
        """陈述在会话里的序号（v0.11）：form 给（form.statement_seq）。sseq {陈述: 序号}；sat {(会话, 序号): 陈述}；ssess {陈述: 会话}。"""
        self.sseq, self.sat, self.ssess = form.statement_seq(conn)

    def where(self, nid):
        """话题带上所在的线：L3:T5；还没挂上线写 L?:T5。"""
        return f"{self.nodes[nid]['parent'] or 'L?'}:{nid}"

    def stmt_handle(self, rid, fallback):
        """任一条陈述（现在的或已换掉的）的句柄：这段会话现在的话题 + 会话里的序号；会话没有话题写 L?:T?。"""
        if rid in self.stmts:
            return self.stmts[rid]["h"]
        k = self.ssess.get(rid)
        if k is None:
            return fallback
        cur = self.by_session.get(k)
        return f"{self.where(cur) if cur else 'L?:T?'}:S{self.sseq[rid]}"

    # 下面几样只有拉取用得上，手记推送不建（省一半内存）
    @functools.cached_property
    def cites(self):
        """E 编号 → 引用它的陈述。"""
        out = defaultdict(list)
        ids = [s["id"] for s in self.stmts.values()]
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            for r in self.conn.execute(f"SELECT id, refs FROM form_record WHERE id IN ({','.join('?' * len(chunk))})", chunk):
                for e in json.loads(r["refs"]).get("event", []):
                    out[e].append(r["id"])
        return out

    @functools.cached_property
    def corpus(self):
        return retrieve.Corpus(self.conn)

    @functools.cached_property
    def stmt_index(self):
        return Index({sid: s["text"] for sid, s in self.stmts.items()})



# 旧 J 的时间取它出自的那几轮（旧会话记录的窗口里最后一轮的开始），不取形成的时间：形成常常晚一两天（K20285 是 10-09 的事，10-10 才形成）
J_ROWS = """SELECT j.id, j.text, j.holders, j.relations, j.basis, d.source, d.session, d.cwd, d.title,
                   coalesce(t.started, o.started) AS started
            FROM judgment j JOIN occurrence o ON o.id=j.born
            LEFT JOIN form_record f ON f.id=json_extract(j.basis, '$.form_record[0]')
            LEFT JOIN act_detail a ON a.occurrence=f.born
            LEFT JOIN session_detail d ON d.occurrence=json_extract(a.window, '$.session')
            LEFT JOIN turn_detail td ON td.source=d.source AND td.session=d.session
                                    AND td.ordinal=json_extract(a.window, '$.ordinals[#-1]')
            LEFT JOIN occurrence t ON t.id=td.occurrence"""


def _ws(cwd):
    from ...engram.store import workspace
    return "" if absent(cwd or "") else workspace(cwd)


def stamp(conn):
    return conn.execute("SELECT max(id) FROM occurrence WHERE role != 'use'").fetchone()[0]


_CACHE = {}


def layers(conn):
    """常驻进程（MCP）里按库缓存；库里有新的 act 就重建。"""
    key = id(conn)
    got = _CACHE.get(key)
    if got is None or got.stamp != stamp(conn):
        got = _CACHE[key] = Layers(conn)
    return got


# ---- 排版 ----
# 只给主要内容（2026-10-19）：来源、原话、历史、话题里的其余陈述都不带，怎么取写在工具说明和手记推送里，要看再取。
# 返回记录的措辞以后再设计。

def _day(t):
    return t[:10] if t and not absent(t) else "日期未知"


def _who(j):
    return "、".join(h["who"] for h in j["holders"] if not absent(h.get("who", ""))) or "持有者未知"


def name(nid):
    """对外的句柄：form 的编号原样（话题线 L3、话题 T5、骨架 k-…；form 10-19 以前写 l-3、s-5）。"""
    return nid


def _node_line(L, nid):
    n = L.nodes[nid]
    if n["kind"] == "entry":
        return f"{L.where(nid)} {n['name']}（{_day(n['date'])}，{len(n.get('items', []))} 条陈述）：{n['topic']}"
    if n["kind"] == "line":
        return f"{nid} {n['name']}（{_size(L, nid)}）"
    return f"{nid} {n['name']}（骨架）"


def _size(L, nid):
    """线的规模：子线几条、下面一共几个话题、最近的日期（中间层的线下面是子线，2026-10-24）。"""
    subs = [k for k in L.kids[nid] if L.nodes[k]["kind"] == "line"]
    ents, todo = [], [nid]
    while todo:
        k = todo.pop()
        ents += [x for x in L.kids[k] if L.nodes[x]["kind"] == "entry"]
        todo += [x for x in L.kids[k] if L.nodes[x]["kind"] == "line"]
    last = max((L.nodes[k]["date"] for k in ents), default="")
    out = (f"{len(subs)} 条子线，共 " if subs else "") + f"{len(ents)} 个话题" + (f"，最近 {_day(last)}" if last else "")
    if L.nodes[nid].get("retired"):
        heads = [f"L{h}" for h in form.line_heads(L.conn, L.nodes[nid]["row"]) if f"L{h}" != nid] if L.conn else []
        out += "；已清空，话题都改挂走了" + (f"，接替它的是 {'、'.join(heads)}" if heads else "")
    return out


_WIN = contextvars.ContextVar("pull_window", default=(SHOW, 1))     # 这次调用每页几条、第几页


def paging(page=None):
    """每页几条、第几页：每页固定 SHOW 条（v0.24 去掉 limit，用户 2026-10-27）；page 从 1 起，给错了按第 1 页。"""
    try:
        return SHOW, max(1, int(page)) if page not in (None, "") else 1
    except (TypeError, ValueError):
        return SHOW, 1


def _slice(seq):
    n, p = _WIN.get()
    return list(seq)[(p - 1) * n:p * n]


def _more(total):
    """截断的说明：还有几条、下一页怎么要；翻过头了说一共几条。"""
    n, p = _WIN.get()
    if total > p * n:
        return [f"（还有 {total - p * n} 条：下一页 page={p + 1}）"]
    return [f"（第 {p} 页没有了：一共 {total} 条）"] if total and (p - 1) * n >= total else []


def _cap(lines, total):
    """这一页的几条，加上截断的说明。"""
    return _slice(lines) + _more(total)


def _span(hs):
    """几条陈述句柄 → 一段的写法（同一个话题、序号连着时写 L2:T5:S9-S16，否则给第一条）。"""
    m = [re.fullmatch(r"(.+):S(\d+)", h) for h in hs]
    if all(m) and len({x.group(1) for x in m}) == 1 and \
            [int(x.group(2)) for x in m] == list(range(int(m[0].group(2)), int(m[0].group(2)) + len(m))):
        return f"{hs[0]}-S{m[-1].group(2)}" if len(hs) > 1 else hs[0]
    return hs[0]


def _head(L, nid):
    n = L.nodes[nid]
    head = f"[{L.where(nid)}] {n['name']}（{_day(n['date'])}，{len(n.get('items', []))} 条陈述）：{n['topic']}"
    if n.get("review"):          # 中间插入留下的标记（v0.23）：这条挂线形成时没看到后来补进的内容，用到时由你带着新内容再看
        head += f"\n挂线{n['review']}；线挂得不对就用 feedback 改挂（改挂到同一条线表示看过了）"
    return head


def show_node(L, nid, recall=None, conn=None, start=0):
    n = L.nodes[nid]
    if n["kind"] == "entry":
        # 话题：按顺序读（v0.13，2026-10-22），从第 start 条起一次一页（默认 SHOW 条；给了 page 从那一页起），
        # 后面还有的写出接着读的句柄。上一级的线先不给。
        items = n.get("items", [])
        size, p = _WIN.get()
        if len(items) > size and not start and p == 1 and recall is None and conn is not None:
            secs = _sections(conn, L, items)
            if secs:
                return _show_sections(L, nid, secs)
        start = start or (p - 1) * size
        page = items[start:start + size]
        out = [_head(L, nid)] + [f"- {L.stmts[it['id']]['h']} {it['text']}" for it in page]
        if not page and items:
            out.append(f"（第 {p} 页没有了：一共 {len(items)} 条）")
        rest = [L.stmts[it["id"]]["h"] for it in items[start + size:start + 2 * size]]
        if rest:
            out.append(f"接着读：{_span(rest)}")
        if recall is not None and conn is not None:
            out += _entry_history(conn, n, recall)
        return "\n".join(out), {"session_entry": [n["row"]], "form_record": [it["id"] for it in page]}
    kids = L.kids[nid]
    sub = _recent(L, [k for k in kids if L.nodes[k]["kind"] != "entry"])
    ents = sorted((k for k in kids if L.nodes[k]["kind"] == "entry"), key=lambda k: L.nodes[k]["date"], reverse=True)
    kind = "骨架" if n["kind"] == "node" else _size(L, nid)
    out = [f"[{nid}] {n['name']}（{n['ws'] or '工作区未知'}，{kind}）"]
    out += _cap([f"- {_node_line(L, k)}" for k in sub + ents], len(sub) + len(ents))
    ins = defaultdict(list)
    for k in _slice(sub + ents):
        ins[INPUT_KIND[L.nodes[k]["kind"]]].append(L.nodes[k].get("row", k))
    if n["kind"] == "line":
        ins["topic_line"].insert(0, n["row"])
    return "\n".join(out), dict(ins)


def _sections(conn, L, items):
    """话题超窗时的目录（v0.19，2026-10-27）：陈述按 observe 预览的分组归节，节名用预览写的“类型”，不另调模型。
    陈述看它第一条 E 出自哪一轮，那一轮以最近一次预览的分组为准；对不上预览的（旧做法观察的）归“其余”。全都对不上给空。"""
    turns = {}
    for it in items:
        row = conn.execute("SELECT refs FROM form_record WHERE id=?", (it["id"],)).fetchone()
        refs = json.loads(row[0]).get("event", []) if row else []
        r = refs and conn.execute("SELECT turn FROM event WHERE id=?", (refs[0],)).fetchone()
        turns[it["id"]] = r[0] if r else None
    group = _preview_groups(conn, turns.values())
    secs = {}
    for it in items:
        k = group.get(turns[it["id"]])
        secs.setdefault(k, []).append(it["id"])
    if list(secs) == [None]:
        return []
    return [(k, ids) for k, ids in secs.items()]


def _preview_groups(conn, turns):
    """轮 → 它最近一次 observe 预览的分组 (预览, 从第几轮, 到第几轮, 类型)：经 observe 的接口（observe.groups）。"""
    from ...engram import observe
    return observe.groups(conn, turns)


def _group_name(k):
    return (f"第 {k[1]}–{k[2]} 轮" if k[1] != k[2] else f"第 {k[1]} 轮") + (f" {k[3]}" if k[3] else "")


def source_sections(conn, evs, size=None, structures=None, files=False):
    """陈述的来源怎么分节（v0.20，用户 2026-10-27：页对齐，两页要有门槛）。返回 [(节名或 None, [E 编号…])]，按 form 写下的顺序。
    一页以内不分。form 写下了结构的（v0.21）按结构分：直接来源一节，每个『引用』『文件』各一节，节名是引用名、文件路径，
    前面给过的不重给（refs.event 就是这几样的并集：sol 10-27 查的，超过 16 条的 106 条里 91 条是被引用、文件指向撑大的）。
    没有结构的：两页以内、全在同一个预览组的顺着切成两页；否则按预览组分，只有一组的按轮分，只有一轮的顺着切。
    每节再按窗口切齐，后面的标“续”。"""
    size = size or _WIN.get()[0]
    st_ = structures or {}
    structured = bool(st_.get("basis") or st_.get("quotes") or st_.get("files"))
    if structured:              # 『文件』指向和来源分开：默认不给，files 时只给它（用户 2026-10-27，v0.22）
        own = set(st_.get("basis", [])) | {x for q in st_.get("quotes", []) for x in q.get("to", [])}
        fil = [x for f in st_.get("files", []) for x in f.get("to", [])]
        evs = [e for e in fil if e not in own] if files else [e for e in evs if e in own]
    elif files:
        evs = []
    rows = {r[0]: r[1] for r in conn.execute(f"SELECT id, turn FROM event WHERE id IN ({','.join('?' * len(evs))})", evs)} if evs else {}
    evs = [e for e in dict.fromkeys(evs) if e in rows]
    if len(evs) <= size and not files:
        return [(None, evs)]
    if structured:
        parts = [(f"文件：{f.get('path', '')}", [x for x in f.get("to", []) if x in evs]) for f in st_.get("files", [])] if files else \
            [("直接来源", st_.get("basis", []))] + [(f"引用：{q.get('name', '')}", q.get("to", [])) for q in st_.get("quotes", [])]
        out, given = [], set()
        group = _preview_groups(conn, rows.values())
        for nm, ids in parts:
            ids = [e for e in dict.fromkeys(ids) if e in rows and e not in given]
            given.update(ids)
            subs = {}
            for e in ids:                  # 一节超过两页的，节内再按预览组分（数字范围展开会夹进别的组：sol 10-27 查的 64872）
                subs.setdefault(group.get(rows[e]) if len(ids) > 2 * size else None, []).append(e)
            for k, sub in subs.items():
                sn = nm if k is None or len(subs) == 1 else f"{nm} · {_group_name(k)}"
                for i in range(0, len(sub), size):
                    out.append((sn if i == 0 else f"{sn}（续）", sub[i:i + size]))
        rest = [e for e in evs if e not in given]
        for i in range(0, len(rest), size):
            out.append(("其余" if i == 0 else "其余（续）", rest[i:i + size]))
        return out
    group = _preview_groups(conn, rows.values())
    keys = {e: group.get(rows[e]) for e in evs}
    if len(set(keys.values())) > 1:
        by = {e: ("g", keys[e]) for e in evs}
    elif len(evs) > 2 * size and len(set(rows[e] for e in evs)) > 1:
        by = {e: ("t", rows[e]) for e in evs}
    else:
        by = {e: None for e in evs}
    order = {}
    for e in evs:
        order.setdefault(by[e], []).append(e)
    out = []
    for k, ids in order.items():
        nm = None if k is None else (_group_name(k[1]) if k[1] and k[0] == "g" else "其余" if k[0] == "g" else _turn_name(conn, k[1]))
        for i in range(0, len(ids), size):
            out.append((nm if i == 0 or nm is None else f"{nm}（续）", ids[i:i + size]))
    return out


def _turn_name(conn, t):
    r = conn.execute("SELECT ordinal FROM turn_detail WHERE occurrence=?", (t,)).fetchone()
    return f"第 {r[0] + 1} 轮" if r else "某一轮"


def _show_sections(L, nid, secs):
    """目录一行一节：第几轮、类型、这一节的陈述（连着的写成一段，读它就是 relive 这个句柄）。节多了照窗口翻页。"""
    out = [_head(L, nid) + "（陈述多，先给目录：relive 某一节的句柄读它）"]
    lines = []
    for k, ids in secs:
        hs = [L.stmts[i]["h"] for i in ids]
        span = _span(hs)
        hs_text = span if span != hs[0] or len(hs) == 1 else "、".join(hs)
        name = "其余" if k is None else _group_name(k)
        lines.append(f"- {name}：{hs_text}（{len(ids)} 条）")
    out += _cap(lines, len(lines))
    return "\n".join(out), {"session_entry": [L.nodes[nid]["row"]]}


def _recent(L, ids):
    return sorted(ids, key=lambda k: -_latest(L, k))


def _entry_history(conn, n, recall):
    """回想模式：同一段会话以前的话题（续上、重新整理、反馈都写新的，旧的照留）。"""
    rows = _window(conn.execute("SELECT id, name, topic FROM session_entry WHERE source=? AND session=? AND id<? "
                                "ORDER BY id DESC", (*n["session"], n["row"])).fetchall(), recall)
    return ["历史：无"] if not rows else ["历史："] + [f"- T{r['id']} {r['name']}：{r['topic']}" for r in _slice(rows)]


def _window(items, recall):
    if recall in (None, "all"):
        return list(items)
    try:
        return list(items)[:max(0, int(recall))]
    except ValueError:
        return list(items)


def show_context(L, sid):
    """relive 一条现在的陈述（v0.13）：它所在的话题里前后各 CONTEXT 条，按原来的顺序，要的那条标 ▶。"""
    nid = L.stmts[sid]["entry"]
    items = L.nodes[nid].get("items", [])
    i = next(k for k, it in enumerate(items) if it["id"] == sid)
    near_ = items[max(0, i - CONTEXT):i + CONTEXT + 1]
    out = [_head(L, nid)] + [f"{'▶' if it['id'] == sid else '-'} {L.stmts[it['id']]['h']} {it['text']}" for it in near_]
    return "\n".join(out), {"form_record": [it["id"] for it in near_]}


def show_stmt(conn, L, sid, h, source=False, said=False, recall=None, section=0, files=False):
    """陈述是最小单位（2026-10-19）：只取它时给正文；带 source、said、recall 是取它的属性，只给句柄和属性，
    正文不再重复（evoke、relive 已经给过）。"""
    row = conn.execute("SELECT * FROM form_record WHERE id=?", (sid,)).fetchone()
    now = sid in L.stmts
    head = f"[{L.stmt_handle(sid, h)}]" + ("" if now else "（已不是现在的陈述）")
    attrs = source or said or files or recall is not None
    out = [head if attrs else head + (" " if now else "") + row["text"]]
    evs = json.loads(row["refs"]).get("event", [])
    shown, secs, name = [], [], None
    if files and not any(f.get("to") for f in json.loads(row["structures"] or "{}").get("files", [])):
        out.append("这条陈述没有『文件』指向。")
    elif (source or said or files) and evs:
        secs = source_sections(conn, evs, structures=json.loads(row["structures"] or "{}"), files=files)   # 页对齐分节（v0.20）；section 是这一轮第几次要它
        if section >= len(secs):
            out.append(f"来源一共 {len(secs)} 节，这一轮都给过了。")
        else:
            name, shown = secs[section]
    if shown:
        lines = []     # 按 form 写下的顺序给（10-27：原来按编号排，相关的被挤出第一页，和足迹按顺序记的那页也对不上）
        seen = set()   # said 在陈述上只给原话的位置和开头，同一段只给一次；全文 relive 那条 E 带 said（v0.19）
        got = {r["id"]: r for r in conn.execute(f"SELECT * FROM event WHERE id IN ({','.join('?' * len(shown))})", shown)}
        for r in (got[e] for e in shown if e in got):
            lines.append(f"- E{r['id']}{_speaker(r)} {r['text']}" + ("".join(f"\n    {x}" for x in _said(conn, r, brief=seen)) if said else ""))
        if len(secs) > 1:      # 真的分了节才说（用户 2026-10-27：常态不提示）
            out += [f"{'文件指向' if files else '来源'}（第 {section + 1} 节，共 {len(secs)} 节" + (f"：{name}" if name else "") + "）："] + lines
            if section + 1 < len(secs):
                out.append("（再执行一次同样的 relive，给出下一节）")
        else:
            out += [(f"文件指向（{name}）：" if files and name else "文件指向：" if files else "来源：")] + lines
    if recall is not None:
        out += _stmt_history(conn, L, row)
    return "\n".join(out), {"form_record": [sid], "event": shown, **({"_sections": len(secs)} if len(secs) > 1 else {})}


def _stmt_history(conn, L, row):
    """陈述的历史：什么时候写出、最早收在这段会话的哪个话题，现在还在不在（续上、重新整理、反馈都写新的话题，旧的照留）。"""
    k = L.ssess.get(row["id"])
    if k is None:
        return ["历史：无"]
    first = conn.execute("SELECT id FROM session_entry WHERE source=? AND session=? AND born>? ORDER BY id LIMIT 1",
                         (*k, row["born"])).fetchone()
    out = ["历史：", f"- 写于 {_day(form._date(conn, row['born']))}" + (f"，最早收在话题 T{first[0]}" if first else "")]
    cur = L.by_session.get(k)
    if row["id"] in L.stmts:
        out.append(f"- 现在在 {L.where(cur)} 里")
    else:
        out.append("- 已不在现在的话题里" + (f"（这段会话现在是 {L.where(cur)}）" if cur else ""))
    return out


def _speaker(r):
    """说话人：新做法的 observe 还没写（?not_done），没有就不列。"""
    return "" if absent(r["speaker"]) else f"（{r['speaker']}）"


SAID_HEAD = 40           # 陈述带 said 时每段原话给多少字（v0.19）


def _said(conn, ev, brief=None):
    """E 的出处一路到原话：几轮一起观察的写“轮:段落”，轮号是这一轮在整段会话里的序号（ordinal + 1，observe 的
    deep_rough_read.turn_blocks 这样编），不是观察窗口里的第几轮；旧版本逐轮的只写段落，就是 E 的 turn 那一轮。"""
    if absent(ev["turn"]):
        return ["（没有出处）"]
    base = conn.execute("SELECT source, session FROM turn_detail WHERE occurrence=?", (ev["turn"],)).fetchone()
    out, cache = [], {}
    for seg in json.loads(ev["segments"]):
        n, _, label = seg.rpartition(":")
        if not n.isdigit():
            t = ev["turn"]
        else:
            r = base and conn.execute("SELECT occurrence FROM turn_detail WHERE source=? AND session=? AND ordinal=?",
                                      (base["source"], base["session"], int(n) - 1)).fetchone()
            if not r:
                out.append(f"[{seg}]（找不到这一轮）")
                continue
            t = r[0]
        segs = cache.setdefault(t, show.segments(conn, t, ev["born"]))
        text = segs.get(label)
        if brief is not None:      # 目录：位置加开头；前面给过的这一段只写位置
            key = (t, label)
            out.append(f"[{seg}]" + ("（同上）" if key in brief else " " + (_head_of(text) if text else "（找不到）")))
            brief.add(key)
            continue
        out.append(f"[{seg}] " + (text[:400] + ("…" if len(text) > 400 else "") if text else "（找不到）"))
    return out or ["（没有出处）"]


def _head_of(text):
    one = " ".join(text.split())
    return one[:SAID_HEAD] + ("…" if len(one) > SAID_HEAD else "")


def show_event(conn, L, eid, source=False, said=False):
    r = conn.execute("SELECT * FROM event WHERE id=?", (eid,)).fetchone()
    if r is None:
        return f"E{eid}：没有这一条", {}
    t = conn.execute("SELECT started FROM occurrence WHERE id=?", (r["turn"],)).fetchone() if not absent(r["turn"]) else None
    out = [f"[E{eid}]（{_day(t[0] if t else '')}）{_speaker(r)}{r['text']}"]
    if source or said:
        out += ["原话："] + _said(conn, r)
    return "\n".join(out), {"event": [eid]}


RETIRED = "（旧版本 J，已退役：全量重读后由陈述代替，2026-10-23；现在的说法用 evoke 查）"


def show_j(conn, L, jid, recall=None, source=False):
    j = L.j.get(jid)
    if j is None:
        return f"J{jid}：新库里没有（v6 以前的 K 编号留在旧库归档）；换成 evoke 查词。", {}
    out = [f"[J{jid}]（{_day(j['date'])}，{_who(j)}）{j['text']}", RETIRED]
    if source:
        entry = L.by_session.get(j["session"]) if j["session"] else None
        out.append("来源：" + (f"同一段会话现在的话题 {entry} {L.nodes[entry]['name']}" if entry
                             else f"会话「{j['title'] or '无标题'}」"))
        if j["basis"]:
            rows = conn.execute(f"SELECT id, text FROM form_record WHERE id IN ({','.join('?' * len(j['basis']))}) ORDER BY id",
                                j["basis"]).fetchall()
            out += _cap([f"- {r['text']}" for r in rows], len(rows))
    if recall is not None:
        out += _j_history(L, jid, recall)
    return "\n".join(out), {"judgment": [jid], "form_record": _slice(j["basis"]) if source else []}


def _j_history(L, jid, recall):
    """回想模式：沿关系往回走（它取代、细化、相同的那些），带关系。"""
    seen, chain, todo = {jid}, [], [(jid, 0)]
    limit = None if recall in (None, "all") else int(recall) if str(recall).isdigit() else None
    while todo:
        cur, depth = todo.pop(0)
        if limit is not None and depth >= limit:
            continue
        for rel in L.j.get(cur, {}).get("relations", []):
            to = rel["to"]
            if to in seen or to not in L.j:
                continue
            seen.add(to)
            chain.append(f"- J{cur} {REL_ZH.get(rel['kind'], rel['kind'])} J{to}（{_day(L.j[to]['date'])}）：{L.j[to]['text']}")
            todo.append((to, depth + 1))
    return ["历史：无"] if not chain else ["历史："] + _cap(chain, len(chain))


# ---- 句柄 ----

_H = re.compile(r"(?:(k-[\w\-]+)|(?:[Ll]|l-)(\d+)|(?:(?:[Ll]|l-)(?:\d+|\?):)?(?:[Tt]|s-)(\d+)|[Ee](\d+)|[JjKk]?(\d+))")
_S = re.compile(r"(?:(?:[Ll]|l-)(?:\d+|\?):)?(?:[Tt]|s-)(\d+):[Ss](\d+)(?:-[Ss]?(\d+))?")    # 陈述：话题 + 会话里的序号，线只是位置
_S_LINE = re.compile(r"(?:[Ll]|l-)(\d+):[Ss](\d+)(?:-[Ss]?(\d+))?")                        # v0.9–v0.10 按线编的序号，接旧句柄


def resolve(L, h):
    """句柄 → (种类, 值)。名字要逐字对上而且只有一个才取（话题、线、骨架），否则列出候选，不猜。"""
    h = h.strip()
    m = _S.fullmatch(h)
    if m:
        tid, lo = int(m.group(1)), int(m.group(2))
        hi = min(int(m.group(3) or lo), lo + _WIN.get()[0] - 1)  # 一段最多一页
        k = L.t_session.get(tid)
        cur = L.by_session.get(k) if k else None
        at = L.where(cur) if cur else f"L?:T{tid}"
        return "stmts", [(f"{at}:S{i}", L.sat.get((k, i)) if k else None) for i in range(lo, hi + 1)]
    m = _S_LINE.fullmatch(h)
    if m:
        line, lo = int(m.group(1)), int(m.group(2))
        hi = min(int(m.group(3) or lo), lo + _WIN.get()[0] - 1)
        return "stmts", [(f"L{line}:S{i}", L.at.get((line, i))) for i in range(lo, hi + 1)]
    m = _H.fullmatch(h)
    if m:
        nid = m.group(1) or (f"L{m.group(2)}" if m.group(2) else f"T{m.group(3)}" if m.group(3) else None)
        if nid in L.nodes:
            return "node", nid
        if m.group(3) and L.by_session.get(L.t_session.get(int(m.group(3)))):   # 同一段会话以前的话题：给现在的那个
            return "earlier", (h, L.by_session[L.t_session[int(m.group(3))]])
        if nid:
            return "missing", h
        if m.group(4):
            return "event", int(m.group(4))
        return "j", int(m.group(5))
    if OLD_NODE.fullmatch(h):
        return "old", h
    named = [nid for nid, n in L.nodes.items() if n["name"] == h]
    if len(named) == 1:
        return "node", named[0]
    return "ambiguous" if named else "missing", (named, h)


MISSING = ("没有这个句柄。话题线 L3、话题 L3:T5（T5）、陈述 L3:T5:S1（T5:S1，一段 L3:T5:S1-S8；没挂上线写 L?）、骨架 k-、E、"
           "J；名字要逐字对上。")


def relive(conn, handles, said=False, recall=None, session="", purpose="interactive", cwd="", note="", source=False,
           page=None, files=False, **_):
    tok = _WIN.set(paging(page))
    try:
        return _relive(conn, handles, said, recall, session, purpose, cwd, note, source, files)
    finally:
        _WIN.reset(tok)


def _window_query():
    """不是第 1 页才记进足迹。"""
    p = _WIN.get()[1]
    return {"page": p} if p != 1 else {}


def _relive(conn, handles, said, recall, session, purpose, cwd, note, source, files=False):
    """按位置读（v0.13；v0.12 以前叫 get）：一条陈述给它前后几条，话题按顺序分页（接着读给范围句柄），线、骨架给下一层，E、J 给正文。
    source：来源（陈述的 E、J 的旧会话记录和同一段会话现在的话题）；said：E 走到原话；recall：历史。"""
    L = layers(conn)
    out, ins = [], defaultdict(list)
    attrs = source or said or files or recall is not None
    round_metas = _round_metas(conn, session, "relive") if (source or said or files) else []
    sections = {}
    for h in handles:
        kind, v = resolve(L, h)
        if kind == "node":
            text, got = show_node(L, v, recall, conn)
        elif kind == "earlier":
            text, got = show_node(L, v[1], recall, conn)
            text = f"{v[0]}：这段会话以前的话题，现在是 {L.where(v[1])}\n" + text
        elif kind == "stmts" and len(v) == 1 and v[0][1] in L.stmts and not attrs:
            text, got = show_context(L, v[0][1])
        elif kind == "stmts":
            parts, got = [], defaultdict(list)
            for hh, sid in v:
                if sid is None:
                    parts.append(f"{hh}：没有这一条")
                    continue
                key = f"{sid}/{'s' if source else ''}{'d' if said else ''}{'f' if files else ''}"     # 内部按“陈述/第几节”记（v0.20）
                k = sum(1 for m in round_metas if key in m.get("sections", {}))
                t, g = show_stmt(conn, L, sid, hh, source, said, recall, section=k, files=files)
                if g.pop("_sections", 0):
                    sections[key] = k
                parts.append(t)
                for k2, xs in g.items():
                    got[k2] += xs
            text = "\n".join(parts) if not attrs else "\n\n".join(parts)
        elif kind == "event":
            text, got = show_event(conn, L, v, source, said)
        elif kind == "j":
            text, got = show_j(conn, L, v, recall, source)
        elif kind == "old":
            text, got = f"{v}：旧地图的节点编号，换库以后没有了；用 roam 或 evoke。", {}
        elif kind == "ambiguous":
            text, got = (f"{v[1]}：有几个同名的，不猜：" + "、".join(f"{x}（{KIND_ZH[L.nodes[x]['kind']]}）" for x in v[0])), {}
        else:
            text, got = f"{h}：{MISSING}", {}
        out.append(text)
        for k, xs in got.items():
            ins[k] += [x for x in xs if x not in ins[k]]
    text = "\n\n".join(out)
    text = track.deliver(conn, VERSION, "recall" if recall is not None else "relive",
              {"relive": {"handles": list(handles), "said": said, "source": source, "recall": recall, **({"files": True} if files else {}),
                          **_window_query()}},
              text, dict(ins), session=session, purpose=purpose, cwd=cwd, note=note, raw=said,
              meta={"sections": sections} if sections else None)
    return text


def query_workspace(conn, session, cwd):
    """查询方的工作区：会话采集进来了（只对上一段）就按会话算（含按会话的更正），否则按 cwd。"""
    from ...engram.store import session_workspace, workspace
    rows = conn.execute("SELECT source, cwd FROM session_detail WHERE session=?", (session or "",)).fetchall()
    return session_workspace(rows[0][0], session, rows[0][1]) if len(rows) == 1 else workspace(cwd)


def _alloc(rem, n):
    """一页 n 条怎么分（v0.14 把 v0.13 的 8 条按比例推开）：话题和线、陈述各先占一半，哪一路不够就让给别的。"""
    take = {"node": min((n + 1) // 2, len(rem["node"])), "stmt": min(n // 2, len(rem["stmt"]))}
    spare = n - sum(take.values())
    for k in ("node", "stmt"):                         # 让出来的名额按顺序补给还有剩的
        more = min(spare, len(rem[k]) - take[k])
        take[k] += more
        spare -= more
    return take


def seen_this_round(conn, session):
    """这一轮前面的 evoke 已经给看过的（v0.17）。一轮从这段会话最近一次联想推送算起（用户每说一句，宿主的钩子写一次）；
    这段会话没有推送的（没挂钩子，如隔离的实验），整段会话算一轮。没有会话编号认不出轮，不去重。"""
    seen = set()
    for meta in _round_metas(conn, session, "evoke"):
        seen.update(meta.get("evoked", []))
    return seen


def _round_metas(conn, session, mode):
    """这一轮里前面几次拉取（mode）的 meta；一轮怎么算见 seen_this_round。没有会话编号给空。"""
    if not session:
        return []
    cond, args = track.by_session(conn, session)
    start = conn.execute(f"SELECT MAX(d.occurrence) FROM use_detail d WHERE {cond} AND d.channel='push' AND d.mode='associate'",
                         args).fetchone()[0] or 0
    return [json.loads(m) for (m,) in conn.execute(f"SELECT d.meta FROM use_detail d WHERE {cond} AND d.channel='pull' "
                                                   "AND d.mode=? AND d.occurrence>? ORDER BY d.occurrence", [*args, mode, start])]


def _fit(rem, line):
    """一页几条（v0.17）：至少 EVOKE_PAGE[0] 条，再往上加到 EVOKE_PAGE[1]，字数超过 EVOKE_CHARS 就停。每个条数都按 _alloc 分。"""
    best = None
    for n in range(EVOKE_PAGE[0], EVOKE_PAGE[1] + 1):
        take = _alloc(rem, n)
        if best is not None and (take == best or sum(len(line(k, x)) for k in rem for x in rem[k][:take[k]]) > EVOKE_CHARS):
            break
        best = take
    return best


def evoke(conn, words, session="", purpose="interactive", cwd="", note="", grain=None, **_):
    """按相关度排，一页 6–8 条（按字数定，_fit），按 _alloc 分给话题和线、陈述。同一轮前面 evoke 给过的不再列（seen_this_round），
    所以同一轮再 evoke 同样的词就是接着往下看，不用翻页（v0.17，用户 2026-10-27：减少模型理解成本；v0.14 的 limit、page 去掉）。
    略去的里面，不去重时本该出现在这一页的，页脚附上句柄。
    grain：最小给到哪一层（v0.14，2026-10-22）：L 只给线和骨架，T 给到话题，S（默认）给到陈述。"""
    g = str(grain or "S").strip().upper()[:1]
    g = g if g in GRAINS else "S"
    L = layers(conn)
    ws = query_workspace(conn, session, cwd)
    big = [w for w in words if L.corpus.share(w, ws) >= retrieve.UBIQUITOUS]   # 几乎处处都有的词：BM25 几路不拿它查（v0.12）；
    # 索引词那一路照旧拿全部查词，大词（两成以上）由它自己处理
    text_q = " ".join(w for w in words if w not in big)
    hits = {"node": [nid for nid, _ in retrieve.search(words, text_q, {k: v for k, v in L.nodes.items() if not v.get("retired")}, ws, L.corpus, deep=True)
                     if L.nodes[nid]["kind"] in GRAINS[g]],
            "stmt": [sid for sid, _ in L.stmt_index.top(text_q, None)] if g == "S" else []}
    seen = seen_this_round(conn, session)
    key = {"node": lambda x: f"n:{x}", "stmt": lambda x: f"s:{x}"}
    rem = {k: [x for x in xs if key[k](x) not in seen] for k, xs in hits.items()}
    skipped = sum(map(len, hits.values())) - sum(map(len, rem.values()))
    line = lambda k, x: f"- {_node_line(L, x)}" if k == "node" else f"- {L.stmts[x]['h']} {L.stmts[x]['text']}"
    take = _fit(rem, line)
    top = _fit(hits, line) if skipped else {}           # 不去重时这一页会给的：其中前面给过的就是这次也相关的，附句柄（用户 2026-10-27）
    again = [(L.stmts[x]["h"] if k == "stmt" else L.where(x) if L.nodes[x]["kind"] == "entry" else x) for k in top for x in hits[k][:top[k]] if key[k](x) in seen]
    shown = {k: rem[k][:take[k]] for k in rem}
    left = sum(len(rem[k]) - take[k] for k in rem)
    out, ins = [], defaultdict(list)
    for nid in shown["node"]:
        out.append(line("node", nid))
        nd = L.nodes[nid]
        ins[INPUT_KIND[nd["kind"]]].append(nd.get("row", nid))
    for x in shown["stmt"]:
        out.append(line("stmt", x))
        ins["form_record"].append(x)
    if out and (left or skipped):
        out.append("（" + "；".join(([f"这一轮前面给过的 {skipped} 条不再列" + (f"，其中这次也排在前面的：{'、'.join(again)}" if again else "")]
                                     if skipped else []) +
                                    ([f"还有 {left} 条：再 evoke 就接着往下"] if left else [])) + "）")
    text = "\n".join(out) or (f"没有命中（查词都是几乎每段会话都有的词：{'、'.join(big)}；换个更具体的说法）。" if big and len(big) == len(words)
                               else f"这一轮前面已经都给过了（{skipped} 条" + (f"，排在前面的：{'、'.join(again)}" if again else "") + "），往下没有了。" if skipped else "没有命中。")
    query = {"words": list(words), **({"big": big} if big else {}), **({"skipped": skipped} if skipped else {}),
             **({"grain": g} if g != "S" else {})}
    evoked = [key[k](x) for k in shown for x in shown[k]]
    text = track.deliver(conn, VERSION, "evoke", query, text, dict(ins), session=session, purpose=purpose, cwd=cwd, note=note,
                         meta={"evoked": evoked} if evoked else None)
    return text


def roam(conn, node=None, session="", purpose="interactive", cwd="", note="", page=None, **_):
    tok = _WIN.set(paging(page))
    try:
        return _roam(conn, node, session, purpose, cwd, note)
    finally:
        _WIN.reset(tok)


def _roam(conn, node, session, purpose, cwd, note):
    """roam（v0.12 以前叫 map）：不给 node 是入口（各工作区）；给工作区名是那个工作区的第一层（有骨架的，是骨架的根下面那层）；给句柄同 relive 的一层。"""
    L = layers(conn)
    from ...engram.store import workspace
    here = workspace(cwd)
    roots = defaultdict(list)
    for nid, n in L.nodes.items():
        if not n["parent"]:
            roots[n["ws"] or "工作区未知"].append(nid)
    ins = {}
    if not node:
        def count(ws, kind):
            return sum(1 for n in L.nodes.values() if n["ws"] == ws and n["kind"] == kind and not n.get("retired"))
        order = sorted(roots, key=lambda w: (w != here, -count(w, "entry")))
        text = "\n".join(f"- {ws}{'（当前）' if ws == here else ''}：{count(ws, 'line')} 条话题线，{count(ws, 'entry')} 个话题"
                         for ws in order)
    elif node in roots:
        ids = [x for k in roots[node] for x in (L.kids[k] if L.nodes[k]["kind"] == "node" else [k])]   # 骨架的根不占一层（2026-10-22）
        ids = sorted(ids, key=lambda k: (L.nodes[k]["kind"] != "node", -_latest(L, k)))
        text = "\n".join([f"[{node}]"] + _cap([f"- {_node_line(L, k)}" for k in ids], len(ids)))
        ins = {"workspace": [node]}
    else:
        kind, v = resolve(L, node)
        if kind != "node":
            return _relive(conn, [node], False, None, session, purpose, cwd, note, False)
        text, ins = show_node(L, v, conn=conn)
    text = track.deliver(conn, VERSION, "roam", {"roam": {"node": node, **_window_query()}}, text, ins, session=session, purpose=purpose, cwd=cwd, note=note)
    return text


def _latest(L, nid):
    """一个节点下面最近的话题时间（排序用，越近越大）。"""
    dates, todo = [], [nid]
    while todo:
        k = todo.pop()
        n = L.nodes[k]
        if n["kind"] == "entry":
            dates.append(n["date"])
        todo += L.kids[k]
    d = max(dates, default="")
    return int(re.sub(r"\D", "", d[:19]) or 0)
