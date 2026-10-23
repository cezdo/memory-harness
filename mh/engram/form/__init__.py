"""form：event → 会话记录 → 会话入口 → 话题线（design/engram/form/设计.md）。模型只管语义，其余都是程序。

三步，每步一次模型调用（B2，2026-10-18 定下；实验 tmp/logical-session/、tmp/form-attach/）：
- ① pack（pack.py）：一个会话（一个单位）的全部 E，加上每轮写进的文件 → 会话记录（名字、主题、陈述）。陈述存 form_record
  （kind = session，出生于 pack 的 act），来源是 E。先按物理会话，不切逻辑会话。
- ② label（label.py）：会话记录 → 会话入口（名字、停在、模型写的索引词），存 session_entry。程序补的词、别名是缓存，读时重算。
- ③ attach（attach.py）：用 Synapse 的检索（mh/synapse/pull/retrieve.py）在已有的会话入口和话题线里挑候选，有骨架的工作区再给骨架
  （skeleton.py）和写进的文件算出的落点；模型选节点和线（或新起一条），存 attachment（新线存 topic_line）。

单位：一个会话里还没整理过的新做法 E（observe 一轮或多轮一起读出的；旧版本逐轮的 E 先不动，2026-10-18）。
一个会话的 E 太长时按轮切成连续的几个单位（MAX_CHARS）。晚到的 E（B4.1，2026-10-18：头尾接上不重审，中间插入再审视）：
- 续上（轮次在已整理的之后）：新的一个单位；label 看得到这段会话原来的会话入口，写一个新的入口取代它（一段会话一个入口），
  索引词和原来的合并（重新整理的也合并），挂线由程序沿用，不调 attach；
- 中间插入（轮次落进已整理的范围）：那个单位整个重新 pack（旧 E + 新 E），label 重写入口，挂线沿用、标“待审视”。
什么时候整理（design“程序做的事”）：
- 会话最后一轮开始后 IDLE 秒没有新的一轮（告一段落）；
- 门槛：以后还会产出 E 的轮次（observe.progress 里待观测的）里最早的开始时间是 T，只整理最后一轮早于 T 的会话；
- 按会话最后一轮的时间排；attach 顺序执行，后一个要看到前一个挂好的线。一个单位失败满次数（MAX_FAILURES）就跳过，不挡后面；
  还没失败满、只是这一批没做完的，挡住后面的 attach（下一批接着来）。
每次调用是一条 act（只追加，失败也记），窗口里写单位（来源、会话、第一条 E）。
"""
import collections
import concurrent.futures
import hashlib
import json
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

from ... import express
from ..store import instance
from .. import invoke, observe
from .. import store as st
from ..invoke import Codex
from ..store import NA, UNKNOWN, absent, cost, dumps, occurrence, operator as register, outcome
from ..observe.label import USER_NAME
from ...synapse import track
from ...synapse.pull import retrieve
from . import attach, glossary, label, pack, restate as _restate, skeleton

PROMPTS = Path(__file__).parent / "prompts"
# 当前的做法：步骤 → (模型定义, 提示词)，只在这里定义。提示词版本从 0.1 起：大改动加前面的数，小改动加后面的数
CURRENT = {"pack": ("sol-low", "pack-v0.3"),   # v0.3：结构标记写『引用：』『文件：』（2026-10-22，用户看过样本）
           "label": ("sol-low", "label-v0.2"),
           "attach": ("sol-low", "attach-v0.2"),               # 有骨架的工作区（v0.2：句柄写 L3，2026-10-19）
           "attach_lines": ("sol-low", "attach-lines-v0.2"),   # 没骨架的工作区：只有线（v0.2：已有的入口可能比新文件夹晚，2026-10-19）
           "restate": ("sol-low", "restate-v0.1")}             # 修订提示：只改写依据那几条 E 的陈述（2026-10-21）
STEP_OF = {"attach_lines": "attach"}                           # act 里记的 step
MODELS = {"sol-low": lambda: invoke.whole(Codex("gpt-6.1-sol", "low"))}
DESC = {"sol-low": "gpt-6.1-sol low（经 codex exec）"}          # 环境里“现在回看的我”
MAX_FAILURES = 3
IDLE = 3600              # 会话最后一轮开始后这么久没有新的一轮，就算告一段落
MAX_CHARS = 40000        # 一个单位的 E 最多这么多字，超过按轮切开
NEW_SCALES = ("turns", "segment")     # 新做法读出的 E（一轮或多轮）；segment 是 2026-10-17 上线当天的写法


def window_params(step):
    """影响模型看到什么的程序参数：进 Operator 的名字（摘要）和登记的 params，改了任何一个就是新的 Operator。"""
    if step == "pack":
        return {"idle": IDLE, "max_chars": MAX_CHARS, "written": "observe.written", "redact": "store.SECRET"}
    if step == "restate":
        return {"changes": "observe.fate", "format": "pack-v0.3 的陈述格式"}
    if step == "label":
        return {"terms": "label.terms-v7", "aliases": "glossary", "similar": "retrieve.word_hits(名字)", "similar_top": label.SIMILAR_TOP,
                "dedup": "label.dedup", "fork": "shared-statements-v1"}
    return {"key_top": retrieve.KEY_TOP, "handles": "L3/T5", "bm25_top": retrieve.BM25_TOP, "big": retrieve.BIG, "aliases": "glossary",
            "skeleton": step == "attach", **({"reach": "store.reachable-v2"} if step.startswith("attach") else {})}


def operator(conn, step):
    """登记并返回 (编号, 名字)。名字 = 模型定义/提示词.隔离-摘要（同 observe）。"""
    name, prompt = CURRENT[step]
    body = (PROMPTS / f"{prompt}.md").read_bytes()
    model = MODELS[name]()
    iso = getattr(model, "isolation", "") or NA
    wp = {**window_params(step), "expression": express.signature()}
    full = f"{name}/{prompt}.{iso}-{hashlib.sha256(body + iso.encode() + dumps(wp).encode()).hexdigest()[:8]}"
    params = {"definition": name, "platform": getattr(model, "platform", UNKNOWN), "step": STEP_OF.get(step, step), **wp}
    oid = register(conn, full, "form", "former", model=getattr(model, "model", UNKNOWN), prompt=prompt,
                   prompt_hash=hashlib.sha256(body).hexdigest()[:16], isolation=iso, window="session", params=params)
    return oid, full


def prompt(step):
    return express.prompt((PROMPTS / f"{CURRENT[step][1]}.md").read_text(), USER_NAME)


# ---- 密钥 ----

SECRET = re.compile("|".join([
    r"(?<![A-Za-z0-9])sk-(?:[A-Za-z0-9]+-){0,2}[A-Za-z0-9_]{20,}",          # OpenAI、OpenRouter、Anthropic……
    r"(?<![A-Za-z0-9])tskey-[A-Za-z0-9-]{20,}",                             # Tailscale
    r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",   # JWT
    r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{30,}",                          # GitHub
    r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![0-9A-Z])",                        # AWS
    r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_\-]{35}",                              # Google
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
]))


def redact(text):
    """形成前把密钥换掉，不发给模型（从旧的 formation 搬来）。"""
    return SECRET.sub("〔密钥已略去〕", text)


# ---- 形成史（只读 form 自己的 act）----

ACTS = """SELECT o.id, o.operator, o.outcome, o.inputs, a.step, a.window, a.meta, a.output FROM act_detail a
          JOIN occurrence o ON o.id=a.occurrence WHERE a.process='form' AND a.step IN ('pack','label','attach') ORDER BY o.id"""


# 修订提示（observe 写来的：E 被取代、不成立、撤下、恢复）认领以后怎么做（2026-10-21：两个方案都留着做成选项，先用前者）：
# statements 只改写依据那几条 E 的陈述（默认：一次决策的影响范围不超出它所在的那一级，2026-10-21）；
# pack       从 pack 起整段重做，起名接原来的入口、挂线沿用（第一版的默认，同日改掉）。
REVISE = "statements"


def revision_due(conn, r):
    """一张修订提示该不该认领：lazy，写下以后，依据这些 E 的陈述被主会话的拉取取到了（不算 form 内部的拉取，也不算推送：
    推送只算给看到），或者取到了这些陈述所在的话题（会话入口）。"""
    events = r["inputs"].get("event", [])
    if not events:
        return False
    marks = ",".join("?" * len(events))
    stmts = {}
    for fid, born in conn.execute(f"SELECT DISTINCT f.id, f.born FROM form_record f, json_each(f.refs, '$.event') j "
                                  f"WHERE j.value IN ({marks})", events):
        stmts[fid] = born
    if not stmts:
        return False
    entries = {x for (x,) in conn.execute(f"SELECT id FROM session_entry WHERE record IN ({','.join('?' * len(set(stmts.values())))})",
                                          list(set(stmts.values())))}
    for inputs, meta in conn.execute("SELECT o.inputs, d.meta FROM occurrence o JOIN use_detail d ON d.occurrence=o.id "
                                     "WHERE o.id>? AND d.channel='pull' AND d.purpose!='form'", (r["id"],)):
        if set(json.loads(inputs).get("form_record", [])) & stmts.keys():
            return True
        handles = json.loads(meta).get("handles", {}) if meta.startswith("{") else {}
        if set(handles.get("session_entry", [])) & entries:
            return True
    return False


def _by_pack(conn, r):
    """这张申请是不是被一次 pack 认领过（整段重做的那条路）。"""
    return any(conn.execute("SELECT 1 FROM act_detail WHERE occurrence=? AND step='pack'", (a,)).fetchone() for a, _ in r["acts"])


def redo_requests(conn):
    """给 form 的申请 → 会话：{(来源, 会话): [申请…]}（按编号排）。被质疑的陈述（form_record）从写它的 pack 推出会话，
    E 从它的轮次推出。现在只认这两种：都从 pack 起整段重做（C1，2026-10-19）。
    observe 写来的修订提示（E 被取代、不成立、撤下、恢复）是 lazy 的：基于那些 E 的陈述被用到（revision_due）才认领，
    认领以后按 REVISE 做；不等用到就认会马上整段重做（2026-10-21）。"""
    out = collections.defaultdict(list)
    for r in st.requests(conn, "form", "all"):
        if r["from"] != st.MAIN and not (_by_pack(conn, r) or (REVISE == "pack" and revision_due(conn, r))):
            continue                       # 修订提示按 statements 做的不经过整段重做；以前由 pack 认领过的照旧算数
        keys = set()
        for rid in r["inputs"].get("form_record", []):
            row = conn.execute("SELECT born FROM form_record WHERE id=?", (rid,)).fetchone()
            if row:
                keys.add(_unit_of(conn, row[0])[:2])
        for eid in r["inputs"].get("event", []):
            row = conn.execute("SELECT d.source, d.session FROM event e JOIN turn_detail d ON d.occurrence=e.turn WHERE e.id=?",
                               (eid,)).fetchone()
            if row:
                keys.add(tuple(row))
        for k in keys:
            out[k].append(r)
    return out


def history(conn):
    """ok        {(step, 单位): act}：成功的那次（同一单位同一步以最后一次成功为准）；
    fails     {(step, 单位, Operator): {timeout | content_error: 次数}}，环境错误不算；
    last      {(step, 单位): 最近一次 act}，下一次以它为前身；
    packed    已经整理进会话记录的 E。单位 = (来源, 会话, 第一条 E)。
    empty     最后一次成功的 pack 整理出“无”的单位（没有陈述，不写会话入口）；
    carried   {单位: label 的 act}：这次 label 沿用了原来入口的挂线，不用再 attach。
    申请重做（redo_requests）：一段会话里早于最新一张申请的整理、起名、挂线都不再算数，这段会话从 pack 起重做。"""
    ok, fails, last, packed = {}, collections.defaultdict(collections.Counter), {}, set()
    empty, carried = set(), {}
    cut = {k: rs[-1]["id"] for k, rs in redo_requests(conn).items()}
    for oid, op, result, inputs, step, window, meta, output in conn.execute(ACTS):
        w = json.loads(window)
        if "unit" not in w:
            continue                         # 旧做法导进来的整理、打包（逐轮记录、会话记录），不是这里的单位
        unit = tuple(w["unit"])
        last[(step, unit)] = oid             # 被重做掉的也是前身
        if oid < cut.get(unit[:2], 0):
            continue                         # 申请之前的：被重做掉
        if result == "ok":
            ok[(step, unit)] = oid
            if step == "pack":
                packed.update(json.loads(inputs).get("event", []))
                (empty.add if (output or "").strip() == "无" else empty.discard)(unit)
            if step == "label" and json.loads(meta or "{}").get("carry") is not None:
                carried[unit] = oid
        elif result in ("timeout", "content_error"):
            fails[(step, unit, op)][result] += 1
    return types.SimpleNamespace(ok=ok, fails=fails, last=last, packed=packed, empty=empty, carried=carried)


def _complete(h, unit):
    """这个单位三步都做完了：最后一次 pack 之后起了名，起名之后挂了线（或沿用了原来的挂线）；整理出“无”的也算。"""
    pa, la, aa = h.ok.get(("pack", unit)), h.ok.get(("label", unit)), h.ok.get(("attach", unit))
    if not pa:
        return False
    if unit in h.empty:
        return True
    if la is None or la < pa:
        return False
    return h.carried.get(unit) == la or (aa is not None and aa > la)


def _failed(h, step, unit, op):
    return any(n >= MAX_FAILURES for n in h.fails[(step, unit, op)].values())


# ---- 单位 ----

def _session(conn, source, session):
    s = conn.execute("SELECT cwd, model FROM session_detail WHERE source=? AND session=?", (source, session)).fetchone()
    turns = conn.execute("SELECT d.occurrence, d.ordinal, o.started FROM turn_detail d JOIN occurrence o ON o.id=d.occurrence "
                         "WHERE d.source=? AND d.session=? ORDER BY d.ordinal", (source, session)).fetchall()
    return {"source": source, "session": session, "cwd": s["cwd"] if s else "", "model": s["model"] if s else UNKNOWN,
            "workspace": st.session_workspace(source, session, s["cwd"] if s else ""), "turns": turns,
            "begin": turns[0]["started"] if turns else UNKNOWN, "end": turns[-1]["started"] if turns else UNKNOWN}


def _since(ts, now):
    if absent(ts):
        return float("inf")
    return (now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds()


def units(conn, now=None, session=None):
    """要处理的单位，按会话最后一轮的时间排：[{"unit", "session"（_session）, "events", "lo", "hi", "redo"}]。
    已经整理过的单位（有成功的 pack）也在里面，后面的 label、attach 还没做完的接着做；全部做完的不列。
    晚到的 E 落进已整理的范围（轮次不大于已整理的最后一轮）：它所在的单位 redo，整个重新 pack。
    lo、hi：这个单位附写进的文件的轮次范围（ordinal）。"""
    now = now or datetime.now(timezone.utc)
    h = history(conn)
    gate = min((p["started"] for p in observe.progress(conn) if p["state"] in observe.TO_OBSERVE), default=None)
    sql = ("SELECT DISTINCT d.source, d.session FROM event e JOIN occurrence o ON o.id=e.born "
           f"JOIN turn_detail d ON d.occurrence=e.turn WHERE o.scale IN ({','.join('?' * len(NEW_SCALES))})")
    args = list(NEW_SCALES)
    if session:
        sql += " AND d.session LIKE ?"
        args.append(session + "%")
    out = []
    asked = {k: [r["id"] for r in rs if r["state"] != "handled"] for k, rs in redo_requests(conn).items()}
    infos = sorted((_session(conn, source, sess) for source, sess in conn.execute(sql, args).fetchall()),
                   key=lambda i: (absent(i["end"]), i["end"]))
    claimed = set()                        # 这一次已经排进单位、还没整理的 E（分叉的共用轮次）
    for info in infos:
        source, sess = info["source"], info["session"]
        # 纠正写的 E 不当新 E：它们只经修订提示进来，改写依据旧 E 的那几条陈述（restate）；当新 E 会按中间插入整段重做，
        # 越过了这次纠正所在的那一级（2026-10-21：01a10aa7、f5cdabd1、session-1cf6 被这样整段重做过）
        evs = [e for e in observe.frame(conn, source, sess)["events"] if e["scale"] in NEW_SCALES and e.get("step") != "correct"]
        if not evs:
            continue
        if claimed & {e["id"] for e in evs if e["id"] not in h.packed}:
            continue                       # 开头和先告一段落的会话共用（分叉）：等那边整理好，下一批只整理自己的部分
        by_id = {e["id"]: e for e in evs}
        # 已经整理过的单位：按 pack 的 act 认（它的输入里有哪些 E）
        done_units = sorted({u for (step, u) in h.ok if step == "pack" and u[:2] == (source, sess)}, key=lambda u: u[2])
        ranges = []                                                # [(单位, 它的 E, 最小轮次, 最大轮次)]
        for u in done_units:
            ids = json.loads(conn.execute("SELECT inputs FROM occurrence WHERE id=?", (h.ok[("pack", u)],)).fetchone()[0]).get("event", [])
            got = [by_id[i] for i in ids if i in by_id]
            ords = [e["ordinal"] for e in got] or [-1]
            ranges.append((u, got, min(ords), max(ords)))
        prev_hi = max([r[3] for r in ranges], default=-1)
        rest = [e for e in evs if e["id"] not in h.packed]
        ready = bool(rest) and _since(info["end"], now) >= IDLE and not (
            gate is not None and not absent(info["end"]) and info["end"] >= gate)   # 告一段落，门槛前面没有会产出 E 的轮次
        late = collections.defaultdict(list)                       # 中间插入：单位 → 晚到的 E
        if ready:
            for e in rest:
                if e["ordinal"] <= prev_hi:
                    k = max([i for i, r in enumerate(ranges) if r[2] <= e["ordinal"]], default=0)
                    late[k].append(e)
        tail = [e for e in rest if e["ordinal"] > prev_hi]
        for k, (u, got, lo_k, hi_k) in enumerate(ranges):
            if late.get(k):
                evs_k = sorted(got + late[k], key=lambda e: (e["ordinal"], e["id"]))
                lo = ranges[k - 1][3] + 1 if k else 0
                hi = max(e["ordinal"] for e in evs_k) if k < len(ranges) - 1 or tail else 10 ** 9
                out.append({"unit": u, "session": info, "events": evs_k, "anchor": info["end"], "lo": lo, "hi": hi, "redo": True})
            elif not _complete(h, u):                              # 整理过了，起名或挂线还没做完
                req = json.loads(conn.execute("SELECT inputs FROM occurrence WHERE id=?", (h.ok[("pack", u)],)).fetchone()[0]).get("request", [])
                out.append({"unit": u, "session": info, "events": got, "anchor": info["end"], "lo": 0, "hi": 0, "redo": False,
                            "request": req})
        if not ready or not tail:
            continue
        chunks, cur, size = [], [], 0
        for e in tail:
            if cur and size + len(e["text"]) > MAX_CHARS and e["ordinal"] != cur[-1]["ordinal"]:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(e)
            size += len(e["text"])
        chunks.append(cur)
        for i, c in enumerate(chunks):
            lo = prev_hi + 1
            hi = c[-1]["ordinal"] if i < len(chunks) - 1 else 10 ** 9
            out.append({"unit": (source, sess, c[0]["id"]), "session": info, "events": c, "anchor": info["end"],
                        "lo": lo, "hi": hi, "redo": False, "request": asked.get((source, sess), [])})
            claimed |= {e["id"] for e in c}
            prev_hi = c[-1]["ordinal"]
    out.sort(key=lambda x: (x["anchor"], x["unit"][2]))
    return out


# ---- 读回：会话记录、会话入口、池 ----

def replacements(conn):
    """改写过的陈述：{原来的陈述: 现在的那条，撤下的是 None}，沿取代链走到最后。"""
    nxt = {}
    for rid, rel in conn.execute("SELECT id, relations FROM form_record WHERE relations!='[]' ORDER BY id"):
        for x in json.loads(rel):
            if x["kind"] == "supersedes":
                nxt[x["to"]] = rid
    for (meta,) in conn.execute("SELECT a.meta FROM act_detail a JOIN occurrence o ON o.id=a.occurrence "
                                "WHERE a.process='form' AND a.step='restate' AND o.outcome='ok'"):
        for k in json.loads(meta).get("withdrawn", {}):
            nxt[int(k)] = None
    out = {}
    for old in nxt:
        cur, seen = old, set()
        while cur in nxt and cur not in seen:
            seen.add(cur)
            cur = nxt[cur]
            if cur is None:
                break
        out[old] = cur
    return out


def statement_seq(conn, swap=None):
    """陈述在会话里的序号（Synapse v0.11 起的句柄 S…，2026-10-22 从 Synapse 移到这里，cognize 的引用也要用）：同一段会话的 pack
    写出的陈述按写出的先后编（pack 的 act、它里面的顺序），只增不减；续上、重新整理都是新的 pack，接着往后编。会话按窗口里的 unit
    （_act 每次都写 [来源, 会话, …]）认，没有 unit 再看 session 是不是 [来源, 会话]；旧系统导入的 pack（没有 unit，session 是编号）不编。
    修订提示改写的陈述沿用原来那条的序号（句柄不变、内容换新）。返回 (seq {陈述: 序号}, at {(会话, 序号): 陈述}, sess {陈述: 会话})，
    at 里是现在的那条。"""
    seq, at, sess, nxt = {}, {}, {}, collections.Counter()
    for rid, src, ses in conn.execute(
            "SELECT f.id, coalesce(json_extract(a.window, '$.unit[0]'), json_extract(a.window, '$.session[0]')), "
            "coalesce(json_extract(a.window, '$.unit[1]'), json_extract(a.window, '$.session[1]')) "
            "FROM form_record f JOIN act_detail a ON a.occurrence=f.born "
            "WHERE f.kind='session' AND a.step='pack' "
            "AND (json_type(a.window, '$.unit')='array' OR json_type(a.window, '$.session')='array') ORDER BY f.born, f.idx"):
        k = (src, ses)
        nxt[k] += 1
        seq[rid], at[(k, nxt[k])], sess[rid] = nxt[k], rid, k
    for old, new in (replacements(conn) if swap is None else swap).items():
        if new is not None and old in seq:
            k, n = sess[old], seq[old]
            seq[new], at[(k, n)], sess[new] = n, new, k
    return seq, at, sess


def latest_entries(conn):
    """{(来源, 会话): 现在的会话入口编号}：一段会话以最后写的那个入口为准（和 entries 同一条规则，不建内容）。"""
    return {(r[0], r[1]): r[2] for r in conn.execute("SELECT source, session, max(id) FROM session_entry GROUP BY source, session")}


def line_heads(conn, lid):
    """一条线现在的样子：沿 from 往后找没有后继的线（改名、合并写后继线）；自己没有后继就是 [自己]。"""
    after = collections.defaultdict(list)
    for nid, frm in conn.execute('SELECT id, "from" FROM topic_line WHERE "from"!=\'[]\''):
        for p in json.loads(frm):
            after[p].append(nid)
    out, seen, todo = set(), set(), [lid]
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


def record(conn, pack_act, swap=None):
    """pack 的 act → (名字, 主题, 陈述 [{"n", "text", "id", "was"}], 写进的文件 {ordinal: [...]})。
    改写过的陈述换成现在的那条（位置、编号不变，was 是 pack 写的那条），撤下的去掉（修订提示，2026-10-21）。
    正文里旧写法的「引用：」「文件：」换成『』（store.restyle，2026-10-22）。"""
    meta = json.loads(conn.execute("SELECT meta FROM act_detail WHERE occurrence=?", (pack_act,)).fetchone()[0])
    swap = replacements(conn) if swap is None else swap
    items = []
    for r in conn.execute("SELECT * FROM form_record WHERE born=? ORDER BY idx", (pack_act,)):
        cur = swap.get(r["id"], r["id"])
        if cur is None:
            continue
        text = r["text"] if cur == r["id"] else conn.execute("SELECT text FROM form_record WHERE id=?", (cur,)).fetchone()[0]
        items.append({"n": json.loads(r["labels"])[0], "text": st.restyle(text), "id": cur, "was": r["id"]})
    for it in items:
        it["n"] = int(re.sub(r"\D", "", str(it["n"])) or 0)
    return meta.get("name", ""), meta.get("topic", ""), items, {int(k): v for k, v in meta.get("written", {}).items()}


def _date(conn, pack_act):
    turns = json.loads(conn.execute("SELECT inputs FROM occurrence WHERE id=?", (pack_act,)).fetchone()[0]).get("turn", [])
    if not turns:
        return ""
    r = conn.execute(f"SELECT max(started) FROM occurrence WHERE id IN ({','.join('?' * len(turns))})", turns).fetchone()
    return r[0] or ""


def _stop_text(stop, nums_by_id):
    nums = [nums_by_id[i] for i in stop["basis"] if i in nums_by_id]
    return stop["text"] + (f"（{'、'.join(f'陈述{n}' for n in nums)}）" if nums else "")


HANDLE = re.compile(r"(?:([Ll])|[TtSs])-?(\d+)")


def handle_of(text):
    """句柄（2026-10-19，和 Synapse v0.9 一致）：话题线 L3、会话入口（对外叫话题）T5，编号是 topic_line、session_entry 的编号。
    2026-10-19 以前 form 内部写 l-3、s-5（库里旧 act 的 meta、挂线的 why 里照样留着），这里换成现在的写法；别的（骨架 k-…）照原样。"""
    m = HANDLE.fullmatch(text.strip())
    return (("L" if m.group(1) else "T") + m.group(2)) if m else text.strip()


def entries(conn, h=None):
    """{会话入口编号: 入口}：每段会话现在的会话入口（同一段会话以最后写的那个为准，续上、重新整理都写新的），
    索引词是模型写的 + 程序补的（重算，补词看这段会话全部现行的会话记录）；parent 是它最后一次挂到的线（L…），没挂上是 None。"""
    h = h or history(conn)
    place = {}
    for r in conn.execute("SELECT entry, line FROM attachment ORDER BY id"):
        place[r["entry"]] = r["line"]
    ws = {(r["source"], r["session"]): st.session_workspace(r["source"], r["session"], r["cwd"])
          for r in conn.execute("SELECT source, session, cwd FROM session_detail")}
    latest = {}
    for f in conn.execute("SELECT * FROM session_entry ORDER BY id"):
        latest[(f["source"], f["session"])] = f
    out, swap = {}, replacements(conn)
    for key, f in latest.items():
        packs = [h.ok[("pack", u)] for u in sorted(u for (step, u) in h.ok if step == "pack" and u[:2] == key)
                 if u not in h.empty] or [f["record"]]
        items = [it for pa in packs for it in record(conn, pa, swap)[2]]
        mine = record(conn, f["record"], swap)[2]
        nums = {**{it["was"]: it["n"] for it in mine}, **{it["id"]: it["n"] for it in mine}}   # 停在的依据写的是 pack 的那条
        out[f"T{f['id']}"] = {
            "id": f"T{f['id']}", "kind": "entry", "name": f["name"], "topic": f["topic"],
            "words": label.words(json.loads(f["words"]), items),
            "stops": [_stop_text(s, nums) for s in json.loads(f["stops"])],
            "parent": f"L{place[f['id']]}" if f["id"] in place else None, "date": _date(conn, f["record"]),
            "session": (f["source"], f["session"]), "row": f["id"], "ws": ws.get((f["source"], f["session"]), ""),
            "items": items}                # 这段会话全部现行的陈述（Synapse 的 get 往下一层用）
    return out


def line_words(pool, line):
    """线的索引词由程序从子会话入口合成：线名一组，再加至少两个子会话入口都有的组（归一后的词有交集算同一组）。"""
    groups = []
    for kid in [e for e in pool.values() if e["parent"] == line and e["kind"] == "entry"]:
        for items in kid["words"]["lists"]:
            w = {retrieve.norm(a) for a in items}
            g = next((g for g in groups if g["words"] & w), None)
            if g:
                g["words"] |= w
                g["items"] += [a for a in items if a not in g["items"]]
                g["kids"].add(kid["id"])
            else:
                groups.append({"words": set(w), "items": list(items), "kids": {kid["id"]}})
    return {"lists": [[pool[line]["name"]]] + [g["items"] for g in groups if len(g["kids"]) >= 2], "constraints": []}


def pool(conn):
    """检索和挂线用的池：骨架节点（k-…）、话题线（L…）、挂上了线的会话入口（T…）。"""
    out = {}
    registry = skeleton.adopted(conn)
    skeletons = {ws: skeleton.nodes(ws, conn, registry) for ws in dict.fromkeys([*skeleton.WORKSPACES, *registry])}
    for ws, ns in skeletons.items():
        for p, n in (ns or {}).items():
            names = [n["name"]] + [a for a in glossary.aliases(n["name"]) if a != n["name"]]
            out[n["id"]] = {"id": n["id"], "kind": "node", "name": n["name"], "topic": n.get("topic", ""), "stops": [], "date": "",
                            "words": {"lists": [names], "constraints": []}, "ws": ws, "path": p,
                            "parent": ns[n["parent"]]["id"] if n["parent"] is not None else None}
    for r in conn.execute("SELECT * FROM topic_line ORDER BY id"):
        ns = skeletons.get(r["workspace"]) or {}
        placement = registry.get(r["workspace"], {}).get("placements", {})
        node = placement.get(f"L{r['id']}", r["node"])
        parent = (ns[node]["id"] if node in ns else (f"L{r['parent']}" if not absent(r["parent"]) else None))
        out[f"L{r['id']}"] = {"id": f"L{r['id']}", "kind": "line", "name": r["name"], "topic": "", "stops": [], "date": "",
                               "words": {"lists": [], "constraints": []}, "ws": st.canonical(r["workspace"]), "parent": parent,
                               "node": node, "row": r["id"]}
    for fid, f in entries(conn).items():
        if f["parent"]:
            out[fid] = f
    for lid in [k for k, e in out.items() if e["kind"] == "line"]:
        out[lid]["words"] = line_words(out, lid)
    return out


# ---- 调用与记录 ----

def _usage(meta):
    u = meta.pop("usage", None)
    return u if isinstance(u, list) else None


def _call(model, text):
    try:
        output, meta = invoke.call(model, text)
        return output, meta, None
    except Exception as e:          # 失败也是一次发生
        return "", {}, repr(e)


def _act(conn, op_id, step, unit, window, inputs, text, output, meta, error, h, cause):
    result, err = outcome(error)
    meta["expression"] = express.evidence(instance.expression(f"form.{step}", author="memory-harness", addressee="model"), text)
    seconds = meta.pop("seconds", None)
    act = occurrence(conn, "act", op_id, started=st.iso(st.now(), seconds), ended=st.now(), scale="session", inputs=inputs,
                     cause=cause, predecessor=h.last.get((step, unit), NA), outcome=result, error=err,
                     cost=cost(seconds, _usage(meta)))
    conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                 (act, "form", step, dumps({"unit": list(unit), **window}), text, output, dumps(meta)))
    h.last[(step, unit)] = act
    if result in ("timeout", "content_error"):
        h.fails[(step, unit, op_id)][result] += 1
    return act, result, err


def build_pack(conn, u, model_desc):
    info, evs = u["session"], u["events"]
    turn_ord = {t["occurrence"]: t["ordinal"] for t in info["turns"]}
    got = observe.written(conn, info["source"], info["session"])
    written = {turn_ord[t]: lines for t, lines in got.items() if t in turn_ord and u["lo"] <= turn_ord[t] <= u["hi"]}
    text = "\n\n".join([prompt("pack").replace("{{条数}}", str(len(evs))), pack.env(info, model_desc),
                        "## 事件", pack.e_block(evs, written)])
    return redact(text), written           # 写进的文件和 E 在同一份文本里一起打码


def store_pack(conn, op_id, u, text, written, output, meta, error, h, cause):
    evs = u["events"]
    turns = sorted({e["turn"] for e in evs} | {t["occurrence"] for t in u["session"]["turns"]
                                                if t["ordinal"] in written})
    if error is None and not output.strip():
        error = "content_error: 空输出"
    name, topic, items, dropped, unknown = pack.parse(output, evs) if error is None else ("", "", [], [], [])
    if error is None and items == [] and output.strip() != "无":
        error = "content_error: 没有解析出陈述"
    meta.update(name=name, topic=topic, dropped=dropped, unknown=unknown, written={str(k): v for k, v in written.items()},
                events=len(evs))
    inputs = {"event": [e["id"] for e in evs], "turn": turns}
    if u.get("request"):                   # 按申请重做：这次 pack 就是认领（cause = request，inputs 带申请）
        inputs["request"], cause = u["request"], "request"
    act, result, err = _act(conn, op_id, "pack", u["unit"], {"session": list(u["unit"][:2])},
                            inputs, text, output, meta, error, h, cause)
    if result == "ok":
        for i, it in enumerate(items):
            conn.execute("INSERT INTO form_record(born, idx, kind, text, refs, labels, structures, unresolved) VALUES(?,?,?,?,?,?,?,?)",
                         (act, i, "session", it["text"], dumps({"event": pack.refs(it)}), dumps([f"陈述{it['n']}"]),
                          dumps({"basis": it["basis"], "quotes": it["quotes"], "files": it["files"]}),
                          dumps([x for x in pack.refs(it) if x in unknown])))
        h.ok[("pack", u["unit"])] = act
    conn.commit()
    return f"{result}：{err[:160]}" if result != "ok" else f"陈述 {len(items)} 条（E {len(evs)}）"


def _others(conn, u):
    """同一工作区已有的会话入口（不含这个会话自己的）。"""
    return {k: e for k, e in entries(conn).items() if e["ws"] == u["session"]["workspace"] and e["session"] != tuple(u["unit"][:2])}


def _unit_of(conn, act):
    return tuple(json.loads(conn.execute("SELECT window FROM act_detail WHERE occurrence=?", (act,)).fetchone()[0])["unit"])


def _restart(conn, u):
    """这次重做要不要连起名、挂线一起从头来：主会话的申请（会话被切成两份这类）要；observe 的修订提示不要（2026-10-21：
    只是修正几条 E，接原来的入口、沿用挂线，T 一级的细节对不上可以，L 一级要有明确的接续）。"""
    ids = u.get("request") or []
    return bool(ids) and conn.execute(f"SELECT COUNT(*) FROM request_detail WHERE occurrence IN ({','.join('?' * len(ids))}) "
                                      f"AND from_process=?", (*ids, st.MAIN)).fetchone()[0] > 0


def _previous(conn, u):
    """这段会话原来的会话入口 → (入口那一行, 续上 | 重新整理, 它最后挂到的线编号或 None)；没有就是 None。"""
    f = conn.execute("SELECT * FROM session_entry WHERE source=? AND session=? ORDER BY id DESC LIMIT 1", tuple(u["unit"][:2])).fetchone()
    if f is None:
        return None
    labeled = any(_unit_of(conn, r["record"]) == tuple(u["unit"]) for r in conn.execute(
        "SELECT record FROM session_entry WHERE source=? AND session=?", tuple(u["unit"][:2])))
    kind = "重新整理" if labeled else "续上"                     # 这个单位以前起过名，就是重新整理
    a = conn.execute("SELECT line FROM attachment WHERE entry=? ORDER BY id DESC LIMIT 1", (f["id"],)).fetchone()
    return f, kind, a["line"] if a else None


def shared_context(conn, u, h=None):
    """分叉（2026-10-19）：这段会话开头和别的会话是同一段对话（fork、续接的副本），那几轮的 E 已经被先整理的会话用掉，
    不再整理。起名时附上那几份会话记录里依据全在这些 E 上的陈述（跨过分叉点的不附：里面有对方分开以后的事）。
    → (附上的文本, 附了几条, 跨过分叉点没附的几条)；没有共用的轮次就是 ("", 0, 0)。"""
    h = h or history(conn)
    source, sess = u["unit"][:2]
    mine = {e["id"] for e in u["events"]}
    shared = {e["id"] for e in observe.frame(conn, source, sess)["events"]
              if e["scale"] in NEW_SCALES and e["id"] in h.packed and e["id"] not in mine}
    if not shared:
        return "", 0, 0
    lines, mixed = [], 0
    for (step, unit), act in sorted(h.ok.items(), key=lambda x: x[1]):
        if step != "pack" or unit[:2] == (source, sess):
            continue
        for r in conn.execute("SELECT text, structures FROM form_record WHERE born=? ORDER BY idx", (act,)):
            basis = set(json.loads(r["structures"]).get("basis", []))
            if basis and basis <= shared:
                lines.append(f"- {r['text']}")
            elif basis & shared:
                mixed += 1
    if not lines:
        return "", 0, mixed
    at = min(e["ordinal"] for e in u["events"])
    return ("\n\n## 这段对话的前 " + str(at) + " 轮\n\n（这几轮和别的会话是同一段对话，已经整理过；下面是那份会话记录里只关于这几轮的陈述。"
            "这次的会话记录只有之后的部分）\n" + "\n".join(lines)), len(lines), mixed


def build_label(conn, u, pack_act, corpus=None, h=None):
    """→ (输入, 附上的相近会话入口)。这段会话原来有会话入口的（续上、重新整理），附上原来的；开头和别的会话共用的（分叉），
    附上共用那几轮的陈述（shared_context）。"""
    name, topic, items, _ = record(conn, pack_act)
    rec = label.record_text(name, topic, items)
    others = _others(conn, u)
    near = label.similar(name, others, u["session"]["workspace"], corpus)
    prev = None if _restart(conn, u) else _previous(conn, u)    # 主会话申请的重做：下游跟着重做，不接原来的入口
    before = label.previous_text(prev[0]["name"], prev[0]["topic"], [s["text"] for s in json.loads(prev[0]["stops"])],
                                 json.loads(prev[0]["words"]), prev[1]) if prev else shared_context(conn, u, h)[0]
    return redact(prompt("label") + "\n\n## 会话记录\n\n" + rec + glossary.text(rec) + before
                  + label.existing_text(others, near)), near


def store_label(conn, op_id, u, pack_act, text, near, output, meta, error, h, cause):
    pname, topic, items, _ = record(conn, pack_act)
    name, stops, lists = label.parse(output) if error is None else ("", [], [])
    if error is None and not lists:
        error = "content_error: 没有解析出索引词"
    by_n = {it["n"]: it["id"] for it in items}
    meta.update(near=near)
    if error is None:                      # 程序查重：同一工作区已有的名字（并行起名的同一批也看得到，已经先写进库）
        name, dup = label.dedup(name or pname, [e["name"] for e in _others(conn, u).values()], _date(conn, pack_act))
        if dup is not None:
            meta.update(renamed_from=dup)
    prev = None if _restart(conn, u) else _previous(conn, u)
    if prev:                               # 续上、重新整理：取代原来的入口，挂线由程序沿用
        meta.update(previous={"entry": prev[0]["id"], "kind": prev[1]})
        lists = label.merge(json.loads(prev[0]["words"]), lists)     # 重新整理的可能只是前面一段，原来的词也留着
        if prev[2] is not None:
            meta.update(carry=prev[2])
    meta.update(name=name or pname)
    act, result, err = _act(conn, op_id, "label", u["unit"], {"pack": pack_act}, {"act": [pack_act]},
                            text, output, meta, error, h, cause)
    track.internal(conn, act, "label", [handle_of(x) for x in near], u["unit"][1])     # 查相近的话题是一次内部拉取
    if result == "ok":
        stops_j = [{"text": s, "basis": [by_n[n] for n in nums if n in by_n]} for s, nums in stops]
        unresolved = [n for _, nums in stops for n in nums if n not in by_n]
        entry = conn.execute("INSERT INTO session_entry(born, record, source, session, name, topic, stops, words, unresolved) "
                             "VALUES(?,?,?,?,?,?,?,?,?)", (act, pack_act, u["unit"][0], u["unit"][1], name or pname, topic,
                                                          dumps(stops_j), dumps(lists), dumps(unresolved))).lastrowid
        h.ok[("label", u["unit"])] = act
        if prev and prev[2] is not None:
            late = [e["id"] for e in u["events"] if e["id"] not in json.loads(
                conn.execute("SELECT inputs FROM occurrence WHERE id=?", (prev[0]["record"],)).fetchone()[0]).get("event", [])]
            why = (f"续上：沿用 T{prev[0]['id']} 挂的线" if prev[1] == "续上" else
                   f"待审视：晚到的 E 插进了已整理的范围（{'、'.join(f'E{i}' for i in late[:10])}），沿用 T{prev[0]['id']} 挂的线")
            conn.execute("INSERT INTO attachment(born, entry, line, why) VALUES(?,?,?,?)", (act, entry, prev[2], why))
            h.carried[u["unit"]] = act
    conn.commit()
    return f"{result}：{err[:160]}" if result != "ok" else f"「{name or pname}」，索引词 {len(lists)} 组"


def build_attach(conn, u, label_act, the_pool, corpus):
    """→ (步骤 attach | attach_lines, 输入, 骨架, 检索挑出的, 写进的文件落到的节点)。"""
    f = conn.execute("SELECT * FROM session_entry WHERE born=?", (label_act,)).fetchone()
    _, _, items, written = record(conn, f["record"])
    nums = {it["id"]: it["n"] for it in items}
    words = label.words(json.loads(f["words"]), items)
    stops = [_stop_text(s, nums) for s in json.loads(f["stops"])]
    ws = u["session"]["workspace"]
    flat = [a for g in words["lists"] for a in g]
    reach = st.reachable(conn, ws)                   # 候选和检查用同一个范围：看得到的都挂得上
    home = lambda e: the_pool[e["parent"]]["ws"] if e["kind"] == "entry" and e["parent"] in the_pool else e["ws"]
    view = {k: e for k, e in the_pool.items() if home(e) in reach}   # 会话入口按它所在的线算：模型挂的是那条线
    picks = [eid for eid, _ in retrieve.search(flat, f["name"] + " " + f["topic"] + " " + " ".join(flat), view, ws, corpus)]
    entry = attach.entry_text(f["name"], f["topic"], stops, words, _date(conn, f["record"]))
    ns = skeleton.nodes(ws, conn)
    count = collections.Counter()
    if ns:
        for lines in written.values():
            for line in lines:
                paths = [line[3:]] if line.startswith("写了 ") else (line.split("」：", 1)[1].split("、") if "」：" in line else [])
                for p in paths:
                    n = skeleton.node_of(p.strip(), ws, ns)
                    if n is not None:
                        count[n] += 1
    step = "attach" if ns else "attach_lines"
    return step, redact(attach.build(prompt(step), entry, the_pool, picks, ns, dict(count))), ns, picks, dict(count)


def store_attach(conn, op_id, u, label_act, step, text, ns, picks, count, the_pool, output, meta, error, h, cause):
    node, target, parent, why = attach.parse(output, bool(ns)) if error is None else (None, "", None, "")
    name = attach.new_line(target)
    target, parent = (target if name else handle_of(target)), (handle_of(parent) if parent else parent)
    if error is None:
        if not target:
            error = "content_error: 认不出挂到哪里"
        elif name is None and (target not in the_pool or the_pool[target]["kind"] != "line"):
            error = f"content_error: {target} 不是已有的线"
        elif name is None and the_pool[target]["ws"] not in st.reachable(conn, u["session"]["workspace"]):
            error = f"content_error: {target} 属于别的工作区"
        elif ns and (not node or node not in {n["id"] for n in ns.values()}):
            error = f"content_error: 节点 {node} 不在骨架里"
        elif ns and name is None and the_pool[target]["parent"] != node:
            error = f"content_error: {target} 没挂在节点 {node} 下"
    meta.update(node=node, target=target, parent=parent, why=why, picks=picks, nodes=count)
    inputs = {"act": [label_act]}
    registered = skeleton.adopted(conn).get(u["session"]["workspace"])
    if registered:
        meta["skeleton_act"] = registered["act"]
        inputs["act"].append(registered["act"])
    act, result, err = _act(conn, op_id, "attach", u["unit"], {"label": label_act}, inputs,
                            text, output, meta, error, h, cause)
    track.internal(conn, act, "attach", picks, u["unit"][1])                     # 挂线的候选是一次内部拉取
    if result == "ok":
        entry_id = conn.execute("SELECT id FROM session_entry WHERE born=?", (label_act,)).fetchone()[0]
        if name is not None:
            path = next((p for p, n in ns.items() if n["id"] == node), None) if ns else None
            par = the_pool.get(parent) if parent else None
            line = conn.execute('INSERT INTO topic_line(born, name, workspace, node, parent, "from") VALUES(?,?,?,?,?,?)',
                                (act, name, u["session"]["workspace"], path if path is not None else NA,
                                 par["row"] if par and par["kind"] == "line" else NA, "[]")).lastrowid
        else:
            line = the_pool[target]["row"]
        conn.execute("INSERT INTO attachment(born, entry, line, why) VALUES(?,?,?,?)", (act, entry_id, line, why))
        h.ok[("attach", u["unit"])] = act
    conn.commit()
    return f"{result}：{err[:160]}" if result != "ok" else (f"新线「{name}」" if name else f"挂到 {target}") + (f"（{node}）" if node else "")


def revisions(conn):
    """该改写陈述的修订提示：observe 写来的、还没处理的、已经被用到（revision_due）的，按编号。现在只认纠正写来的
    （parent 是 observe 的 correct act）；开关的撤下、恢复还没有真实案例，留着。"""
    out = []
    for r in st.requests(conn, "form", "open"):
        if r["from"] != "observe" or absent(r["parent"]):
            continue
        step = conn.execute("SELECT step FROM act_detail WHERE occurrence=?", (r["parent"],)).fetchone()
        if step and step[0] == "correct" and revision_due(conn, r):
            out.append(r)
    return out


def restate_job(conn, r):
    """一张修订提示 → 改写的输入：(文本, 受影响的陈述 [(编号 N, 陈述行)], 给的 E, 变化) 或 None（没有受影响的陈述）。
    受影响的：这段会话现在的陈述里，依据（来源、引用、文件）有这些 E 的。"""
    changes, events = [], {}
    for eid in r["inputs"].get("event", []):
        old = conn.execute("SELECT * FROM event WHERE id=?", (eid,)).fetchone()
        fates = [f for f in observe.fate(conn, eid) if f["how"] in ("corrected", "invalid")]
        if old is None or not fates:
            continue
        last = fates[-1]["act"]                 # 最近那次纠正；一条旧 E 可能拆成几条新 E，各自一条关系，都要给
        f = [x for x in fates if x["act"] == last]
        new = [conn.execute("SELECT * FROM event WHERE id=?", (y,)).fetchone() for x in f for y in x["by"]] \
            if f[0]["how"] == "corrected" else None
        changes.append((old, new))
    if not changes:
        return None
    changed = {o["id"] for o, _ in changes}
    swap, stmts = replacements(conn), []
    for e in entries(conn).values():
        for it in e["items"]:
            row = conn.execute("SELECT * FROM form_record WHERE id=?", (it["id"],)).fetchone()
            if changed & set(json.loads(row["refs"]).get("event", [])):
                stmts.append((int(re.sub(r"\D", "", str(it["n"])) or 0), row))
    if not stmts:
        return None
    ids = sorted({x for _, row in stmts for x in json.loads(row["refs"]).get("event", [])}
                 | {x["id"] for _, new in changes for x in (new or [])})
    by = {e["id"]: e for e in observe.frame(conn)["events"] if e["id"] in set(ids)}
    rows = {r_["id"]: r_ for r_ in conn.execute(f"SELECT * FROM event WHERE id IN ({','.join('?' * len(ids))})", ids)}
    evs = lambda row: [by.get(x) or {"id": x, "text": rows[x]["text"], "speaker": "?", "mode": "?", "segments": []}
                       for x in json.loads(row["refs"]).get("event", []) if x in rows]
    head = prompt("restate")
    text = _restate.build(head, changes, [(n, row, evs(row)) for n, row in stmts])
    given = [by.get(x) or {"id": x} for x in ids]
    return text, stmts, given, changes


def restate(conn, models=None, log=sys.stderr, limit=10, cause="request"):
    """修订提示认领以后只改写依据那几条 E 的陈述（REVISE = statements，2026-10-21）：一张修订提示一次调用；
    改写的陈述取代原来那条（relations），撤下的记在 act 的 meta.withdrawn。失败按这张申请记次数，满 MAX_FAILURES 不再试。
    返回 (处理了几张, 环境错误几次)。"""
    if REVISE != "statements":
        return 0, 0
    todo = revisions(conn)[:limit]
    if not todo:
        return 0, 0
    mod = (models or {}).get("restate") or MODELS[CURRENT["restate"][0]]()
    op = operator(conn, "restate")[0]
    conn.commit()
    done = env = 0
    for r in todo:
        fails = sum(1 for _, x in r["acts"] if x in ("timeout", "content_error"))
        if fails >= MAX_FAILURES:
            continue
        job = restate_job(conn, r)
        sess = None
        if job:
            first = job[1][0][1]
            sess = _unit_of(conn, first["born"])[:2] if first else None
        unit = (*(sess or ("?", "?")), f"R{r['id']}")
        if job is None:                    # 没有受影响的陈述：不调模型，记一次处理
            act = occurrence(conn, "act", op, started=st.now(), ended=st.now(), scale="session",
                             inputs={"request": [r["id"]], "event": r["inputs"].get("event", [])}, cause=cause, outcome="ok")
            conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (act, "form", "restate", dumps({"unit": list(unit)}), "", "",
                                                                          dumps({"reason": "没有依据这些 E 的现行陈述"})))
            conn.commit()
            done += 1
            continue
        text, stmts, given, changes = job
        output, meta, error = _call(mod, text)
        got = _restate.parse(output, given) if error is None else {}
        if error is None and not got:
            error = "content_error: 没有解析出陈述"
        wanted = {n for n, _ in stmts}
        withdrawn = {str(row["id"]): got[n]["withdraw"] for n, row in stmts if n in got and "withdraw" in got[n]}
        meta.update(withdrawn=withdrawn, missing=sorted(wanted - set(got)), extra=sorted(set(got) - wanted))
        h = types.SimpleNamespace(last={}, fails=collections.defaultdict(collections.Counter))
        act, result, err = _act(conn, op, "restate", unit, {}, {"request": [r["id"]], "form_record": [row["id"] for _, row in stmts],
                                "event": [x["id"] for x in given]}, text, output, meta, error, h, cause)
        n_new = 0
        if result == "ok":
            known = {x["id"] for x in given}
            for i, (n, row) in enumerate(stmts):
                it = got.get(n)
                if not it or "withdraw" in it:
                    continue
                conn.execute("INSERT INTO form_record(born, idx, kind, text, refs, labels, structures, unresolved, relations) "
                             "VALUES(?,?,?,?,?,?,?,?,?)",
                             (act, i, "session", it["text"], dumps({"event": pack.refs(it)}), row["labels"],
                              dumps({"basis": it["basis"], "quotes": it["quotes"], "files": it["files"]}),
                              dumps([x for x in pack.refs(it) if x not in known]), dumps([{"kind": "supersedes", "to": row["id"]}])))
                n_new += 1
        conn.commit()
        done += 1
        env += result == "env_error"
        print(f"[改写] 修订提示 {r['id']} {unit[1][:12]}：" + (f"{result}：{err[:160]}" if result != "ok" else
              f"改写 {n_new} 条、撤下 {len(withdrawn)} 条（受影响 {len(stmts)} 条）"), file=log, flush=True)
        if env:
            break
    return done, env


def run(conn, limit=10, workers=4, session=None, cause="new_text", log=sys.stderr, models=None, now=None, corpus=None):
    """跑一批（rhythm 调它）：最多 limit 个单位。pack、label 可以并行（模型调用并行，库只在主线程里写）；attach 按时间顺序一个个来。
    返回 (处理了几件, 环境错误几次)；有环境错误时这一批提前结束。"""
    mods = models or {step: MODELS[name]() for step, (name, _) in CURRENT.items()}
    ops = {step: operator(conn, step)[0] for step in CURRENT}
    conn.commit()                       # 登记完就提交：没活干时不能带着写事务回去休眠（A14 撞锁）
    done, env = restate(conn, models=mods, log=log, limit=limit) if session is None else (0, 0)   # 修订提示：只改写那几条陈述
    if env:
        return done, env
    h = history(conn)
    todo = units(conn, now, session)[:limit]

    def parallel(jobs, store_one, tag):
        nonlocal done, env
        with concurrent.futures.ThreadPoolExecutor(max(1, workers)) as pool_:
            futures = {pool_.submit(_call, mods[j["step"]], j["text"]): j for j in jobs}
            for fu in concurrent.futures.as_completed(futures):
                j = futures[fu]
                output, meta, error = fu.result()
                state = store_one(j, output, meta, error)
                env += state.startswith("env_error")
                done += 1
                print(f"[{tag}] {j['u']['unit'][1][:12]}：{state}", file=log, flush=True)

    # ① pack
    jobs = []
    for u in todo:
        if (("pack", u["unit"]) not in h.ok or u.get("redo")) and not _failed(h, "pack", u["unit"], ops["pack"]):
            text, written = build_pack(conn, u, DESC.get(CURRENT["pack"][0], CURRENT["pack"][0]))
            jobs.append({"u": u, "step": "pack", "text": text, "written": written})
    parallel(jobs, lambda j, o, m, e: store_pack(conn, ops["pack"], j["u"], j["text"], j["written"], o, m, e, h, cause), "整理")
    if env:
        return done, env
    # ② label（整理出“无”的单位没有陈述，不写会话入口）
    corpus = corpus or retrieve.Corpus(conn)
    jobs = []
    for u in todo:
        pa, la = h.ok.get(("pack", u["unit"])), h.ok.get(("label", u["unit"]))
        before = [x for (step, x) in h.ok if step == "pack" and x[:2] == u["unit"][:2] and x[2] < u["unit"][2]]
        if any(not _complete(h, x) and not any(_failed(h, s, x, ops[s]) for s in ("pack", "label")) for x in before):
            continue                       # 续上的要等这段会话前面的单位挂好线，才看得到原来的入口
        if pa and (la is None or la < pa) and not _failed(h, "label", u["unit"], ops["label"]) and u["unit"] not in h.empty:
            text, near = build_label(conn, u, pa, corpus, h)
            jobs.append({"u": u, "step": "label", "text": text, "near": near, "pack": pa})
    parallel(jobs, lambda j, o, m, e: store_label(conn, ops["label"], j["u"], j["pack"], j["text"], j["near"], o, m, e, h, cause),
             "起名")
    if env:
        return done, env
    # ③ attach：按时间顺序；前面的单位还没做完（也没失败满）就停，下一批接着来
    for u in todo:
        unit = u["unit"]
        if _complete(h, unit):
            continue                                                   # 挂好了、沿用了原来的挂线，或者整理出“无”
        pa, la = h.ok.get(("pack", unit)), h.ok.get(("label", unit))
        if not la or la < pa:
            if _failed(h, "pack", unit, ops["pack"]) or _failed(h, "label", unit, ops["label"]):
                continue
            break
        step = "attach" if skeleton.nodes(u["session"]["workspace"], conn) else "attach_lines"
        if _failed(h, "attach", unit, ops[step]):
            continue
        the_pool = pool(conn)
        step, text, ns, picks, count = build_attach(conn, u, la, the_pool, corpus)
        output, meta, error = _call(mods[step], text)
        state = store_attach(conn, ops[step], u, la, step, text, ns, picks, count, the_pool, output, meta, error, h, cause)
        done += 1
        print(f"[挂线] {unit[1][:12]}：{state}", file=log, flush=True)
        if state.startswith("env_error"):
            env += 1
            break
        if not _complete(h, unit) and not _failed(h, "attach", unit, ops[step]):
            break                                                      # 这次没挂上：后面的等下一批
    return done, env


# ---- 反馈（主会话直写，2026-10-18；第一次用在 2026-10-19）----

def feedback_entry(conn, operator_id, entry_id, reason, name=None, topic=None, stops=None, holder="我"):
    """会话入口的反馈：主会话给出改好的名字、主题或停在，写一个新入口取代原来的（同一段会话以最后写的为准），form 不重判。
    一次 form 的 act（step = feedback，cause = manual，Operator 是主会话的模型），inputs 指向写出原入口的那次 act；
    挂线由程序沿用。新名字和同一工作区已有的入口重名就拒绝（名字是主会话给的，不自动加日期）。返回新入口的编号。"""
    f = conn.execute("SELECT * FROM session_entry WHERE id=?", (entry_id,)).fetchone()
    if f is None:
        raise ValueError(f"没有这个会话入口：T{entry_id}")
    latest = conn.execute("SELECT max(id) FROM session_entry WHERE source=? AND session=?", (f["source"], f["session"])).fetchone()[0]
    if latest != entry_id:
        raise ValueError(f"T{entry_id} 已经被 T{latest} 取代：对现在的入口反馈")
    if not reason.strip() or not (name or topic or stops):
        raise ValueError("反馈要给出改好的内容和理由；只说不对、给不出对的，写申请")
    if name:
        cwd = conn.execute("SELECT cwd FROM session_detail WHERE source=? AND session=?", (f["source"], f["session"])).fetchone()[0]
        ws = st.session_workspace(f["source"], f["session"], cwd)
        clash = [e["id"] for e in entries(conn).values() if e["ws"] == ws and e["session"] != (f["source"], f["session"])
                 and label.norm(e["name"]) == label.norm(name)]
        if clash:
            raise ValueError(f"「{name}」和 {'、'.join(clash)} 重名")
    mine = record(conn, f["record"])[2]
    nums = {**{it["was"]: it["n"] for it in mine}, **{it["id"]: it["n"] for it in mine}}
    by_n = {n: i for i, n in nums.items()}
    old_stops = json.loads(f["stops"])
    new_stops = old_stops if stops is None else [{"text": t, "basis": [by_n[n] for n in ns if n in by_n]} for t, ns in stops]
    change = {k: v for k, v in (("name", name), ("topic", topic), ("stops", stops)) if v is not None}
    act = occurrence(conn, "act", operator_id, started=st.now(), ended=st.now(), scale="session", inputs={"act": [f["born"]]},
                     cause="manual", outcome="ok")
    conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                 (act, "form", "feedback", dumps({"entry": entry_id}), dumps(change), "",
                  dumps({"reason": reason, "holder": holder, "supersedes": f"T{entry_id}"})))
    new = conn.execute("INSERT INTO session_entry(born, record, source, session, name, topic, stops, words, unresolved) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (act, f["record"], f["source"], f["session"], name or f["name"], topic or f["topic"],
                                                    dumps(new_stops), f["words"], f["unresolved"])).lastrowid
    line = conn.execute("SELECT line FROM attachment WHERE entry=? ORDER BY id DESC LIMIT 1", (entry_id,)).fetchone()
    if line:
        conn.execute("INSERT INTO attachment(born, entry, line, why) VALUES(?,?,?,?)",
                     (act, new, line[0], f"反馈：{'、'.join({'name': '改名', 'topic': '改主题', 'stops': '改停在'}[k] for k in change)}，"
                                         f"沿用 T{entry_id} 挂的线"))
    conn.commit()
    return new


def feedback_attach(conn, operator_id, entry_id, reason, line=None, new_line=None, node=None, holder="我"):
    """挂线的反馈（主会话直写，2026-10-20 第一次用在 Synapse 换库的两段会话上）：把一个会话入口改挂到已有的线 line（L 编号），
    或者新起一条线 new_line（名字），有骨架的工作区要给节点 node（k-…）。一次 form 的 act（step = feedback，cause = manual），
    新写一条挂线（同一个入口以最后一条为准），原来的挂线留着。线的工作区按这段会话的工作区（st.session_workspace）。返回挂到的线编号。"""
    f = conn.execute("SELECT * FROM session_entry WHERE id=?", (entry_id,)).fetchone()
    if f is None:
        raise ValueError(f"没有这个会话入口：T{entry_id}")
    latest = conn.execute("SELECT max(id) FROM session_entry WHERE source=? AND session=?", (f["source"], f["session"])).fetchone()[0]
    if latest != entry_id:
        raise ValueError(f"T{entry_id} 已经被 T{latest} 取代：对现在的入口反馈")
    if not reason.strip() or (line is None) == (new_line is None):
        raise ValueError("挂线的反馈要给理由，并且只给一个去处：已有的线，或者新线的名字")
    cwd = conn.execute("SELECT cwd FROM session_detail WHERE source=? AND session=?", (f["source"], f["session"])).fetchone()[0]
    ws = st.session_workspace(f["source"], f["session"], cwd)
    ns = skeleton.nodes(ws, conn)
    path = NA
    if new_line is not None:
        if ns:
            path = next((p for p, n in ns.items() if n["id"] == node), None)
            if path is None:
                raise ValueError(f"节点 {node} 不在 {ws} 的骨架里")
        if any(label.norm(l["name"]) == label.norm(new_line) for l in pool(conn).values() if l["kind"] == "line" and l["ws"] == ws):
            raise ValueError(f"「{new_line}」和 {ws} 已有的线重名：挂到那条线上")
    else:
        lid = int(handle_of(line)[1:])
        if conn.execute("SELECT 1 FROM topic_line WHERE id=?", (lid,)).fetchone() is None:
            raise ValueError(f"没有这条线：{line}")
    change = {"line": line} if line is not None else {"new_line": new_line, "node": node}
    act = occurrence(conn, "act", operator_id, started=st.now(), ended=st.now(), scale="session", inputs={"act": [f["born"]]},
                     cause="manual", outcome="ok")
    conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                 (act, "form", "feedback", dumps({"entry": entry_id}), dumps(change), "", dumps({"reason": reason, "holder": holder})))
    if new_line is not None:
        lid = conn.execute('INSERT INTO topic_line(born, name, workspace, node, parent, "from") VALUES(?,?,?,?,?,?)',
                           (act, new_line, ws, path, NA, "[]")).lastrowid
    conn.execute("INSERT INTO attachment(born, entry, line, why) VALUES(?,?,?,?)", (act, entry_id, lid, f"反馈：{reason}"))
    conn.commit()
    return lid


def feedback(conn, operator_id, handle, reason, name=None, topic=None, stops=None, holder="我"):
    """反馈的入口（和拉取工具并列的 feedback 工具调它）：现在只接会话入口（T…，旧写法 s-… 也认）。给了改好的名字、主题或停在，写成反馈
    （feedback_entry）；只说了不对、没给内容的，转成给 form 的申请，指向这个入口的会话记录，从 pack 起重做这段会话。
    stops：几条文字，可以带“（陈述3、陈述4）”指出依据。→ 一行说明。"""
    h = handle_of(handle)
    if not re.fullmatch(r"T\d+", h):
        raise ValueError(f"现在只接会话入口（T 编号）：{handle}")
    entry_id = int(h[1:])
    if name or topic or stops:
        parsed = None if stops is None else [(re.sub(r"（(?:陈述\d+[、，, ]*)+）\s*$", "", t).strip(),
                                               [int(n) for n in re.findall(r"陈述(\d+)", t)]) for t in stops]
        new = feedback_entry(conn, operator_id, entry_id, reason, name=name, topic=topic, stops=parsed, holder=holder)
        return f"反馈已写：{h} → T{new}"
    f = conn.execute("SELECT record FROM session_entry WHERE id=?", (entry_id,)).fetchone()
    if f is None:
        raise ValueError(f"没有这个会话入口：{h}")
    recs = [r[0] for r in conn.execute("SELECT id FROM form_record WHERE born=? ORDER BY idx", (f["record"],))]
    rid = st.request(conn, st.MAIN, "form", {"form_record": recs}, reason, operator_id, cause="manual")
    conn.commit()
    return f"没给改好的内容，转成申请 {rid}：form 会从整理起重做这段会话"


def redo(conn):
    """待重做清单：当前 Operator 下失败满次数、又没有成功的单位。[(步骤, 单位, {失败种类: 次数})]"""
    h = history(conn)
    ops = {operator(conn, step)[0] for step in CURRENT}
    return [(step, unit, dict(c)) for (step, unit, op), c in h.fails.items()
            if op in ops and (step, unit) not in h.ok and any(n >= MAX_FAILURES for n in c.values())]
