"""手记推送的“项目现状”（design/synapse/设计.md，2026-10-20）：新会话一开头要 ls、git log、看服务、跑检查才摸得清项目，
这些由程序事先编好。手写的部分在 design/synapse/workspace-status/<工作区>.md（仓库、服务、检查命令、目录说明），其余从 git、
systemd 和检查命令读。整理循环（rhythm --tidy）定时调 write 编好、调 run_check 跑检查，都存进数据目录；手记推送只读编好的（read），
按宿主去掉它自己给了的节，读不到就不给这一节。纯程序。"""
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ...engram.store.instance import resource

SOURCES = resource("workspace_status", Path(__file__).resolve().parents[3] / "design/synapse/workspace-status", "workspace-status")
ACTIVE_DAYS = 3          # 近几天有提交算活跃
COMMITS = 6              # 最近的提交列几条
DIRTY = 6                # 没提交的改动列几个文件
CUT = 72                 # 一行最多几个字
ERR_HOURS = 6            # 服务看近几个小时的出错
ERR = re.compile(r"：(env_error|timeout|content_error)：|出错：|Traceback")   # act 失败（observe、form 的日志行）、循环出错
CHECK_TIMEOUT = 300      # 检查命令最多跑几秒
# 宿主自己在会话接入时给了的，不再给（2026-10-20）：Claude Code 自带 git 状态和最近 5 次提交
HOST_GIVES = {"claude": ("最近的提交", "没提交的改动")}


def _out():
    from ...engram import store
    return store.HOME / "status"


def _run(args, cwd=None):
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _cut(s, n=CUT):
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


def source(ws):
    """手写的部分 → {"repo", "services", "check", "notes": {目录: 说明}}；这个工作区没写就返回 None。"""
    f = SOURCES / f"{ws}.md"
    if not f.exists():
        return None
    text = f.read_text()
    cfg = {"repo": None, "services": [], "check": None, "notes": {}}
    for line in text.splitlines():
        m = re.match(r"- (仓库|服务|检查)：(.+)", line)
        if m and "## 目录" not in text.split(line)[0]:
            k, v = m.groups()
            if k == "仓库":
                cfg["repo"] = Path(v.strip()).expanduser()
            elif k == "检查":
                cfg["check"] = v.strip()
            else:
                cfg["services"] = [x.strip() for x in re.split(r"[、,，]", v) if x.strip()]
    body = text.split("## 目录", 1)[1] if "## 目录" in text else ""
    for m in re.finditer(r"^- (\S+?)：(.+)$", body, re.M):
        cfg["notes"][m.group(1)] = m.group(2).strip()
    return cfg


def _dirs(repo, notes):
    """目录压成一行（2026-10-24：说明和 AGENTS.md 重复，只留活跃度）：近几天有提交的、不进 git 的、很久没动的。"""
    since = (datetime.now() - timedelta(days=ACTIVE_DAYS)).strftime("%Y-%m-%d")
    names = sorted(p.name + ("/" if p.is_dir() else "") for p in repo.iterdir() if not p.name.startswith("."))
    names = [n for n in names if n in notes or n.rstrip("/") in {x.rstrip("/") for x in notes}] + \
            [n for n in names if n not in notes and n.endswith("/")]
    active, ignored, still = [], [], []
    for n in dict.fromkeys(names):
        if subprocess.run(["git", "check-ignore", "-q", n], cwd=repo).returncode == 0:
            ignored.append(n)
            continue
        k = len(_run(["git", "log", f"--since={since}", "--format=%h", "--", n], repo).splitlines())
        if k:
            active.append(f"{n}（{k}）")
        else:
            last = _run(["git", "log", "-1", "--format=%cs", "--", n], repo)
            still.append(f"{n}（{last[5:]} 起）" if last else n)
    bits = [f"近 {ACTIVE_DAYS} 天有提交：" + "、".join(active)] if active else []
    bits += ["不进 git：" + "、".join(ignored)] if ignored else []
    bits += ["没动：" + "、".join(still)] if still else []
    return ["- " + "；".join(bits)] if bits else []


def _worktrees(repo):
    out, cur = [], {}
    for line in _run(["git", "worktree", "list", "--porcelain"], repo).splitlines() + [""]:
        if not line:
            if cur.get("worktree") and Path(cur["worktree"]) != repo:
                br = cur.get("branch", "").removeprefix("refs/heads/") or "（游离）"
                if br != "（游离）" and subprocess.run(["git", "merge-base", "--is-ancestor", br, "HEAD"], cwd=repo).returncode == 0:
                    state = "已合进当前分支，可以删"
                else:
                    ahead = _run(["git", "rev-list", "--count", f"HEAD..{br}"], repo) if br != "（游离）" else "?"
                    state = f"没合，领先 {ahead} 个提交"
                out.append(f"{cur['worktree'].replace(str(Path.home()), '~')}（{br}，{state}）")
            cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v
    return out


def _services(names):
    """都在跑、近几个小时没出错就压成一行；有异常的才单列（2026-10-24：只推异常）。"""
    ok, bad = [], []
    for s in names:
        st = _run(["systemctl", "--user", "show", s, "-p", "ActiveState", "--value"]).strip()
        if not st:
            continue
        errs = [x for x in _run(["journalctl", "--user", "-u", s, "--since", f"-{ERR_HOURS}h", "-o", "short-iso",
                                 "--no-pager"]).splitlines() if ERR.search(x)]
        short = s.removeprefix("memory-harness-")
        if st == "active" and not errs:
            ok.append(short)
        else:
            bad.append(f"- {s}：{'在跑' if st == 'active' else st}" + (_errors(errs) if errs else ""))
    head = [f"- {'、'.join(ok)} {'都' if len(ok) > 1 else ''}在跑，近 {ERR_HOURS} 小时没有出错"] if ok else []
    return head + bad


def _errors(lines):
    """journalctl -o short-iso 里挑出的出错行 → “；近 6 小时出错 N 行，最后一次 HH:MM：……”；没有就说没有。"""
    if not lines:
        return f"；近 {ERR_HOURS} 小时没有出错"
    at, _, msg = lines[-1].partition(" ")
    msg = msg.split(": ", 1)[1] if ": " in msg else msg                 # 去掉“主机 进程[pid]: ”
    return f"；近 {ERR_HOURS} 小时出错 {len(lines)} 行，最后一次 {at[11:16]}：{_cut(msg, 60)}"


def compile(ws, home=None):
    """编一个工作区的项目现状 → 文本；这个工作区没有手写的部分、或者仓库不在了，返回 None。"""
    cfg = source(ws)
    if not cfg or not cfg["repo"] or not (cfg["repo"] / ".git").exists():
        return None
    repo = cfg["repo"]
    parts = ["### 目录", *_dirs(repo, cfg["notes"])]
    wts = _worktrees(repo)
    if wts:
        parts.append("- 别处的 worktree：" + "；".join(wts))
    log = _run(["git", "log", f"-{COMMITS}", "--format=%h %cd %s", "--date=format:%m-%d %H:%M"], repo).splitlines()
    if log:
        parts += ["", "### 最近的提交", *[f"- {_cut(x)}" for x in log]]
    dirty = [x[3:] for x in _run(["git", "-c", "core.quotepath=false", "status", "--porcelain"], repo).splitlines() if x.strip()]
    parts += ["", "### 没提交的改动"] + ([f"- {len(dirty)} 个文件（可能是别的会话做到一半）：" + "、".join(dirty[:DIRTY])
                                         + ("……" if len(dirty) > DIRTY else "")] if dirty else ["- 没有"])
    svc = _services(cfg["services"])
    if svc:
        parts += ["", "### 服务", *svc]
    check = _read_stamped((home or _out()) / f"{ws}.check") if cfg["check"] else None
    if check:
        at, body = check
        prev = _read_stamped((home or _out()) / f"{ws}.check.prev")
        parts += ["", f"### 检查（`{cfg['check']}`，{at:%m-%d %H:%M} 跑）", *changed(body, prev)]
    near = _recent(ws)                 # 放最后：送手记时 recent.drop_self 只切这一节
    if near:
        parts += ["", near]
    return "\n".join(parts)


def _recent(ws):
    """最近停下的几件事（recent.py）；读库出任何错都不给这一节，不影响项目现状。"""
    try:
        from .. import library
        from . import recent
        conn = library()
        try:
            return recent.compile(conn, ws)
        finally:
            conn.close()
    except Exception:
        return None


def run_check(ws, home=None):
    """跑手写部分里写的检查命令，结果存进数据目录 status/<工作区>.check（第一行是跑的时刻）；没写命令返回 False。"""
    cfg = source(ws)
    if not cfg or not cfg["check"] or not cfg["repo"]:
        return False
    try:
        r = subprocess.run(cfg["check"], shell=True, cwd=cfg["repo"], capture_output=True, text=True, timeout=CHECK_TIMEOUT)
        text = r.stdout.strip() or f"（没有输出，退出码 {r.returncode}：{_cut(r.stderr, 120)}）"
    except subprocess.TimeoutExpired:
        text = f"（超过 {CHECK_TIMEOUT} 秒没跑完）"
    d = home or _out()
    d.mkdir(parents=True, exist_ok=True)
    if (d / f"{ws}.check").exists():                    # 上一次的留一份，编现状时只推变了的
        (d / f"{ws}.check").replace(d / f"{ws}.check.prev")
    (d / f"{ws}.check").write_text(f"<!-- 编于 {datetime.now(timezone.utc).isoformat(timespec='seconds')} -->\n{text}\n")
    return True


VOLATILE = ("observe：待观测",)       # 每次都在变、不算异常的项


def _key(line):
    """一行检查结果的键：数字前面那部分（“- 记下的引用未命中（event，条数）”）。"""
    return re.split(r"：\s*\d", line, 1)[0]


def changed(body, prev):
    """检查结果只推和上一次相比变了的项（2026-10-24）：新出现的、数字变了的；第一行的总数和 VOLATILE 不算。
    没有上一次就全推；都没变就一行。"""
    lines = [x for x in body.splitlines() if x.strip().startswith("- ")]
    if not prev:
        return [x for x in body.splitlines() if x.strip()]
    at, old = prev
    before = {_key(x): x for x in old.splitlines() if x.strip().startswith("- ")}
    was = lambda k: before[k][len(k):].lstrip("：").strip()[:30]
    diff = [x + (f"（上次：{was(_key(x))}）" if _key(x) in before else "（新出现）")
            for x in lines if before.get(_key(x)) != x and not any(v in x for v in VOLATILE)]
    return diff or [f"- 和上一次（{at:%m-%d %H:%M}）比没有变化"]


def _read_stamped(f):
    """数据目录里编好的文件 → (编好的时刻, 正文)；没有就 None。"""
    try:
        lines = f.read_text().splitlines()
    except OSError:
        return None
    m = re.match(r"<!-- 编于 (\S+) -->", lines[0]) if lines else None
    at = datetime.fromisoformat(m.group(1)).astimezone() if m else None
    return at, "\n".join(lines[1:] if m else lines).strip()


def write(ws, home=None):
    """编好存进数据目录 status/<工作区>.md（第一行是编好的时刻）。返回写了没有。"""
    text = compile(ws, home)
    if text is None:
        return False
    d = home or _out()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ws}.md").write_text(f"<!-- 编于 {datetime.now(timezone.utc).isoformat(timespec='seconds')} -->\n{text}\n")
    return True


def read(ws, home=None, host=None):
    """手记推送用：编好的项目现状，带编好的时刻，去掉这个宿主自己给了的节（HOST_GIVES）；没有就返回 ""。"""
    got = _read_stamped((home or _out()) / f"{ws}.md")
    if not got:
        return ""
    at, body = got
    drop = HOST_GIVES.get(host, ())
    if drop:
        body = "\n\n".join(p for p in re.split(r"\n\n(?=### )", body)
                           if not any(p.startswith(f"### {h}") for h in drop)).strip()
    return f"## 近况（{ws}，{at:%m-%d %H:%M} 编）\n\n{body}" if at else f"## 近况（{ws}）\n\n{body}"


def workspaces():
    """有手写部分的工作区。"""
    return sorted(p.stem for p in SOURCES.glob("*.md"))
