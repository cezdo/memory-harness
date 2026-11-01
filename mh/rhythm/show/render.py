"""排页：把 collect 的条目排成一页“此刻的我”（此刻、事、流、在哪、认识），返回 HTML。
同一份条目，每节是按不同字段的一种投影；措辞只陈述事实和时间，不催（用户 2026-11-01）。"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape as e

from ...engram.store.instance import SHOW
from .collect import SPAN, DAYS, ago, span

BIG, SHOWN = 6, 6   # 一个工作区近几天有几段会话算大的（占半行）；每块最多列几段
DOT = {"ok": "ok", "live": "ok", "warn": "warn", "bad": "bad", "idle": "mute", "next": "ok", "wait": "warn", "open": "mute"}


def row_spans(n, each):
    """n 块排成一行一行，每块默认占 each 格（共 12 格），最后一行不满时把格子分给这一行，免得空一截。"""
    per, spans = 12 // each, []
    while n > 0:
        k = min(per, n)
        spans += [12 // k] * k
        n -= k
    return spans


def dot(s):
    return f'<i class="dot {DOT.get(s, "mute")}"></i>'


def kv(state, name, value, what=""):
    tip = f' title="{e(what)}"' if what else ""
    return f'<div class="kv"{tip}>{dot(state)}<b>{e(name)}</b><span class="v">{e(value)}</span></div>'


def bar(label, used, note):
    cls = "ok" if used < 70 else "warn" if used < 90 else "bad"
    return (f'<div class="bar"><span class="lbl">{e(label)}</span><span class="track"><span class="fill {cls}" style="width:{used}%"></span></span>'
            f'<span class="num">{used}%</span><span class="mute small">{e(note)}</span></div>')


class Page:
    def __init__(self, items, now=None, cfg=SHOW):
        self.items, self.cfg = items, cfg
        self.now = now or datetime.now(timezone.utc)
        self.tz = datetime.now().astimezone().tzinfo

    def of(self, kind):
        return [i for i in self.items if i["kind"] == kind]

    def one(self, kind):
        return (self.of(kind) or [None])[0]

    def loc(self, d, f="%m-%d %H:%M"):
        return d.astimezone(self.tz).strftime(f)

    # ---------- 此刻 ----------
    def now_view(self):
        out = ['<div class="tiles">',
               '<div class="tile"><h4>常驻服务</h4>' + "".join(kv(i["state"], i["title"], i["note"], i.get("what", "")) for i in self.of("服务")) + "</div>"]
        q = ['<div class="tile"><h4>额度</h4>']
        for i in self.of("额度"):
            q.append(f'<div class="qname">{e(i["title"])}</div>' + "".join(bar(w["win"], w["used"], w["reset"] + " 重置") for w in i["windows"]))
        out.append("".join(q) + "</div>")
        az = self.one("local_model")
        if az:
            last = f'{"上次" if az.get("held") == "released" else "现在"}：{az.get("holder") or "?"}'
            if az.get("time") and az.get("held") == "released":
                last += f"，{self.loc(datetime.fromisoformat(az['time']))} 放开"
            out.append(f'<div class="tile"><h4>本地模型</h4>{kv(az["state"], "空闲" if az["state"] == "ok" else "有人占着", "")}'
                       + "".join(kv("ok" if s == "loaded" else "idle", m, "已加载" if s == "loaded" else "没加载") for m, s in az["models"])
                       + (f'<p class="small mute">{e(az["err"])}</p>' if az.get("err") else "") + f'<p class="small mute">{e(last)}</p></div>')
        out.append('<div class="tile"><h4>整理循环</h4>' + "".join(kv(i["state"], i["title"], i["note"], i.get("what", "")) for i in self.of("整理任务")) + "</div>")
        out.append(self.qq_tile() + self.host_tile())
        out += [self.machine_tile(i) for i in self.of("机器")]
        live = [i for i in self.of("会话") if i["state"] == "live"]
        out.append('<div class="tile wide"><h4>正在进行的会话（一小时内有轮次）</h4>' + ("".join(
            f'<div class="row">{dot("live")}<b>{e(i["title"])}</b><span class="tag">{e(i["host"])}</span><span class="tag">{e(i["thing"] or "")}</span>'
            f'<span class="when">{i["turns"]} 轮</span><span class="handle">{e(i["handle"])}</span></div>' for i in live) or '<p class="mute">没有</p>') + "</div>")
        out.append("</div>")
        return "".join(out)

    def qq_tile(self):
        q = self.one("QQ")
        if not q:
            return ""
        lm, lr = (datetime.fromisoformat(x) if x else None for x in (q.get("last_msg"), q.get("last_reply")))
        rows = [kv(st, n, x) for n, st, x in q["tunnels"]]
        rows.append(kv("idle", "最近一条群消息", f"{self.loc(lm)}，{ago(lm, self.now)}" if lm else "没有"))
        rows.append(kv("idle", "上次回话", f"{self.loc(lr)}，今天回了 {q['today']} 条" if lr else "没有"))
        return (f'<div class="tile" style="--span:6"><h4>QQ</h4>' + (f'<p class="chain small mute">{e(q["chain"])}</p>' if q.get("chain") else "")
                + "".join(rows) + "</div>")

    def machine_tile(self, h):
        """一台机器：在不在、开机多久、CPU、内存、GPU、磁盘。本机的磁盘在“重启后看这里”，不重复列。"""
        at = datetime.fromisoformat(h["at"])
        old = f'<p class="small mute">上次量是 {ago(at, self.now)}</p>' if self.now - at > timedelta(minutes=15) else ""
        if h["state"] != "ok":
            return f'<div class="tile"><h4>{dot("bad")}{e(h["title"])}</h4><p class="small mute">{e(h.get("err", ""))}</p>{old}</div>'
        gb = lambda b: f"{b / 2**30:.0f}" if b >= 10 * 2**30 else f"{b / 2**30:.1f}"   # noqa: E731
        up = f'开机 {span(self.now - datetime.fromisoformat(h["boot"]))}' if h.get("boot") else ""
        rows = []
        if h.get("cpu") is not None:
            rows.append(bar("CPU", round(h["cpu"]), f'{h["cores"]} 核' + (f'，负载 {h["load"]:.1f}' if h.get("load") is not None else "")))
        used, total = h["mem"]
        rows.append(bar("内存", round(100 * used / total), f"{gb(used)}/{gb(total)} GB"))
        if h.get("gpu") is not None:
            mem = ("占内存" if h.get("unified") else "显存") + f' {gb(h["vram"])} GB' if h.get("vram") else ""
            rows.append(bar("GPU", min(round(h["gpu"]), 100), mem))
        if not h.get("local"):
            rows += [bar(m, round(100 * u / s), f"{gb(s - u)} GB 空") for m, s, u in h.get("disks", []) if s > 2**30]
        return (f'<div class="tile"><h4>{dot("ok")}{e(h["title"])}<span class="mute small">{e(up)}</span></h4>'
                + '<div class="meters">' + "".join(rows) + "</div>" + old + "</div>")

    def host_tile(self):
        h = self.one("宿主")
        if not h:
            return ""
        rows = [kv("idle", "开机", h["boot"][5:16])]
        if h.get("dsh"):
            port, st, since = h["dsh"]
            rows.append(kv(st, f"dsh（{port}）", ("在" if st == "ok" else "不在") + (f"，{since[4:16]} 起" if since else "")))
        if h.get("backup"):
            res, when = (h["backup"] + ["", ""])[:2]
            rows.append(kv("ok" if res == "success" else "bad", "dsh 备份", f"上次 {when[4:20] or '?'}，{'成功' if res == 'success' else res}"))
        disks = "".join(bar(m, u, sz) for m, sz, u in h["disks"])
        return (f'<div class="tile" style="--span:6"><h4>重启后看这里</h4>' + "".join(rows)
                + f'<div class="disks">{disks}</div></div>')

    # ---------- 事 ----------
    def age(self, i):
        days = (self.now - datetime.fromisoformat(i["time"])).days
        return f'<span class="age">{f"上次动是 {days} 天前" if days else "今天动过"}</span>'

    def thing_row(self, i, hint=True):
        body = f'<p class="small mute body">{e(i["body"])}{"……" if i["size"] > 400 else ""}</p>'
        note = (f'<div class="hint next">→ {e(i["next"])}</div>' if i.get("next") else
                f'<div class="hint wait">{e(i["wait"])}</div>' if i.get("wait") else "") if hint else ""
        return f'<details class="item"><summary><span class="ttl">{e(i["title"])}</span>{self.age(i)}</summary>{body}</details>{note}'

    def thing_view(self):
        xs = sorted(self.of("事"), key=lambda x: x["time"])
        nxt, wait, rest = ([i for i in xs if i["state"] == s] for s in ("next", "wait", "open"))
        out = ['<div class="pair">',
               f'<div class="panel"><h4>{dot("next")}有下一步 <span class="mute">{len(nxt)}</span></h4>' + "".join(map(self.thing_row, nxt)) + "</div>",
               f'<div class="panel"><h4>{dot("wait")}在等 <span class="mute">{len(wait)}</span></h4>' + "".join(map(self.thing_row, wait)) + "</div>",
               "</div>"]
        groups = defaultdict(list)
        for i in rest:
            groups[i["thing"] or "?"].append(i)
        out.append(f'<details class="panel open-list"><summary><h4>{dot("open")}开着 <span class="mute">{len(rest)}</span>'
                   f'<span class="mute small">：没写下一步，也没写在等什么，程序判不出状态（点开）</span></h4></summary><div class="groups">')
        for g, ys in sorted(groups.items(), key=lambda kv_: -len(kv_[1])):
            out.append(f'<div><div class="gname">{e(g)}</div>' + "".join(self.thing_row(i, hint=False) for i in ys) + "</div>")
        out.append("</div></details>")
        by_ws = defaultdict(list)
        for i in self.of("会话"):
            by_ws[i["thing"] or "?"].append(i)
        big = sorted([(w, ys) for w, ys in by_ws.items() if len(ys) >= BIG], key=lambda kv_: -len(kv_[1]))
        mid = sorted([(w, ys) for w, ys in by_ws.items() if 1 < len(ys) < BIG], key=lambda kv_: -len(kv_[1]))
        one = sorted([ys[0] for ys in by_ws.values() if len(ys) == 1], key=lambda x: x["time"], reverse=True)
        panels = [(w, ys, False) for w, ys in big + mid] + ([("其他工作区", one, True)] if one else [])
        spans = row_spans(len(big), 6) + row_spans(len(panels) - len(big), 4)   # 大的两个一行，小的三个一行
        out.append(f'<h3>线头：近 {DAYS} 天停下的会话</h3><div class="ws">')
        for (ws, ys, mixed), span in zip(panels, spans):
            out.append(f'<div class="panel" style="--span:{span}"><h4>{e(ws)} <span class="mute">{len(ys)}</span></h4>' + "".join(
                f'<div class="sess"><div class="row">{dot(i["state"])}<span class="ttl">{e(i["title"])}</span>'
                f'<span class="when">{self.loc(datetime.fromisoformat(i["time"]))} · {i["turns"]} 轮</span></div><div class="said handle">'
                f'{e(i["thing"] or "?") + " · " if mixed else ""}{e(i["handle"])}</div>'
                f'<div class="said small mute">最后：{e(i["said"])}</div></div>' for i in ys[:SHOWN])
                + (f'<p class="mute small more">还有 {len(ys) - SHOWN} 段</p>' if len(ys) > SHOWN else "") + "</div>")
        out.append("</div>")
        return "".join(out)

    # ---------- 流 ----------
    def flow_view(self):
        W, L, top = 1100, 120, 28
        x0 = self.now - timedelta(hours=SPAN)
        fi = datetime.fromisoformat

        def X(d):
            return L + (W - L - 10) * max(0, (d - x0).total_seconds()) / (SPAN * 3600)

        rows = []
        by_host = defaultdict(list)
        for i in self.of("会话"):
            if fi(i["time"]) > x0:
                by_host[i["host"]].append(i)
        for host, xs in by_host.items():   # 同一宿主里重叠的会话分几行
            tracks, at = [], {}
            for i in sorted(xs, key=lambda x: x["start"]):
                k = next((k for k, end in enumerate(tracks) if end < fi(i["start"])), None)
                if k is None:
                    tracks.append(None)
                    k = len(tracks) - 1
                tracks[k], at[id(i)] = fi(i["time"]), k
            rows += [(f"会话 · {host}" if k == 0 else "", [i for i in xs if at[id(i)] == k], "sess") for k in range(len(tracks))]
        rows += [("提交", self.of("提交"), "commit"), ("群里说话", self.of("发言"), "say")]
        acts = (self.one("流量") or {}).get("acts", [])
        rows += [(f"{p} 每小时", [a for a in acts if a[0] == p], "heat") for p in ("observe", "form")]
        H = top + 22 * len(rows) + 10
        g = [f'<svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" class="flow">']
        for h in range(0, SPAN + 1, 3):
            d = (x0 + timedelta(hours=h)).replace(minute=0, second=0, microsecond=0)
            lab = self.loc(d, "%H") if self.loc(d, "%H") != "00" else self.loc(d, "%m-%d")
            g.append(f'<line x1="{X(d):.0f}" y1="{top - 6}" x2="{X(d):.0f}" y2="{H}" class="grid"/><text x="{X(d):.0f}" y="{top - 10}" class="ax">{lab}</text>')
        cs = sorted(a[2] for a in acts) or [1]
        mx = cs[int(len(cs) * 0.9)] if len(cs) > 1 else cs[0]   # 按九成分位定最深，免得一次出错的尖峰把别的都压淡
        for r, (name, xs, kind) in enumerate(rows):
            y = top + 22 * r
            g.append(f'<text x="4" y="{y + 14}" class="ln">{e(name)}</text>')
            for i in xs:
                if kind == "sess":
                    s, f = max(fi(i["start"]), x0), fi(i["time"])
                    g.append(f'<rect x="{X(s):.1f}" y="{y + 3}" width="{max(3, X(f) - X(s)):.1f}" height="14" rx="3" class="s {i["state"]}">'
                             f'<title>{e(i["title"])}（{e(i["thing"] or "")}，{i["turns"]} 轮）{self.loc(fi(i["start"]))}–{self.loc(f)}</title></rect>')
                elif kind == "commit":
                    g.append(f'<rect x="{X(fi(i["time"])) - 1:.1f}" y="{y + 2}" width="3" height="16" class="c"><title>{self.loc(fi(i["time"]))} {e(i["title"])}</title></rect>')
                elif kind == "say":
                    g.append(f'<circle cx="{X(fi(i["time"])):.1f}" cy="{y + 10}" r="3.5" class="m"><title>{self.loc(fi(i["time"]))} {e(i["body"])}</title></circle>')
                else:
                    d = fi(i[1])
                    g.append(f'<rect x="{X(d):.1f}" y="{y + 3}" width="{max(2, X(d + timedelta(hours=1)) - X(d) - 1):.1f}" height="14" '
                             f'class="h{" err" if i[3] else ""}" style="opacity:{0.25 + 0.75 * min(1, i[2] / mx):.2f}">'
                             f'<title>{self.loc(d)} {i[0]} {i[2]} 次{f"，出错 {i[3]}" if i[3] else ""}</title></rect>')
        g.append(f'<line x1="{X(self.now):.0f}" y1="{top - 6}" x2="{X(self.now):.0f}" y2="{H}" class="now"/></svg>')
        legend = [("sw-s", "会话"), ("sw-live", "一小时内还在说"), ("sw-c", "提交"), ("sw-m", "群里说话"),
                  ("sw-h", "观察、形成每小时次数，越深越多"), ("sw-err", "有失败"), ("sw-now", "现在")]
        return ('<div class="legend">' + "".join(f'<span><i class="sw {c}"></i>{e(x)}</span>' for c, x in legend) + "</div>"
                '<div class="scroll">' + "".join(g) + "</div>")

    # ---------- 在哪：入口、机器、路径都来自实例配置 [show] ----------
    def where_view(self):
        entries, hosts, paths = (self.cfg.get(k, []) for k in ("entries", "hosts", "paths"))
        out = '<div class="pair"><div class="panel"><h4>入口</h4>' + "".join(
            f'<div class="sess"><div class="row"><span class="ttl">{e(x["name"])}</span><a class="small" href="{e(x["url"])}">{e(x["url"])}</a></div>'
            f'<div class="said small mute">{e(x.get("note", ""))}</div></div>' for x in entries) + '</div><div class="panel"><h4>机器</h4>' + "".join(
            f'<div class="sess"><div class="row"><span class="ttl">{e(x["name"])}</span></div>'
            f'<div class="said small mute" style="white-space:normal">{e(x.get("note", ""))}</div></div>' for x in hosts) + "</div></div>"
        if paths:
            out += '<div class="panel"><h4>东西都在哪</h4><div class="paths">' + "".join(
                f'<div class="p-name">{e(x["name"])}</div><code class="p-path">{e(x["path"])}</code><div class="p-note small mute">{e(x.get("note", ""))}</div>'
                for x in paths) + "</div></div>"
        return out

    def cases_view(self):
        return '<ul class="cases">' + "".join(f'<li><span class="when">{e(i["body"])}</span><span>{e(i["title"])}</span></li>' for i in self.of("案例")) + "</ul>"

    def gist(self):
        svc = self.of("服务")
        ok = sum(i["state"] == "ok" for i in svc)
        live = sum(i["state"] == "live" for i in self.of("会话"))
        chips = [("ok" if ok == len(svc) else "warn", f"服务 {ok}/{len(svc)} 正常"),
                 *[("ok" if w["used"] < 70 else "warn" if w["used"] < 90 else "bad", f"{i['name']} 周额度 {w['used']}%")
                   for i in self.of("额度") for w in i["windows"] if w["win"] == "week"]]
        hosts = self.of("机器")
        if hosts:
            on = sum(i["state"] == "ok" for i in hosts)
            chips.append(("ok" if on == len(hosts) else "warn", f"机器 {on}/{len(hosts)} 在线"))
        az = self.one("local_model")
        if az:
            chips.append((az["state"], "本地模型 " + ("空闲" if az["state"] == "ok" else "有人在用")))
        chips += [("ok" if live else "idle", f"{live} 段会话在进行"), ("next", f"{sum(i['state'] == 'next' for i in self.of('事'))} 件事写了下一步")]
        return '<div class="gist">' + "".join(f'<span class="chip">{dot(c)}{e(x)}</span>' for c, x in chips) + "</div>"

    def html(self):
        sections = [("now", "此刻", "现在好不好：绿是正常，黄是要留意，红是出错，灰是闲着。", self.now_view),
                    ("thing", "事项", "runbook 里没做的每一项是一件事；按上次动的时间排，早的在前（按 runbook 那一行最后一次改动算，大致看看）。点开看全文。", self.thing_view),
                    ("flow", "节奏", f"近 {SPAN} 小时谁在什么时候动，停在上面看详情。", self.flow_view),
                    ("where", "在哪", "记不住的地址、机器和路径。", self.where_view),
                    ("cases", "案例", "memory-harness 的案例日志最近几条：这几天想清楚了什么。", self.cases_view)]
        page = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta http-equiv="refresh" content="300">',
                f"<title>此刻的我 · mh</title><style>{CSS}</style></head><body><main>",
                f'<header class="top"><h1>此刻的我</h1><span class="stamp">{self.loc(self.now, "%Y-%m-%d %H:%M")} 现编 · 只读 · <a href="../preview/">预览站</a></span>{self.gist()}</header><nav>'
                + "".join(f'<a href="#{k}">{n}</a>' for k, n, *_ in sections) + "</nav>"]
        page += [f'<section id="{k}"><h2>{n}</h2><p class="cap">{cap}</p>{fn()}</section>' for k, n, cap, fn in sections]
        # 节奏图比手机屏宽，先滚到“现在”那一端
        page.append("<script>document.querySelectorAll('.scroll').forEach(s=>s.scrollLeft=s.scrollWidth)</script>")
        return "\n".join(page + ["</main></body></html>"])


def render(items, now=None, cfg=SHOW):
    return Page(items, now, cfg).html()


CSS = """
/* 版式：一栏读下来，先一句话的“现在”，再按节展开；面板只用底色区分，不加框和阴影。
   字：系统自带的中文字体（谷歌字体在国内连不上），节名用宋体，正文黑体，句柄、时刻、数字用等宽。
   色：带一点青灰的纸色和墨色，强调色是靛蓝；绿黄红只表示状态。 */
:root{
  --paper:#f2f4f2; --panel:#e8ece9; --ink:#1d2327; --mute:#647079; --rule:#d3d9d5;
  --indigo:#2d4b8e; --ok:#2f8a57; --warn:#b7791f; --bad:#c0392b; --say:#7a4fb0;
  --serif:"Songti SC","Noto Serif CJK SC","Source Han Serif SC","SimSun",serif;
  --sans:-apple-system,"PingFang SC","Noto Sans CJK SC","Source Han Sans SC","Microsoft YaHei",sans-serif;
  --mono:"SF Mono","JetBrains Mono","Cascadia Mono",Consolas,"Noto Sans Mono CJK SC",monospace;
  --bg:var(--paper); --fg:var(--ink); --line:var(--rule); --card:var(--panel); --acc:var(--indigo);
  color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){
  --paper:#14181b; --panel:#1c2226; --ink:#e2e7e4; --mute:#93a0a8; --rule:#2c353a;
  --indigo:#8fa9e6; --ok:#5cbf86; --warn:#e2a64a; --bad:#ec6f5f; --say:#b896e6; color-scheme:dark}}
:root[data-theme=dark]{
  --paper:#14181b; --panel:#1c2226; --ink:#e2e7e4; --mute:#93a0a8; --rule:#2c353a;
  --indigo:#8fa9e6; --ok:#5cbf86; --warn:#e2a64a; --bad:#ec6f5f; --say:#b896e6; color-scheme:dark}
body{background:var(--paper);color:var(--ink);font:15px/1.65 var(--sans);margin:0;
  padding-block:28px 64px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
main{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:6px}
a{color:var(--indigo)} a:focus-visible,summary:focus-visible{outline:2px solid var(--indigo);outline-offset:2px}
header.top{display:flex;flex-direction:column;gap:6px;padding-bottom:14px;border-bottom:1px solid var(--rule)}
h1{font:600 26px/1.3 var(--serif);margin:0;letter-spacing:.02em;text-wrap:balance}
.stamp{font:12px var(--mono);color:var(--mute);font-variant-numeric:tabular-nums}
.gist{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:13px;padding:3px 10px;border-radius:999px;background:var(--panel)}
nav{display:inline-flex;flex-wrap:wrap;gap:2px;margin-top:4px;padding:3px;background:var(--panel);border-radius:8px;align-self:flex-start;max-width:100%}
nav a{text-decoration:none;color:var(--ink);font-size:13.5px;padding:4px 14px;border-radius:6px;transition:background .15s,color .15s}
nav a:hover{background:var(--paper);color:var(--indigo)}
h2{font:600 19px/1.4 var(--serif);margin:34px 0 0;text-wrap:balance}
h3{font:600 15px/1.4 var(--serif);margin:22px 0 8px} h4{margin:0 0 10px;font-size:13px;font-weight:600;color:var(--mute);letter-spacing:.04em;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.cap{color:var(--mute);margin:2px 0 12px;font-size:13px;max-width:68ch}
.mute{color:var(--mute)} .small{font-size:12.5px}
.tag{font:11px var(--mono);color:var(--mute);white-space:nowrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex:none;background:var(--mute);opacity:.6}
.dot.ok{background:var(--ok);opacity:1}.dot.warn{background:var(--warn);opacity:1}.dot.bad{background:var(--bad);opacity:1}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;min-width:0}
.tiles{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:10px}.tiles>.tile{grid-column:span var(--span,3)}
@media (max-width:1000px){.tiles>.tile{grid-column:span 6}}@media (max-width:620px){.tiles>.tile{grid-column:1/-1}}
.chain{margin:-2px 0 8px}.disks{margin-top:10px;display:flex;flex-direction:column;gap:3px}.disks .bar{grid-template-columns:minmax(110px,auto) 1fr 38px 44px}
.tile,.panel,.col,.wsg,.tier{background:var(--panel);border-radius:8px;padding:14px 16px;min-width:0}
.tile.wide{grid-column:1/-1}.tile .row{margin:4px 0}
.kv{display:grid;grid-template-columns:auto auto 1fr;align-items:center;column-gap:8px;margin:5px 0}
.kv .v{justify-self:end;text-align:right;font:12px var(--mono);color:var(--mute);font-variant-numeric:tabular-nums}
.paths{display:grid;grid-template-columns:max-content minmax(0,max-content) minmax(0,1fr);gap:6px 18px;align-items:baseline}
.p-name{font-weight:600;white-space:nowrap}.p-path{font:12.5px var(--mono);color:var(--indigo);overflow-wrap:anywhere}
@media (max-width:760px){.paths{grid-template-columns:1fr}.p-note{margin-bottom:6px}}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12.5px;color:var(--mute);margin:0 0 8px}.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{display:inline-block;width:14px;height:10px;border-radius:2px}.sw-s{background:var(--indigo);opacity:.7}.sw-live{background:var(--ok)}
.sw-c{width:3px;height:12px;background:var(--ink);opacity:.55}.sw-m{width:8px;height:8px;border-radius:50%;background:var(--say)}
.sw-h{background:linear-gradient(90deg,color-mix(in srgb,var(--indigo) 25%,transparent),var(--indigo))}.sw-err{background:var(--bad)}
.sw-now{width:0;height:12px;border-left:2px dashed var(--bad);border-radius:0}
.qname{font-size:12.5px;margin:8px 0 2px}.qname:first-of-type{margin-top:0}
.bar{display:grid;grid-template-columns:42px 1fr 38px auto;gap:8px;align-items:center;font-size:12px;font-variant-numeric:tabular-nums}
.bar .lbl,.bar .num{font-family:var(--mono)} .num{text-align:right}
.meters{display:grid;grid-template-columns:42px 1fr 38px auto;gap:6px 8px;align-items:center}.meters .bar{display:contents}
.track{height:6px;background:var(--rule);border-radius:3px;overflow:hidden}.fill{display:block;height:100%}
.fill.ok{background:var(--ok)}.fill.warn{background:var(--warn)}.fill.bad{background:var(--bad)}
.pair{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(330px,100%),1fr));gap:10px;margin-bottom:10px}
.item{border-top:1px solid var(--rule);padding:8px 0 4px}.item:first-of-type{border-top:0;padding-top:0}
.item summary{display:flex;gap:10px;align-items:baseline;cursor:pointer;list-style:none}.item summary::-webkit-details-marker{display:none}
.item summary:hover .ttl{color:var(--indigo)}
.ttl{font-weight:600;flex:1;min-width:0;overflow-wrap:anywhere}.age{font:12px var(--mono);color:var(--mute);white-space:nowrap}
.hint{font-size:13.5px;margin:2px 0 8px;padding-left:10px;border-left:2px solid var(--rule)}
.hint.next{color:var(--ok);border-color:var(--ok)}.hint.wait{color:var(--warn);border-color:var(--warn)}
.body{margin:4px 0 8px;max-width:72ch}
.open-list>summary{list-style:none;cursor:pointer}.open-list>summary::-webkit-details-marker{display:none}.open-list h4{margin:0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:4px 28px;margin-top:12px}.gname{font-size:12px;color:var(--mute);margin:10px 0 4px}
.ws{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:10px}.ws>.panel{grid-column:span var(--span,4);display:flex;flex-direction:column}.ws .more{margin:auto 0 0;padding-top:6px}
@media (max-width:760px){.ws>.panel{grid-column:1/-1}}
.sess{border-top:1px solid var(--rule);padding:7px 0}.sess:first-of-type{border-top:0;padding-top:0}
.when{font:12px var(--mono);color:var(--mute);white-space:nowrap;font-variant-numeric:tabular-nums}
.said{margin:2px 0 0 16px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.handle{font:11.5px var(--mono);color:var(--indigo)}
.scroll{overflow-x:auto;background:var(--panel);border-radius:8px;padding:10px}
.flow text{fill:var(--mute);font:11px var(--mono)}.flow .ln{fill:var(--ink);font-family:var(--sans)}.grid{stroke:var(--rule)}.now{stroke:var(--bad);stroke-dasharray:3 3}
.s{fill:var(--indigo);opacity:.7}.s.live{fill:var(--ok);opacity:1}.c{fill:var(--ink);opacity:.55}.m{fill:var(--say)}.h{fill:var(--indigo)}.h.err{fill:var(--bad)}
ul.cases{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:6px;max-width:80ch}
ul.cases li{display:flex;gap:12px}ul.cases .when{flex:none}
/* 手机：边距收紧，节目录吸顶 */
@media (max-width:620px){
  body{padding-block:16px 48px;font-size:14.5px}
  h1{font-size:22px} h2{margin-top:26px}
  nav{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;box-shadow:0 0 0 6px var(--paper)}
  nav a{padding:4px 11px}
  .tile,.panel,.col,.wsg,.tier{padding:12px 14px}
  .disks .bar{grid-template-columns:minmax(90px,auto) 1fr 38px auto}
  ul.cases li{flex-direction:column;gap:0}
  .item summary{flex-wrap:wrap;gap:2px 10px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""
