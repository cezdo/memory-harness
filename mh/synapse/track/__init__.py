"""track：足迹（design/synapse/设计.md“track”）。换库（重构 E1a，2026-10-19）起每次拉取在新库记一条 use；
换库之前写进旧库的 deliveries、reads 补成了 use（Operator legacy/synapse-…；补的代码随 E4 归档旧库删了，见 git 历史）。只追加，不改已有的行。"""
import json
import re

from ... import express
from ...engram.store import instance
from ...engram import store as st
from ...engram.store import NA, UNKNOWN, dumps

# 各版 Synapse 对外的句柄写法（use 的 shown、query、meta 里出现的句柄按这个认；表只追加，旧行不改，按 Operator 的版本区分）。
# v0.8 只在 2026-10-19 下午用了几个小时（新库里 3 条足迹：1 次 dev 查询、2 次只记录的联想），那时线写 T3、陈述 T3:S1、话题 s-5；
# v0.9 起线 L3、话题 T5、陈述 L3:S1（2026-10-19）；v0.11 起话题带线 L3:T5、陈述带线和话题 L3:T5:S1，陈述按会话编号，
# 没挂上线写 L?（2026-10-19）。对话原文里的旧写法不管。
HANDLE_STYLE = {"v0.8": {"line": "T3", "topic": "s-5", "statement": "T3:S1"},
                "v0.9": {"line": "L3", "topic": "T5", "statement": "L3:S1"},
                "v0.10": {"line": "L3", "topic": "T5", "statement": "L3:S1"},
                "v0.11": {"line": "L3", "topic": "L3:T5", "statement": "L3:T5:S1"},
                "v0.12": {"line": "L3", "topic": "L3:T5", "statement": "L3:T5:S1"}}
CAUSES = {"interactive": "conversation", "prefetch": "new_text", "replay": "manual", "dev": "manual"}


def operator(conn, version):
    rule = express.signature()
    return st.operator(conn, f"synapse/{version}.express-{rule['version']}-{rule['rules_hash'][:8]}", "synapse", "synapse", model=NA, prompt=NA, prompt_hash=NA,
                       isolation=NA, window=NA, params={"version": version, "expression": rule})


def _session(conn, session):
    """会话编号 → (这段会话的 Occurrence, 宿主)；还没采集进来或对不上唯一一段，写标记。"""
    rows = conn.execute("SELECT occurrence, source FROM session_detail WHERE session=?", (session or "",)).fetchall()
    return (rows[0][0], rows[0][1]) if len(rows) == 1 else (NA if not rows else UNKNOWN, UNKNOWN)


BORN = {"session_entry": "session_entry", "topic_line": "topic_line"}    # 不是 Claim 的产物：inputs 里记写出它的那次 act


def _split(conn, got):
    """取到的 → (inputs, 看到的句柄)。inputs 只写 Claim（E、陈述、J……）和 act：会话入口、话题线照 form 的写法
    记成写出它的 act（mh engram check 按这个查来源），骨架、工作区不进 inputs；具体给看了哪些另记在 meta 里。"""
    inputs, handles = {}, {}
    for kind, ids in got.items():
        if kind in BORN:
            handles[kind] = ids
            for i in ids:
                r = conn.execute(f"SELECT born FROM {BORN[kind]} WHERE id=?", (i,)).fetchone()
                if r and r[0] not in inputs.setdefault("act", []):
                    inputs["act"].append(r[0])
        elif kind in ("skeleton", "workspace"):
            handles[kind] = ids
        elif ids:
            inputs[kind] = ids
    return inputs, handles


def use(conn, version, mode, query, shown, got, session="", purpose="interactive", cwd="", note="", channel="pull",
        meta=None, format_shown=None):
    """记一次拉取（或推送）。got：{"<种类>": [编号…]}，取到或送达的是哪些（会话入口、话题线、骨架也可以，见 _split）。
    写完就提交，不留写事务（观察、形成也在写这个库）。没带会话编号的交互调用记为 dev（开发调试，不算使用；照旧的足迹）。"""
    if purpose == "interactive" and not session:
        purpose = "dev"
    parent, host = _session(conn, session)
    inputs, handles = _split(conn, got)
    meta = dict(meta or {})
    t = st.now()
    oid = st.occurrence(conn, "use", operator(conn, version), started=t, ended=t, scale="turn", inputs=inputs,
                        cause=CAUSES.get(purpose, UNKNOWN), parent=parent, outcome="ok")
    shown = format_shown(oid) if format_shown else shown
    meta["expression"] = express.evidence(meta.get("expression") or instance.expression(purpose), shown)
    conn.execute("INSERT INTO use_detail VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (oid, channel, mode, purpose, host, session or UNKNOWN, cwd or UNKNOWN, NA, dumps(query), shown,
                  note or NA, dumps(dict(meta or {}, **({"handles": handles} if handles else {})))))
    conn.commit()
    return oid


def deliver(conn, version, mode, query, text, got, *, session="", purpose="interactive", cwd="", note="", channel="pull", meta=None, raw=False):
    """最终交付与 shown 使用同一个字符串；原来源内容和句柄不变。"""
    ctx = instance.expression("standing" if mode == "assist" else "memory" if purpose in ("interactive", "prefetch", "dev") else purpose)
    shown = express.present(text, ctx, raw=raw)
    info = dict(meta or {}, expression={**express.evidence(ctx, shown), "source_raw": raw})
    final = []
    def finish(oid):
        final.append(shown + f"\n\n〔送达 use {oid}〕" if shown and ctx["purpose"] in ("memory", "standing") else shown)
        return final[0]
    use(conn, version, mode, query, shown, got, session=session, purpose=purpose, cwd=cwd, note=note, channel=channel, meta=info, format_shown=finish)
    return final[0]


def presentation_feedback(conn, version, target, reason, *, session="", cwd="", note=""):
    """呈现反馈只追加 use；来源 Claim 和已有送达保持原样。"""
    if isinstance(target, bool) or not isinstance(target, int) or target <= 0 or not reason.strip():
        raise ValueError("呈现反馈需要有效的 use 编号与理由")
    row = conn.execute("SELECT o.inputs, o.operator, d.shown, d.meta FROM occurrence o JOIN use_detail d ON d.occurrence=o.id WHERE o.id=?", (target,)).fetchone()
    if row is None:
        raise ValueError("找不到这条送达记录")
    shown = f"已记录对 use {target} 的呈现反馈；来源记录保留。"
    detail = {"scope": "presentation", "target_use": target, "reason": reason,
              "target_operator": row["operator"], "target_text_hash": express.evidence({}, row["shown"])["text_hash"]}
    got = json.loads(row["inputs"])
    got["use"] = [target]
    use(conn, version, "feedback", {"feedback": detail}, shown, got, session=session, cwd=cwd, note=note,
        channel="feedback", meta={"feedback": detail, "expression": express.evidence(instance.expression("feedback"), shown)})
    return shown


INTERNAL = {"label": "retrieve.word_hits", "attach": "retrieve.search"}    # form 的哪一步、用的是哪个检索


def _kinds(handles):
    """form 的句柄（L3、T5、k-…，已经按 form.handle_of 换成现在的写法）→ {"<种类>": [编号…]}，给 _split。"""
    got = {}
    for h in handles:
        if h.startswith("k-"):
            got.setdefault("skeleton", []).append(h)
        elif h[:1] in ("L", "T") and h[1:].isdigit():
            got.setdefault("topic_line" if h[0] == "L" else "session_entry", []).append(int(h[1:]))
    return got


def internal(conn, act, step, handles, session, purpose="form"):
    """Engram 内部的一次拉取记成 use（design/synapse/设计.md“Engram 内部的拉取”）：form 起名时查相近的话题（label）、
    挂线时查候选（attach）。parent 是那次 act，Operator 照那次 act 的，时刻取 act 开始的时刻（检索在调模型之前）。不提交，由调用方提交。"""
    op, started = conn.execute("SELECT operator, started FROM occurrence WHERE id=?", (act,)).fetchone()
    inputs, hs = _split(conn, _kinds(handles))
    oid = st.occurrence(conn, "use", op, started=started, ended=started, scale="session", inputs=inputs, cause="process",
                        parent=act, outcome="ok")
    conn.execute("INSERT INTO use_detail VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                 (oid, "pull", "evoke", purpose, NA, session or UNKNOWN, NA, NA, dumps({"step": step, "by": INTERNAL[step]}),
                  NA, NA, dumps({"handles": hs} if hs else {})))
    return oid


def backfill_internal(conn):
    """2026-10-20 以前 form 内部的拉取没记 use：从 label、attach 的 act 的 meta（near、picks）补。按 parent 去重，可以重复跑；
    meta 里没有这一项的 act 不补。返回补了几条。"""
    from ...engram.form import handle_of
    done = {str(r[0]) for r in conn.execute("SELECT o.parent FROM occurrence o JOIN use_detail d ON d.occurrence=o.id "
                                            "WHERE o.role='use' AND d.purpose='form'")}
    n = 0
    for act, step, window, meta in conn.execute("SELECT occurrence, step, window, meta FROM act_detail "
                                                "WHERE process='form' AND step IN ('label', 'attach') ORDER BY occurrence").fetchall():
        m = json.loads(meta)
        key = "near" if step == "label" else "picks"
        if str(act) in done or key not in m:
            continue
        unit = json.loads(window).get("unit") or []
        internal(conn, act, step, [handle_of(h) for h in m[key]], unit[1] if len(unit) > 1 else UNKNOWN)
        n += 1
    conn.commit()
    return n


def legacy_results(results):
    """旧库 reads.results 里给看到的认识编号：查词 [[编号, 分]……]，get ["K123", "O:…"]，联想 [编号……]。地图节点不算（旧地图不在新库）。"""
    out = []
    for r in results or []:
        if isinstance(r, int):
            out.append(r)
        elif isinstance(r, list) and r and isinstance(r[0], int):
            out.append(r[0])
        elif isinstance(r, str) and re.fullmatch(r"K\d+", r):
            out.append(int(r[1:]))
    return out
