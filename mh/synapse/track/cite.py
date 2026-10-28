"""〖〗回填（design/synapse/设计.md 足迹一节，runbook F2，2026-10-22）：“我”用到了记忆里的内容，就在正文里用〖〗标出那半句
（可以转述）。程序认出转述的是哪一条：
1. 取出回复里〖〗中的内容（至少 MIN_CHARS 个字）；
2. 候选是这段会话到这一轮结束为止给看到过的陈述（旧 J 2026-10-23 退役；evoke / search、relive / get、roam / map 和送达的联想；form 内部的检索、
   只记录的联想不算）。设计上是“在场”（最后一次压缩之后），现在先按曝光算；
3. 字面（BM25）留前 TOP 条，放进同一个问题交给 Jev 选“转述说的是哪一条”，另有“都不是”；
4. 最高的一项不是“都不是”、概率不低于 THRESHOLD 才连；原文一字不差的几条当作同一条，都连上。
片段里直接写了句柄（L3:T5:S1、L3:T5、T5、L3，2026-10-27 起）的，程序解析后直接连上，不看曝光、不问 Jev；去掉句柄剩下的
正文够长，照上面再认一遍。只认 L、T 开头的写法：E、J 的写法和 runbook 的条目编号（E5）撞，真实案例里也没有。
轮次的结束时间来源都不记，到这一轮结束为止按下一轮开始算（v0.3，审查 08，2026-10-28；v0.2 截在这一轮开始，同一轮里先拉取
再引用的都漏了）；还没有下一轮的，等会话告一段落（observe 的 IDLE）再回填，截到当下。v0.2 的记录整轮重做一次。
v0.1 的记录没认句柄：片段里有句柄的轮次按新版本整轮重做一次，新记录的 inputs 带 use = [旧记录]，表示取代；
回填以后这段会话又有拉取更正进来（track / rebind，/clear 后记错会话的）的，同样重做。
每一轮写一条 use（channel = track，mode = cite，parent 是那段会话，inputs 带那一轮和连上的陈述、J），可以重复跑：
inputs 里已经有这一轮的就跳过。整理循环每天跑一次。"""
import json
import re
from datetime import datetime, timezone

from ...engram import store as st
from ...engram.store import NA, UNKNOWN, dumps

MARK = re.compile(r"〖([^〖〗\n]{2,200})〗")
HANDLE = re.compile(r"(?<![A-Za-z0-9_:])(?:L(?:\d+|\?)(?::T\d+)?|T\d+)(?::S\d+(?:-S?\d+)?)?(?![A-Za-z0-9_:])")
MIN_CHARS, TOP, THRESHOLD = 4, 5, 0.5
SEEN_MODES = ("search", "get", "map", "recall", "evoke", "relive", "roam")
QUESTION = "转述说的是哪一条候选记忆里的那件事？"
VERSION = "cite-v0.3"


def operator(conn):
    return st.operator(conn, f"synapse/{VERSION}", "synapse", "program", model="jev", prompt=NA, prompt_hash=NA, isolation=NA,
                       window="turn", params={"top": TOP, "threshold": THRESHOLD, "question": QUESTION, "min_chars": MIN_CHARS})


def spans(text):
    """回复里〖〗中的内容，太短的（名字之类）不要。"""
    return [m.strip() for m in MARK.findall(text or "") if len(m.strip()) >= MIN_CHARS]


def direct(L, quote):
    """片段里直接写的句柄 → ([(种类, 编号)…] 解析得到的, [句柄…], 去掉句柄剩下的正文)。种类照 inputs 的写法。"""
    from .. import pull
    got, hs = [], HANDLE.findall(quote)
    for h in hs:
        kind, v = pull.resolve(L, h)
        if kind == "earlier":
            kind, v = "node", v[1]
        if kind == "node":
            got.append(("topic_line" if v[0] == "L" else "session_entry", int(v[1:])))
        elif kind == "stmts":
            got.extend(("form_record", i) for _, i in v if i is not None)
    rest = HANDLE.sub(" ", quote).strip(" ，、。,;；:：()（）")
    return list(dict.fromkeys(got)), hs, rest


def exposed(conn, session, before, texts):
    """这段会话在 before 之前给看到过的陈述、旧 J → [(种类, 编号)]，去重保序。texts：{(种类, 编号): 正文}，取不到正文的不要。"""
    from . import by_session
    out = []
    where, args = by_session(conn, session, "u")
    for inputs, mode, channel, purpose, query in conn.execute(
            "SELECT o.inputs, u.mode, u.channel, u.purpose, u.query FROM occurrence o JOIN use_detail u ON u.occurrence=o.id "
            f"WHERE {where} AND o.started<=? ORDER BY o.id", (*args, before)):
        if channel not in ("pull", "push") or purpose not in ("interactive", "prefetch"):
            continue
        if channel == "push" and (mode != "associate" or json.loads(query).get("associate", {}).get("shadow", True)):
            continue                                  # 只记录、没送达的联想不算看到
        if channel == "pull" and mode not in SEEN_MODES:
            continue
        for kind, ids in json.loads(inputs).items():
            for i in ids if kind in ("form_record", "judgment") else ():
                if (kind, i) in texts and (kind, i) not in out:
                    out.append((kind, i))
    return out


def texts_of(conn):
    """候选的正文：现在的陈述（旧写法的结构标记换成『』）。旧 J 2026-10-23 退役，不再做候选。"""
    return {("form_record", i): st.restyle(x) for i, x in conn.execute("SELECT id, text FROM form_record WHERE kind='session'")}


def match(quote, cands, texts, judge):
    """一条转述 → {"quote", "cands", "probs", "pick", "linked"}。judge(state, instructions, criteria) → {"value", "probabilities"}。"""
    from ..pull import Index
    top = [k for k, _ in Index({i: texts[c] for i, c in enumerate(cands)}).top(quote, TOP)]
    rec = {"quote": quote, "cands": [list(cands[i]) for i in top], "linked": []}
    if not top:
        rec["skip"] = "字面没有候选"
        return rec
    keys = "ABCDEFGH"[:len(top)]
    state = f"“我”在回复里转述了一条记忆：\n转述：{quote}\n\n候选的记忆：\n" + "\n".join(f"{k}. {texts[cands[i]]}" for k, i in zip(keys, top))
    crit = {k: f"转述说的是候选 {k} 里的那件事" for k in keys} | {"无": "转述说的不是这些候选里的任何一件"}
    a = judge(state, QUESTION, crit)
    probs = a.get("probabilities") or {}
    rec["probs"] = probs
    best = max(probs, key=probs.get) if probs else None
    if best in keys and probs[best] >= THRESHOLD:
        pick = cands[top[keys.index(best)]]
        rec["pick"] = list(pick)
        rec["linked"] = [list(cands[i]) for i in top if texts[cands[i]] == texts[pick]]   # 原文一字不差的当作同一条
    return rec


def cite(quote, cands, texts, judge, L):
    """一个片段：先认直接写的句柄，剩下的正文够长再走 match。"""
    got, hs, rest = direct(L, quote) if HANDLE.search(quote) else ([], [], quote)
    if not hs:
        return match(quote, cands, texts, judge)
    rec = match(rest, cands, texts, judge) if len(rest) >= MIN_CHARS else {"linked": []}
    rec = {"quote": quote, "handles": hs, **{k: v for k, v in rec.items() if k != "quote"}}
    rec["linked"] = [list(x) for x in got] + [x for x in rec["linked"] if tuple(x) not in got]
    return rec


def _todo(conn):
    """回填过的轮次 → 那条记录的编号。不算做过的：v0.1 的记录没认句柄、片段里有句柄的；回填以后这段会话又有拉取更正进来的
    （track / rebind，候选变了）。"""
    done, redo, rebound_at = {}, {}, {}
    for rid, ses in conn.execute("SELECT occurrence, session FROM use_detail WHERE channel='track' AND mode='rebind'"):
        rebound_at[ses] = max(rid, rebound_at.get(ses, 0))
    for oid, inp, q, name, ses in conn.execute("SELECT o.id, o.inputs, u.query, p.name, u.session FROM occurrence o JOIN use_detail u "
                                               "ON u.occurrence=o.id JOIN operator p ON p.id=o.operator WHERE u.channel='track' AND u.mode='cite'"):
        old = (name == "synapse/cite-v0.1" and any(HANDLE.search(r.get("quote", "")) for r in json.loads(q).get("cite", []))) \
            or name == "synapse/cite-v0.2" or rebound_at.get(ses, 0) > oid
        for i in json.loads(inp).get("turn", []):
            if old:
                redo.setdefault(i, oid)
            else:
                done[i] = oid
    return done, {i: oid for i, oid in redo.items() if i not in done}


def until(conn, source, session, ordinal, started, now):
    """这一轮到什么时候为止（拉取的曝光截到这里）：下一轮开始；还没有下一轮的，会话告一段落了截到当下，没有就是 None（先不回填）。"""
    from ...engram import observe
    nxt = conn.execute("SELECT o.started FROM turn_detail d JOIN occurrence o ON o.id=d.occurrence "
                       "WHERE d.source=? AND d.session=? AND d.ordinal=?", (source, session, ordinal + 1)).fetchone()
    if nxt and not st.absent(nxt[0]):
        return nxt[0]
    return st.iso(now) if nxt or observe.idle(started, source, now) else None


def backfill(conn, judge=None, limit=200, now=None):
    """回复里有〖〗、还没回填过的轮次，逐轮认出引用了哪些记忆，写成 use。Jev 出错的那一轮这次不写，下次再试。返回 (写了几轮, 出错几轮)。
    now：测试替换。"""
    from .. import pull
    from . import _split
    if judge is None:
        from .judge import choose as judge
    done, redo = _todo(conn)
    now = now or datetime.now(timezone.utc)
    rows = conn.execute("SELECT d.occurrence, d.session, o.started, d.source, d.ordinal, x.assistant FROM turn_detail d "
                        "JOIN turn_text x ON x.id=d.text JOIN occurrence o ON o.id=d.occurrence "
                        "WHERE instr(x.assistant, '〖')>0 ORDER BY o.id").fetchall()
    rows = [(r[0], r[1], r[2], cut, r[5]) for r in rows if r[0] not in done and spans(r[5])
            for cut in [until(conn, r[3], r[1], r[4], r[2], now)] if cut][:limit]
    if not rows:
        return 0, 0
    texts, op, L = texts_of(conn), operator(conn), pull.layers(conn)
    wrote = failed = 0
    for tid, session, when, cut, reply in rows:
        cands = exposed(conn, session, cut, texts)
        try:
            recs = [cite(q, cands, texts, judge, L) for q in spans(reply)]
        except Exception as e:
            failed += 1
            print(f"〖〗回填：轮次 {tid} 出错：{repr(e)[:200]}")
            continue
        linked = {}
        for r in recs:
            for kind, i in r["linked"]:
                if i not in linked.setdefault(kind, []):
                    linked[kind].append(i)
        inputs, hs = _split(conn, linked)
        if tid in redo:
            inputs["use"] = [redo[tid]]
        parent = conn.execute("SELECT occurrence FROM session_detail WHERE session=?", (session,)).fetchone()
        oid = st.occurrence(conn, "use", op, started=when, ended=when, scale="turn", inputs={"turn": [tid], **inputs},
                            cause="process", parent=parent[0] if parent else NA, outcome="ok")
        conn.execute("INSERT INTO use_detail VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (oid, "track", "cite", "interactive", UNKNOWN, session or UNKNOWN, NA, NA, dumps({"cite": recs}),
                      NA, NA, dumps({"exposed": len(cands), **({"handles": hs} if hs else {})})))
        conn.commit()
        wrote += 1
    return wrote, failed
