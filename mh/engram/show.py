"""按读者的方式把新库里的一片历史排出来：一条 Claim 沿 Occurrence 回溯（文本），一段会话从原话到判断（HTML）。只读。
在 Engram 这一层（重构 A10）：要读各过程的产物，observe 的预览分组经 observe.reads 读。

回溯回答设计里的六个问题：什么时候、谁、按什么做法、看了什么、因为什么发生、之前有没有发生过同样的事。
"""
import html
import json
import re

from .observe import label, modes, reads
from .store import HANDLES, blocks, NA, NOT_DONE, UNKNOWN, absent

MARK_ZH = {UNKNOWN: "未知", NOT_DONE: "没做", NA: "不适用"}
STEP_ZH = {"split": "observe · 逐轮拆成 event（旧版本）", "preview": "observe · 预览", "deep": "observe · 精读",
           "rough": "observe · 粗读", "correct": "observe · 纠正", "withdraw": "observe · 开关撤下", "restore": "observe · 开关恢复",
           "organize": "form · 逐轮记录", "pack": "form · 会话记录", "judge": "form · 写判断", "relate": "form · 判关系"}
REL_ZH = {"same": "相同", "supersedes": "取代", "refines": "细化", "contradicts": "矛盾",
          "split_from": "拆分自", "merged_from": "合并自"}


def zh(v):
    return MARK_ZH.get(v, v) if absent(v) else v


def short(name):
    """Operator 名太长：留模型定义和提示词。提示词版本带点（v0.1）的，按版本号截；旧名按第一个点截。"""
    head, _, rest = name.partition("/")
    if not rest:
        return name
    m = re.match(r"(.+?-v\d+\.\d+)\.", rest)
    return f"{head}/{m.group(1) if m else rest.split('.')[0].rsplit('-', 1)[0]}"


def compaction_cut(conn, born):
    """产出 E 的那次观察有没有把压缩总结换成标记（Operator 参数 notice.compaction，2026-10-20 起）：决定回溯时的切法。"""
    if born is None or absent(born):
        return False
    row = conn.execute("SELECT json_extract(p.params, '$.notice.compaction') FROM occurrence o JOIN operator p ON p.id=o.operator "
                       "WHERE o.id=?", (born,)).fetchone()
    return bool(row and row[0])


def segments(conn, turn_occ, born=None):
    """一轮原文按观察时的切法编号（U1… / A1…），程序可以随时重算。born：产出 E 的 act，按它当时的切法（压缩总结给不给全文）。"""
    row = conn.execute("SELECT x.user, x.assistant, x.commentary, d.ending FROM turn_detail d JOIN turn_text x "
                       "ON x.id=d.text WHERE d.occurrence=?", (turn_occ,)).fetchone()
    if row is None:
        return {}
    return blocks.number({"user": label.user_text(row["user"], compaction_cut(conn, born))[0], "assistant": row["assistant"],
                          "commentary": json.loads(row["commentary"]),
                          "ending": None if absent(row["ending"]) or row["ending"] == "normal" else row["ending"]})[0]


def _occ(conn, oid):
    return conn.execute("SELECT o.*, p.name AS op_name, a.step, a.window FROM occurrence o JOIN operator p ON p.id=o.operator "
                        "LEFT JOIN act_detail a ON a.occurrence=o.id WHERE o.id=?", (oid,)).fetchone()


def _cost(text):
    if absent(text):
        return zh(text)
    c = json.loads(text)
    tok = c.get("tokens")
    t = zh(tok) if absent(tok) else f"输入 {tok.get('input', 0) + tok.get('cache_write', 0):,} / 输出 {tok.get('output', 0):,} token"
    s = c.get("seconds")
    return f"{zh(s) if absent(s) else f'{s:.0f} 秒'}，{t}"


def resolve(conn, h):
    m = re.fullmatch(r"([EJC])(\d+)", h.strip())
    if not m:
        return None, None
    table = HANDLES[m.group(1)]
    return table, conn.execute(f"SELECT * FROM {table} WHERE id=?", (int(m.group(2)),)).fetchone()


def trace(conn, h):
    """一条 Claim 沿 Occurrence 回溯到原话（文本）。"""
    table, row = resolve(conn, h)
    if row is None:
        return f"{h}：没有这一条"
    act = _occ(conn, row["born"])
    win = json.loads(act["window"] or "{}")
    out = [f"{h}  {row['text']}", ""]
    if table == "judgment":
        out.append("持有者：" + "、".join(x["who"] + ("" if absent(x["role"]) else f"（{x['role']}）") for x in json.loads(row["holders"])))
        rels = json.loads(row["relations"])
        out.append("关系：" + ("；".join(f"{REL_ZH.get(r['kind'], r['kind'])} J{r['to']}" for r in rels) or "无"))
    if table == "event":
        out.append(f"mode：{zh(row['mode'])}；限定：{zh(row['qualifier'])}；说话人：{zh(row['speaker'])}")
        out += _supersession(conn, row)
    out += ["", "沿 Occurrence 回溯："]
    out.append(f"- 什么时候：{act['started']} – {act['ended']}（窗口：{act['scale']}）")
    out.append(f"- 谁、按什么做法：{act['op_name']}（{STEP_ZH.get(act['step'], act['step'])}）")
    ins = json.loads(act["inputs"])
    out.append("- 看了什么：" + "；".join(f"{k} {len(v)} 条" for k, v in ins.items()))
    for u in conn.execute("SELECT o.inputs, d.mode FROM occurrence o JOIN use_detail d ON d.occurrence=o.id "
                          "WHERE o.role='use' AND o.parent=?", (act["id"],)):
        n = len(json.loads(u["inputs"]).get("judgment", []))
        out.append(f"  - 经由拉取（{u['mode']}）看到已有判断 {n} 条")
    out.append(f"- 因为什么：{zh(act['cause'])}")
    for rid in ins.get("request", []):
        out += ["  " + x for x in _request(conn, rid)]
    for rid, to, reason in conn.execute("SELECT o.id, d.to_process, d.reason FROM occurrence o JOIN request_detail d "
                                        "ON d.occurrence=o.id WHERE o.parent=?", (act["id"],)):
        out.append(f"- 这次写下的申请：{rid}（给 {to}）：{reason[:120]}")
    pred, chain = act["predecessor"], []
    while not absent(pred):
        p = _occ(conn, pred)
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE born=?", (p["id"],)).fetchone()[0]
        chain.append(f"{p['started'][:10]} {short(p['op_name'])}（{p['outcome']}，{n} 条）")
        pred = p["predecessor"]
    out.append("- 之前同样的事：" + (" ← ".join(chain) if chain else "没有"))
    out.append(f"- 结果：{act['outcome']}；成本：{_cost(act['cost'])}")
    if "session" in win and not absent(win["session"]):
        s = conn.execute("SELECT * FROM session_detail WHERE occurrence=?", (win["session"],)).fetchone()
        out.append(f"- 所在会话：{s['source']} {s['session']}（{zh(s['cwd'])}，{zh(s['title'])}）")
    out += ["", "依据，一路到原话："]
    out += _down(conn, table, row, "  ")
    return "\n".join(out)


def _request(conn, rid):
    """一张申请：谁写给谁、理由、点名了什么、发现它的那次 act。"""
    r = conn.execute("SELECT o.inputs, o.parent, o.started, d.from_process, d.to_process, d.reason, p.name FROM occurrence o "
                     "JOIN request_detail d ON d.occurrence=o.id JOIN operator p ON p.id=o.operator WHERE o.id=?", (rid,)).fetchone()
    if r is None:
        return [f"申请 {rid}：没有这一张"]
    ins = json.loads(r["inputs"])
    what = "；".join(f"{k} " + "、".join(map(str, v[:8])) + ("……" if len(v) > 8 else "") for k, v in ins.items())
    return [f"申请 {rid}（{r['started'][:16]}，{r['from_process']} → {r['to_process']}，{short(r['name'])}）：{r['reason']}",
            f"  点名：{what}" + ("" if absent(r["parent"]) else f"；发现它的 act {r['parent']}")]


def _supersession(conn, row):
    """E 的取代：它取代了谁（关系），它后来被谁取代（observe.fate 推出）。"""
    out = []
    for rel in json.loads(row["relations"]):
        old = conn.execute("SELECT text FROM event WHERE id=?", (rel["to"],)).fetchone()
        out.append(f"取代：E{rel['to']}  {old[0][:120] if old else '（没有这一条）'}")
    from . import observe
    for f in observe.fate(conn, row["id"]):
        req = "、".join(map(str, f["request"]))
        if f["how"] == "corrected":
            out.append(f"被取代：纠正写了 " + "、".join(f"E{x}" for x in f["by"]) + f"（act {f['act']}，申请 {req}）")
        elif f["how"] == "kept":
            out.append(f"核对过，保留（act {f['act']}，申请 {req}）：{f['why']}")
        elif f["how"] == "invalid":
            out.append(f"不成立：纠正核对的结论（act {f['act']}，申请 {req}）：{f['why']}")
        else:
            out.append(f"被取代：这一轮被范围申请重新观察过（读 act {f['act']}，申请 {req or '没找到'}），这一轮现在的 E："
                       + ("、".join(f"E{x}" for x in f["by"]) or "没有"))
    return out


def _turn_of(conn, turn_occ, n):
    """E 挂的那一轮所在的会话里，第 n 轮（出处段落写成 n:A2 时）。"""
    row = conn.execute("SELECT d2.occurrence FROM turn_detail d JOIN turn_detail d2 ON d2.source=d.source AND d2.session=d.session "
                       "WHERE d.occurrence=? AND d2.ordinal=?", (turn_occ, n - 1)).fetchone()
    return row[0] if row else None


def _down(conn, table, row, pad, depth=0):
    if table == "event":
        if absent(row["turn"]):
            return [f"{pad}[{s}] （找不到）" for s in json.loads(row["segments"])]
        out, cache = [], {}
        for s in json.loads(row["segments"]):
            n, _, k = s.rpartition(":")      # 几轮一起读写“轮:段落”（3:A2），旧版本逐轮写 U1、A2
            occ = _turn_of(conn, row["turn"], int(n)) if n else row["turn"]
            if occ not in cache:
                cache[occ] = segments(conn, occ, row["born"]) if occ else {}
            out.append(f"{pad}[{s}] {cache[occ].get(k, '（找不到）')[:160]}")
        return out
    refs = json.loads(row["basis"] if table == "judgment" else row["refs"])
    out = []
    for kind, ids in refs.items():
        for i in ids:
            r = conn.execute(f"SELECT * FROM {kind} WHERE id=?", (i,)).fetchone()
            label = f"E{i}" if kind == "event" else ("会话记录" if r["kind"] == "session" else "逐轮记录") + f" #{i}"
            out.append(f"{pad}{label}  {r['text'][:160]}")
            if depth < 3:
                out += _down(conn, kind, r, pad + "  ", depth + 1)
    return out


# ---- 一段会话的阅读页 ----

CSS = """
/* 布局：单栏阅读，一条判断的回溯在前，后面按原话 → 判断的层次往下排；长列表折叠 */
:root{
  --paper:#f6f7f9; --ink:#1c2230; --muted:#5d6678; --rule:#d9dde5; --wash:#eceff4;
  --event:#2f6b4f; --judg:#34508f; --rec:#6b5d45; --warn:#9a5b12;
  --display:"Noto Serif SC","Songti SC",serif; --body:"Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;
  --mono:"JetBrains Mono",ui-monospace,Menlo,monospace;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --paper:#14171d; --ink:#e3e6ec; --muted:#9aa3b3; --rule:#2c323d; --wash:#1c2029;
  --event:#7fc3a0; --judg:#9db4ea; --rec:#c9b48e; --warn:#e3a85a; color-scheme:dark}}
:root[data-theme="dark"]{
  --paper:#14171d; --ink:#e3e6ec; --muted:#9aa3b3; --rule:#2c323d; --wash:#1c2029;
  --event:#7fc3a0; --judg:#9db4ea; --rec:#c9b48e; --warn:#e3a85a; color-scheme:dark}
body{background:var(--paper);color:var(--ink);font:15px/1.75 var(--body);
  padding-block:32px 64px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
main{max-width:46rem;margin:0 auto;display:flex;flex-direction:column;gap:40px}
h1,h2,h3{font-family:var(--display);font-weight:600;text-wrap:balance;margin:0}
h1{font-size:30px;line-height:1.3} h2{font-size:21px;border-bottom:1px solid var(--rule);padding-bottom:6px}
h3{font-size:16px}
section{display:flex;flex-direction:column;gap:14px}
p{margin:0}
.lede{color:var(--muted)}
.meta{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;font-size:14px}
.meta dt{color:var(--muted)} .meta dd{margin:0}
.h{font-family:var(--mono);font-size:13px;font-weight:600;white-space:nowrap}
.h.E{color:var(--event)} .h.J{color:var(--judg)} .h.R{color:var(--rec)}
.mark{font-size:12px;color:var(--muted);border:1px dashed var(--rule);border-radius:3px;padding:0 4px}
.rel{font-size:13px;color:var(--muted)} .rel b{color:var(--warn);font-weight:600}
.item{display:grid;grid-template-columns:5.5rem 1fr;gap:4px 12px;padding-block:8px;border-top:1px solid var(--rule)}
.item > .body{display:flex;flex-direction:column;gap:4px}
.sub{font-size:13px;color:var(--muted)}
.trace{background:var(--wash);border-radius:6px;padding:18px 20px;display:flex;flex-direction:column;gap:10px}
.trace .q{display:grid;grid-template-columns:7.5rem 1fr;gap:12px;font-size:14px}
.trace .q span:first-child{color:var(--muted)}
.chain{border-left:2px solid var(--rule);padding-left:12px;display:flex;flex-direction:column;gap:6px;font-size:14px}
.chain .chain{margin-top:4px}
.raw{font-size:13px;color:var(--muted);white-space:pre-wrap;word-break:break-word}
details{border-top:1px solid var(--rule);padding-block:8px}
summary{cursor:pointer;font-weight:500}
summary:focus-visible{outline:2px solid var(--judg);outline-offset:2px}
.table{overflow-x:auto}
table{border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums;min-width:40rem;width:100%}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--rule);vertical-align:top}
th{color:var(--muted);font-weight:500;letter-spacing:.04em}
.turn-user{white-space:pre-wrap;word-break:break-word;background:var(--wash);border-radius:4px;padding:10px 12px;font-size:14px}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;600'
         '&family=Noto+Sans+SC:wght@400;500&family=Noto+Serif+SC:wght@600&display=swap">')


def e(s):
    return html.escape(str(s))


def v(x):
    return f'<span class="mark">{MARK_ZH[x]}</span>' if absent(x) else e(x)


def _acts(conn, where, args):
    return conn.execute(f"""SELECT o.*, p.name AS op_name, a.step, a.window FROM occurrence o
                            JOIN act_detail a ON a.occurrence=o.id JOIN operator p ON p.id=o.operator
                            WHERE {where} ORDER BY o.id""", args).fetchall()


def _latest_ok(acts):
    ok = [a for a in acts if a["outcome"] == "ok"]
    return ok[-1] if ok else None


def page(conn, source, session, focus=None, title=None, note=""):
    s = conn.execute("SELECT * FROM session_detail WHERE source=? AND session LIKE ?", (source, session + "%")).fetchone()
    sid = s["occurrence"]
    turns = conn.execute("SELECT d.*, x.user, x.assistant, o.started FROM turn_detail d JOIN turn_text x ON x.id=d.text "
                         "JOIN occurrence o ON o.id=d.occurrence WHERE o.parent=? ORDER BY d.ordinal", (sid,)).fetchall()
    tids = [t["occurrence"] for t in turns]
    qs = ",".join("?" * len(tids))
    turn_acts = _acts(conn, f"a.step IN ('split','organize') AND json_extract(a.window,'$.turn') IN ({qs})", tids)
    mine = set(tids)
    previews = [a for a in _acts(conn, "a.process='observe' AND a.step='preview'", ()) if mine & set(json.loads(a["window"])["turns"])]
    group_acts = [a for a in _acts(conn, "a.process='observe' AND a.step IN ('deep','rough')", ())
                  if mine & set(json.loads(a["window"])["turns"])]
    packs = _acts(conn, "a.step='pack' AND json_extract(a.window,'$.session')=?", (sid,))
    judges = _acts(conn, "a.step='judge' AND json_extract(a.window,'$.session')=?", (sid,))
    judge = _latest_ok(judges)
    relates = _acts(conn, "a.step='relate' AND o.parent=?", (judge["id"],)) if judge else []
    js = conn.execute("SELECT * FROM judgment WHERE born=? ORDER BY idx", (judge["id"],)).fetchall() if judge else []
    pack = next((p for p in reversed(packs) if p["outcome"] == "ok" and judge and
                 json.loads(judge["window"])["unit"] == p["id"]), _latest_ok(packs))
    lines = conn.execute("SELECT * FROM form_record WHERE born=? ORDER BY idx", (pack["id"],)).fetchall() if pack else []
    uses = conn.execute("SELECT o.*, d.* FROM occurrence o JOIN use_detail d ON d.occurrence=o.id WHERE o.parent IN "
                        f"({','.join('?' * (len(judges) + len(relates) + 1))}) ORDER BY o.id",
                        [sid] + [a["id"] for a in judges + relates]).fetchall()
    n_ev = sum(conn.execute("SELECT COUNT(*) FROM event WHERE born=?", (a["id"],)).fetchone()[0]
               for a in turn_acts + group_acts if a["step"] in ("split", "deep", "rough"))
    n_rel = sum(len(json.loads(j["relations"])) for j in js)

    def jtext(jid):
        r = conn.execute("SELECT j.text, o.started, a.window FROM judgment j JOIN occurrence o ON o.id=j.born "
                         "JOIN act_detail a ON a.occurrence=o.id WHERE j.id=?", (jid,)).fetchone()
        if r is None:
            return "（不在库里）", ""
        anchor = json.loads(r["window"]).get("anchor", UNKNOWN)
        return r["text"], anchor[:10] if not absent(anchor) else ""

    out = [f"<title>{e(title or zh(s['title']))}</title>", '<meta name="claude:viewport-fit-sides" content="cover">', FONTS,
           f"<style>{CSS}</style>", "<main>"]
    out.append(f"""<header style="display:flex;flex-direction:column;gap:12px">
<p class="sub">新库 engram.db · 一段会话从原话到判断</p>
<h1>{e(zh(s['title']))}</h1>
<p class="lede">原话是 turn；observe 先预览{e(label.USER_NAME)}的话、把轮次分组定读法，再精读、粗读，写下 event（E）；旧版本的 E 是逐轮观察的。
逐轮记录、会话记录是 form 的内部数据，judgment（J）是判断，旧认识 K 是旧版本 J（编号不变）。每一步都是一次 act。{e(note)}</p>
<dl class="meta"><dt>会话</dt><dd>{e(s['source'])} · <span class="h">{e(s['session'])}</span></dd>
<dt>工作区</dt><dd>{v(s['cwd'])}</dd><dt>当时的我</dt><dd>{v(s['model'])}</dd>
<dt>开始</dt><dd>{e(conn.execute('SELECT started FROM occurrence WHERE id=?', (sid,)).fetchone()[0])}</dd>
<dt>这一片</dt><dd>{len(turns)} 轮 · {len(turn_acts) + len(previews) + len(group_acts) + len(packs) + len(judges) + len(relates)} 次 act · {n_ev} 条 E · {len(js)} 条 J（当前一版）· {n_rel} 条关系 · {len(uses)} 次 use</dd></dl>
</header>""")

    if focus:
        out.append(f'<section><h2>沿 Occurrence 回溯一条判断</h2>{_trace_html(conn, focus)}</section>')

    # observe：预览分组和精读、粗读（2026-10-17 起）
    if previews:
        out.append(_observe_html(conn, previews, group_acts, tids))
        out.append(_correct_html(conn, set(tids)))

    # 判断
    if judge:
        out.append(f"<section><h2>形成的判断</h2><p class=\"sub\">{e(short(judge['op_name']))} · {e(judge['started'])} · "
                   f"{_cost(judge['cost'])}。取代、拆分、合并后面附上被取代的那条和它出自哪天。</p>")
    else:
        out.append("<section><h2>形成的判断</h2><p class=\"sub\">这段会话还没有形成过。</p>")
    for j in js:
        hs = "、".join(f"{e(h['who'])}{'' if absent(h['role']) else '（' + e(h['role']) + '）'}" for h in json.loads(j["holders"]))
        rel_html = []
        for r in json.loads(j["relations"]):
            t, day = jtext(r["to"])
            kind = REL_ZH.get(r["kind"], r["kind"])
            if r["kind"] in ("supersedes", "split_from", "merged_from", "contradicts"):
                rel_html.append(f'<div class="rel"><b>{kind}</b> <span class="h J">J{r["to"]}</span> {e(day)} · {e(t)}</div>')
            else:
                rel_html.append(f'<div class="rel">{kind} <span class="h J">J{r["to"]}</span> {e(day)}</div>')
        out.append(f'<div class="item"><span class="h J">J{j["id"]}</span><div class="body"><span>{e(j["text"])}</span>'
                   f'<span class="sub">持有者 {hs} · 依据 {e("、".join(json.loads(j["basis_labels"])))} · 句式 {v(j["pattern"])}</span>'
                   + "".join(rel_html) + "</div></div>")
    older = [a for a in judges if a["id"] != (judge["id"] if judge else None)]
    if older:
        out.append("<details><summary>同一段会话更早的形成（旧 View 并存，不是取代）</summary>")
        for a in older:
            n = conn.execute("SELECT COUNT(*) FROM judgment WHERE born=?", (a["id"],)).fetchone()[0]
            ex = conn.execute("SELECT id, text FROM judgment WHERE born=? ORDER BY idx LIMIT 40", (a["id"],)).fetchall()
            out.append(f'<h3 style="margin-top:10px">{e(short(a["op_name"]))} · {e(a["started"][:16])} · {a["outcome"]} · {n} 条</h3>')
            out += [f'<div class="item"><span class="h J">J{x["id"]}</span><div class="body">{e(x["text"])}</div></div>' for x in ex]
        out.append("</details>")
    out.append("</section>")

    # 会话记录
    if pack:
        out.append(f"<section><h2>会话记录</h2><p class=\"sub\">form 的内部数据，形成读的就是它 · {e(short(pack['op_name']))} · "
                   f"{e(pack['started'][:16])} · {_cost(pack['cost'])}</p>")
        for i, ln in enumerate(lines, 1):
            refs = json.loads(ln["refs"]).get("form_record", [])
            out.append(f'<div class="item"><span class="h R">S{i}</span><div class="body"><span>{e(ln["text"])}</span>'
                       f'<span class="sub">引用逐轮记录 {e("、".join(json.loads(ln["labels"])))} → '
                       f'{e("、".join("#" + str(x) for x in refs)) or "无"}</span></div></div>')
        out.append("</section>")

    # 逐轮
    if not turn_acts:
        out.append("<section><h2>逐轮的原话</h2>")
        for t in turns:
            out.append(f'<h3>第 {t["ordinal"] + 1} 轮 · {e(t["started"][:16])} · 开头是 {e(t["origin"])}</h3>')
            out.append(f'<div class="turn-user">{e(label.user_text(t["user"])[0][:600])}{"…" if len(t["user"]) > 600 else ""}</div>')
        turns = []
    else:
        out.append("<section><h2>逐轮：原话 → event → 逐轮记录</h2>")
    for t in turns:
        mine = [a for a in turn_acts if json.loads(a["window"])["turn"] == t["occurrence"]]
        split, org = _latest_ok([a for a in mine if a["step"] == "split"]), _latest_ok([a for a in mine if a["step"] == "organize"])
        out.append(f'<h3>第 {t["ordinal"] + 1} 轮 · {e(t["started"][:16])} · 开头是 {e(t["origin"])}</h3>')
        out.append(f'<div class="turn-user">{e(label.user_text(t["user"])[0][:600])}{"…" if len(t["user"]) > 600 else ""}</div>')
        segs = segments(conn, t["occurrence"])          # 旧版本逐轮观察（split）：读过压缩总结全文
        if split:
            evs = conn.execute("SELECT * FROM event WHERE born=? ORDER BY idx", (split["id"],)).fetchall()
            out.append(f'<details><summary>{len(evs)} 条 E · {e(short(split["op_name"]))} · {_cost(split["cost"])}</summary>')
            for x in evs:
                seg = "；".join(f"[{s_}] {segs.get(s_, '（找不到）')[:80]}" for s_ in json.loads(x["segments"])[:2])
                out.append(f'<div class="item"><span class="h E">E{x["id"]}</span><div class="body"><span>{e(x["text"])}</span>'
                           f'<span class="sub">mode {v(x["mode"])} · 说话人 {v(x["speaker"])}</span>'
                           f'<span class="raw">{e(seg)}</span></div></div>')
            out.append("</details>")
        if org:
            recs = conn.execute("SELECT * FROM form_record WHERE born=? ORDER BY idx", (org["id"],)).fetchall()
            out.append(f'<details><summary>{len(recs)} 条逐轮记录 · {e(short(org["op_name"]))}</summary>')
            for x in recs:
                refs = json.loads(x["refs"]).get("event", [])
                out.append(f'<div class="item"><span class="h R">#{x["id"]}</span><div class="body"><span>{e(x["text"])}</span>'
                           f'<span class="sub">引用 {e(" ".join("E" + str(i) for i in refs))}</span></div></div>')
            out.append("</details>")
    out.append("</section>")

    # 时间线
    rows = sorted(turn_acts + previews + group_acts + packs + judges + relates, key=lambda a: a["started"])
    out.append('<section><h2>这一片的发生</h2><p class="sub">每次 act 和 use 都是一条 Occurrence；前身是对同一份输入的上一次同类发生。</p>'
               '<div class="table"><table><thead><tr><th>编号</th><th>步骤</th><th>Operator</th><th>开始</th><th>结果</th>'
               '<th>成本</th><th>前身</th></tr></thead><tbody>')
    for a in rows:
        out.append(f'<tr><td class="h">{a["id"]}</td><td>{e(STEP_ZH.get(a["step"], a["step"]))}</td><td>{e(short(a["op_name"]))}</td>'
                   f'<td>{e(a["started"][:16])}</td><td>{e(a["outcome"])}</td><td>{e(_cost(a["cost"]))}</td>'
                   f'<td>{v(a["predecessor"])}</td></tr>')
    for u in uses:
        n = sum(len(x) for x in json.loads(u["inputs"]).values())
        out.append(f'<tr><td class="h">{u["occurrence"]}</td><td>use · {e(u["channel"])} / {e(u["mode"])}</td>'
                   f'<td>{e(u["purpose"])}</td><td>{e(u["started"][:16])}</td><td>{e(u["outcome"])}</td>'
                   f'<td>读到 {n} 条</td><td>{v(u["predecessor"])}</td></tr>')
    out.append("</tbody></table></div></section>")
    out.append('<footer class="sub">虚线框里的是三种标记：<span class="mark">未知</span> <span class="mark">没做</span> '
               '<span class="mark">不适用</span>。旧数据没有的字段（event 的 mode、说话人，判断的句式）照实标出，不默认成某个值。</footer>')
    out.append("</main>")
    return "\n".join(out)


def _observe_html(conn, previews, group_acts, tids):
    """预览定下的分组（轮次、类型、读法、计划），每组下面是精读、粗读写下的 E（说话人、行为由 modes.read 从正文读出）。"""
    ordinal = {t: o for t, o in conn.execute(f"SELECT occurrence, ordinal FROM turn_detail WHERE occurrence IN "
                                             f"({','.join('?' * len(tids))})", tids)}
    by_turns = {}
    for a in group_acts:
        by_turns.setdefault(tuple(json.loads(a["window"])["turns"]), []).append(a)
    from . import observe
    gone = observe.superseded(conn)
    out = [f'<section><h2>observe：预览与精读、粗读</h2><p class="sub">预览只看{e(label.USER_NAME)}的话，把轮次分组、定读法；精读、粗读把那几轮拆成 E。'
           '预览写的东西是 observe 的内部数据，不给 form。</p>']
    for p in previews:
        meta = json.loads(conn.execute("SELECT meta FROM act_detail WHERE occurrence=?", (p["id"],)).fetchone()[0])
        w = json.loads(p["window"])["turns"]
        span = f"第 {ordinal.get(w[0], -1) + 1}–{ordinal.get(w[-1], -1) + 1} 轮"
        out.append(f'<h3>预览 {span} · {e(short(p["op_name"]))} · {e(p["started"][:16])} · {e(p["outcome"])} · {_cost(p["cost"])}</h3>')
        for g in reads(meta):
            how = g["how"] + (f"（预览写的是{g['asked']}，超出精读上限改的）" if g.get("asked") and g["asked"] != g["how"] else "")
            out.append(f'<div class="item"><span class="h">第 {g["from"]}–{g["to"]} 轮</span><div class="body">'
                       f'<span><b>{e(how)}</b> · {e(g.get("type") or "")}</span><span class="raw">{e(g.get("plan", ""))}</span>')
            for a in by_turns.get(tuple(g.get("turns", [])), []):
                evs = conn.execute("SELECT * FROM event WHERE born=? ORDER BY idx", (a["id"],)).fetchall()
                out.append(f'<details><summary>{len(evs)} 条 E · {e(short(a["op_name"]))} · {e(a["outcome"])} · {_cost(a["cost"])}'
                           f'</summary>')
                for x in evs:
                    r = modes.read(x["text"])
                    out.append(f'<div class="item"><span class="h E">E{x["id"]}</span><div class="body"><span>{e(x["text"])}</span>'
                               f'<span class="sub">{e(r["speaker"] if not absent(r["speaker"]) else "说话人未知")} · '
                               f'{e(r["mode"] if not absent(r["mode"]) else "行为未知")} · 出处 '
                               f'{e("、".join(json.loads(x["segments"])))}'
                               + (' · <span class="mark">已被取代，见“申请与纠正”或回溯</span>' if x["id"] in gone else "")
                               + '</span></div></div>')
                out.append("</details>")
            out.append("</div></div>")
    out.append("</section>")
    return "\n".join(out)


def _correct_html(conn, mine):
    """申请与纠正：这段会话里每次纠正，申请的理由，被点名的旧 E、核对结论、改正的新 E 并排（C2，2026-10-21）。"""
    acts = [a for a in _acts(conn, "a.process='observe' AND a.step='correct'", ()) if mine & set(json.loads(a["window"])["turns"])]
    if not acts:
        return ""
    out = ['<section><h2>申请与纠正</h2><p class="sub">点名申请：被点名的旧 E 回到原文逐条核对，改正的写新 E 取代它，'
           '不成立的不再交给 form。范围申请（再观察）的整轮取代不在这里，见各条 E 的回溯。</p>']
    for a in acts:
        ins = json.loads(a["inputs"])
        if a["outcome"] != "ok":            # 没成功的只留一行：环境错误不计次数，超时、内容错误计
            out.append(f'<p class="sub">纠正 act {a["id"]}（申请 {"、".join(map(str, ins.get("request", [])))}）：'
                       f'{e(a["outcome"])}，没出结果 · {e(short(a["op_name"]))}</p>')
            continue
        meta = json.loads(conn.execute("SELECT meta FROM act_detail WHERE occurrence=?", (a["id"],)).fetchone()[0])
        for rid in ins.get("request", []):
            r = conn.execute("SELECT d.from_process, d.reason, o.started FROM request_detail d JOIN occurrence o ON o.id=d.occurrence "
                             "WHERE d.occurrence=?", (rid,)).fetchone()
            out.append(f'<h3>申请 {rid} · {e(r["from_process"])} · {e(r["started"][:16])} → 纠正 act {a["id"]} · '
                       f'{e(short(a["op_name"]))} · {e(a["outcome"])}</h3><p class="raw">{e(r["reason"])}</p>')
        news = conn.execute("SELECT * FROM event WHERE born=? ORDER BY idx", (a["id"],)).fetchall()
        check = meta.get("check", {})
        for eid in ins.get("event", []):
            old = conn.execute("SELECT text, segments FROM event WHERE id=?", (eid,)).fetchone()
            c = check.get(str(eid), {"verdict": "没写结论", "why": ""})
            by = [x for x in news if eid in [r["to"] for r in json.loads(x["relations"])]]
            out.append(f'<div class="item"><span class="h E">E{eid}</span><div class="body">'
                       f'<span{" style=\"text-decoration:line-through\"" if by or c["verdict"] == "不成立" else ""}>{e(old["text"])}</span>'
                       f'<span class="sub">出处 {e("、".join(json.loads(old["segments"])))}</span>'
                       f'<span class="rel"><b>{e(c["verdict"])}</b> {e(c["why"])}</span>'
                       + "".join(f'<span><span class="h E">→ E{x["id"]}</span> {e(x["text"])}</span>' for x in by)
                       + '</div></div>')
        for x in news:
            if not json.loads(x["relations"]):
                out.append(f'<div class="item"><span class="h E">+ E{x["id"]}</span><div class="body"><span>{e(x["text"])}</span>'
                           f'<span class="sub">申请说漏了的，补上</span></div></div>')
    out.append("</section>")
    return "\n".join(out)


def _trace_html(conn, h):
    table, row = resolve(conn, h)
    act = _occ(conn, row["born"])
    ins = json.loads(act["inputs"])
    looked = "；".join(f"{'会话记录' if k == 'form_record' else k} {len(x)} 条" for k, x in ins.items())
    for u in conn.execute("SELECT o.inputs, d.mode FROM occurrence o JOIN use_detail d ON d.occurrence=o.id "
                          "WHERE o.role='use' AND o.parent=?", (act["id"],)):
        looked += f"；经由拉取（{u['mode']}）看到已有判断 {len(json.loads(u['inputs']).get('judgment', []))} 条"
    pred, chain = act["predecessor"], []
    while not absent(pred):
        p = _occ(conn, pred)
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE born=?", (p["id"],)).fetchone()[0]
        chain.append(f"{p['started'][:10]} {short(p['op_name'])}（{n} 条）")
        pred = p["predecessor"]
    qa = [("什么时候", f"{act['started']} 到 {act['ended']}；窗口是一段会话"),
          ("谁、按什么做法", f"{short(act['op_name'])}（{STEP_ZH.get(act['step'])}）"),
          ("看了什么", looked),
          ("因为什么", f'<span class="mark">{MARK_ZH.get(act["cause"], act["cause"])}</span> 旧库没记触发'),
          ("之前同样的事", " ← ".join(chain) or "没有"),
          ("结果与成本", f"{act['outcome']}，{_cost(act['cost'])}")]
    rels = json.loads(row["relations"]) if table == "judgment" else []
    rel = "".join(f'<div class="rel"><b>{REL_ZH[r["kind"]]}</b> <span class="h J">J{r["to"]}</span> '
                  f'{e(conn.execute("SELECT text FROM judgment WHERE id=?", (r["to"],)).fetchone()[0])}</div>' for r in rels)
    return (f'<div class="trace"><div><span class="h J">{e(h)}</span> {e(row["text"])}</div>{rel}'
            + "".join(f'<div class="q"><span>{a}</span><span>{b if a == "因为什么" else e(b)}</span></div>' for a, b in qa)
            + f'<div class="chain">{_down_html(conn, table, row)}</div></div>')


def _down_html(conn, table, row, depth=0):
    if table == "event":
        segs = segments(conn, row["turn"], row["born"]) if not absent(row["turn"]) else {}
        return "".join(f'<div class="raw">原话 [{e(s)}] {e(segs.get(s, "（找不到）")[:220])}</div>'
                       for s in json.loads(row["segments"]))
    refs = json.loads(row["basis"] if table == "judgment" else row["refs"])
    out = []
    for kind, ids in refs.items():
        for i in ids:
            r = conn.execute(f"SELECT * FROM {kind} WHERE id=?", (i,)).fetchone()
            if kind == "event":
                label = f'<span class="h E">E{i}</span>'
            else:
                label = f'<span class="h R">{"会话记录" if r["kind"] == "session" else "逐轮记录"} #{i}</span>'
            out.append(f"<div>{label} {e(r['text'])}<div class=\"chain\">{_down_html(conn, kind, r, depth + 1)}</div></div>")
    return "".join(out)
