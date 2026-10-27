"""删除（用户要求时照办，删除本身留一条记录）。2026-10-22 第一次用：tmp/cexp 下两段测试会话（i2、s1）。

删什么：会话和它的轮次、原文；只依赖它们的发生（act 的输入全落在要删的东西上；use 是这段会话自己的）和这些 act 生出的 Claim；
挂在删掉的话题、线上的挂线。混着别的会话的发生（批处理 act、申请、别的会话的检索）留着，它们指向删掉的东西，
由删除记录说明（check 认得，按条数列）。宿主的原文件不动；采集器认得删除记录，不再采回来。"""
import json

from . import IMMUTABLE, NA, _triggers, dumps, main_operator, now, occurrence
from .instance import USER_NAME

OCC_KINDS = ("turn", "session", "act", "use", "request", "deletion")
CLAIMS = ("event", "form_record", "judgment", "session_entry", "topic_line", "attachment", "cognition")
DETAILS = ("session_detail", "turn_detail", "act_detail", "use_detail", "request_detail")


def footprint(conn, sessions):
    """sessions：[(source, session)] → {"occurrence": set, 各 Claim 表: set}。"""
    occ = set()
    for src, ses in sessions:
        occ |= {r[0] for r in conn.execute("SELECT occurrence FROM session_detail WHERE source=? AND session=?", (src, ses))}
        occ |= {r[0] for r in conn.execute("SELECT occurrence FROM turn_detail WHERE source=? AND session=?", (src, ses))}
    names = {ses for _, ses in sessions}
    moved = {}                       # 更正过会话的 use（track / rebind）按更正算
    for inputs, ses in conn.execute("SELECT o.inputs, d.session FROM occurrence o JOIN use_detail d ON d.occurrence=o.id "
                                    "WHERE d.channel='track' AND d.mode='rebind' ORDER BY o.id"):
        moved.update({u: ses for u in json.loads(inputs).get("use", [])})
    own_use = {r[0] for r in conn.execute("SELECT occurrence, session FROM use_detail") if moved.get(r[0], r[1]) in names}
    claims = {t: set() for t in CLAIMS}
    rows = conn.execute("SELECT id, role, inputs, parent FROM occurrence").fetchall()
    changed = True
    while changed:
        changed = False
        for oid, role, inputs, parent in rows:
            if oid in occ:
                continue
            if role == "use":
                hit = oid in own_use
            else:
                refs = [(k, t) for k, ids in json.loads(inputs).items() if k in OCC_KINDS or k in claims for t in ids]
                refs += [("occurrence", parent)] if isinstance(parent, int) else []
                hit = bool(refs) and all(t in occ if k in OCC_KINDS or k == "occurrence" else t in claims[k] for k, t in refs)
            if hit:
                occ.add(oid)
                changed = True
        marks = ",".join(map(str, occ)) or "NULL"
        for t in CLAIMS:
            new = {r[0] for r in conn.execute(f"SELECT id FROM {t} WHERE born IN ({marks})")}
            if t == "attachment":           # 挂在删掉的话题、线上的挂线（可能由别的 act 写）
                new |= {r[0] for r in conn.execute("SELECT id, entry, line FROM attachment")
                        if r[1] in claims["session_entry"] or r[2] in claims["topic_line"]}
            if new - claims[t]:
                claims[t] |= new
                changed = True
    return {"occurrence": occ, **claims}


def delete_sessions(conn, sessions, reason, requested_by=USER_NAME, model=NA):
    """删几段会话（见模块说明），返回删除记录的编号。先留记录，再删；整个在一个事务里。"""
    if not reason.strip():
        raise ValueError("删除要写理由")
    fp = footprint(conn, sessions)
    texts = {r[0] for o in fp["occurrence"] for r in conn.execute("SELECT text FROM turn_detail WHERE occurrence=?", (o,))}
    target = {"sessions": [list(s) for s in sessions], **{k: sorted(v) for k, v in fp.items() if v}}
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        op = main_operator(conn, model)
        did = occurrence(conn, "deletion", op, started=now(), ended=now(), scale="session", cause="manual", outcome="ok")
        conn.execute("INSERT INTO deletion_detail VALUES(?,?,?,?)", (did, dumps(target), reason, requested_by))
        for t in IMMUTABLE:
            conn.execute(f"DROP TRIGGER IF EXISTS {t}_no_delete")
        marks = lambda ids: ",".join(map(str, ids)) or "NULL"     # noqa: E731
        o = marks(fp["occurrence"])
        for t in DETAILS:
            conn.execute(f"DELETE FROM {t} WHERE occurrence IN ({o})")
        conn.execute(f"DELETE FROM expression WHERE occurrence IN ({o})")
        conn.execute(f"DELETE FROM occurrence WHERE id IN ({o})")
        for t in CLAIMS:
            conn.execute(f"DELETE FROM {t} WHERE id IN ({marks(fp[t])})")
        conn.execute(f"DELETE FROM alias WHERE born IN ({o})")
        for tid in texts:
            if conn.execute("SELECT 1 FROM turn_detail WHERE text=?", (tid,)).fetchone() is None:
                conn.execute("DELETE FROM turn_text WHERE id=?", (tid,))
        for _, ses in sessions:
            conn.execute("DELETE FROM legacy WHERE key LIKE ?", (f"%:{ses}%",))
        for stmt in _triggers().split("\n"):
            conn.execute(stmt)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return did


def deleted(conn):
    """删除记录里删掉的东西：({(source, session)}, {种类: {编号}})，种类是 occurrence 或 Claim 表名。"""
    sessions, ids = set(), {}
    for (target,) in conn.execute("SELECT target FROM deletion_detail"):
        t = json.loads(target)
        sessions |= {tuple(s) for s in t.get("sessions", [])}
        for k, v in t.items():
            if k != "sessions":
                ids.setdefault(k, set()).update(v)
    return sessions, ids
