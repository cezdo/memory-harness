"""self：“我”自己的工作区（design/rhythm/self/设计.md）。读便签、判到点，到点由节律叫醒 self 里的一段会话，跑完核查、收尾；
给 mh 手记近况列一节。

- ROOT：~/.memory-harness/self（用户 2026-11-08 定）
- notes(root)：便签 [{name, path, at, every, body, bad}]；头几行是醒的条件，`---` 以后是正文
- due(note, now, woke)：到点了没有；woke 是这张便签上次醒的时刻（还没叫醒过是 None）
- wakes(root)：每次醒来的目录 `<日期>-<便签短名>/`，按日期排
- tick(log, now)：整理循环每小时一次：先收上一次醒来，再看有没有该叫醒的便签
- turns(now, db)：最近 RATIO 里 self 和别的工作区各有几轮（只读库）
- section(repo, now, root)：手记近况里的一节（只给 memory-harness）

世界的现状、开始和跑完的快照、核查比出来的变化在 world.py（2026-11-08 从 explore 搬来，self 公开时不依赖冻结着的 explore）。
宿主怎么起、怎么续接、怎么读出自己做了什么，在 hosts/ 里一个宿主一个模块（接入标准：design/rhythm/self/接入.md）。
"""
import json
import os
import re
import signal
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ...engram import store
from ...engram.observe.label import SELF_WORKSPACE
from ...engram.store.instance import USER_NAME
from . import world
from . import hosts

ROOT = store.SELF_ROOT
REPO = Path(__file__).resolve().parents[3]
NOTES = "便签"
TORN = "撕了"
TO_USER = f"给{USER_NAME}.md"                 # 写给用户的（名字取实例配置）
SHOW = timedelta(days=14)          # 醒来留下的东西在手记里列多久（用户一开始会看，帮着找方向；以后只列 TO_USER，设计“回声与读者”）
TIMEOUT = timedelta(hours=4)       # 醒着这么久还没完就停掉
DAILY = 3                          # 24 小时里最多醒几次：自我交流要一直比对外的少得多（设计“回声与读者”）
RETRY = timedelta(days=1)          # 一次醒来没正常结束，这张便签隔这么久再叫
RATIO = timedelta(days=7)          # 手记里比 self 和外界的轮次看多久：和 self.json 的 log 留的一样长，盖得住“每 7 天”的便签，
                                   # 又短到偏了能及时看出来（设计“回声与读者”：偏了再收，先只列数）
_AT = re.compile(r"^到[:：]\s*(\d{4}-\d{2}-\d{2})\s*$")
_EVERY = re.compile(r"^每[:：]\s*(\d+)\s*天\s*$")
_WHO = re.compile(r"^谁[:：]\s*(\S+)\s*$")
_WAKE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+?)(?:-\d+)?$")
_RISKY = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:commit|push|merge|rebase|reset|checkout|switch|stash|worktree)\b"
                    r"|\bsystemctl\b|\bp?kill\b|\brm\s|\bmh\s+(?:check|rhythm|quota)\b|-m\s+mh\s+(?:check|rhythm|quota)\b")


def notes(root=ROOT):
    """便签目录下的每张便签（撕掉的不算）。头里认不出的行记在 bad，不猜（`谁：` 写了没接入的宿主也算认不出）。"""
    d = Path(root) / NOTES
    out = []
    for p in sorted(d.glob("*.md")) if d.is_dir() else []:
        head, sep, body = p.read_text().partition("\n---")
        if not sep:
            head, body = "", p.read_text()
        n = {"name": p.stem, "path": p, "at": None, "every": None, "host": hosts.DEFAULT, "body": body.lstrip("-\n").strip(), "bad": []}
        for line in filter(None, (x.strip() for x in head.splitlines())):
            if m := _AT.match(line):
                n["at"] = date.fromisoformat(m.group(1))
            elif m := _EVERY.match(line):
                n["every"] = int(m.group(1))
            elif (m := _WHO.match(line)) and m.group(1) in hosts.HOSTS:
                n["host"] = m.group(1)
            else:
                n["bad"].append(line)
        out.append(n)
    return out


def due(note, now, woke=None):
    """到点：`到` 的日期已到（醒过以后就不再算，除非又改了日期）；`每` 离上次醒满 N 天（没醒过算到）。两样都写的，任一到了就算。"""
    today = now.date()
    if note["at"] and today >= note["at"] and not (woke and woke.date() >= note["at"]):
        return True
    return bool(note["every"]) and (woke is None or now - woke >= timedelta(days=note["every"]))


# ---- 状态：数据目录的 self.json ----
# {"woke": {便签: 时刻}, "retry": {便签: 时刻}, "threads": {便签: 会话}, "log": [醒的时刻…], "run": {pid, note, dir, logs, session, started}}

def _state_path():
    return store.HOME / "self.json"


def load():
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save(s):
    _state_path().write_text(json.dumps(s, ensure_ascii=False, indent=1))


def state(st=None):
    """每张便签上次醒的时刻：{便签名: datetime}。"""
    st = load() if st is None else st
    return {k: datetime.fromisoformat(v) for k, v in st.get("woke", {}).items()}


def wakes(root=ROOT):
    """每次醒来的目录：[(日期, 便签短名, 路径)]，按日期排。"""
    r = Path(root)
    out = []
    for p in r.iterdir() if r.is_dir() else []:
        m = _WAKE.match(p.name)
        if p.is_dir() and m:
            try:
                out.append((date.fromisoformat(m.group(1)), m.group(2), p))
            except ValueError:
                continue
    return sorted(out)


# ---- 叫醒 ----

def thread(st, name, host):
    """这张便签接着哪段会话：(会话编号, 是不是续接, 上一段)。上一段是同一个宿主的、还接得上就续接；否则另开一段，开头指回它。
    threads 里存 {"host", "session"}（11-08 最早几次存的是 Claude 的会话编号字符串，照旧认）。"""
    prev = st.get("threads", {}).get(name)
    if isinstance(prev, str):
        prev = {"host": "claude", "session": prev}
    if prev and prev["host"] == host.NAME and host.resumable(prev["session"]):
        return prev["session"], True, prev["session"]
    return host.new_session(), False, prev and prev["session"]


def awaken(note, now, d, resumed, prev, last=None, scratch=None):
    """醒来那一轮眼前的东西：便签正文，加程序写的“此刻”。没有别的说明，怎么待在这里写在 self 的 AGENTS.md。"""
    cond = "；".join(filter(None, [f"到：{note['at']}" if note["at"] else "", f"每：{note['every']} 天" if note["every"] else ""]))
    home_ = Path.home()
    show = lambda p: "~/" + str(Path(p).relative_to(home_)) if Path(p).is_relative_to(home_) else str(p)
    lines = [note["body"], "", "---",
             f"此刻：{now:%Y-%m-%d %H:%M}。叫醒我的便签：`{NOTES}/{note['name']}.md`（{cond or '没写条件'}"
             + (f"，上次醒在 {last:%m-%d %H:%M}" if last else "") + "）。",
             f"这次醒来的目录：`{show(d)}`（已经建好）。"]
    if scratch:
        lines.append(f"草稿纸：`{show(scratch)}`（数据目录里，不进 self、不进记忆；克隆、临时文件放这里）。")
    if resumed:
        lines.append("这是接着上次那段会话醒来的。")
    elif prev:
        lines.append(f"上一段会话（{prev}）太长了，这次另开一段；要回看它，用 relive 或读那时醒来的目录。")
    lines += ["", "世界的现状（程序编的）：", world.world(now)]
    return "\n".join(lines)


def start(note, now=None, root=ROOT, st=None):
    """叫醒一次：建这次醒来的目录、记下开始时的快照、交给便签的宿主在后台起来。返回目录。"""
    now = now or datetime.now()
    st = load() if st is None else st
    root = Path(root)
    host = hosts.HOSTS[note["host"]]
    d = root / f"{now:%Y-%m-%d}-{note['name']}"
    n = 2
    while d.exists():
        d, n = root / f"{now:%Y-%m-%d}-{note['name']}-{n}", n + 1
    d.mkdir(parents=True)
    logs = store.HOME / "self-runs" / d.name
    (logs / "草稿").mkdir(parents=True, exist_ok=True)
    session, resumed, prev = thread(st, note["name"], host)
    last = state(st).get(note["name"])
    (logs / "开始时.json").write_text(json.dumps(world.snapshot(), ensure_ascii=False, indent=1))
    (logs / "此刻.md").write_text(awaken(note, now, d, resumed, prev, last, logs / "草稿"))
    p = host.launch(root, logs / "此刻.md", session, resumed, logs)
    st.setdefault("woke", {})[note["name"]] = now.isoformat(timespec="seconds")
    st["log"] = [t for t in st.get("log", []) if now - datetime.fromisoformat(t) < timedelta(days=7)] + [now.isoformat(timespec="seconds")]
    st["run"] = {"pid": p.pid, "note": note["name"], "host": host.NAME, "dir": str(d), "logs": str(logs), "session": session,
                 "resumed": resumed, "started": now.isoformat(timespec="seconds")}
    _save(st)
    return d


def own_actions(acts, root=ROOT, scratch=None):
    """宿主读出的这次会话自己做的事（hosts 的 actions）→ (写了 self 和草稿纸以外的哪些文件, 要看一眼的命令)。
    只看这次会话（含子代理）自己做的，不拿仓库前后的快照比：用户和别的会话同时也在改。"""
    roots = [Path(x).resolve() for x in (root, scratch) if x]
    outside = [f for kind, f in acts if kind == "write" and not any(Path(f).expanduser().resolve().is_relative_to(r) for r in roots)]
    risky = [" ".join(c.split())[:200] for kind, c in acts if kind == "run" and _RISKY.search(c)]
    return list(dict.fromkeys(outside)), list(dict.fromkeys(risky))


def finish(now=None, root=ROOT):
    """上一次醒来结束了（或超时停掉）就收尾：核查写进那次醒来的目录 `核查.md`，self 里没提交的一并提交。返回一句说明。"""
    now = now or datetime.now()
    st = load()
    run = st.get("run")
    if not run:
        return ""
    started = datetime.fromisoformat(run["started"])
    if world.alive(run["pid"]):
        if now - started < TIMEOUT:
            return ""
        try:
            os.killpg(run["pid"], signal.SIGTERM)
        except OSError:
            pass
        timed_out = True
    else:
        timed_out = False
    d, logs = Path(run["dir"]), Path(run["logs"])
    host = hosts.HOSTS.get(run.get("host", hosts.DEFAULT))
    ok, said, session = host.outcome(logs) if host else (False, "", "")
    outside, risky = own_actions(host.actions(logs) if host else [], root, logs / "草稿")
    session = session or run.get("session") or ""
    if session:
        st.setdefault("threads", {})[run["note"]] = {"host": run.get("host", hosts.DEFAULT), "session": session}
    try:
        before = json.loads((logs / "开始时.json").read_text())
        moved = world.audit(before, world.snapshot())
    except (OSError, ValueError):
        moved = []
    lines = [f"# 核查（程序，{now:%m-%d %H:%M}）", "",
             f"- 便签：{run['note']}；{run.get('host', hosts.DEFAULT)} 会话 `{session}`" + ("（续接）" if run.get("resumed") else ""),
             f"- 醒着：{started:%m-%d %H:%M} 起" + ("，超时停掉" if timed_out else "") + ("，正常结束" if ok else "，没有正常结束"),
             f"- 越界：{'**有**' if outside else '没有'}" + ("（写了 self 以外的文件）" if outside else "")]
    lines += [f"  - `{f}`" for f in outside]
    if risky:
        lines.append("- 要看一眼的命令（不一定越界）：")
        lines += [f"  - `{c}`" for c in risky]
    if moved:
        lines.append(f"- 同一段时间 mh 那边的变化（快照比的，不一定是我，{USER_NAME}和别的会话也在改）：")
        lines += ["  - " + m.replace("\n", "\n    ") for m in moved]
    if said:
        lines += ["", "最后一句：", "", said[:2000]]
    d.mkdir(parents=True, exist_ok=True)
    (d / "核查.md").write_text("\n".join(lines) + "\n")
    subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
    subprocess.run(["git", "-c", "user.name=self", "-c", "user.email=self@localhost", "commit", "-q", "-m",
                    f"醒来收尾：{d.name}（程序）"], cwd=root, capture_output=True)
    if not ok:
        st.setdefault("retry", {})[run["note"]] = (now + RETRY).isoformat(timespec="seconds")
        st.get("woke", {}).pop(run["note"], None)
    else:
        st.get("retry", {}).pop(run["note"], None)
    st["last"] = {**run, "finished": now.isoformat(timespec="seconds"), "ok": ok, "outside": len(outside)}
    st.pop("run")
    _save(st)
    return f"收了 {d.name}：" + ("正常结束" if ok else "没有正常结束，隔一天再叫") + ("；越界，看核查.md" if outside else "")


def pick(now, st, root=ROOT, busy=None):
    """该叫醒哪张便签 → (便签或 None, 为什么)。程序先判：有在跑的不叫；24 小时里醒满 DAILY 次不叫；便签的宿主说现在不能（额度）不叫。
    busy：测试替换，给了就不问宿主。
    到点的里面，先叫到点最早的（`到` 按日期，`每` 按上次醒 + 间隔）。"""
    if st.get("run"):
        return None, "有一次醒来还没收"
    woke, retry = state(st), st.get("retry", {})
    ready = [n for n in notes(root) if due(n, now, woke.get(n["name"]))
             and not (n["name"] in retry and now < datetime.fromisoformat(retry[n["name"]]))]
    if not ready:
        return None, ""
    recent = [t for t in st.get("log", []) if now - datetime.fromisoformat(t) < timedelta(days=1)]
    if len(recent) >= DAILY:
        return None, f"24 小时里已经醒了 {len(recent)} 次"
    if busy is not None:
        ready = [] if busy() else ready
        if not ready:
            return None, "额度吃紧"
    else:
        why = {h: hosts.HOSTS[h].ready() for h in {n["host"] for n in ready}}
        ready = [n for n in ready if not why[n["host"]]]
        if not ready:
            return None, "；".join(sorted(set(why.values())))
    since = lambda n: datetime.combine(n["at"], datetime.min.time()) if n["at"] and not woke.get(n["name"]) \
        else (woke[n["name"]] + timedelta(days=n["every"]) if n["every"] and woke.get(n["name"]) else datetime.min)
    return min(ready, key=since), ""


def tick(log=None, now=None):
    """整理循环每小时一次：先收上一次醒来，再看有没有该叫醒的便签。返回一句说明（没事返回空）。"""
    now = now or datetime.now()
    out = [x for x in [finish(now)] if x]
    st = load()
    note, why = pick(now, st)
    if note:
        d = start(note, now, st=st)
        out.append(f"叫醒：{d.name}")
    elif why and log:
        log(f"self：{why}，先不叫")
    return "；".join(out)


def turns(now=None, db=store.DEFAULT_DB):
    """最近 RATIO 里的轮次 → (self 工作区的, 别的工作区的)。只读库；库不在、读不了算 (0, 0)。
    工作区按会话认（store.session_workspace，和 observe 的 self_talk 同一个判法）；开始时刻不知道的轮不算。
    陈述不数：同一轮重新观察过会有新旧几批陈述，要数准得先排掉被取代的，不便宜。"""
    now = now or datetime.now()
    since = now.astimezone(timezone.utc) - RATIO
    try:
        conn = store.connect(db, readonly=True)
    except Exception:
        return 0, 0
    try:
        rows = conn.execute("""SELECT d.source, d.session, s.cwd, COUNT(*) FROM occurrence o
                               JOIN turn_detail d ON d.occurrence = o.id
                               LEFT JOIN session_detail s ON s.source = d.source AND s.session = d.session
                               WHERE o.role = 'turn' AND o.started >= ? AND o.started NOT LIKE '?%'
                               GROUP BY d.source, d.session""", (since.strftime("%Y-%m-%dT%H:%M:%S"),)).fetchall()
    except Exception:
        return 0, 0
    finally:
        conn.close()
    own = sum(n for src, ses, cwd, n in rows if store.session_workspace(src, ses, cwd or "") == SELF_WORKSPACE)
    return own, sum(n for *_, n in rows) - own


def section(repo, now=None, root=ROOT, woke=None, st=None, db=store.DEFAULT_DB):
    """手记近况里的一节（只给 memory-harness 这个仓库）：醒着的、到点的便签、头里有认不出的行的便签、最近醒来留下的东西，
    再加 self 和别的工作区最近的轮次数（self 一轮都没有时不列）。"""
    if Path(repo).resolve() != REPO:
        return []
    now = now or datetime.now()
    st = load() if st is None else st
    woke = state(st) if woke is None else woke
    home_ = Path.home()
    show = lambda p: "~/" + str(Path(p).relative_to(home_)) if Path(p).is_relative_to(home_) else str(p)
    out = []
    if st.get("run"):
        out.append(f"- 醒着：{st['run']['note']}，{datetime.fromisoformat(st['run']['started']):%m-%d %H:%M} 起（`{show(st['run']['dir'])}`）")
    running = (st.get("run") or {}).get("note")
    for n in notes(root):
        if n["name"] != running and due(n, now, woke.get(n["name"])):
            first = n["body"].splitlines()[0] if n["body"] else ""
            out.append(f"- 到点的便签 `{show(n['path'])}`" + (f"：{first[:60]}" if first else ""))
        if n["bad"]:
            out.append(f"- 便签 `{show(n['path'])}` 头里有认不出的条件：" + "；".join(n["bad"][:2]))
    for day, name, p in wakes(root):
        if now.date() - day < SHOW:
            check = (p / "核查.md").read_text() if (p / "核查.md").exists() else ""
            out.append(f"- {day:%m-%d} 醒来（{name}）：`{show(p)}`" + (f"，有写给你的 `{TO_USER}`" if (p / TO_USER).exists() else "")
                       + ("；核查：越界" if "越界：**有**" in check else ""))
    own, others = turns(now, db)
    if own:                            # 只列数，不设线：还没有案例说多少算偏（设计“回声与读者”）
        out.append(f"- 最近 {RATIO.days} 天的轮次：self {own}，别的工作区 {others}")
    return out
