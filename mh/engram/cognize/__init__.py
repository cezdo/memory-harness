"""cognize：form 的产物 → cognition（design/engram/cognize/设计.md）。D1a（2026-10-21）：数据结构与程序接口，纯程序，还不调模型。

- C 是会话树上的快捷节点：不是每件事都有。成员和引用写“身份加钉住的版本”（D1a′，2026-10-22；设计“引用：身份加钉住的版本”）：
  {"kind", "id": 钉住的版本, 身份…}，种类见 KINDS；成员另带 rel（part / instance）。身份：陈述 session（[来源, 会话]）+ seq（S 序号，
  旧系统导入的陈述没有，按编号冻结）；话题 session；骨架节点 workspace + path，id 是骨架的版本（skeleton.version）；线、C 的身份是
  from 链，旧 J 不变，这三种只写 id。pin 由编号（骨架给 k-…）写出完整引用；now 按身份找现在的版本，fresh 比一比。
- 节点不可变：更新就是写后继（from 空 = 新生，一个 = 接替，多个 = 合并）。没被 from 指向的是现状（current），身份是 from 链（chain）。
- 候选与已验证（2026-10-21：两者结合）：不设字段，按出生的 act 推出（verified）：主会话的 act 写的是已验证，别的是候选。
  主会话确认（confirm）就是写一个后继。骨架算已验证的 C，留在原处（form 的骨架），C 把骨架节点当成员引用（kind = skeleton）。
- 名字能直接当句柄（lookup）：现状里的名字和别名不重名（不分大小写）。别名只追加（alias 表）。
"""
import json

from .. import store as st
from ..store import NA, dumps, occurrence
from ..store.instance import USER_NAME

KINDS = {"form_record": "form_record", "session_entry": "session_entry", "topic_line": "topic_line",
         "cognition": "cognition", "judgment": "judgment", "skeleton": None}     # 引用的种类 → 表；skeleton 查骨架
RELS = ("part", "instance")              # 整体→部分、类→实例
KIND = ("concrete", "abstract")          # 具体 / 抽象
STATES = ("进行中", "搁置", "结束")


def _norm(text):
    return " ".join(text.split()).casefold()


class Refs:
    """解析引用要的对照（读一次库，写入、检查一个 C 的全部引用共用）。成员、表述引用的东西都在 form：陈述的序号、会话现在的入口、
    线的后继、骨架和它的版本，都由 form 提供。"""

    def __init__(self, conn):
        from ..form import skeleton     # form 提供的接口
        from .. import form
        self.conn, self.skeleton, self.form = conn, skeleton, form
        self.swap = form.replacements(conn)
        self.seq, self.at, self.sess = form.statement_seq(conn, self.swap)
        self.entry_now = form.latest_entries(conn)
        registry = skeleton.adopted(conn)
        self.kid, self.sk_now = {}, {}                  # k-… → (工作区, 路径)；工作区 → 现在的版本
        for ws in dict.fromkeys([*skeleton.WORKSPACES, *registry]):
            ns = skeleton.nodes(ws, conn, registry) or {}
            self.kid.update({n["id"]: (ws, p) for p, n in ns.items()})
            if ns:
                self.sk_now[ws] = skeleton.version(ws, conn, registry)
        self.nodes = set(self.kid.values())

    def row(self, kind, rid):
        return isinstance(rid, int) and self.conn.execute(f"SELECT 1 FROM {KINDS[kind]} WHERE id=?", (rid,)).fetchone() is not None


def _refs(conn, R):
    return R if R is not None else Refs(conn)


def pin(conn, kind, x, R=None):
    """写一条引用：钉住现在看到的这一版。x 是编号（陈述、话题、线、C、J），骨架给节点句柄 k-…。解析不到就 ValueError。"""
    R = _refs(conn, R)
    if kind == "skeleton":
        if x not in R.kid:
            raise ValueError(f"没有这个骨架节点：{x}")
        ws, path = R.kid[x]
        return {"kind": kind, "id": R.sk_now[ws], "workspace": ws, "path": path}
    if kind not in KINDS or not R.row(kind, x):
        raise ValueError(f"解析不到：{kind}:{x}")
    ref = {"kind": kind, "id": x}
    if kind == "form_record" and x in R.sess:
        ref.update(session=list(R.sess[x]), seq=R.seq[x])
    if kind == "session_entry":
        ref["session"] = list(conn.execute("SELECT source, session FROM session_entry WHERE id=?", (x,)).fetchone())
    return ref


def resolves(conn, ref, R=None):
    """一条引用解析得到吗：钉住的版本存在，身份和它对得上（钉住的陈述确实是那段会话的那个 S 序号，等等）。"""
    R = _refs(conn, R)
    kind, rid = ref.get("kind"), ref.get("id")
    if kind not in KINDS:
        return False
    if kind == "skeleton":
        ws, path = ref.get("workspace"), ref.get("path")
        if rid is None or not isinstance(path, str):
            return False
        if rid == R.sk_now.get(ws):                     # 现在这一版：看现在的节点
            return (ws, path) in R.nodes
        return R.skeleton.had(ws, path, rid, conn)
    if not R.row(kind, rid):
        return False
    if kind == "form_record":
        if rid not in R.sess:
            return "session" not in ref and "seq" not in ref
        return ref.get("session") == list(R.sess[rid]) and ref.get("seq") == R.seq[rid]
    if kind == "session_entry":
        return ref.get("session") == list(conn.execute("SELECT source, session FROM session_entry WHERE id=?", (rid,)).fetchone())
    return True


def _heads(conn, cid):
    after = {}
    for nid, frm in conn.execute('SELECT id, "from" FROM cognition WHERE "from"!=\'[]\''):
        for p in json.loads(frm):
            after.setdefault(p, []).append(nid)
    out, seen, todo = set(), set(), [cid]
    while todo:
        x = todo.pop()
        if x in seen:
            continue
        seen.add(x)
        if after.get(x):
            todo.extend(after[x])
        else:
            out.add(x)
    return sorted(out)


def now(conn, ref, R=None):
    """按身份找现在的版本：[版本…]。一般一个；陈述撤下了、骨架里没有这个节点了是 []；线、C 被拆开（不同后继）可能几个。"""
    R = _refs(conn, R)
    kind, rid = ref["kind"], ref["id"]
    if kind == "form_record":
        if "session" not in ref:
            return [rid]
        cur = R.at.get((tuple(ref["session"]), ref["seq"]))
        return [] if cur is None or (cur in R.swap and R.swap[cur] is None) else [cur]
    if kind == "session_entry":
        cur = R.entry_now.get(tuple(ref["session"]))
        return [cur] if cur is not None else []
    if kind == "topic_line":
        return R.form.line_heads(conn, rid)
    if kind == "cognition":
        return _heads(conn, rid)
    if kind == "skeleton":
        ws = ref["workspace"]
        return [R.sk_now[ws]] if (ws, ref["path"]) in R.nodes else []
    return [rid]


def fresh(conn, ref, R=None):
    """新鲜吗：现在的版本就是钉住的那一版。不新鲜就是下一级变了，等这个 C 被用到时给它写修订提示（设计“节点结构”第 4 条）。"""
    return now(conn, ref, R) == [ref["id"]]


def current(conn):
    """现状：没被任何 from 指向的 C 编号，按编号排。"""
    pointed = {p for (frm,) in conn.execute('SELECT "from" FROM cognition') for p in json.loads(frm)}
    return [cid for (cid,) in conn.execute("SELECT id FROM cognition ORDER BY id") if cid not in pointed]


def chain(conn, cid):
    """身份：从这个节点沿 from 往回走到的全部节点（含自己），按编号排。"""
    seen, todo = set(), [cid]
    while todo:
        c = todo.pop()
        if c in seen:
            continue
        seen.add(c)
        row = conn.execute('SELECT "from" FROM cognition WHERE id=?', (c,)).fetchone()
        if row:
            todo.extend(json.loads(row[0]))
    return sorted(seen)


def verified(conn, cid):
    """已验证还是候选：出生在主会话的 act 上就是已验证。"""
    row = conn.execute("SELECT p.process FROM cognition c JOIN occurrence o ON o.id=c.born JOIN operator p ON p.id=o.operator "
                       "WHERE c.id=?", (cid,)).fetchone()
    if row is None:
        raise ValueError(f"没有这个节点：C{cid}")
    return row[0] == st.MAIN


def names(conn, cids=None):
    """{规范化的名字或别名: C 编号}，只看现状（或给定的几个节点）。别名挂在身份上：链里任何一个节点的别名都算现状的。"""
    out = {}
    for cid in (cids if cids is not None else current(conn)):
        ids = chain(conn, cid)
        out[_norm(conn.execute("SELECT name FROM cognition WHERE id=?", (cid,)).fetchone()[0])] = cid
        for (text,) in conn.execute(f"SELECT text FROM alias WHERE cognition IN ({','.join('?' * len(ids))})", ids):
            out.setdefault(_norm(text), cid)
    return out


def lookup(conn, text):
    """按名字或别名查现状的节点（不经模型）；查不到返回 None。"""
    return names(conn).get(_norm(text))


def get(conn, cid):
    r = conn.execute('SELECT * FROM cognition WHERE id=?', (cid,)).fetchone()
    if r is None:
        raise ValueError(f"没有这个节点：C{cid}")
    return {"id": r["id"], "born": r["born"], "kind": r["kind"], "name": r["name"], "statements": json.loads(r["statements"]),
            "members": json.loads(r["members"]), "from": json.loads(r["from"]), "state": r["state"],
            "window": json.loads(r["window"]) if r["window"].startswith("{") else r["window"]}


def write(conn, act, kind, name, statements, members, from_=(), state=NA, window=NA):
    """写一个 C，出生于 act。检查：种类、成员（种类、rel、能解析；抽象的至少两个）、表述（每句带引用、引用能解析）、
    from（指向现状的节点）、名字（和现状里别的节点的名字、别名不重名；接替的那几个不算）。返回编号；不对就 ValueError，不写。"""
    if kind not in KIND:
        raise ValueError(f"种类只能是 {KIND}：{kind}")
    if not name.strip():
        raise ValueError("要有名字")
    if state not in STATES and not st.absent(state):
        raise ValueError(f"状态只能是 {STATES} 或标记：{state}")
    R = Refs(conn)
    if not members:
        raise ValueError("要有成员")
    for m in members:
        if m.get("rel") not in RELS:
            raise ValueError(f"成员要带种类 {RELS}：{m}")
        if not resolves(conn, m, R):
            raise ValueError(f"成员解析不到：{m}")
    if kind == "abstract" and len(members) < 2:
        raise ValueError("抽象 C 至少要两个成员")
    if not statements:
        raise ValueError("要有表述")
    for s in statements:
        if not s.get("text", "").strip() or not s.get("refs"):
            raise ValueError(f"每句表述要带引用：{s}")
        bad = [r for r in s["refs"] if not resolves(conn, r, R)]
        if bad:
            raise ValueError(f"表述的引用解析不到：{bad}")
    now = set(current(conn))
    stale = [p for p in from_ if p not in now]
    if stale:
        raise ValueError(f"from 只能指向现状的节点：{['C%d' % p for p in stale]}")
    clash = names(conn, [c for c in now if c not in from_]).get(_norm(name))
    if clash is not None:
        raise ValueError(f"「{name}」和 C{clash} 重名")
    return conn.execute('INSERT INTO cognition(born, kind, name, statements, members, "from", state, window) VALUES(?,?,?,?,?,?,?,?)',
                        (act, kind, name, dumps(statements), dumps(members), dumps(sorted(from_)), state,
                         window if isinstance(window, str) else dumps(window))).lastrowid


def alias(conn, act, cid, text):
    """给一个现状的节点加别名（只追加）。和现状里别的名字、别名重名就拒绝。"""
    if cid not in current(conn):
        raise ValueError(f"C{cid} 不是现状：给它的后继加")
    clash = names(conn).get(_norm(text))
    if clash is not None and clash != cid:
        raise ValueError(f"「{text}」和 C{clash} 重名")
    return conn.execute("INSERT INTO alias(born, cognition, text) VALUES(?,?,?)", (act, cid, text)).lastrowid


def confirm(conn, operator_id, cid, reason, holder=USER_NAME, **change):
    """主会话确认一个现状的节点：写一个后继，出生在主会话的 act 上（cause = manual），就是已验证。change 可以改 name、statements、
    members、state、window；没给的沿用。operator_id 要是主会话的 Operator（st.main_operator）。返回新节点的编号。"""
    if conn.execute("SELECT process FROM operator WHERE id=?", (operator_id,)).fetchone()[0] != st.MAIN:
        raise ValueError("确认只能由主会话写")
    if not reason.strip():
        raise ValueError("确认要写理由")
    old = get(conn, cid)
    bad = set(change) - {"name", "statements", "members", "state", "window"}
    if bad:
        raise ValueError(f"不能改：{sorted(bad)}")
    act = occurrence(conn, "act", operator_id, started=st.now(), ended=st.now(), scale="cognition",
                     inputs={"cognition": [cid]}, cause="manual", outcome="ok")
    conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                 (act, "cognize", "confirm", dumps({"cognition": cid}), dumps(change), "",
                  dumps({"reason": reason, "holder": holder})))
    new = write(conn, act, old["kind"], change.get("name", old["name"]), change.get("statements", old["statements"]),
                change.get("members", old["members"]), from_=[cid], state=change.get("state", old["state"]),
                window=change.get("window", old["window"]))
    conn.commit()
    return new
