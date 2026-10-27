"""新库上的检查：三条不变量、字段取值、覆盖。只列事件和计数，不打分（只写一份快照文件）。
在 Engram 这一层（重构 A10）：不变量只看存储；覆盖要问各过程，按它们的对外接口查，不认过程内部的步骤。

1. 原文只追加：轮次的 ID 是内容哈希，复算对不上就是原文被改过；turn 要指向存在的原文。
2. 派生物带来源：每条 Claim 出生于一次成功的 act，act 的 Operator 已登记；引用（event 的轮次、记录的 refs、
   判断的依据、C 的成员和表述、发生的 inputs / parent / predecessor）都解析得到，而且不指向更晚的东西。
3. 只取代，不覆盖：关系、`from` 的两端都存在，新的晚于旧的，取代类不成环；E 的取代（点名申请）还要出处的轮次有重叠。
   范围申请的取代是推出来的，不在这里查。
4. 申请：只写给直接相邻的一层或同一过程里更高的一级（主会话除外），认领它的 act 属于接收的那一层。
另外：不可变的表（触发器挡着）与上一次检查的快照比对：按 1000 行一块记摘要，上次已有的行这次必须一字不差。
"""
import collections
import hashlib
import json
from pathlib import Path

from . import cognize, observe
from .store import delete
from .store import claimed as store_claimed
from .store import CLAIM_KINDS, MARKERS, REPLACING, UNKNOWN, absent, adjacent, consumed

SNAPSHOT = "engram-check-snapshot.json"
BLOCK = 1000
SNAPSHOT_TABLES = {      # 表 → (主键, 内容)
    "turn_text": ("rowid", "id || char(30) || user || char(30) || assistant"),
    "occurrence": ("id", "role || char(30) || started || char(30) || operator || char(30) || inputs || char(30) || outcome"),
    "act_detail": ("occurrence", "input || char(30) || output"),
    "event": ("id", "born || char(30) || turn || char(30) || text || char(30) || mode || char(30) || segments"),
    "event_relations": ("id", "relations"),     # 2026-10-21 加的列另记一份，event 已存的摘要不动（表名 → 实际的表见 SNAPSHOT_FROM）
    "form_record_relations": ("id", "relations"),
    "form_record": ("id", "born || char(30) || text || char(30) || refs"),
    "judgment": ("id", "born || char(30) || text || char(30) || holders || char(30) || basis || char(30) || relations"),
    "cognition": ("id", "born || char(30) || name || char(30) || statements || char(30) || members || char(30) || \"from\""),
    "operator": ("id", "name || char(30) || params"),
}
SNAPSHOT_FROM = {"event_relations": "event", "form_record_relations": "form_record"}
CLAIMS = CLAIM_KINDS


class Events:
    def __init__(self):
        self.n, self.eg = collections.Counter(), collections.defaultdict(list)

    def hit(self, kind, example, k=1):
        self.n[kind] += k
        if len(self.eg[kind]) < 3:
            self.eg[kind].append(example)

    def lines(self):
        out = []
        for kind, n in sorted(self.n.items()):
            eg = f"（例：{'、'.join(map(str, self.eg[kind]))}）" if self.eg[kind] else ""
            out.append(f"- {kind}：{n}{eg}")
        return out or ["- 没有发现事件"]


def check(conn, db_path, brief=False):
    """brief：只和快照比对、不改写快照，不列未知、没做、不适用的分布（整理循环给项目现状用，手动跑的 check 比对的基准不动）。"""
    ev = Events()
    occ = {r["id"]: (r["role"], r["operator"]) for r in conn.execute("SELECT id, role, operator FROM occurrence")}
    ops = {r[0] for r in conn.execute("SELECT id FROM operator")}
    acts_ok = {r[0] for r in conn.execute("SELECT id FROM occurrence WHERE role='act' AND outcome='ok'")}

    # 1. 原文只追加
    texts = 0
    for tid, user, assistant in conn.execute("SELECT id, user, assistant FROM turn_text"):
        texts += 1
        if hashlib.sha256((user + "\x1e" + assistant).encode("utf-8")).hexdigest()[:24] != tid:
            ev.hit("原文：轮次内容与 ID 对不上", tid)
    for oid, tid in conn.execute("SELECT occurrence, text FROM turn_detail WHERE text NOT IN (SELECT id FROM turn_text)"):
        ev.hit("原文：turn 指向不存在的原文", oid)

    # 2. 派生物带来源
    for oid, (role, op) in occ.items():
        if op not in ops:
            ev.hit("来源：发生的 Operator 没登记", oid)
    born = {}
    for table in CLAIMS:
        for cid, b in conn.execute(f"SELECT id, born FROM {table}"):
            born[(table, cid)] = b
            if b not in occ or occ[b][0] != "act":
                ev.hit(f"来源：{table} 的出生不是一次 act", cid)
            elif b not in acts_ok:
                ev.hit(f"来源：{table} 出生于一次失败的 act", cid)
    for eid, turn in conn.execute("SELECT id, turn FROM event"):
        if absent(turn):
            ev.hit(f"来源：event 的轮次是 {turn}", eid)
        elif occ.get(turn, ("",))[0] != "turn":
            ev.hit("来源：event 指向的轮次不存在", eid)

    roles = {"turn", "session", "act", "use", "request", "deletion"}
    alias = {"reread": "act"}                              # 重读的输入指向旧的读 act（2026-10-26）
    gone = delete.deleted(conn)[1]                        # 按要求删掉的（store.delete）：留下的发生、记录指向它们不算事件，按条数列
    def is_gone(kind, t):
        return t in gone.get("occurrence" if kind in roles else kind, ())

    def refs_ok(kind, cid, refs, what):
        for target_kind, ids in refs.items():
            for t in ids:
                tb = born.get((target_kind, t))
                if tb is None and is_gone(target_kind, t):
                    ev.n["指向按要求删掉的（条数）"] += 1
                elif tb is None:
                    ev.hit(f"来源：{what}指向不存在的 {target_kind}", f"{kind} {cid} → {t}")
                elif tb > born[(kind, cid)]:
                    ev.hit(f"来源：{what}指向更晚出生的 {target_kind}", f"{kind} {cid} → {t}")

    unresolved = collections.Counter()
    for rid, kind, refs, lost in conn.execute("SELECT id, kind, refs, unresolved FROM form_record"):
        refs_ok("form_record", rid, json.loads(refs), "记录的引用")
        if lost != "[]":
            unresolved[f"{'逐轮' if kind == 'turn' else '会话'}记录"] += 1
    for jid, basis, lost in conn.execute("SELECT id, basis, unresolved FROM judgment"):
        b = json.loads(basis)
        refs_ok("judgment", jid, b, "判断的依据")
        if not any(b.values()):
            ev.hit("来源：判断没有依据", f"J{jid}")
        if lost != "[]":
            unresolved["判断"] += 1
    unresolved["event"] = conn.execute("SELECT COUNT(*) FROM event WHERE unresolved!='[]'").fetchone()[0]
    for k, n in unresolved.items():
        if n:
            ev.n[f"记下的引用未命中（{k}，条数）"] += n

    for r in conn.execute("SELECT id, role, inputs, parent, predecessor FROM occurrence"):
        for kind, ids in json.loads(r["inputs"]).items():
            kind = alias.get(kind, kind)
            for t in ids:
                if absent(t):
                    ev.hit(f"来源：发生的输入里有 {t}", r["id"])
                elif (kind in roles and t not in occ or kind not in roles and (kind, t) not in born) and is_gone(kind, t):
                    ev.n["指向按要求删掉的（条数）"] += 1
                elif kind in roles:
                    if occ.get(t, ("",))[0] != kind:
                        ev.hit(f"来源：发生的输入指向不存在的 {kind}", f"{r['id']} → {t}")
                elif (kind, t) not in born:
                    ev.hit(f"来源：发生的输入指向不存在的 {kind}", f"{r['id']} → {t}")
                elif born[(kind, t)] > r["id"]:
                    ev.hit(f"来源：发生的输入指向更晚出生的 {kind}", f"{r['id']} → {t}")
        for col in ("parent", "predecessor"):
            v = r[col]
            if absent(v):
                continue
            if v not in occ and v in gone.get("occurrence", ()):
                ev.n["指向按要求删掉的（条数）"] += 1
            elif v not in occ:
                ev.hit(f"来源：{col} 指向不存在的发生", r["id"])
            elif col == "predecessor" and (occ[v][0] != r["role"] or v >= r["id"]):
                ev.hit("来源：前身不是更早的同类发生", f"{r['id']} → {v}")

    # 3. 只取代，不覆盖
    jop = {jid: occ.get(b, (None, None))[1] for (t, jid), b in born.items() if t == "judgment"}
    edges, rels, cross = collections.defaultdict(set), 0, collections.Counter()
    for jid, relations in conn.execute("SELECT id, relations FROM judgment"):
        for rel in json.loads(relations):
            rels += 1
            to, kind = rel["to"], rel["kind"]
            if to not in jop:
                ev.hit("取代：关系另一端的判断不存在", f"J{jid}→J{to}")
                continue
            if born[("judgment", to)] > born[("judgment", jid)] or (to >= jid and kind != "same"):
                ev.hit("取代：新判断不晚于旧判断", f"J{jid} {kind} J{to}")
            if jop[to] != jop[jid]:
                cross[kind] += 1
            if kind in REPLACING:
                edges[jid].add(to)
    for kind, n in cross.items():
        ev.n[f"关系跨了 Operator（{kind}）"] += n
    for cid, frm in conn.execute('SELECT id, "from" FROM cognition'):
        for p in json.loads(frm):
            if ("cognition", p) not in born:
                ev.hit("取代：C 的 from 指向不存在的节点", f"C{cid}→C{p}")
            elif p >= cid:
                ev.hit("取代：C 的 from 不早于自己", f"C{cid}→C{p}")
            else:
                edges[("C", cid)].add(("C", p))
    R = cognize.Refs(conn) if conn.execute("SELECT 1 FROM cognition LIMIT 1").fetchone() else None
    for cid, members, statements in conn.execute("SELECT id, members, statements FROM cognition"):
        refs = json.loads(members) + [r for s in json.loads(statements) for r in s.get("refs", [])]
        for r in refs:
            if not cognize.resolves(conn, r, R):
                ev.hit("引用：C 的成员或表述解析不到", f"C{cid}→{r.get('kind')}:{r.get('id')}")
    eturn = {}
    for eid, turn in conn.execute("SELECT e.id, d.text FROM event e JOIN turn_detail d ON d.occurrence=e.turn"):
        eturn[eid] = turn
    for eid, born_act, relations in conn.execute("SELECT id, born, relations FROM event WHERE relations!='[]'"):
        window = {t for (t,) in conn.execute("SELECT d.text FROM occurrence o, json_each(o.inputs, '$.turn') j "
                                             "JOIN turn_detail d ON d.occurrence=j.value WHERE o.id=?", (born_act,))}
        for rel in json.loads(relations):
            rels += 1
            to = rel["to"]
            if ("event", to) not in born:
                ev.hit("取代：关系另一端的 E 不存在", f"E{eid}→E{to}")
                continue
            if born[("event", to)] >= born_act or to >= eid:
                ev.hit("取代：新 E 不晚于旧 E", f"E{eid} {rel['kind']} E{to}")
            if eturn.get(to) not in window:
                ev.hit("取代：旧 E 的出处不在新 E 读的那几轮里", f"E{eid}→E{to}")
            if rel["kind"] == "supersedes":
                edges[("E", eid)].add(("E", to))
    for cyc in _cycles(edges):
        ev.hit("取代：取代链成环", "→".join(map(str, cyc)))

    for rid, born_act, relations in conn.execute("SELECT id, born, relations FROM form_record WHERE relations!='[]'"):
        for rel in json.loads(relations):
            rels += 1
            to = rel["to"]
            if ("form_record", to) not in born:
                ev.hit("取代：关系另一端的陈述不存在", f"{rid}→{to}")
            elif born[("form_record", to)] >= born_act or to >= rid:
                ev.hit("取代：新陈述不晚于旧陈述", f"{rid}→{to}")
            else:
                edges[("S", rid)].add(("S", to))
    for cyc in _cycles({k: v for k, v in edges.items() if isinstance(k, tuple) and k[0] == "S"}):
        ev.hit("取代：取代链成环", "→".join(map(str, cyc)))

    # 4. 申请
    act_process = dict(conn.execute("SELECT occurrence, process FROM act_detail"))
    for rid, frm, to, inputs in conn.execute("SELECT d.occurrence, d.from_process, d.to_process, o.inputs FROM request_detail d "
                                             "JOIN occurrence o ON o.id=d.occurrence"):
        if not adjacent(frm, to):
            ev.hit("申请：没写给直接相邻的一层", f"{rid} {frm}→{to}")
        for a, _ in store_claimed(conn, rid):
            if act_process.get(a) != to:
                ev.hit("申请：认领它的 act 不属于接收的那一层", f"{rid} ← {a}")

    # 字段取值：不用 NULL；三种标记各有多少（只是分布，不是事件）
    marks = []
    for table in ("occurrence", "session_detail", "turn_detail", "act_detail", "use_detail", "event", "form_record",
                  "judgment", "cognition", "operator"):
        cols = [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]
        for c in cols:
            n_null = conn.execute(f'SELECT COUNT(*) FROM {table} WHERE "{c}" IS NULL').fetchone()[0]
            if n_null:
                ev.hit(f"取值：{table}.{c} 有 NULL", n_null, n_null)
            counts = dict(conn.execute(f'SELECT "{c}", COUNT(*) FROM {table} WHERE "{c}" IN (?,?,?) GROUP BY 1',
                                       MARKERS).fetchall())
            if counts:
                marks.append(f"  {table}.{c}：" + "，".join(f"{k} {v}" for k, v in sorted(counts.items())))

    # 与上次快照比对
    path = Path(db_path).parent / SNAPSHOT
    old = json.loads(path.read_text()) if path.exists() else {}
    now = {}
    for table, (key, expr) in SNAPSHOT_TABLES.items():
        prev = old.get(table, {})
        full = collections.defaultdict(lambda: [0, -1, hashlib.sha256()])     # 这次的全部行
        part = collections.defaultdict(lambda: [0, hashlib.sha256()])         # 其中上次已有的行
        src = SNAPSHOT_FROM.get(table, table)
        for k, v in conn.execute(f"SELECT {key}, {expr} FROM {src} ORDER BY {key}"):
            blk, data = str(k // BLOCK), (v or "").encode("utf-8") + b"\x1f"
            f = full[blk]
            f[0], f[1] = f[0] + 1, k
            f[2].update(data)
            if blk in prev and k <= prev[blk][1]:
                part[blk][0] += 1
                part[blk][1].update(data)
        for blk, (n, last, digest) in prev.items():
            if part[blk][0] != n or part[blk][1].hexdigest() != digest:
                ev.hit(f"覆盖：{table} 里上次有的行被改过或不见了", f"第 {blk} 块")
        now[table] = {blk: [n, last, h.hexdigest()] for blk, (n, last, h) in full.items()}
    if not brief:
        path.write_text(json.dumps(now))

    counts = {r[0]: r[1] for r in conn.execute("SELECT role, COUNT(*) FROM occurrence GROUP BY 1")}
    claims = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in CLAIMS}
    head = (f"检查了 {texts} 份原文；发生 " + "、".join(f"{k} {v}" for k, v in sorted(counts.items()))
            + "；Claim " + "、".join(f"{k} {v}" for k, v in claims.items()) + f"；关系 {rels} 条"
            + ("；上次快照已比对" if old else "；第一次检查，已存下快照，下次起比对"))
    return "\n".join([head, ""] + ev.lines() + ["", "覆盖（哪一层静默丢了东西）："] + coverage(conn)
                     + ([] if brief else ["", "未知、没做、不适用（分布）："] + (marks or ["  无"])))


def _cycles(edges):
    seen, onstack, stack, out = set(), set(), [], []

    def dfs(v):
        seen.add(v)
        stack.append(v)
        onstack.add(v)
        for w in edges.get(v, ()):
            if w in onstack:
                out.append(stack[stack.index(w):] + [w])
            elif w not in seen:
                dfs(w)
        stack.pop()
        onstack.discard(v)

    for v in list(edges):
        if v not in seen:
            dfs(v)
    return out


def coverage(conn):
    """逐个过程按接口查有没有静默丢掉的，只列计数和例子：
    observe（progress）：每个结束了的轮次都有 observe 的进度；失败满次数的进了待重做清单，待观测的单列；
    form：交给 form 的 E（observe.frame）有没有被某次 form 读过（store.consumed：成功的 form act 的 inputs 里有它）。"""
    ev = Events()
    where = {t: f"{s[:20]}#{o + 1}" for t, s, o in conn.execute("SELECT occurrence, session, ordinal FROM turn_detail")}
    for p in sorted(observe.progress(conn), key=lambda p: p["turn"]):
        turn, d = p["turn"], p["state"]
        if d == "failed":
            ev.hit("observe：失败满次数（进待重做清单）", where[turn])
        elif d in observe.TO_OBSERVE:
            ev.hit(f"observe：待观测（{observe.STATES[d]}）", where[turn])
        else:
            ev.n[f"observe：{observe.STATES[d]}（轮次，不是事件）"] += 1
    handed = observe.frame(conn)
    read = consumed(conn, "form")
    unread = collections.Counter()
    waiting = 0
    for x in handed["events"]:
        if x["id"] not in read:
            if x.get("step") == "correct":       # 纠正写的：等修订提示被用到才进 form（lazy），不是静默丢失
                waiting += 1
            else:
                unread[(x["source"], x["session"])] += 1
    for (source, session), n in sorted(unread.items(), key=lambda kv: -kv[1]):
        ev.hit("form：交给 form 的 E 还没被读过（条数）", f"{source}:{session[:20]} {n} 条", n)
    if waiting:
        ev.n["form：纠正写的 E，等修订提示被用到（条数，不是事件）"] += waiting
    gone = observe.superseded(conn)
    if gone:
        ev.n["observe：按申请被取代的 E（不再交出，留在那一轮上；条数，不是事件）"] += len(gone)
    if handed["withdrawn"]:
        ev.n["form：被 form 读过、又因开关撤下的 E（条数，不是事件）"] += len(handed["withdrawn"])
    from .form import history                                    # 只算现在用着的那次 pack 写的（重做取代的旧陈述还留在库里）
    current = {a for (step, _), a in history(conn).ok.items() if step == "pack"}
    for act, meta in conn.execute("SELECT occurrence, meta FROM act_detail WHERE process='form' AND step='pack' AND meta LIKE '%\"violations\"%'"):
        if act not in current:
            continue                                             # 已经被重做取代的，问题不在了（2026-10-27）
        for n, why in json.loads(meta).get("violations", []):    # pack-v0.4 起：重写一次后还不合规的（2026-10-27）
            ev.hit("form：陈述的来源重写后仍不合 pack 的检查", f"act {act} {f'陈述{n}' if n else '整份'}：{why[:40]}")
    big = sum(1 for born, r in conn.execute("SELECT born, refs FROM form_record") if born in current and len(json.loads(r).get("event", [])) > 16)
    if big:
        ev.n["form：现行陈述的来源超过两页（16 条；条数，不是事件，pack-v0.4 以前写的多）"] += big
    return ev.lines()
