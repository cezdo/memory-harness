"""〖〗回填（design/synapse/设计.md 足迹一节，runbook F2，2026-10-22）：“我”用到了记忆里的内容，就在正文里用〖〗标出那半句
（可以转述）。程序认出转述的是哪一条：
1. 取出回复里〖〗中的内容（至少 MIN_CHARS 个字）；
2. 候选是这段会话在这一轮之前给看到过的陈述（旧 J 2026-10-23 退役；evoke / search、relive / get、roam / map 和送达的联想；form 内部的检索、
   只记录的联想不算）。设计上是“在场”（最后一次压缩之后），现在先按曝光算；
3. 字面（BM25）留前 TOP 条，放进同一个问题交给 Jev 选“转述说的是哪一条”，另有“都不是”；
4. 最高的一项不是“都不是”、概率不低于 THRESHOLD 才连；原文一字不差的几条当作同一条，都连上。
每一轮写一条 use（channel = track，mode = cite，parent 是那段会话，inputs 带那一轮和连上的陈述、J），可以重复跑：
inputs 里已经有这一轮的就跳过。整理循环每天跑一次。"""
import json
import re

from ...engram import store as st
from ...engram.store import NA, UNKNOWN, dumps

MARK = re.compile(r"〖([^〖〗\n]{2,200})〗")
MIN_CHARS, TOP, THRESHOLD = 4, 5, 0.5
SEEN_MODES = ("search", "get", "map", "recall", "evoke", "relive", "roam")
QUESTION = "转述说的是哪一条候选记忆里的那件事？"
VERSION = "cite-v0.1"


def operator(conn):
    return st.operator(conn, f"synapse/{VERSION}", "synapse", "program", model="jev", prompt=NA, prompt_hash=NA, isolation=NA,
                       window="turn", params={"top": TOP, "threshold": THRESHOLD, "question": QUESTION, "min_chars": MIN_CHARS})


def spans(text):
    """回复里〖〗中的内容，太短的（名字之类）不要。"""
    return [m.strip() for m in MARK.findall(text or "") if len(m.strip()) >= MIN_CHARS]


def exposed(conn, session, before, texts):
    """这段会话在 before 之前给看到过的陈述、旧 J → [(种类, 编号)]，去重保序。texts：{(种类, 编号): 正文}，取不到正文的不要。"""
    out = []
    for inputs, mode, channel, purpose, query in conn.execute(
            "SELECT o.inputs, u.mode, u.channel, u.purpose, u.query FROM occurrence o JOIN use_detail u ON u.occurrence=o.id "
            "WHERE u.session=? AND o.started<=? ORDER BY o.id", (session, before)):
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


def backfill(conn, judge=None, limit=200):
    """回复里有〖〗、还没回填过的轮次，逐轮认出引用了哪些记忆，写成 use。Jev 出错的那一轮这次不写，下次再试。返回 (写了几轮, 出错几轮)。"""
    if judge is None:
        from .judge import choose as judge
    done = {i for (inp,) in conn.execute("SELECT o.inputs FROM occurrence o JOIN use_detail u ON u.occurrence=o.id "
                                         "WHERE u.channel='track' AND u.mode='cite'") for i in json.loads(inp).get("turn", [])}
    rows = conn.execute("SELECT d.occurrence, d.session, o.started, o.ended, x.assistant FROM turn_detail d "
                        "JOIN turn_text x ON x.id=d.text JOIN occurrence o ON o.id=d.occurrence "
                        "WHERE instr(x.assistant, '〖')>0 ORDER BY o.id").fetchall()
    rows = [r for r in rows if r[0] not in done and spans(r[4])][:limit]
    if not rows:
        return 0, 0
    texts, op = texts_of(conn), operator(conn)
    wrote = failed = 0
    for tid, session, started, ended, reply in rows:
        when = ended if not st.absent(ended) else started
        cands = exposed(conn, session, when, texts)
        try:
            recs = [match(q, cands, texts, judge) for q in spans(reply)]
        except Exception as e:
            failed += 1
            print(f"〖〗回填：轮次 {tid} 出错：{repr(e)[:200]}")
            continue
        linked = {}
        for r in recs:
            for kind, i in r["linked"]:
                if i not in linked.setdefault(kind, []):
                    linked[kind].append(i)
        parent = conn.execute("SELECT occurrence FROM session_detail WHERE session=?", (session,)).fetchone()
        oid = st.occurrence(conn, "use", op, started=when, ended=when, scale="turn", inputs={"turn": [tid], **linked},
                            cause="process", parent=parent[0] if parent else NA, outcome="ok")
        conn.execute("INSERT INTO use_detail VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (oid, "track", "cite", "interactive", UNKNOWN, session or UNKNOWN, NA, NA, dumps({"cite": recs}),
                      NA, NA, dumps({"exposed": len(cands)})))
        conn.commit()
        wrote += 1
    return wrote, failed
