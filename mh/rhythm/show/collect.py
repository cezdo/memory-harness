"""收条目：把 mh 和宿主现在的真实状态收成一份条目，每条 {kind, title, time?, state, thing?, body, ...}。
只读：库只读打开，服务只问状态；额度读 serve 在后台写的缓存，没有就现查（慢，codex 最多等 60 秒）。"""
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ...engram.store import HOME
from ...engram.store.instance import LISTEN, SHOW, _D, _dir

REPO = Path(__file__).resolve().parents[3]
SPAN = 48                  # 时间线看几个小时
DAYS = 3                   # 线头看几天的会话
QUOTA_FRESH = 1200         # 额度缓存多久内算新（秒）；serve 每 10 分钟写一次
LOOKBACK = 72             # 服务的日志往回找几个小时的出错（找上次出错在什么时候）
TAIL = 400_000             # 监听记录从末尾读多少字节找最近一条群消息
LIVE = ("426", "400", "200", "401", "403")   # 隧道口有这些应答，说明协议端在（WS 端口对普通 GET 一般回 426）


def sh(*a, timeout=90):
    try:
        return subprocess.run(a, cwd=REPO, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as e:
        return f"?{e}"


def t(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def span(delta):
    """一段时长 → “5 分钟”“3 小时”“2 天”。"""
    m = int(delta.total_seconds() // 60)
    return f"{m} 分钟" if m < 90 else f"{m // 60} 小时" if m < 48 * 60 else f"{m // 1440} 天"


def ago(d, now):
    m = int((now - d).total_seconds() // 60)
    return "刚才" if m < 1 else f"{m} 分钟前" if m < 90 else f"{m // 60} 小时前" if m < 48 * 60 else f"{m // 1440} 天前"


def tunnel(listening, code):
    """隧道口的状态：端口没开是隧道没起；连上有应答是隧道通、协议端在；连上没应答是协议端没应。"""
    if not listening:
        return "bad", "隧道没起"
    return ("ok", "通，协议端在") if code in LIVE else ("warn", f"隧道在，协议端没应（{code or '没回'}）")


def runbook_state(text):
    """runbook 一项的正文 → (state, 下一步, 在等什么)：写了“下一步：”的是 next，写了“等……”的是 wait，都没有是 open。"""
    nxt = re.search(r"下一步[：:]([^。；]*)", text)
    wait = re.search(r"(?<!待)等(?!待)([^，。；）]{2,24})", text)
    if nxt:
        return "next", nxt[1].strip(), None
    return ("wait", None, "等" + wait[1]) if wait else ("open", None, None)


def collect(now=None, home=HOME, cfg=SHOW):
    now = now or datetime.now(timezone.utc)
    local = datetime.now().astimezone().tzinfo
    items = []
    add = lambda **k: items.append(k)   # noqa: E731

    # 常驻服务：装着的都列，出错按项目现状的同一把尺子数
    from .. import register
    from ...synapse.push import status
    for name in register.ALL:
        st = register.state(name)
        if st in ("", "not-found"):
            continue
        errs = [x for x in register.lines(name, LOOKBACK) if status.ERR.search(x[1])]
        last = t(errs[-1][0]) if errs else None
        start = sh("systemctl", "--user", "show", name, "-p", "ActiveEnterTimestamp", "--value").strip() if register.PLATFORM == "systemd" else ""
        start = datetime.strptime(start[4:23], "%Y-%m-%d %H:%M:%S").replace(tzinfo=local).astimezone(timezone.utc) if len(start) > 23 else None
        quiet = last if last and (not start or last > start) else start      # 从上次出错（或这次起来）到现在
        add(kind="服务", title=name.removeprefix("memory-harness-"),
            state="bad" if st != "active" else "warn" if last and now - last < timedelta(hours=status.ERR_HOURS) else "ok",
            note={"inactive": "停着", "failed": "挂了", "activating": "正在起"}.get(st, st) if st != "active" else f"{span(now - last)}前出错" if last and now - last < timedelta(hours=status.ERR_HOURS)
            else f"{span(now - quiet)}没出错" if quiet else "在跑",
            what=f"{start.astimezone(local):%m-%d %H:%M} 起" + (f"；上次出错 {last.astimezone(local):%m-%d %H:%M}：{errs[-1][1][:80]}" if last else "") if start else "")

    # 额度
    cache = home / "show" / "quota.txt"
    quota = cache.read_text() if cache.exists() and time.time() - cache.stat().st_mtime < QUOTA_FRESH else sh(sys.executable, "-m", "mh", "quota")
    for ln in quota.splitlines():
        m = re.match(r"(\S+?)（(\w+)）：(.*)", ln)
        if m:
            windows = [dict(win=w, used=int(u), reset=r) for w, u, r in re.findall(r"(\w+) 已用 (\d+)%，(\d\d-\d\d \d\d:\d\d) 重置", m[3])]
            add(kind="额度", title=f"{m[1]}（{m[2]}）", name=m[1], windows=windows)

    # 本地模型：占用记录 + 哪个模型加载着
    if cfg.get("local_model"):
        try:
            hold = json.loads((home / cfg.get("local_model_status", "local_model.json")).read_text())
        except Exception:
            hold = {}
        try:
            key = Path(_dir(cfg["local_model_key"])).read_text().strip() if cfg.get("local_model_key") else ""
            req = urllib.request.Request(cfg["local_model"].rstrip("/") + "/v1/models", headers={"Authorization": f"Bearer {key}"})
            models = [[x["id"], x["status"]["value"]] for x in json.load(urllib.request.urlopen(req, timeout=5))["data"]]
            err = None
        except Exception as e:
            models, err = [], f"读不到（{e.__class__.__name__}）"
        add(kind="local_model", title="本地模型", state="ok" if hold.get("status", "released") == "released" else "warn", models=models, err=err,
            holder=hold.get("holder"), held=hold.get("status"), time=t(hold["until"]).isoformat() if hold.get("until") else None)

    # QQ 一链：协议端 → ssh 隧道（监听、回复）→ listen、reply
    ports = [("监听隧道", int(LISTEN.get("local", 13001))), ("回复隧道", int(_D.get("reply", {}).get("local", 13002)))] if LISTEN.get("ssh") else []
    if ports:
        ss = sh("ss", "-ltn")
        tunnels = [[n, *tunnel(f"127.0.0.1:{p} " in ss, sh("curl", "-s", "-m", "3", "-o", "/dev/null", "-w", "%{http_code}", f"http://127.0.0.1:{p}/").strip())]
                   for n, p in ports]
        last_msg = None
        try:
            with open(home / "listen-onebot.jsonl", "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - TAIL))
                for ln in f.read().decode("utf-8", "ignore").splitlines()[1:]:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    ev = r.get("event") or {}
                    if ev.get("post_type") == "message" and ev.get("message_type") == "group":
                        last_msg = r.get("received")
        except OSError:
            pass
        last_reply, today = None, 0
        day0 = now.astimezone(local).replace(hour=0, minute=0, second=0, microsecond=0)
        for d in _replies(home):
            last_reply = d
            today += d >= day0
        add(kind="QQ", title="QQ", tunnels=tunnels, last_msg=last_msg, today=today,
            last_reply=last_reply.isoformat() if last_reply else None, chain=cfg.get("chain", ""))

    # 重启后要看的：本机开机、宿主 dsh、每天的备份、磁盘
    dsh = None
    if cfg.get("dsh_port"):
        port = int(cfg["dsh_port"])
        pid = re.search(rf"127\.0\.0\.1:{port} .*pid=(\d+)", sh("ss", "-ltnp"))
        code = sh("curl", "-s", "-m", "3", "-o", "/dev/null", "-w", "%{http_code}", f"http://127.0.0.1:{port}/").strip()
        dsh = [port, "ok" if code and code != "000" else "bad", sh("ps", "-o", "lstart=", "-p", pid[1]).strip() if pid else ""]
    backup = sh("systemctl", "--user", "show", cfg["backup_unit"], "-p", "Result", "-p", "ExecMainExitTimestamp", "--value").split("\n")[:2] \
        if cfg.get("backup_unit") else None
    disks = []
    for ln in sh("df", "-h", "--output=target,size,pcent", "-x", "tmpfs", "-x", "devtmpfs", "-x", "squashfs",
                 "-x", "overlay", "-x", "efivarfs").splitlines()[1:]:
        m = re.match(r"(\S+)\s+(\S+)\s+(\d+)%", ln.strip())
        if m and not m[1].startswith("/boot"):
            disks.append([m[1], m[2], int(m[3])])
    add(kind="宿主", title="本机", boot=sh("uptime", "-s").strip(), dsh=dsh, backup=backup, disks=disks)

    # 整理循环：每个任务上次什么时候跑
    from .. import TIDY
    try:
        state = json.loads((home / "tidy-state.json").read_text())
    except (OSError, ValueError):
        state = {}
    for name, how, period, fn in TIDY:
        last = state.get(name)
        d = datetime.fromtimestamp(last, timezone.utc) if last else None
        add(kind="整理任务", title=name, state="ok" if d and (now - d).total_seconds() < period * 1.5 else "warn",
            note=("上次 " + ago(d, now)) if d else "没跑过", what=(fn.__doc__ or "").strip().split("\n")[0].split("（")[0])

    # 会话（近 DAYS 天，不分工作区）
    from ...synapse.push import recent
    conn = sqlite3.connect(f"file:{home / 'engram.db'}?mode=ro", uri=True)
    try:
        for x in recent.active(conn, (now - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")):
            first, n = conn.execute("""SELECT MIN(o.started), COUNT(*) FROM turn_detail d JOIN occurrence o ON o.id=d.occurrence
                                       WHERE d.source=? AND d.session=?""", (x["source"], x["session"])).fetchone()
            said = conn.execute("""SELECT x.user FROM turn_detail d JOIN turn_text x ON x.id=d.text
                                   WHERE d.source=? AND d.session=? AND d.origin='human' ORDER BY d.ordinal DESC LIMIT 1""",
                                (x["source"], x["session"])).fetchone()
            said = (said[0] if said else "") or ""
            add(kind="会话", title=x["title"] if (x["title"] or "?")[:1] != "?" else said[:30] or "?", thing=recent.workspace(x["cwd"]),
                host=x["source"], start=t(first).isoformat(), time=x["last"].isoformat(), turns=n, handle=recent.handle(conn, x),
                state="live" if now - x["last"] < timedelta(hours=1) else "idle", said=said[:60])

        # 观察、形成每小时的认知行为，带出错数
        proc = dict(conn.execute("SELECT id, process FROM operator"))
        acts, errors = Counter(), Counter()
        for started, op, err in conn.execute("SELECT started, operator, error FROM occurrence WHERE role='act' AND started>=?",
                                             ((now - timedelta(hours=SPAN)).strftime("%Y-%m-%dT%H:%M:%S"),)):
            key = (proc.get(op, "?"), t(started).replace(minute=0, second=0, microsecond=0).isoformat())
            acts[key] += 1
            errors[key] += bool(err and err != "?n/a")
        add(kind="流量", title="认知行为", acts=[[p, h, c, errors[(p, h)]] for (p, h), c in acts.items()])
    finally:
        conn.close()

    # 提交
    for ln in sh("git", "log", f"--since={SPAN} hours ago", "--format=%aI\t%h\t%s").splitlines():
        when, h, s = ln.split("\t", 2)
        add(kind="提交", title=s[:70], time=t(when).isoformat(), body=h)

    # 群里说的话
    for d, sent in _replies(home, texts=True):
        if now - d < timedelta(hours=SPAN):
            add(kind="发言", title=sent[:40], time=d.isoformat(), body=sent[:120])

    # runbook 没做的每一项是一件事；“上次动”取那一行最后一次改动（git blame），大致看看
    touched, cur = {}, None
    for ln in sh("git", "blame", "--line-porcelain", "design/global/runbook.md").splitlines():
        if ln.startswith("author-time "):
            cur = int(ln.split()[1])
        elif ln.startswith("\t"):
            touched.setdefault(ln[1:], cur)
    sec = None
    for ln in (REPO / "design/global/runbook.md").read_text().splitlines():
        if ln.startswith("## "):
            sec = re.sub(r"（.*", "", ln[3:]).strip()
        m = re.match(r"\s*- \[ \] (\S+) (.*)", ln)
        if m:
            state, nxt, wait = runbook_state(m[2])
            add(kind="事", title=f"{m[1]} {re.split(r'[：（]', m[2], maxsplit=1)[0]}"[:60], thing=sec, state=state, next=nxt, wait=wait,
                body=m[2][:400], size=len(m[2]), time=datetime.fromtimestamp(touched.get(ln) or 0, timezone.utc).isoformat())

    # 机器：serve 在后台量好的那份（measure.py）
    try:
        hosts = json.loads((home / "show" / "hosts.json").read_text())
    except Exception:
        hosts = []
    for h in hosts:
        add(kind="机器", title=h["name"], state="ok" if h["ok"] else "bad", **{k: v for k, v in h.items() if k not in ("name", "ok")})

    # 最近的案例
    for ln in [x for x in (REPO / "design/global/案例.md").read_text().splitlines() if re.match(r"- \d\d-\d\d · ", x)][:6]:
        m = re.match(r"- (\d\d-\d\d) · \[(.*?)\]", ln)
        if m:
            add(kind="案例", title=m[2], body=m[1])
    return items


def _replies(home, texts=False):
    """reply-log 里发出去的：时刻（texts 时连同发的话），按时间顺序。"""
    try:
        lines = (home / "reply-log.jsonl").read_text().splitlines()[-500:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if r.get("sent") and r.get("at"):
            d = t(r["at"]) if isinstance(r["at"], str) else datetime.fromtimestamp(r["at"], timezone.utc)
            out.append((d, "；".join(r["sent"])) if texts else d)
    return out
