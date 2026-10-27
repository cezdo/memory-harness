"""observe：原话 → turn → event（design/engram/observe/设计.md）。模型只管语义，其余都是程序。

先预览再观察（2026-10-17，实验 tmp/observe-e3/、tmp/observe-e4/）：
- 预览（preview.py，Opus）：一个会话里还没预览的、已经结束的轮次，只给用户的话和每轮统计，把轮次分成几组（每组一轮或多轮）、
  每组定读法（精读、粗读、不读）。分组和读法存在预览 act 的 meta["reads"] 里（2026-10-17 上线时叫 segments，旧的照读）。
  什么时候预览：会话最后一轮开始后 IDLE 秒没有新的一轮（告一段落，按宿主分），整段一次预览，轮数不设上限（2026-10-19）。
  会话续上时，之前预览定下的分组（类型、判断、读法）给新的预览接着划分；前面的原话不再给，前面的轮次不重看、不重观察。
  一轮被一次成功的预览覆盖，就算处理过（不读也是处理）；旧版本的 E（逐轮观察）也算，旧的不重做。
- 再观察（2026-10-19）：只被旧版本逐轮观察过的轮次，用当前的做法从头预览、再精读、粗读一遍。入口是一张范围申请
  （request_reobserve：主会话写给 observe，inputs 列出会话；2026-10-21 之前是一条 step = reobserve 的标记，各补了一张申请），
  到达时间就是它写下的时刻；为它做的预览在 inputs 里带上这张申请（认领）。读完的那几轮，更早的 E 整轮被取代（superseded），
  留在那一轮上作历史锚点，不再交出去；form 只读新做法的 E。
- 一批怎么排（2026-10-19）：以会话为单位，一段会话的窗口在一批里接着预览完，form 不会看到半段；新会话和再观察资源平等，
  先到先得：新会话在它可以预览的那一刻到，再观察的会话在点名范围的那一刻到（同一刻到的从新往旧）。
- 观察：预览分出的每一组一次调用，给这几轮的全文和预览写的计划。精读 = dsh web minimal 原样带 bash（只放行字数命令），high；
  粗读 = 去掉全部工具，low。路径、文件名、链接打码后给观察者，输出后换回（guard.py）。不读的不调用。
- 不观察：用户用 /no-observe 关掉的会话（最后一次开关说了算），以及开关那一轮本身（switch/）。开关影响 observe 全流程
  （2026-10-17）：关掉的会话不预览、预览过的也不读；已经产出的 E 不再交给上一层（withheld）。开关只在 E 跨过
  observe → form 的边界时留记录（mark_switches）：已被 form 读过的，关掉记 withdraw、恢复记 restore（程序 act：哪些 E、
  因为哪一轮开关、理由）；没被 form 读过的两头都不记。E 本身不删。
- 失败：环境错误（额度、限流、连接……）不计数；超时和内容错误分开计，同一个预览窗口或同一组轮次（观察）在同一 Operator 下
  任一种满 MAX_FAILURES 次就不再自动重试，进待重做清单（redo），不挡后面的过程。被内容过滤拦下的就不要了：不重试，也不进清单。
- 每次调用是一条 act（只追加，失败也记）：预览的分组和读法存在它的 meta 里；观察解析出的每条 event 出生于观察的 act。
"""
import collections
import concurrent.futures
import hashlib
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import invoke
from ... import express
from ..store import instance
from ..store import blocks
from .. import store as st
from ..store import NA, NOT_DONE, UNKNOWN, absent, cost, dumps, occurrence, operator as register, outcome
from . import collect, correct as _correct, deep_rough_read, label, modes, parse, preview, switch, written_files as _written
from .label import USER_NAME

PROMPTS = Path(__file__).parent / "prompts"
# 当前的做法：步骤 → (模型定义, 提示词)，只在这里定义。提示词版本从 0.1 起：大改动加前面的数，小改动加后面的数（2026-10-17）
CURRENT = {"preview": ("sol", "preview-v0.6"),   # 2026-10-20 试用 Sol low；对照 tmp/preview-sonnet/
           "deep": ("deepseek-ark-web-deep", "deep-rough-read-v0.2"),
           "rough": ("luna-codex-rough", "rough-read-v0.2")}     # 2026-10-19 起（之前 deepseek-ark-web-rough + deep-rough-read-v0.1）；v0.2（10-26）：原话不是中文的，提法照原文写进「」
MODELS = {**preview.MODELS, **deep_rough_read.MODELS}
# 预览可以用的模型，按额度轮着用（2026-10-20：有程序兜底，Sol、Sonnet 效果差不多）。先后由节律按额度给（rhythm.pick），
# 不给时用 CURRENT 里的；一个环境错误就换下一个。各自是一个 Operator，同一个提示词。
POOL = {"preview": ("sol", "sonnet")}
MAX_FAILURES = 3
# 预览的窗口（2026-10-17）：决定预览一次看哪些轮，是 observe 的参数，计入预览的 Operator，不是节律的参数。
# 会话最后一轮开始后这么久没有新的一轮，就算告一段落，可以预览；按宿主分，尽量一次读完、少重复读
IDLE = {"claude": 3600, "codex": 2 * 3600, "dsh": 3 * 3600}
IDLE_DEFAULT = 3600
# 原来还有 MAX_WINDOW = 60（一次预览最多 60 轮，攒够了不等告一段落），2026-10-19 撤掉（用户）：一次都没触发过；提早预览没有
# 消费方（form 自己等会话告一段落）；最长的会话整段预览输入约 4 万字，放得下。碰到预览写粗的真实案例再在停顿最长处切开


NOTICE = {"condense": True, "event_cap": label.EVENT_CAP,      # 后台通知只留主要信息（label.user_text，2026-10-20）
          "compaction": True,                                    # 压缩总结只留标记（回溯按它定切法：show.compaction_cut）
          "fold": list(preview.FOLD)}                            # 通知、压缩不界定轮：预览里接在前一轮下面


def window_params(step):
    """影响模型看到什么的程序参数：进 Operator 的名字（摘要）和登记的 params，改了任何一个就是新的 Operator。"""
    if step == "preview":
        return {"idle": IDLE, "idle_default": IDLE_DEFAULT, "precise_share": preview.PRECISE_SHARE,
                "precise_turns": preview.PRECISE_TURNS, "precise_free": preview.PRECISE_FREE,
                "user_cap": preview.USER_CAP, "previous": True, "notice": NOTICE,
                "host_skip": preview.HOST_SKIP, "host_floor": preview.HOST_FLOOR, "host_hidden": True}
    if step == "rough":
        return {"block_cap": blocks.BLOCK_CAP, "tools": deep_rough_read.TOOLS[step], "masked": True, "cap": deep_rough_read.ROUGH_CAP,
                "split": [deep_rough_read.ROUGH_MIN, deep_rough_read.ROUGH_MAX], "rho": deep_rough_read.ROUGH_RHO, "float": deep_rough_read.ROUGH_FLOAT, "content_min": deep_rough_read.CONTENT_MIN, "long_cap": deep_rough_read.LONG_CAP,
                "files": "mention", "notice": NOTICE}      # files：附上读写的文件；mention（2026-10-25）：只有提到它们的段落合算 1 条
    return {"block_cap": blocks.BLOCK_CAP, "tools": deep_rough_read.TOOLS[step], "masked": True, "notice": NOTICE}


def operator(conn, step="preview", register_new=True, name=None, prompt=None, fast=False):
    """登记并返回 (编号, 名字)。名字 = 模型定义/提示词.隔离-摘要，摘要覆盖提示词、隔离和窗口参数（window_params）；
    任何一项变了就是新的 Operator，旧结果照样留着。register_new=False 只查不登记（只读的接口用），没登记过的编号是 None。
    name：用 POOL 里的另一个模型定义（默认 CURRENT 里的）。prompt：换一个提示词（纠正用 correct-v0.1，模型和窗口参数沿用这一步的）。
    fast：快道的粗读（不经预览、按封窗读，runbook I7）：窗口参数多一项 fast，是另一个 Operator。"""
    default, current = CURRENT[step]
    prompt = prompt or current
    name = name or default
    body = (PROMPTS / f"{prompt}.md").read_bytes()   # 模板属于做法；实际称呼在 act 的表达上下文中
    model = MODELS[name]()
    iso = getattr(model, "isolation", "") or NA
    wp = {**window_params(step), **({"fast": FAST} if fast else {}), "expression": express.signature()}
    full = f"{name}/{prompt}.{iso}-{hashlib.sha256(body + iso.encode() + dumps(wp).encode()).hexdigest()[:8]}"
    params = {"definition": name, "platform": getattr(model, "platform", UNKNOWN), "step": step, **wp}
    if not register_new:
        row = conn.execute("SELECT id FROM operator WHERE name=?", (full,)).fetchone()
        return (row[0] if row else None), full
    oid = register(conn, full, "observe", "observer", model=getattr(model, "model", UNKNOWN), prompt=prompt,
                   prompt_hash=hashlib.sha256(body).hexdigest()[:16], isolation=iso, window="turns", params=params)
    return oid, full


TURNS = """SELECT d.*, x.user, x.assistant, x.commentary, x.tools, o.started, s.cwd, s.model, s.title,
                  so.started AS session_started
           FROM turn_detail d JOIN occurrence o ON o.id=d.occurrence JOIN turn_text x ON x.id=d.text
           JOIN session_detail s ON s.source=d.source AND s.session=d.session JOIN occurrence so ON so.id=s.occurrence"""
ACTS = """SELECT o.id, o.operator, o.outcome, a.step, a.window, a.meta FROM act_detail a JOIN occurrence o ON o.id=a.occurrence
          WHERE a.process='observe' ORDER BY o.id"""


def history(conn):
    """观察史（只读 act）：
    done      已经处理过的原文 ID（成功或被过滤的预览覆盖的、旧版本逐轮观察过的；预览漏分组的 missing 不算）；
    previewed 其中被成功或被过滤的预览覆盖的；legacy  其中旧版本逐轮观察过的（再观察按这两个分）；
    fails     {(键, Operator): {timeout | content_error: 次数}}，环境错误不算；
    last      {键: 最近一次 act}，下一次同键的 act 以它为前身；
    finished  {("turns", (轮次…)): ok | filtered}：观察过的各组轮次；观察的键只认那几轮，不依赖窗口里别的键；
    windows   {("preview", 第一轮原文): [这个窗口各轮的原文]}：预览窗口最近一次覆盖到的轮次（失败满次数时整窗跳过）。"""
    turn_text = dict(conn.execute("SELECT occurrence, text FROM turn_detail"))
    ordinal = dict(conn.execute("SELECT occurrence, ordinal FROM turn_detail"))
    done, fails, last, finished, windows = set(), collections.defaultdict(collections.Counter), {}, {}, {}
    previewed, legacy = set(), set()
    for oid, op, result, step, window, meta in conn.execute(ACTS):
        w = json.loads(window)
        if step == "split":                                   # 旧版本：逐轮
            key = ("turn", turn_text.get(w.get("turn")))
            if result in ("ok", "filtered"):
                done.add(key[1])
                legacy.add(key[1])
        elif step == "preview":
            texts = [turn_text.get(t) for t in w["turns"]]
            key = ("preview", texts[0])
            windows[key] = texts
            if result in ("ok", "filtered"):
                miss = set(json.loads(meta or "{}").get("missing") or []) if result == "ok" else set()
                got = [x for t, x in zip(w["turns"], texts) if ordinal.get(t, -1) + 1 not in miss]
                done.update(got)                   # 预览没分进任何一组的轮次（missing）不算处理过：下一次照常预览（2026-10-27）
                previewed.update(got)
        elif step in MARKS.values() or step in ("reobserve", "prune", "unprune") or "turns" not in w:
            continue                                   # 开关、再观察标记、收回与放回：不是一次观察
        else:
            key = ("turns", tuple(w["turns"]))
            if result in ("ok", "filtered"):
                finished[key] = result
        last[key] = oid
        if result in ("timeout", "content_error"):
            fails[(key, op)][result] += 1
    return types.SimpleNamespace(done=done, fails=fails, last=last, finished=finished, windows=windows,
                                 previewed=previewed, legacy=legacy)


def reads(meta):
    """预览 act 的 meta 里定下的分组。2026-10-17 上线时键叫 segments，之后叫 reads；act 只追加，旧的照读。"""
    return meta.get("reads", meta.get("segments", []))


def _failed(fails, key, op):
    return any(n >= MAX_FAILURES for n in fails[(key, op)].values())


def pool_ops(conn, step="preview", register_new=True):
    """{模型定义: Operator 编号}：这一步能用的各个模型（POOL，没有就是 CURRENT 里的那个）。"""
    return {n: operator(conn, step, register_new, n)[0] for n in POOL.get(step, (CURRENT[step][0],))}


def _stuck(h, op_ids):
    """当前预览 Operator（池里的几个合计）下失败满次数、又一直没成功的预览窗口覆盖的原文：整窗跳过，不挡同一会话后面的轮次。"""
    ops = {op_ids} if isinstance(op_ids, int) else set(op_ids)
    total = collections.defaultdict(collections.Counter)
    for (key, op), c in h.fails.items():
        if key[0] == "preview" and op in ops:
            total[key].update(c)
    out = set()
    for key, c in total.items():
        if key[1] not in h.done and any(n >= MAX_FAILURES for n in c.values()):
            out.update(h.windows.get(key, [key[1]]))
    return out


def ready(conn, op_id, session=None, now=None, reobserve=None):
    """该预览的窗口：[(这几轮, 前面已经处理过几轮)]。会话里第一串连续的、没处理过、已经结束的轮次；会话告一段落，
    才预览，整段一次（轮数不设上限）。只看人直接参与的会话，没写出文字的轮次不看，关掉的会话和开关那一轮不看；
    预览失败满次数的窗口进待重做清单，整窗跳过，后面的轮次照常预览（不挡后面）。
    reobserve：会话键的集合，给了就是再观察：只看这些会话里有旧版本逐轮观察过、新做法还没预览过的，旧版本观察过的
    不算处理过；按会话开始时间从新到旧排。"""
    h = history(conn)
    done = (h.previewed if reobserve is not None else h.done) | _stuck(h, op_id)
    off = switch.states(conn)
    now = now or datetime.now(timezone.utc)
    sql, args = TURNS + " WHERE s.kind='user'", []
    if session:
        sql += " AND d.session LIKE ?"
        args.append(session + "%")
    by = collections.defaultdict(list)
    for row in conn.execute(sql + " ORDER BY so.started, d.source, d.session, d.ordinal", args):
        by[(row["source"], row["session"])].append(row)
    out = []
    for key, rows in by.items():
        if off.get(key) == "off":
            continue
        if reobserve is not None and not (key in reobserve and any(r["text"] in h.legacy and r["text"] not in done for r in rows)):
            continue
        last = rows[-1]
        window = []
        for i, row in enumerate(rows):
            if row["text"] in done or switch.switch(row["user"]) or not row["assistant"]:
                if window:
                    break
                continue
            if not (row["closed"] or i < len(rows) - 1):      # 还没结束：后面的等下次
                break
            window.append(row)
        if not window:
            continue
        if idle(last["started"], key[0], now):
            earlier = sum(1 for r in rows if r["ordinal"] < window[0]["ordinal"])
            out.append((window, earlier))
    if reobserve is not None:
        out.reverse()                                  # 从最新的会话往前倒着来
    return out


def idle(started, source, now):
    """会话告一段落了吗：最后一轮开始后 IDLE 没有新的一轮（开始时间不知道的当作早就到了）。ready、fast_ready、progress 共用。"""
    started = st.iso(started)
    return absent(started) or (now - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds() >= IDLE.get(source, IDLE_DEFAULT)


def previous(conn, row):
    """这个会话里、这一轮之前已经成功预览定下的分组（按轮次排）。续上的预览拿它接着划分。按原文认：分叉的会话共享的那段
    在别的会话名下预览过，也算这段会话之前的预览（2026-10-19）。"""
    sql = ACTS.replace("WHERE a.process='observe'", "WHERE a.process='observe' AND a.step='preview' AND o.outcome='ok'")
    mine = {r["text"]: r["ordinal"] for r in conn.execute("SELECT text, ordinal FROM turn_detail WHERE source=? AND session=? "
                                                         "ORDER BY ordinal DESC", (row["source"], row["session"]))}
    text_of = dict(conn.execute("SELECT occurrence, text FROM turn_detail"))
    out = []
    for _, _, _, _, window, meta in conn.execute(sql):
        o = mine.get(text_of.get(json.loads(window)["turns"][0]))
        if o is not None and o < row["ordinal"]:
            out += [g for g in reads(json.loads(meta)) if not g.get("program")]     # 程序判的不显示的轮，续上时也不给
    return sorted(out, key=lambda s: s["from"])


def _usage(meta):
    u = meta.pop("usage", None)
    if isinstance(u, dict):                    # claude -p 的 usage
        u = [{"inputTokens": u.get("input_tokens"), "outputTokens": u.get("output_tokens"),
              "cacheReadTokens": u.get("cache_read_input_tokens"), "cacheWriteTokens": u.get("cache_creation_input_tokens")}]
    return u


def _call(model, text):
    try:
        output, meta = invoke.call(model, text)
        return output, meta, None
    except Exception as e:          # 失败也是一次发生
        return "", {}, repr(e)


def _act(conn, op_id, step, window, inputs, text, output, meta, error, last, key, cause, scale="turns"):
    result, err = outcome(error)
    meta["expression"] = express.evidence(instance.expression(f"observe.{step}", author="memory-harness", addressee="model"), text)
    seconds = meta.pop("seconds", None)
    act = occurrence(conn, "act", op_id, started=st.iso(st.now(), seconds), ended=st.now(), scale=scale, inputs=inputs,
                     cause=cause, predecessor=last.get(key, NA), outcome=result, error=err, cost=cost(seconds, _usage(meta)))
    conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (act, "observe", step, dumps(window), text, output, dumps(meta)))
    last[key] = act
    return act, result, err


def store_preview(conn, op_id, rows, text, output, meta, error, last, cause="new_text", req=None):
    """写一次预览：act，分组和读法放进 meta["reads"]。req：为哪张再观察的申请做的（进 inputs 的 request，就是认领）。返回一行日志。"""
    groups, info = preview.parse(output, rows) if error is None else ([], {})
    meta.update(reads=groups, **info)
    if error is None:
        by = {r["ordinal"] + 1: r["occurrence"] for r in rows}
        for s in groups:
            s["turns"] = [by[k] for k in range(s["from"], s["to"] + 1) if k in by and k not in s.get("hidden", ())]
    turns = [r["occurrence"] for r in rows]
    inputs = {"turn": turns, **({"request": [req]} if req else {})}
    act, result, err = _act(conn, op_id, "preview", {"turns": turns}, inputs, text, output, meta, error, last,
                            ("preview", rows[0]["text"]), cause)
    conn.commit()
    if result != "ok":
        return f"{result}：{err[:160]}"
    n = collections.Counter(s["how"] for s in groups)
    return f"{len(groups)} 组（精读 {n['精读']}、粗读 {n['粗读']}、不读 {n['不读']}）"


# ---- 快道（runbook I7，2026-10-25）----
# 会话还没告一段落（没到 IDLE）时，已经结束的轮次按粗读的写窗口攒：累计回复满 ROUGH_CAP 封窗，下一轮加进来超过 ROUGH_MAX 的先封，
# 一轮超长的自成一窗；封上才粗读，不够就等，尾巴留给 IDLE 后的正常预览。快道没有自己的预览，不必完整：读失败不重试、
# 不进待重做清单，漏掉的由正常流程补。之后照常预览：判粗读的不再读（快道 E 就是终稿），判精读的读完后快道 E 退场，
# 判不读的快道 E 收回（留在库里作备份）。
FAST = {"cap": deep_rough_read.ROUGH_CAP, "max": deep_rough_read.ROUGH_MAX, "plan": "fast-v1"}
FAST_PLAN = "按已结束的轮次直接粗读（还没有预览），保留以后需要回想的内容。"


def seal(rows, cap=deep_rough_read.ROUGH_CAP, hi=deep_rough_read.ROUGH_MAX):
    """已经结束的轮次（按顺序）→ (封上的窗口, 尾巴)。只用已经结束的，不看未来：前缀的结果是整体结果的前缀，每轮只读一次。"""
    sealed, tail, size = [], [], 0
    for r in rows:
        n = deep_rough_read.reply_chars(r)
        if tail and size + n > hi:
            sealed.append(tail)
            tail, size = [], 0
        tail.append(r)
        size += n
        if size >= cap:
            sealed.append(tail)
            tail, size = [], 0
    return sealed, tail


def _fast_acts(conn):
    """快道的读：[(act, 结果, 轮次)]。"""
    return [(oid, result, json.loads(window)["turns"]) for oid, _, result, step, window, _ in conn.execute(ACTS)
            if step == "rough" and json.loads(window).get("fast")]


def fast_texts(conn, ok_only=True):
    """快道读过的原文：ok_only 只算成功（或被过滤）的，正常流程据此不再粗读；False 连失败的也算，快道据此不再排（失败不重试）。"""
    text_of = dict(conn.execute("SELECT occurrence, text FROM turn_detail"))
    return {text_of.get(t) for _, result, turns in _fast_acts(conn)
            if not ok_only or result in ("ok", "filtered") for t in turns} - {None}


def fast_ready(conn, session=None, now=None):
    """快道该读的窗口：[几轮]。只看人直接参与、开着观察、还没告一段落的会话；轮次是还没处理（没预览、没被旧版本观察、
    快道没排过）、已经结束、写出了文字的，开关那一轮和不显示的轮（宿主消息后回复不到 100 字）不算；同一份原文只排一次。"""
    h = history(conn)
    skip = h.done | fast_texts(conn, ok_only=False)
    off = switch.states(conn)
    now = now or datetime.now(timezone.utc)
    sql, args = TURNS + " WHERE s.kind='user'", []
    if session:
        sql += " AND d.session LIKE ?"
        args.append(session + "%")
    by = collections.defaultdict(list)
    for row in conn.execute(sql + " ORDER BY so.started, d.source, d.session, d.ordinal", args):
        by[(row["source"], row["session"])].append(row)
    out, taken = [], set()
    for key, rows in by.items():
        if off.get(key) == "off":
            continue
        if idle(rows[-1]["started"], key[0], now):
            continue                                   # 告一段落了：走正常预览
        hidden = preview.hidden(rows)
        ok = [r for i, r in enumerate(rows) if (r["closed"] or i < len(rows) - 1) and r["assistant"] and not switch.switch(r["user"])
              and r["text"] not in skip and r["text"] not in taken and r["ordinal"] + 1 not in hidden]
        sealed, _ = seal(ok)
        for w in sealed:
            taken |= {r["text"] for r in w}
            out.append(w)
    return out


def _without_fast(conn, group, fast):
    """预览定下的一组去掉快道已经读过的轮（按原文认）→ 剩下的组；全被读过返回 None。"""
    if not fast:
        return group
    text_of = {t: x for t, x in conn.execute(f"SELECT occurrence, text FROM turn_detail WHERE occurrence IN ({','.join('?' * len(group['turns']))})",
                                              group["turns"])}
    left = [t for t in group["turns"] if text_of.get(t) not in fast]
    if len(left) == len(group["turns"]):
        return group
    if not left:
        return None
    ords = dict(conn.execute(f"SELECT occurrence, ordinal FROM turn_detail WHERE occurrence IN ({','.join('?' * len(left))})", left))
    return {**group, "turns": left, "from": min(ords.values()) + 1, "to": max(ords.values()) + 1}


def _rough_parts(conn, group, fast):
    """粗读的一组 → 要读的几段（去掉快道读过的轮，再按写窗口切开）；全被快道读过的返回 []。to_read、progress、fast_retired 共用。"""
    g = _without_fast(conn, group, fast)
    return _split(conn, g) if g else []


def fast_retired(conn):
    """快道写的、现在退场的 E：预览判不读的那几轮（收回，留作备份），判精读、精读已经读完的那几轮（由精读取代）。按原文认。"""
    acts = {oid for oid, result, _ in _fast_acts(conn) if result == "ok"}
    if not acts:
        return set()
    h = history(conn)
    text_of = dict(conn.execute("SELECT occurrence, text FROM turn_detail"))
    gone = set()
    sql = ACTS.replace("WHERE a.process='observe'", "WHERE a.process='observe' AND a.step='preview' AND o.outcome='ok'")
    for _, _, _, _, _, meta in conn.execute(sql):
        for g in reads(json.loads(meta)):
            if g["how"] == "不读" or g["how"] == "精读" and h.finished.get(("turns", tuple(g["turns"]))) == "ok":
                gone |= {text_of.get(t) for t in g["turns"]}
    if not gone:
        return set()
    return {eid for eid, born, turn in conn.execute(f"SELECT id, born, turn FROM event WHERE born IN ({','.join('?' * len(acts))})", list(acts))
            if text_of.get(turn) in gone}


def group_parts(conn, h, fast, ops, outcomes=("ok",)):
    """预览定下的每一组怎么读、读到哪了，只在这里推（2026-10-27，J3：to_read 和 progress 各推一遍，一分叉就出永远待观测）：
    [(预览 act, 预览的结果, 这一组, 步骤 | None, 这一段, 状态)]。粗读的组先去掉快道读过的轮、再按写窗口切开，一段一行；
    状态：not_read（预览定了不读）、read、filtered、failed（当前 Operator 下失败满次数）、to_read。outcomes：算哪些预览。"""
    marks = ",".join("?" * len(outcomes))
    sql = ACTS.replace("WHERE a.process='observe'", f"WHERE a.process='observe' AND a.step='preview' AND o.outcome IN ({marks})")
    out = []
    for pid, _, result, _, window, meta in conn.execute(sql, outcomes):
        if result == "filtered":
            continue
        for g in reads(json.loads(meta)):
            step = deep_rough_read.STEPS.get(g["how"])
            parts = _rough_parts(conn, g, fast) if step == "rough" else [g]
            if not parts:                                    # 全被快道读过：没有要读的段，这一组算读过了（快道 E 是终稿）
                out.append((pid, result, g, step, {"turns": []}, "read"))
            for part in parts:
                key = ("turns", tuple(part["turns"]))
                if step is None:
                    state = "not_read"
                elif key in h.finished:
                    state = "read" if h.finished[key] == "ok" else "filtered"
                elif _failed(h.fails, key, ops[step]):
                    state = "failed"
                else:
                    state = "to_read"
                out.append((pid, result, g, step, part, state))
    return out


def to_read(conn, session=None):
    """该观察的几组轮次：[(预览 act, 这一组, 步骤)]。成功或被过滤过的不再挑；当前 Operator 下失败满次数的进待重做清单；
    预览之后会话被关掉了观察的，不读。状态由 group_parts 推。"""
    h = history(conn)
    off = {k for k, v in switch.states(conn).items() if v == "off"}
    ops = {step: operator(conn, step)[0] for step in ("deep", "rough")}
    first_of = {}
    out = []
    for pid, _, g, step, part, state in group_parts(conn, h, fast_texts(conn), ops):
        if state != "to_read":
            continue
        if pid not in first_of:
            window = json.loads(conn.execute("SELECT window FROM act_detail WHERE occurrence=?", (pid,)).fetchone()[0])
            first_of[pid] = conn.execute("SELECT source, session FROM turn_detail WHERE occurrence=?", (window["turns"][0],)).fetchone()
        first = first_of[pid]
        if not first or tuple(first) in off or (session and not first[1].startswith(session)):
            continue
        out.append((pid, part, step))
    return out


def _split(conn, group):
    """粗读的一组超过写窗口（ROUGH_CAP 字的回复）就按轮切开（deep_rough_read.split）。"""
    rows = conn.execute(TURNS + f" WHERE d.occurrence IN ({','.join('?' * len(group['turns']))}) ORDER BY d.ordinal",
                        group["turns"]).fetchall()
    return deep_rough_read.split(group, rows)


_FILES = {}


def touched(conn, source, session):
    """这个会话每一轮“我”读过、写过的文件：{轮次（turn 的 occurrence）: {"read": [...], "wrote": [...]}}（written_files）。
    不存库，按会话的轮数缓存在进程里。粗读的输入用它。"""
    rows = conn.execute("SELECT d.occurrence, o.started FROM turn_detail d JOIN occurrence o ON o.id=d.occurrence "
                        "WHERE d.source=? AND d.session=? ORDER BY d.ordinal", (source, session)).fetchall()
    key = (source, session, len(rows))
    if key in _FILES:
        return _FILES[key]
    s = conn.execute("SELECT file, cwd FROM session_detail WHERE source=? AND session=?", (source, session)).fetchone()
    out = {}
    if s and rows and not any(absent(r["started"]) for r in rows):
        starts = [r["started"] for r in rows]
        for i, paths in _written.read(source, s["file"], s["cwd"], starts).items():
            if i < len(rows):
                out.setdefault(rows[i]["occurrence"], {"read": [], "wrote": []})["read"] = paths
        for i, lines in _written.written(source, s["file"], s["cwd"], starts).items():
            if i < len(rows):
                out.setdefault(rows[i]["occurrence"], {"read": [], "wrote": []})["wrote"] = lines
    _FILES[key] = out
    return out


def build_read(conn, group, step, model):
    """一组轮次的观察输入（deep_rough_read.build）。返回 (各轮段落表, 打码表, 文本)。"""
    rows = conn.execute(TURNS + f" WHERE d.occurrence IN ({','.join('?' * len(group['turns']))}) ORDER BY d.ordinal",
                        group["turns"]).fetchall()
    head = (PROMPTS / f"{CURRENT[step][1]}.md").read_text().strip().replace("{tools}", deep_rough_read.TOOLS[step])
    head = express.prompt(head, USER_NAME)
    files = None
    if step == "rough":
        got = touched(conn, rows[0]["source"], rows[0]["session"])
        files = {r["ordinal"] + 1: got[r["occurrence"]] for r in rows if r["occurrence"] in got}
    return deep_rough_read.build(head, label.environment(rows[0], model), group, rows, files)


def store_read(conn, op_id, pid, group, step, tables, mask, text, output, meta, error, last, cause="new_text", fast=False,
               reread=None, commit=True):
    """写一次观察：act，以及解析出的 event（出处是来源里第一个解析到的轮次，都没解析到就记在这几轮的第一轮）。
    reread：重读（2026-10-25），值是旧的读（粗读、精读或旧版本逐轮观察）的 act；inputs 带上它，读过的轮次更早的 E 整轮被取代（reread_reads）。
    commit=False：由调用方把一段会话的几次读写一起提交（form 不会看到半段）。"""
    restored, seconds = mask.restore(output), meta.get("seconds")
    events, nothing = parse.parse_turns(restored, {n: t for n, (_, t) in tables.items()}) if error is None else ([], False)
    meta.update(masked=dict(mask.back), read=group["how"], empty=nothing, **({"part": group["part"]} if group.get("part") else {}))
    if reread:
        meta["reread"] = reread
    if step == "rough" and error is None:
        first = conn.execute("SELECT source, session FROM turn_detail WHERE occurrence=?", (group["turns"][0],)).fetchone()
        got = touched(conn, first["source"], first["session"]) if first else {}
        files = {n: got[occ] for n, (occ, _) in tables.items() if occ in got}
        meta.update(checks=deep_rough_read.checks(events, {n: t for n, (_, t) in tables.items()}, files))
    window = {"turns": group["turns"]}
    if fast:
        window["fast"] = True
    elif pid:
        window["preview"] = pid
    if reread:
        window["reread"] = reread
    inputs = {"turn": group["turns"]}
    if pid and not fast:
        inputs["act"] = [pid]
    if reread:
        inputs["reread"] = [reread]
    act, result, err = _act(conn, op_id, step, window, inputs, text, output, meta, error, last, ("turns", tuple(group["turns"])), cause)
    write_events(conn, act, events, tables, group["turns"][0])
    if commit:
        conn.commit()
    return f"{result}：{err[:160]}" if result != "ok" else f"{len(events)} 条 event，{seconds}s"


def write_events(conn, act, events, tables, first):
    """写 E 只有这一处（申请.md：加关系只改一处）。出处是来源里第一个解析到的轮次，都没解析到就记在 first；
    e["supersedes"]（纠正写的取代）进 relations。"""
    for i, e in enumerate(events):
        occ = tables[e["turn"]][0] if e["turn"] in tables else first
        conn.execute("INSERT INTO event(born, idx, turn, text, mode, qualifier, speaker, segments, unresolved, relations)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (act, i, occ, e["text"], NOT_DONE, NOT_DONE, NOT_DONE, dumps(e["segments"]), dumps(e["unresolved"]),
                      dumps([{"kind": "supersedes", "to": t} for t in e.get("supersedes", [])])))


EARLIEST = datetime.min.replace(tzinfo=timezone.utc)


def _ts(text):
    """ISO 时间 → datetime；不知道的当作早就到了（和 ready 把开始时间未知的会话当作告一段落同一个口径）。"""
    t = st.iso(text)
    return EARLIEST if absent(t) else datetime.fromisoformat(t.replace("Z", "+00:00"))


def reobserve_candidates(conn, since, until):
    """范围里开始的人直接参与的会话中，有旧版本逐轮观察过、新做法还没预览过、可以预览的轮次的：[(来源, 会话)]，按开始时间排。"""
    h = history(conn)
    out = []
    for r in conn.execute(TURNS + " WHERE s.kind='user' ORDER BY so.started, d.source, d.session, d.ordinal"):
        key = (r["source"], r["session"])
        if since <= st.iso(r["session_started"]) < until and r["text"] in h.legacy and r["text"] not in h.previewed \
                and r["assistant"] and not switch.switch(r["user"]) and key not in out:     # 口径同 ready：没回答的、开关不看
            out.append(key)
    return out


def request_reobserve(conn, keys, reason, operator_id, how=None, at=None):
    """再观察（2026-10-19）：主会话写给 observe 的一张范围申请（cause = manual），inputs 写要再观察的会话，理由里写
    怎么挑的（how）。它写下的时刻就是这些会话排进队列的时刻（at 给了按 at 记，补写用）。返回申请的编号。
    2026-10-21 之前是一条 observe 的 act（step = reobserve 的标记），见 backfill_reobserve。"""
    ids = []
    for k in keys:
        row = conn.execute("SELECT occurrence FROM session_detail WHERE source=? AND session=?", k).fetchone()
        if row is None:
            raise ValueError(f"没有这个会话：{k}")
        ids.append(row[0])
    if not ids or not reason.strip():
        raise ValueError("再观察要写明会话和理由")
    return st.request(conn, st.MAIN, "observe", {"session": ids}, reason + (f"（{how}）" if how else ""), operator_id,
                      cause="manual", at=at)


def backfill_reobserve(conn):
    """以前的再观察标记（step = reobserve 的 act）各补一张范围申请：parent 指向标记，到达时刻、Operator 沿用标记的，
    理由取标记 meta 的 reason，其余 meta 原样附在理由后面。已经补过的不再补（一次性，2026-10-21）。返回补了几张。"""
    have = {r[0] for r in conn.execute("SELECT o.parent FROM occurrence o JOIN request_detail d ON d.occurrence=o.id")}
    n = 0
    for oid, op, started, inputs, meta in conn.execute(
            "SELECT o.id, o.operator, o.started, o.inputs, a.meta FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
            "WHERE a.process='observe' AND a.step='reobserve' AND o.outcome='ok' ORDER BY o.id").fetchall():
        if oid in have:
            continue
        m = json.loads(meta)
        reason = m.pop("reason", UNKNOWN)
        rest = {k: v for k, v in m.items() if v is not None}
        st.request(conn, st.MAIN, "observe", {"session": json.loads(inputs)["session"]},
                   reason + (f"（标记 {oid} 的说明：{dumps(rest)}）" if rest else f"（标记 {oid}）"), op,
                   parent=oid, cause="manual", at=started)
        n += 1
    return n


def revise(conn, events, reason, operator_id, parent):
    """修订提示（往上的申请）：这些 E 变了（被取代、不成立、撤下、恢复），其中 form 读过的，给 form 写一张，parent 是造成变化的
    那次 act。只为点名申请和开关写；范围申请（再观察）造成的取代不写（2026-10-21：读过那些旧 E 的是旧系统的形成，旧 J 只是
    兜底，被用到才改）。返回申请的编号，没有 form 读过的就是 None。"""
    seen = st.consumed(conn, "form") & set(events)
    if not seen:
        return None
    return st.request(conn, "observe", "form", {"event": sorted(seen)}, reason, operator_id, parent=parent)


def correction_targets(conn, rid, eids):
    """点名申请里还没纠正过的 E，按它们出生的窗口分组：[{turns, step, plan, events}]。
    新做法的 E：出生那次精读、粗读（或纠正）的窗口和读法，计划取那一组的预览；旧版本逐轮观察的 E：那一轮，按精读，没有计划。
    这张申请已有成功（或被过滤）的纠正 act 覆盖了的 E 不再做。
    纠错后再次犯错的改用精读（2026-10-21）：被点名的 E 是纠正写出来的，或者以前纠正过（判了保留也算），这一组按精读。"""
    done = set()
    again = {e for (inputs,) in conn.execute("SELECT o.inputs FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
                                             "WHERE a.process='observe' AND a.step='correct' AND o.outcome='ok'")
             for e in json.loads(inputs).get("event", [])}
    for a, outcome in st.claimed(conn, rid):
        if outcome in ("ok", "filtered"):
            done |= set(json.loads(conn.execute("SELECT inputs FROM occurrence WHERE id=?", (a,)).fetchone()[0]).get("event", []))
    groups = {}
    for eid in eids:
        if eid in done:
            continue
        row = conn.execute("SELECT e.turn, a.step, a.window FROM event e JOIN act_detail a ON a.occurrence=e.born WHERE e.id=?",
                           (eid,)).fetchone()
        if row is None:
            continue
        w = json.loads(row["window"])
        if row["step"] in ("deep", "rough", "correct") and w.get("turns"):
            turns, step = tuple(w["turns"]), w.get("of", row["step"])
        else:
            turns, step = (row["turn"],), "deep"
        g = groups.setdefault(turns, {"turns": list(turns), "step": step, "preview": w.get("preview"), "events": []})
        g["events"].append(eid)
        if row["step"] == "correct" or eid in again:
            g["step"] = "deep"
    out = []
    for g in groups.values():
        plan = "（这几轮之前由旧版本逐轮观察，没有预览的计划）"
        if g["preview"]:
            m = conn.execute("SELECT meta FROM act_detail WHERE occurrence=?", (g["preview"],)).fetchone()
            for r in reads(json.loads(m[0])) if m else []:
                if set(g["turns"]) & set(r.get("turns", [])):
                    plan = r.get("plan", plan)
                    break
        out.append({**g, "plan": plan})
    return out


def correct(conn, models=None, log=sys.stderr, cause="request", limit=10):
    """按点名申请纠正（C1b）：给 observe 的、还没处理完的点名申请，每张按 E 出生的窗口分组，一组一次调用（correct-v0.1，模型沿用
    那一步的）。会话关着观察的跳过，申请留着；失败按（窗口、Operator）计，满次数的不再试，申请留着记为 failed。
    改正的 E 写取代，不成立的记在 act 的 meta.invalid；form 读过的旧 E 给 form 写修订提示。返回 (处理了几组, 环境错误几次)。"""
    obs = dict(models) if models else {}
    h = history(conn)
    off = {k for k, v in switch.states(conn).items() if v == "off"}
    done = env = 0
    named = [{"id": rid, "inputs": json.loads(inputs), "reason": reason} for rid, inputs, reason in conn.execute(
        "SELECT o.id, o.inputs, d.reason FROM request_detail d JOIN occurrence o ON o.id=d.occurrence "
        "WHERE d.to_process='observe' ORDER BY o.id")]
    for r in named:            # 分几个窗口的申请做了一部分也算 handled：还剩哪些看 correction_targets（每轮都跑，不逐张问 claimed）
        if st.scope(r["inputs"]) != "named" or done >= limit or env:
            continue
        for g in correction_targets(conn, r["id"], r["inputs"].get("event", [])):
            rows = conn.execute(TURNS + f" WHERE d.occurrence IN ({','.join('?' * len(g['turns']))}) ORDER BY d.ordinal",
                                g["turns"]).fetchall()
            if not rows or (rows[0]["source"], rows[0]["session"]) in off:
                continue
            step = g["step"]
            op_id, _ = operator(conn, step, prompt=_correct.PROMPT)
            key = ("turns", tuple(g["turns"]))
            if _failed(h.fails, key, op_id):
                continue
            model = obs.get(step) or obs.setdefault(step, MODELS[CURRENT[step][0]]())
            head = express.prompt((PROMPTS / f"{_correct.PROMPT}.md").read_text().replace("{tools}", deep_rough_read.TOOLS[step]), USER_NAME)
            files = None
            if step == "rough":
                got = touched(conn, rows[0]["source"], rows[0]["session"])
                files = {x["ordinal"] + 1: got[x["occurrence"]] for x in rows if x["occurrence"] in got}
            group = {"from": rows[0]["ordinal"] + 1, "to": rows[-1]["ordinal"] + 1, "plan": g["plan"], "turns": g["turns"]}
            tables, mask, text = deep_rough_read.build(head, label.environment(rows[0], model), group, rows, files)
            olds = [(e, x["text"], json.loads(x["segments"])) for e in g["events"]
                    for x in [conn.execute("SELECT text, segments FROM event WHERE id=?", (e,)).fetchone()]]
            text += "\n\n" + mask(_correct.section(r["reason"], olds))
            output, meta, error = _call(model, text)
            restored = mask.restore(output)
            events, bad, nothing = _correct.parse_output(restored, {n: t for n, (_, t) in tables.items()}, set(g["events"])) \
                if error is None else ([], {}, False)
            meta.update(masked=dict(mask.back), request=r["id"], check={str(k): v for k, v in bad.items()},
                        invalid=[k for k in _correct.invalid_of(bad)], empty=nothing)
            act, result, err = _act(conn, op_id, "correct", {"turns": g["turns"], "of": step, "preview": g["preview"]},
                                    {"turn": g["turns"], "request": [r["id"]], "event": g["events"]}, text, output, meta,
                                    error, h.last, key, cause)
            write_events(conn, act, events, tables, g["turns"][0])
            if result == "ok":
                gone = sorted({t for e in events for t in e["supersedes"]} | set(_correct.invalid_of(bad)))
                if gone:
                    revise(conn, gone, f"observe 按申请 {r['id']} 重新观察：" + "、".join(f"E{x}" for x in gone)
                           + f" 被取代或不成立（申请的理由：{r['reason']}）", op_id, act)
            conn.commit()
            done += 1
            env += result == "env_error"
            n_sup = sum(len(e["supersedes"]) for e in events)
            print(f"[纠正] 申请 {r['id']} {rows[0]['session'][:12]} 第 {group['from']}–{group['to']} 轮："
                  + (f"{result}：{err[:160]}" if result != "ok" else
                     f"{len(events)} 条 event（取代 {n_sup}），不成立 {len(_correct.invalid_of(bad))}"
                     + (f"，没写结论 {bad['missing']}" if bad.get("missing") else "")),
                  file=log, flush=True)
            if env:
                break
    return done, env


REREAD = "observe/reread-v1"


def reread_groups(conn, pid):
    """补读（2026-10-20：只补读，要节俭）：一次预览的原输出按现在的程序规则（preview.parse：接续轮并回前一轮、宿主消息的
    两条线）重新解析，当时判了不读、现在不是不读的轮次按连续的几轮分组，一律粗读。不调模型；读过的轮次不动，所以不会有两版 E。
    逐轮判断（2026-10-20）：宿主消息后回复不超过 HOST_SKIP 字的那一轮不补，剩下的再按连续的几轮分组。返回 [组]，组的样子同预览（plan 写明是补读，接在前一轮下面的附上那一组的预览）。"""
    row = conn.execute("SELECT a.output, o.inputs, a.meta FROM act_detail a JOIN occurrence o ON o.id=a.occurrence "
                       "WHERE o.id=? AND a.step='preview' AND o.outcome='ok'", (pid,)).fetchone()
    if row is None:
        return []
    ids = json.loads(row["inputs"])["turn"]
    rows = conn.execute(TURNS + f" WHERE d.occurrence IN ({','.join('?' * len(ids))}) ORDER BY d.ordinal", ids).fetchall()
    by = {r["ordinal"] + 1: r for r in rows}
    old = {k: g["how"] for g in reads(json.loads(row["meta"])) for k in range(g["from"], g["to"] + 1)}
    new = {k: g for g in preview.parse(row["output"], rows)[0] for k in range(g["from"], g["to"] + 1)}
    got = [k for k in sorted(old) if old[k] == "不读" and k in new and new[k]["how"] != "不读"
           and not (by[k]["origin"] == "host" and len(by[k]["assistant"]) <= preview.HOST_SKIP)]   # 逐轮：只是几句进度的不补
    runs = []
    for k in got:
        if runs and runs[-1][-1] == k - 1 and new[k] is new[runs[-1][-1]]:
            runs[-1].append(k)
        else:
            runs.append([k])
    out = []
    for run in runs:
        g = new[run[0]]
        plan = ("补读（2026-10-20）：当时的预览判了不读；按现在的规则（接在前一轮下面的通知、压缩并回前一轮，"
                "宿主消息后回复超过 1000 字至少粗读）要读，只读这几轮。")
        if g["from"] < run[0]:
            plan += f"\n这几轮接在第 {g['from']} 轮下面，那一组的预览：\n{g['plan']}"
        out.append({"from": run[0], "to": run[-1], "type": g["type"], "plan": plan, "asked": "不读", "how": "粗读",
                    "chars": sum(preview.reply_chars(by[k]) for k in run), "turns": [by[k]["occurrence"] for k in run]})
    return out


def reread(conn, pids, reason, cause="manual"):
    """补读：每次预览写一条程序的预览 act（Operator REREAD，inputs 带原来的预览），分组见 reread_groups，之后照常粗读。
    已经补读过的预览不再补。返回 [(原来的预览, 新的 act, 组数)]。"""
    if not reason.strip():
        raise ValueError("补读要写明理由")
    op = register(conn, REREAD, "observe", "program", model=NA, prompt=NA, prompt_hash=NA, isolation=NA, window="turns",
                  params={"fold": list(preview.FOLD), "host_skip": preview.HOST_SKIP, "host_floor": preview.HOST_FLOOR,
                          "how": "粗读"})
    done = {a for (inputs,) in conn.execute("SELECT o.inputs FROM occurrence o WHERE o.operator=?", (op,))
            for a in json.loads(inputs).get("act", [])}
    out = []
    for pid in pids:
        groups = [] if pid in done else reread_groups(conn, pid)
        if not groups:
            continue
        turns = [t for g in groups for t in g["turns"]]
        t = st.now()
        act = occurrence(conn, "act", op, started=t, ended=t, scale="turns", inputs={"turn": turns, "act": [pid]},
                         cause=cause, outcome="ok")
        conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                     (act, "observe", "preview", dumps({"turns": turns}), "", "", dumps({"reads": groups, "reason": reason})))
        out.append((pid, act, len(groups)))
    return out


def previews_of(conn, source, session):
    """这段会话成功的预览 act（模型做的，不含补读），按编号。"""
    ids = [r[0] for r in conn.execute("SELECT occurrence FROM turn_detail WHERE source=? AND session=?", (source, session))]
    sql = ACTS.replace("WHERE a.process='observe'", "WHERE a.process='observe' AND a.step='preview' AND o.outcome='ok'")
    reread_op = conn.execute("SELECT id FROM operator WHERE name=?", (REREAD,)).fetchone()
    return [oid for oid, op, _, _, window, _ in conn.execute(sql)
            if set(json.loads(window)["turns"]) & set(ids) and not (reread_op and op == reread_op[0])]


def reobserve_requests(conn):
    """给 observe 的范围申请（再观察）：[(编号, 到达, {会话键})]，按到达排。认领过的也在里面：会话还有没预览的轮次，
    ready 照样挑得出来。"""
    out = []
    for r in st.requests(conn, "observe", "all"):
        if st.scope(r["inputs"]) != "range":
            continue
        ids = r["inputs"].get("session", [])
        keys = {(x[0], x[1]) for x in conn.execute(
            f"SELECT source, session FROM session_detail WHERE occurrence IN ({','.join('?' * len(ids))})", ids)} if ids else set()
        started = conn.execute("SELECT started FROM occurrence WHERE id=?", (r["id"],)).fetchone()[0]
        out.append((r["id"], _ts(started), keys))
    return sorted(out, key=lambda x: (x[1], x[0]))


def queue(conn, op_id, session=None, now=None, reobserve=True):
    """该预览的会话，先到先得：[(到达, 会话键, 再观察的申请或 None)]。新会话在它可以预览的那一刻到（最后一轮开始后 IDLE）；
    再观察的在申请写下的那一刻到，同一张申请里的从新往旧。两边都有的会话按再观察算（它的窗口从更早的轮次起）。
    资源平等，没有高低之分（2026-10-19）。reobserve=False：只看新的。"""
    out = {}
    for req, at, keys in (reobserve_requests(conn) if reobserve else []):
        for rows, _ in ready(conn, op_id, session, now, keys):
            key = (rows[0]["source"], rows[0]["session"])
            out.setdefault(key, (at, key, req))
    for rows, _ in ready(conn, op_id, session, now):
        key = (rows[0]["source"], rows[0]["session"])
        if key in out:
            continue
        last = conn.execute("SELECT max(o.started) FROM turn_detail d JOIN occurrence o ON o.id=d.occurrence "
                            "WHERE d.source=? AND d.session=?", key).fetchone()[0]
        at = _ts(last)
        at = at if at == EARLIEST else at + timedelta(seconds=IDLE.get(key[0], IDLE_DEFAULT))
        out[key] = (at, key, None)
    return sorted(out.values(), key=lambda x: x[0])           # 稳定排序：同一条标记里的保持从新往旧


def run(conn, limit=10, workers=8, preview_workers=2, session=None, cause="new_text", log=sys.stderr, models=None, now=None,
        reobserve=True, order=None):
    """跑一批（rhythm 调它）：先预览最多 limit 段会话（按 queue 先到先得；一段会话的窗口接着预览完），然后观察预览定下要读的
    几组轮次。模型调用可以并行，库只在主线程里写。workers、preview_workers（预览走 Claude，额度和交互会话共用）是节律的参数，
    由调用方给。reobserve：带上再观察的申请（request_reobserve）；False = 只看新的。
    order：预览按这个先后用 POOL 里的模型（节律按额度给）；一个出了环境错误就换下一个，都出了才算环境错误。
    models 给了 preview 时（测试）只用它。
    返回 (处理了几件, 环境错误几次)；有环境错误时这一批提前结束，什么时候再来由调用方定。"""
    obs = dict(models) if models else {step: MODELS[name]() for step, (name, _) in CURRENT.items()}   # 复制：下面会换预览模型
    ops = {step: operator(conn, step) for step in CURRENT}
    preview_ops = pool_ops(conn)
    fast_op = operator(conn, "rough", fast=True)[0]
    order = [n for n in (order or [CURRENT["preview"][0]]) if n in preview_ops] or [CURRENT["preview"][0]]
    if not (models and "preview" in models):
        obs["preview"], ops["preview"] = MODELS[order[0]](), operator(conn, "preview", name=order[0])
    conn.commit()                       # 登记完就提交：没活干时不能带着写事务回去休眠（A14 撞锁）
    m = mark_switches(conn, cause)
    if any(m.values()):
        print(f"[开关] 已被 form 读过的 E：撤下 {m['withdraw']} 条，恢复 {m['restore']} 条", file=log, flush=True)
    last = history(conn).last
    done = env = 0
    with concurrent.futures.ThreadPoolExecutor(max(1, workers)) as pool:
        # 预览（Claude 的额度和交互会话共用：并行最多 2 个）。一轮给每段会话预览下一个窗口，直到这几段都预览完
        active = {key: req for _, key, req in queue(conn, preview_ops.values(), session, now, reobserve)[:limit]}
        prompt = express.prompt((PROMPTS / f"{CURRENT['preview'][1]}.md").read_text(), USER_NAME)
        while active and not env:
            jobs, taken = [], set()
            for key, req in list(active.items()):
                got = [w for w in ready(conn, preview_ops.values(), key[1], now, {key} if req else None)
                       if (w[0][0]["source"], w[0][0]["session"]) == key]
                if not got:
                    del active[key]
                    continue
                rows, earlier = got[0]
                if taken & {r["text"] for r in rows}:
                    continue                   # 同一份原文这一轮已经排给别的会话（分叉、副本）：下一轮再来，只观察一次
                taken |= {r["text"] for r in rows}
                if len(preview.hidden(rows)) == len(rows):        # 全是不显示的轮：不调模型，程序判不读
                    state = store_preview(conn, ops["preview"][0], rows, "", "", {"skipped": "hidden"}, None, last, cause, req)
                    done += 1
                    print(f"[预览] {rows[0]['session'][:12]} 第 {rows[0]['ordinal'] + 1}–{rows[-1]['ordinal'] + 1} 轮：{state}（程序）",
                          file=log, flush=True)
                    continue
                jobs.append((rows, preview.build(prompt, label.environment(rows[0], obs["preview"]), rows, earlier,
                                                 previous(conn, rows[0])), req))
            with concurrent.futures.ThreadPoolExecutor(max(1, preview_workers)) as small:
                futures = {small.submit(_call, obs["preview"], text): (rows, text, req) for rows, text, req in jobs}
                for f in concurrent.futures.as_completed(futures):
                    rows, text, req = futures[f]
                    output, meta, error = f.result()
                    state = store_preview(conn, ops["preview"][0], rows, text, output, meta, error, last, cause, req)
                    env += state.startswith("env_error")
                    done += 1
                    tag = f"再观察预览（申请 {req}）" if req else "预览"
                    print(f"[{tag}] {rows[0]['session'][:12]} 第 {rows[0]['ordinal'] + 1}–{rows[-1]['ordinal'] + 1} 轮：{state}",
                          file=log, flush=True)
            if env and not (models and "preview" in models) and len(order) > 1:      # 换下一个模型，没预览成的下一轮再来
                print(f"[预览] {order[0]} 环境错误，换 {order[1]}", file=log, flush=True)
                order, env = order[1:], 0
                obs["preview"], ops["preview"] = MODELS[order[0]](), operator(conn, "preview", name=order[0])
                conn.commit()
        if env:
            return done, env
        # 观察：预览定下的几组，加上快道封上的窗口
        jobs = []
        for pid, group, step in to_read(conn, session):
            tables, mask, text = build_read(conn, group, step, obs[step])
            jobs.append((pid, group, step, tables, mask, text))
        for w in fast_ready(conn, session, now):
            group = {"turns": [r["occurrence"] for r in w], "from": w[0]["ordinal"] + 1, "to": w[-1]["ordinal"] + 1,
                     "how": "粗读", "plan": FAST_PLAN}
            tables, mask, text = build_read(conn, group, "rough", obs["rough"])
            jobs.append((None, group, "fast", tables, mask, text))
        futures = {pool.submit(_call, obs["rough" if j[2] == "fast" else j[2]], j[5]): j for j in jobs}
        for f in concurrent.futures.as_completed(futures):
            pid, group, step, tables, mask, text = futures[f]
            output, meta, error = f.result()
            if step == "fast":
                state = store_read(conn, fast_op, None, group, "rough", tables, mask, text, output, meta, error, last, cause, fast=True)
            else:
                state = store_read(conn, ops[step][0], pid, group, step, tables, mask, text, output, meta, error, last, cause)
            env += state.startswith("env_error")
            done += 1
            tag = "快道" if step == "fast" else group["how"]
            where = "" if step == "fast" else f"（预览 {pid}）"
            print(f"[{tag}] {_session_of(conn, group)} 第 {group['from']}–{group['to']} 轮{where}：{state}", file=log, flush=True)
    return done, env


SWITCH = "observe/switch-v1"
MARKS = {"off": "withdraw", "on": "restore"}       # 开关 → 跨过 observe → form 边界时记的 step


def _switched(conn):
    """{(来源, 会话): (状态, 最后一次开关那一轮, [E 编号…])}：出现过开关的会话和其中已经产出的 E。"""
    out = {}
    for key, (state, turn) in switch.latest(conn).items():
        ids = [r[0] for r in conn.execute("SELECT e.id FROM event e JOIN turn_detail d ON d.occurrence=e.turn "
                                          "WHERE d.source=? AND d.session=? ORDER BY e.id", key)]
        out[key] = (state, turn, ids)
    return out


def withheld(conn):
    """不交给上一层的 E：会话现在关着观察（最后一次开关是 /no-observe）。form 读 E 时排除它们；/do-observe 以后又交出去。"""
    return {e for state, _, ids in _switched(conn).values() if state == "off" for e in ids}


PRUNE = "observe/prune-v1"


def pruned(conn):
    """程序收回的 E（2026-10-26，Jev 判过类型和有没有判断）：不删，只是不再交给上一层。
    和开关的 withheld 同一层，理由记在 act 里；收回（prune）、放回（unprune）按先后，后一次说了算。"""
    out = set()
    for step, inputs in conn.execute(
            "SELECT a.step, o.inputs FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
            "WHERE a.process='observe' AND a.step IN ('prune','unprune') AND o.outcome='ok' ORDER BY o.id"):
        ids = set(json.loads(inputs).get("event", []))
        out = out | ids if step == "prune" else out - ids
    return out


def _user_said(conn, events):
    """出处里有用户段落（U 段）的 E：用户的输入总要交给 form，不收回（2026-10-26，Jev 收回了 484 条）。"""
    if not events:
        return set()
    q = ",".join("?" * len(events))
    return {e for e, seg in conn.execute(f"SELECT id, segments FROM event WHERE id IN ({q})", list(events))
            if any(str(x).split(":")[-1].startswith("U") for x in json.loads(seg))}


def _prune_act(conn, step, events, reason, cause):
    op = register(conn, PRUNE, "observe", "program", model=NA, prompt=NA, prompt_hash=NA, isolation=NA,
                  window="free", params={"rule": "Jev 判类型、有没有判断；程序收回，用户的段落不收"})
    t = st.now()
    act = occurrence(conn, "act", op, started=t, ended=t, scale="free", inputs={"event": events},
                     cause=cause, outcome="ok", cost={"seconds": 0, "tokens": NA})
    conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                 (act, "observe", step, dumps({"event": len(events)}), "", "",
                  dumps({"reason": reason, "events": len(events)})))
    conn.commit()
    return act


def prune(conn, events, reason, cause="manual"):
    """把 Jev 判为没有必要的 E 收回：写一条程序 act，E 本身不删；出处里有用户段落的跳过。返回 act 编号；没有可收的返回 None。"""
    events = sorted(set(events) - _user_said(conn, events))
    return _prune_act(conn, "prune", events, reason, cause) if events else None


def unprune(conn, events, reason, cause="manual"):
    """把收回的 E 放回：之后照常交给 form（form 把它们当晚到的 E，落进已整理的范围就按中间插入重做）。只放回现在收着的。"""
    events = sorted(set(events) & pruned(conn))
    return _prune_act(conn, "unprune", events, reason, cause) if events else None


def marks(conn):
    """{E 编号: (最后一次记的 step, 那次 act)}：开关在 observe → form 的边界上记过的 E（withdraw 或 restore）。"""
    out = {}
    for oid, step, inputs in conn.execute("SELECT o.id, a.step, o.inputs FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
                                          "WHERE a.process='observe' AND a.step IN ('withdraw','restore') ORDER BY o.id"):
        for e in json.loads(inputs).get("event", []):
            out[e] = (step, oid)
    return out


def mark_switches(conn, cause="new_text"):
    """开关只在 E 跨过 observe → form 的边界时留记录（2026-10-17：暂停和恢复都以是否进入 form 定，没有离开观察环节的
    都不计入 Occurrence）。已经被 form 读过的 E：会话关掉时记一次 withdraw（E 消失的记录和理由），恢复时记一次 restore
    （又交给 form）。没被 form 读过的两头都不记：关着就不交出去（withheld），恢复后照常交出去。
    返回 {"withdraw": 几条 E, "restore": 几条 E}。"""
    switched = _switched(conn)
    done = {"withdraw": 0, "restore": 0}
    if not switched:
        return done
    seen, last = st.consumed(conn, "form"), marks(conn)
    op = None
    for (source, session), (state, turn, ids) in switched.items():
        step = MARKS[state]
        todo = [e for e in ids if e in seen and last.get(e, ("restore",))[0] != step]
        if not todo:
            continue
        op = op or register(conn, SWITCH, "observe", "program", model=NA, prompt=NA, prompt_hash=NA, isolation=NA,
                            window="session", params={"code": "mh/engram/observe/__init__.py",
                                                      "rule": "已被 form 读过的 E：关掉记 withdraw，恢复记 restore"})
        sid = conn.execute("SELECT occurrence FROM session_detail WHERE source=? AND session=?", (source, session)).fetchone()
        where = conn.execute("SELECT session, ordinal FROM turn_detail WHERE occurrence=?", (turn,)).fetchone()
        said = f"{where[0][:12]} 第 {where[1] + 1} 轮"
        reason = (f"{USER_NAME}关掉了这个会话的观察（{said}的 /no-observe），这些 E 已经被 form 读过，从此不再交给上一层"
                  if state == "off" else f"{USER_NAME}恢复了这个会话的观察（{said}的 /do-observe），这些 E 之前被撤下，重新交给上一层")
        act = occurrence(conn, "act", op, started=st.now(), ended=st.now(), scale="session",
                         inputs={"event": todo, "turn": [turn]}, cause=cause, outcome="ok", cost={"seconds": 0, "tokens": NA})
        conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                     (act, "observe", step, dumps({"session": sid[0] if sid else UNKNOWN}), "", "",
                      dumps({"reason": reason, "session": [source, session], "events": len(todo)})))
        revise(conn, todo, reason, op, act)
        done[step] += len(todo)
    conn.commit()
    return done


# ---- 对外接口（重构 A9）：上一层（form）、检查、rhythm 只经这几个函数问 observe，不读 observe 的 act ----
# frame、progress（2026-10-20：原来的 handover → frame，pending、decisions 合成 progress）；B4 加了 written（写进的文件）

# 每一轮的观察进度（progress 的 state）。以后不会产出 E 的不挡上一层；待观测的只有 to_read、not_previewed
STATES = {
    "read": "读过了（精读、粗读，或旧版本逐轮观察过）",
    "not_read": "预览定了不读",
    "filtered": "被服务商的内容过滤拦下，不要了",
    "failed": "失败满次数，进待重做清单",
    "off": "会话关着观察（/no-observe）",
    "switch": "开关那一轮，永远不观察",
    "no_answer": "“我”没写出最终回答，不观察",
    "to_read": "预览定了精读或粗读，还没读完",
    "not_previewed": "还没预览",
}
TO_OBSERVE = ("to_read", "not_previewed")   # 待观测：以后还会产出 E


def progress(conn, session=None, now=None):
    """观察进度：人直接参与的会话里每个已经结束的轮次，observe 走到了哪一步。
    [{turn, source, session, ordinal, started, state}]，按开始时间排；state 见 STATES，在 TO_OBSERVE 里的是待观测的（以后还会产出 E，
    form 取最早的开始时间做门槛，只算 idle（会话告一段落）的：还在进行的会话以后才到、排在已结束的后面，不挡），其余的以后不会产出、不挡上一层。还没结束的轮次不列，会话是否告一段落由上一层自己看。
    同一份原文出现在几处（fork、迁移副本）只观察一次，各处的 state 相同。session：会话 ID 前缀。"""
    h = history(conn)
    ops = {step: operator(conn, step, register_new=False)[0] for step in CURRENT}      # 只读：不登记
    stuck = _stuck(h, {o for o in pool_ops(conn, register_new=False).values() if o is not None})
    states = switch.states(conn)
    # 预览定下的分组 → 每份原文的决定（按原文，fork 的副本跟着走）
    turn_text = dict(conn.execute("SELECT occurrence, text FROM turn_detail"))
    by_text = {}
    fast = fast_texts(conn)
    sql = ACTS.replace("WHERE a.process='observe'", "WHERE a.process='observe' AND a.step='preview' AND o.outcome='filtered'")
    for _, _, _, _, window, _ in conn.execute(sql):
        for t in json.loads(window)["turns"]:
            by_text.setdefault(turn_text.get(t), "filtered")
    for _, _, g, step, part, d in group_parts(conn, h, fast, ops, ("ok",)):
        if step == "rough":                                              # 快道读过的：判粗读就算读过了（快道 E 是终稿）
            for t in g["turns"]:
                if turn_text.get(t) in fast:
                    by_text[turn_text.get(t)] = "read"
        for t in part["turns"]:
            by_text[turn_text.get(t)] = d
    legacy = {}
    for result, text in conn.execute("SELECT o.outcome, t.text FROM act_detail a JOIN occurrence o ON o.id=a.occurrence "
                                     "JOIN turn_detail t ON t.occurrence=json_extract(a.window, '$.turn') "
                                     "WHERE a.process='observe' AND a.step='split' AND o.outcome IN ('ok','filtered')"):
        legacy[text] = "read" if result == "ok" or legacy.get(text) == "read" else "filtered"
    sql, args = TURNS + " WHERE s.kind='user'", []
    if session:
        sql += " AND d.session LIKE ?"
        args.append(session + "%")
    rows = conn.execute(sql + " ORDER BY d.source, d.session, d.ordinal", args).fetchall()
    last, began = {}, {}
    for r in rows:
        last[(r["source"], r["session"])] = r["ordinal"]
        began[(r["source"], r["session"])] = r["started"]
    now = now or datetime.now(timezone.utc)
    out = []
    for r in rows:
        key = (r["source"], r["session"])
        if not (r["closed"] or r["ordinal"] < last[key]):
            continue                                       # 还没结束
        if switch.switch(r["user"]):
            d = "switch"
        elif not r["assistant"]:
            d = "no_answer"
        elif states.get(key) == "off":
            d = "off"
        elif r["text"] in by_text:
            d = by_text[r["text"]]
        elif r["text"] in legacy:
            d = legacy[r["text"]]                          # 旧版本逐轮观察过
        elif r["text"] in stuck:
            d = "failed"
        else:
            d = "not_previewed"
        out.append({"turn": r["occurrence"], "source": r["source"], "session": r["session"], "ordinal": r["ordinal"],
                    "started": r["started"], "state": d, "idle": idle(began[key], key[0], now)})
    return sorted(out, key=lambda x: (x["started"], x["source"], x["session"], x["ordinal"]))


def superseded(conn):
    """按申请重新观察取代的 E（2026-10-21：按申请判，不按 Operator）。两种：
    点名申请：新 E 的 relations 里 supersedes 指向的旧 E，和纠正写了不成立的（invalid）；
    范围申请（再观察）：认领它的预览定下的那几组读完了，读过的每一轮（按原文认），更早出生的 E 不论哪一版整轮被取代。
    预览定了不读的轮次没有新 E，旧的照旧。被取代的 E 不删，留在那一轮上作历史锚点（superseded_by_turn 能找回）。"""
    return set(superseded_by_turn(conn)) | {t for (rel,) in conn.execute("SELECT relations FROM event WHERE relations!='[]'")
                                             for t in (x["to"] for x in json.loads(rel) if x["kind"] == "supersedes")} | invalid(conn)


def invalid(conn):
    """纠正核对为不成立的旧 E（没有新 E 可挂，记在那次纠正 act 的 meta.invalid 里，结论和依据在 meta.check）。"""
    return {int(k) for (meta,) in conn.execute("SELECT a.meta FROM act_detail a JOIN occurrence o ON o.id=a.occurrence "
                                               "WHERE a.process='observe' AND a.step='correct' AND o.outcome='ok'")
            for k in json.loads(meta).get("invalid", [])}


def fate(conn, eid):
    """一条 E 后来怎样了（给回溯、阅读页用）：[{how: corrected | invalid | turn, by: [新 E], act, request, why}]。
    corrected：点名申请的纠正写了新 E 取代它；invalid：纠正核对为不成立；turn：范围申请重新观察了它那一轮，整轮被取代
    （by 是那次读写的这一轮的 E）；kept：纠正核对过、保留（还是当前的）。只有 kept 或没有：它还是当前的。"""
    out = []
    for nid, born, inputs in conn.execute(
            "SELECT e.id, e.born, o.inputs FROM event e JOIN occurrence o ON o.id=e.born, json_each(e.relations) j "
            "WHERE json_extract(j.value, '$.to')=?", (eid,)):
        out.append({"how": "corrected", "by": [nid], "act": born, "request": json.loads(inputs).get("request", [])})
    for act, inputs, meta in conn.execute("SELECT o.id, o.inputs, a.meta FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
                                          "WHERE a.step='correct' AND o.outcome='ok' AND o.inputs LIKE ?", (f"%{eid}%",)):
        m = json.loads(meta)
        c = m.get("check", {}).get(str(eid))
        if eid in m.get("invalid", []):
            out.append({"how": "invalid", "by": [], "act": act, "request": json.loads(inputs).get("request", []),
                        "why": c["why"] if c else ""})
        elif c and c["verdict"] == "保留":              # 核对过、保留：还是当前的，留下这段历史
            out.append({"how": "kept", "by": [], "act": act, "request": json.loads(inputs).get("request", []), "why": c["why"]})
    read = superseded_by_turn(conn).get(eid)
    if read:
        text = conn.execute("SELECT d.text FROM event e JOIN turn_detail d ON d.occurrence=e.turn WHERE e.id=?", (eid,)).fetchone()[0]
        by = [r[0] for r in conn.execute("SELECT e.id FROM event e JOIN turn_detail d ON d.occurrence=e.turn "
                                         "WHERE e.born=? AND d.text=? ORDER BY e.idx", (read, text))]
        reqs = []
        for (inputs,) in conn.execute("SELECT o.inputs FROM occurrence o, json_each(json_extract("
                                      "(SELECT inputs FROM occurrence WHERE id=?), '$.act')) j WHERE o.id=j.value", (read,)):
            x = json.loads(inputs)                # 读 → 预览 → 申请（以前的标记：补写的申请 parent 指向它）
            reqs += x.get("request", [])
            for m in x.get("act", []):
                reqs += [r[0] for r in conn.execute("SELECT o.id FROM occurrence o JOIN request_detail d ON d.occurrence=o.id "
                                                    "WHERE o.parent=?", (m,))]
        out.append({"how": "turn", "by": by, "act": read, "request": reqs})
    return sorted(out, key=lambda f: f["act"])


def range_reads(conn):
    """范围申请重新观察的读：{原文 ID: 最近一次读的 act}。申请 → 认领它的成功预览（连同接在它下面的补读）→ inputs 里带这些预览的
    成功的精读、粗读。form 每段会话都要问（经 frame），所以 observe 的 act 只扫一遍；没有预览带着标记的补写申请才问 claimed。"""
    ranges, backfilled = set(), {}
    for rid, inputs, parent, step in conn.execute(
            "SELECT o.id, o.inputs, o.parent, a.step FROM request_detail d JOIN occurrence o ON o.id=d.occurrence "
            "LEFT JOIN act_detail a ON a.occurrence=o.parent WHERE d.to_process='observe'"):
        if st.scope(json.loads(inputs)) == "range":
            ranges.add(rid)
            if step == "reobserve":
                backfilled[parent] = rid
    if not ranges:
        return {}
    previews = set()
    acts = conn.execute("SELECT o.id, a.step, o.inputs FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
                        "WHERE a.process='observe' AND a.step IN ('preview','deep','rough') AND o.outcome='ok' "
                        "AND (o.inputs LIKE '%\"act\"%' OR o.inputs LIKE '%\"request\"%') ORDER BY o.id").fetchall()
    linked = set()
    for oid, step, inputs in acts:          # 按编号往后：补读的程序预览接在原来的预览下面，同属这张申请
        x = json.loads(inputs)
        marks = backfilled.keys() & set(x.get("act", []))
        if step == "preview" and (ranges & set(x.get("request", [])) or marks or previews & set(x.get("act", []))):
            previews.add(oid)
            linked |= marks
    for mark in backfilled.keys() - linked:     # 补记的那批：预览没带标记，按 claimed 的规则认
        previews |= {a for a, outcome in st.claimed(conn, backfilled[mark]) if outcome == "ok"}
    if backfilled.keys() - linked:              # 补读接在这些预览下面的，再认一遍
        for oid, step, inputs in acts:
            if step == "preview" and previews & set(json.loads(inputs).get("act", [])):
                previews.add(oid)
    text_of = dict(conn.execute("SELECT occurrence, text FROM turn_detail"))
    out = {}
    for oid, step, inputs in acts:
        x = json.loads(inputs)
        if step != "preview" and previews & set(x.get("act", [])):
            for t in x.get("turn", []):
                if t in text_of:
                    out[text_of[t]] = max(out.get(text_of[t], 0), oid)
    return out


def reread_reads(conn):
    """重读的读：{原文 ID: 最近一次读的 act}（inputs 带 reread；2026-10-25 runbook I7 旧粗读重读，2026-10-26 跨语言轮次重读也有精读）。
    按原文认：同一份原文出现在几处（fork、副本）一起取代。"""
    text_of = dict(conn.execute("SELECT occurrence, text FROM turn_detail"))
    out = {}
    for oid, inputs in conn.execute("SELECT o.id, o.inputs FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
                                    "WHERE a.process='observe' AND a.step IN ('rough','deep') AND o.outcome='ok' "
                                    "AND o.inputs LIKE '%\"reread\"%'"):
        for t in json.loads(inputs).get("turn", []):
            if t in text_of:
                out[text_of[t]] = max(out.get(text_of[t], 0), oid)
    return out


def superseded_by_turn(conn):
    """按轮次推出的取代：{被取代的 E: 取代它的那次读}。范围申请（再观察）和重读（2026-10-25）都用这一条：
    读过的每一轮（按原文认）更早出生的 E 整轮被取代。"""
    reads = range_reads(conn)
    for text, oid in reread_reads(conn).items():
        reads[text] = max(reads.get(text, 0), oid)
    if not reads:
        return {}
    return {eid: reads[text] for eid, born, text in conn.execute(
        "SELECT e.id, e.born, d.text FROM event e JOIN turn_detail d ON d.occurrence=e.turn")
        if text in reads and born < reads[text]}


def frame_context(conn):
    """frame 要的全库部分：不交出的 E、边界上的记录。一批里按会话调 frame 很多次的（form 的 units）先算一次传进去，
    不然每段会话都把全库重算一遍（2026-10-26：457 段会话 916 秒，几乎都花在这里）。"""
    hidden = withheld(conn) | superseded(conn) | fast_retired(conn) | pruned(conn)
    marks_by = collections.defaultdict(list)
    for oid, step, inputs, meta in conn.execute(
            "SELECT o.id, a.step, o.inputs, a.meta FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
            "WHERE a.process='observe' AND a.step IN ('withdraw','restore') ORDER BY o.id"):
        reason = json.loads(meta).get("reason", UNKNOWN)
        for e in json.loads(inputs).get("event", []):
            marks_by[e].append({"step": step, "act": oid, "reason": reason})
    return {"hidden": hidden, "marks": marks_by}


def frame(conn, source=None, session=None, ctx=None):
    """现在交给上一层（form）的 E，和撤下的说明。返回 {"events": [...], "withdrawn": [...]}：
    events     [{id, turn, source, session, ordinal, text, segments, unresolved, speaker, mode, qualifier, modes, act, operator,
                 marks}]，按会话、轮次、出生排；不含 withheld（会话关着观察）、按申请被取代的（superseded）和退场的快道 E（fast_retired）。
                 speaker、mode、qualifier 是 modes.read 从正文读出的（modes 是那一版读法），模型自己写过的以模型写的为准。
                 marks：这条 E 在 observe → form 边界上的记录 [{step: withdraw | restore, act, reason}]，恢复后交出去的带着它。
                 step：读出它的那一步（deep、rough、correct……）；纠正写的 E 只经修订提示进 form（2026-10-21）。
                 scale：读出它的那次 act 的窗口，turns 是一轮或多轮一起读（2026-10-17 起的做法；上线当天写过 segment），
                 turn 是旧版本逐轮观察。
    withdrawn  [{event, act, reason}]：已经被 form 读过、现在因为开关撤下的 E（最后一次记的是 withdraw）。
    source、session 给了就只看那一个会话（session 是完整的会话 ID）。ctx：frame_context 先算好的，同一批里共用。"""
    ctx = ctx or frame_context(conn)
    hidden, marks_by = ctx["hidden"], ctx["marks"]
    sql = ("SELECT e.*, d.source, d.session, d.ordinal, d.text AS turn_text, p.name AS op_name, o.scale, a.step FROM event e "
           "JOIN turn_detail d ON d.occurrence=e.turn JOIN occurrence o ON o.id=e.born JOIN operator p ON p.id=o.operator "
           "JOIN act_detail a ON a.occurrence=e.born")
    args, here = [], None
    if source and session:
        # 按原文认（2026-10-19）：这段会话每一轮原文的 E，不管在哪段会话名下观察的（分叉、副本只观察一次）；
        # 轮次、出处换成这段会话里的那一轮。同一份原文被新做法读过几次的，只取最近一次读的
        here = {}
        for occ, ordinal, text in conn.execute("SELECT occurrence, ordinal, text FROM turn_detail WHERE source=? AND session=? "
                                               "ORDER BY ordinal DESC", (source, session)):
            here[text] = (occ, ordinal)
        if not here:
            return {"events": [], "withdrawn": []}
        sql += f" WHERE d.text IN ({','.join('?' * len(here))})"
        args = list(here)
    elif source or session:
        sql += " WHERE " + " AND ".join(c for c, v in (("d.source=?", source), ("d.session=?", session)) if v)
        args = [v for v in (source, session) if v]
    rows = conn.execute(sql + " ORDER BY d.source, d.session, d.ordinal, e.born, e.idx", args).fetchall()
    if here is not None:
        latest = {}                     # 纠正只写回应申请的几条，不是又读了一遍：不参与“只取最近一次读的”
        for r in rows:
            if r["scale"] in ("turns", "segment") and r["step"] != "correct":
                latest[r["turn_text"]] = max(latest.get(r["turn_text"], 0), r["born"])
        rows = [r for r in rows if r["scale"] not in ("turns", "segment") or r["step"] == "correct"
                or r["born"] == latest.get(r["turn_text"])]
        rows.sort(key=lambda r: (here[r["turn_text"]][1], r["born"], r["idx"]))
    out, gone = [], []
    for r in rows:
        m = marks_by.get(r["id"], [])
        if r["id"] in hidden:
            if m and m[-1]["step"] == "withdraw":
                gone.append({"event": r["id"], "act": m[-1]["act"], "reason": m[-1]["reason"]})
            continue
        read = modes.read(r["text"])
        own = lambda col: read[col] if absent(r[col]) else r[col]
        turn, src, sess, ordinal = (r["turn"], r["source"], r["session"], r["ordinal"]) if here is None else \
            (here[r["turn_text"]][0], source, session, here[r["turn_text"]][1])
        out.append({"id": r["id"], "turn": turn, "source": src, "session": sess, "ordinal": ordinal,
                    "observed": r["turn"],
                    "text": r["text"], "segments": json.loads(r["segments"]), "unresolved": json.loads(r["unresolved"]),
                    "speaker": own("speaker"), "mode": own("mode"), "qualifier": own("qualifier"), "modes": read["version"],
                    "act": r["born"], "operator": r["op_name"], "scale": r["scale"], "step": r["step"], "marks": m})
    return {"events": out, "withdrawn": gone}


def languages(conn, source, session):
    """这个会话里换了语言的轮次：{轮次（turn 的 occurrence）: 一句说明}（label.language_note，steady=False）：“我”的回复换了语言的，
    用户用的不是记录语言的。纯程序，不存库；form 把它附在那一轮的 E 后面。精读、粗读的输入里不给（2026-10-26，tmp/cross-language/：
    给了，模型会写出只有“我用英文回复”的空 E，该记的那一轮又常漏掉）。"""
    out, before = {}, None
    for occ, user, assistant in conn.execute("SELECT d.occurrence, x.user, x.assistant FROM turn_detail d JOIN turn_text x "
                                             "ON x.id=d.text WHERE d.source=? AND d.session=? ORDER BY d.ordinal", (source, session)):
        if note := label.language_note(user, assistant, before, steady=False):
            out[occ] = note
        if label.language(assistant):
            before = assistant
    return out


def groups(conn, turns):
    """轮次（turn 的 occurrence）→ 它最近一次预览分在哪一组：(预览 act, 从第几轮, 到第几轮, 类型)；没被预览过的不在里面。
    给上一层用的接口（2026-10-27）：form 拿它查一条陈述的来源是不是一段连着的来回（pack 的检查），Synapse 拿它给话题、来源分节。
    预览的判断、关注、读法不给。"""
    want = {t for t in turns if t is not None}
    out = {}
    if not want:
        return out
    for occ, meta in conn.execute("SELECT occurrence, meta FROM act_detail WHERE process='observe' AND step='preview' ORDER BY occurrence"):
        for g in json.loads(meta).get("reads", json.loads(meta).get("segments", [])):
            for t in set(g.get("turns", [])) & want:
                out[t] = (occ, g.get("from"), g.get("to"), (g.get("type") or "").rstrip("。"))
    return out


def written(conn, source, session):
    """这个会话每一轮写进了哪些文件：{轮次（turn 的 occurrence）: [一行一行的说明]}（written.py）。从原始记录和 git 提交算，
    不存库；路径相对工作区。form 把它附在那一轮的 E 后面。"""
    s = conn.execute("SELECT file, cwd FROM session_detail WHERE source=? AND session=?", (source, session)).fetchone()
    if not s:
        return {}
    rows = conn.execute("SELECT d.occurrence, o.started FROM turn_detail d JOIN occurrence o ON o.id=d.occurrence "
                        "WHERE d.source=? AND d.session=? ORDER BY d.ordinal", (source, session)).fetchall()
    if not rows or any(absent(r["started"]) for r in rows):
        return {}
    got = _written.written(source, s["file"], s["cwd"], [r["started"] for r in rows])
    return {rows[i]["occurrence"]: lines for i, lines in got.items() if i < len(rows)}


def _session_of(conn, group):
    row = conn.execute("SELECT session FROM turn_detail WHERE occurrence=?", (group["turns"][0],)).fetchone()
    return row[0][:12] if row else "?"


def redo(conn):
    """待重做清单：当前 Operator 下失败满次数、又没有成功的预览窗口和几组轮次。[(种类, 键, {失败种类: 次数})]
    种类 preview 的键是窗口第一轮的原文；turns 的键是那几轮。"""
    h = history(conn)
    done, fails, finished = h.done, h.fails, h.finished
    ops = {operator(conn, step)[0] for step in CURRENT} | set(pool_ops(conn).values())
    out = []
    for (key, op), c in fails.items():
        if op in ops and any(n >= MAX_FAILURES for n in c.values()):
            if (key[0] == "preview" and key[1] not in done) or (key[0] == "turns" and key not in finished):
                out.append((key[0], key[1:], dict(c)))
    return out
