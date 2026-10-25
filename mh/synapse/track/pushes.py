"""推送事件的流水：每次推送写两份（同一份数据，各给人或程序看），最近一次联想另落一份全文。

- `events/synapse-<日期>.jsonl`：机器可读的原始记录，字段齐全（词段、档位、候选、未展示数都在）；
- `events/synapse-<日期>.md`：同一批事件的可读版，一行一次推送；
- `events/synapse-latest.md`：最近一次联想的正文，标题写明送达还是只记录。

为什么（2026-10-15）：旧 dsh 版 synapse（`memory-souhatsu`）每推一次写一条事件
（`diary log souhatsu` 进 access.log），最新提示落盘 `state/recall.md`，推送在会话里看得见、在文件里也翻得到。
MH 这边的 `deliveries`/`reads` 表是给程序算的，缺一条人可以直接翻的流水。json 与 md 由同一次调用渲染，
不做第二次判断，所以两份不会互相矛盾；md 随时可以由 jsonl 重放。

只记录、没送达的联想也进流水（2026-10-15：未送达只进审计流，不进会话），调门槛时看这份最省事。

纪律（照 events.py）：只追加、按天分文件、不碰 SQLite（宿主 hook 可能并发调用），任何错误都吞掉——
宁可没有流水，也不影响推送本身。
"""
import json
import os
from datetime import datetime
from pathlib import Path

HOME = Path(os.environ.get("MH_HOME", Path.home() / ".local/share/memory-harness"))   # 同 collect.store.HOME
DIR = HOME / "events"
LATEST = DIR / "synapse-latest.md"
TEXT_CUT = 800       # JSONL 里留多少字正文；全文在 deliveries 表里
KEEP = 6000          # 最新一份 markdown 留多少字


def note(kind, host, session, cwd, delivered, summary="", text=None, **fields):
    """记一条推送事件，返回落下的时刻。给了 text 就同时更新「最近一次联想」那份 markdown。"""
    at = datetime.now().astimezone().isoformat(timespec="seconds")
    ev = {"at": at, "kind": kind, "host": host, "session": session, "cwd": cwd,
          "delivered": delivered, "summary": summary}
    ev.update({k: v for k, v in fields.items() if v not in (None, "", [], {})})
    if text:
        ev["text"] = text[:TEXT_CUT] + (f"…〔共 {len(text)} 字〕" if len(text) > TEXT_CUT else "")
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        with open(DIR / f"synapse-{at[:10]}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        _markdown(ev)
    except Exception:
        pass
    if text:
        try:
            head = [f"# 最近一次联想（{at}）", "",
                    f"- 结果：{'已送达' if delivered else '只记录，未推送'}",
                    f"- 宿主：{host}　会话：{session or '（无）'}　目录：{cwd or '（无）'}",
                    f"- {summary}"]
            DIR.mkdir(parents=True, exist_ok=True)
            LATEST.write_text("\n".join(head) + "\n\n---\n\n" + text[:KEEP] + "\n", encoding="utf-8")
        except Exception:
            pass
    return at


def _markdown(ev):
    """把一条事件渲染成一行，追加到当天的 md；文件不存在时先写表头。"""
    day = ev["at"][:10]
    path = DIR / f"synapse-{day}.md"
    first = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        if first:
            f.write(f"# Synapse 推送事件 · {day}\n\n"
                    f"一次推送一行；同目录 `synapse-{day}.jsonl` 是同一批事件的机器可读版（字段齐全）。\n\n")
        f.write(_line(ev) + "\n")


def _line(ev):
    """一行里放人看的东西；jsonl 里有而这里没有的字段（目录、正文、Atlas 版本以外的细节）去 jsonl 找。"""
    body = []
    if ev["kind"] == "assist":
        head = f"**{ev['at'][11:19]}** · {ev.get('host') or '?'} · 手记推送"
        body.append(ev.get("summary", "").removeprefix("手记推送："))
        if ev.get("knowledge"):
            body.append("推送 " + "、".join(f"K{k}" for k in ev["knowledge"]))
        if ev.get("workspace"):
            body.append(f"工作区 {ev['workspace']}")
        if ev.get("atlas"):
            body.append(f"Atlas 第 {ev['atlas']} 版")
    else:
        # 没送出去有两种原因，分开标：档位本身只记录；或在送达档，但候选都已在上下文、这段对话里出现过（skipped）
        state = "已送达" if ev["delivered"] else (f"未送达：{ev['skipped']}" if ev.get("skipped") else "只记录，未推送")
        head = f"**{ev['at'][11:19]}** · {ev.get('host') or '?'} · 联想 · {state}"
        if ev.get("tier"):
            body.append("档位 " + "、".join(ev["tier"]) + (f"（按 {'+'.join(ev['used'])}）" if ev.get("used") else ""))
        seen, terms = set(), []
        for t in ev.get("terms") or []:
            if t.get("t") in seen:
                continue
            seen.add(t.get("t"))
            terms.append(f"{t.get('t')}（{t.get('df')} 条{('，' + t['tier']) if t.get('tier') else ''}）")
        if terms:
            body.append("词段 " + "、".join(terms))
        body.append(f"候选 {ev.get('candidates', 0)}")
        gone = (ev.get("in_context") or 0) + (ev.get("seen") or 0)
        if gone:
            body.append(f"已出现过 {gone}（上下文里 {ev.get('in_context') or 0}，对话里给过 {ev.get('seen') or 0}）")
        body.append(("展示 " + "、".join(ev.get("shown") or [])) if ev.get("shown") else "没有展示")
        if ev.get("omitted"):
            body.append(f"未展示 {ev['omitted']}")
        if ev.get("workspace"):
            body.append(f"工作区 {ev['workspace']}")
    if ev.get("session"):
        body.append(f"会话 {ev['session']}")
    return "- " + head + " —— " + "；".join(x for x in body if x)


if __name__ == "__main__":
    # 宿主插件（mh/synapse/hosts/dsh/）写流水用：标准输入一条 JSON，字段同 note()
    import sys
    try:
        d = json.load(sys.stdin)
        note(d.pop("kind"), d.pop("host", ""), d.pop("session", ""), d.pop("cwd", ""), bool(d.pop("delivered", False)),
             d.pop("summary", ""), d.pop("text", None), **d)
    except Exception:
        pass
