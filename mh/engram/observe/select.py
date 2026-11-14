"""带视角筛 E（design/engram/observe/设计.md“导入的对话带着视角读”，用户 2026-11-11）。

导入的来源（实例配置 [engram].views：来源 → 视角）读出 E 以后、交给 form 以前，模型带着视角把一段会话的 E 筛一遍：
看{用户}每一轮的原话（长的截断）和这一轮的 E，列出不留的。不留的写一条收回（step = prune，和程序收回同一层，
pruned() 一起认），理由是视角；出处里有用户原话的程序保底留下（同 prune 的规矩）。筛过的 E 记在 act 的 meta.considered，
一段会话还有没筛过的 E，form 就先不整理它（pending）。

试跑在 tmp/chatgpt-eval/（trial.py、select-v0.2 → 这里的 select-view-v0.1；两段会话 E 留下约四成到一半，陈述总字数降到 1/4–1/6）。
"""
import hashlib
import json
import re
import sys

from .. import invoke
from .. import store as st
from ..store import NA, UNKNOWN, cost, dumps, occurrence, operator as register, outcome
from ..store import instance
from ... import express

PROMPTS = {"mh 发展史": "select-view-v0.1"}     # 视角 → 提示词（提示词里写着这个视角要什么；别的视角从原文重读，不在这里筛）
MODEL = ("gpt-6.1-sol", "low")
USER_CUT = 400                                  # 用户原话超过这么多字截断（转贴的长文本只要知道转了什么）
NEW_SCALES = ("turns", "segment")
_DROP = re.compile(r"E(\d+)")


def model():
    return invoke.whole(invoke.Codex(*MODEL))


def operator(conn, view):
    """登记并返回 (编号, 名字)：模型 + 提示词 + 视角。"""
    name = PROMPTS[view]
    body = (_dir() / f"{name}.md").read_bytes()
    iso = getattr(model(), "isolation", "") or NA
    params = {"view": view, "user_cut": USER_CUT, "guard": "出处有用户原话的留下", "expression": express.signature()}
    full = f"sol-low/{name}.{iso}-{hashlib.sha256(body + iso.encode() + dumps(params).encode()).hexdigest()[:8]}"
    oid = register(conn, full, "observe", "observer", model=MODEL[0], prompt=name, prompt_hash=hashlib.sha256(body).hexdigest()[:16],
                   isolation=iso, window="session", params=params)
    conn.commit()                    # 登记完就提交：后面算 todo 要读很久，不能带着写事务（11-11 撞锁，form 报 database is locked）
    return oid, full


def _dir():
    from pathlib import Path
    return Path(__file__).parent / "prompts"


def considered(conn, op_id):
    """这个 Operator 成功筛过的 E。"""
    out = set()
    for (meta,) in conn.execute("SELECT a.meta FROM act_detail a JOIN occurrence o ON o.id=a.occurrence "
                                "WHERE a.process='observe' AND a.step='prune' AND o.operator=? AND o.outcome='ok'", (op_id,)):
        out.update(json.loads(meta or "{}").get("considered", []))
    return out


def _events(conn, source, session):
    """这段会话交给 form 的 E（不看暂停的撤下：筛的正是暂停着的来源），去掉被取代、退场、已收回的。"""
    from .. import observe
    ctx = {"hidden": observe.superseded(conn) | observe.fast_retired(conn) | observe.pruned(conn), "marks": {}}
    return [e for e in observe.frame(conn, source, session, ctx=ctx)["events"]
            if e["scale"] in NEW_SCALES and e.get("step") != "correct"]


def todo(conn, paused=False):
    """要筛的会话：{(来源, 会话): (视角, [还没筛过的 E])}。会话还有轮次没观察完的先不筛（读完再筛一次）。
    暂停的来源（[engram].pause_sources）不筛；paused=True 时也筛（主会话手动重做那一批时用）。"""
    from .. import observe
    if not instance.VIEWS:
        return {}
    waiting = {(p["source"], p["session"]) for p in observe.progress(conn) if p["state"] in observe.TO_OBSERVE}
    out = {}
    for source, view in instance.VIEWS.items():
        if view not in PROMPTS or source in instance.PAUSE_SOURCES and not paused:
            continue
        op_id = operator(conn, view)[0]
        seen = considered(conn, op_id)
        for (session,) in conn.execute("SELECT session FROM session_detail WHERE source=?", (source,)):
            if (source, session) in waiting:
                continue
            new = [e for e in _events(conn, source, session) if e["id"] not in seen]
            if new:
                out[(source, session)] = (view, new)
    return out


def pending(conn):
    """还没筛完的会话（form 先不整理它们）。"""
    return set(todo(conn, paused=True))


def user_said(e):
    segs = e["segments"] if isinstance(e["segments"], list) else json.loads(e["segments"] or "[]")
    return any(str(x).split(":")[-1].startswith("U") for x in segs)


def build(conn, source, session, view, events):
    from ..form import pack
    users = {r[0]: r[1] or "" for r in conn.execute(
        "SELECT d.ordinal, x.user FROM turn_detail d JOIN turn_text x ON x.id=d.text WHERE d.source=? AND d.session=?", (source, session))}
    by = {}
    for e in events:
        by.setdefault(e["ordinal"], []).append(e)
    blocks = []
    for n in sorted(by):
        u = users.get(n, "")
        u = u if len(u) <= USER_CUT else u[:USER_CUT] + f"……（共 {len(u)} 字，截断）"
        blocks.append(f"## 第 {n + 1} 轮\n{instance.USER_NAME}原话：{u}\n" + "\n".join(pack.e_line(e) for e in by[n]))
    head = express.prompt((_dir() / f"{PROMPTS[view]}.md").read_text(), instance.USER_NAME).replace("{{视角}}", view)
    return head + "\n\n## 事件\n\n" + "\n\n".join(blocks)


def parse(output, events):
    """→ (不留的 E 编号, 理由行, 保底留下的)。只认这次给的 E。"""
    ids = {e["id"]: e for e in events}
    drop, reasons = set(), []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("删"):
            got = {int(x) for x in _DROP.findall(line.split("｜")[0])} & set(ids)
            drop |= got
            reasons.append(line)
    guard = {i for i in drop if user_said(ids[i])}
    return sorted(drop - guard), reasons, sorted(guard)


def run_one(conn, key, view, events, m=None, cause="new_text"):
    """筛一段会话，写一条 act。返回 (结果, 一句说明)。模型调用和写库都在这里（批量时一段一段来）。"""
    source, session = key
    op_id = operator(conn, view)[0]
    text = build(conn, source, session, view, events)
    started = st.now()
    try:
        output, meta = invoke.call(m or model(), text)
        error = None
    except Exception as e:           # 失败也是一次发生
        output, meta, error = "", {}, repr(e)
    drop, reasons, guard = parse(output, events) if error is None else ([], [], [])
    if error is None and not reasons and output.strip() != "全留":
        error = "content_error: 没有解析出删留"
    result, err = outcome(error)
    seconds = meta.pop("seconds", None)
    act = occurrence(conn, "act", op_id, started=started, ended=st.now(), scale="session", inputs={"event": drop},
                     cause=cause, outcome=result, error=err, cost=cost(seconds, st.usage_rows(meta.get("usage"))))
    conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                 (act, "observe", "prune", dumps({"session": [source, session]}), text, output,
                  dumps({"reason": f"视角：{view}", "view": view, "considered": [e["id"] for e in events], "reasons": reasons,
                         "kept_by_guard": guard, "events": len(events), "model": meta.get("model")})))
    conn.commit()
    return result, (f"{len(events)} 条 E，收回 {len(drop)}（保底留下 {len(guard)}）" if result == "ok" else f"{result}：{(err or '')[:160]}")


def run(conn, limit=10, log=sys.stderr, paused=False):
    """筛一批（observe 的循环里调）：返回 (处理了几段, 环境错误几次)；有环境错误就停。"""
    done = env = 0
    for key, (view, events) in list(todo(conn, paused).items())[:limit]:
        result, note = run_one(conn, key, view, events)
        print(f"[筛] {key[1][:12]}（{view}）：{note}", file=log, flush=True)
        done += 1
        if result == "env_error":
            env += 1
            break
    return done, env
