"""开场推送的“项目现状”（design/synapse/设计.md，2026-10-20）：新会话一开头要 ls、git log、看服务、跑检查才摸得清项目，
这些由程序事先编好。手写的部分在 design/synapse/workspace-status/<工作区>.md（仓库、服务、检查命令、目录说明），其余从 git、
systemd 和检查命令读。整理循环（rhythm --tidy）定时调 write 编好、调 run_check 跑检查，都存进数据目录；开场推送只读编好的（read），
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
RECENT_TMP = 4           # tmp 里最近动过的列几个
CUT = 72                 # 一行最多几个字
ERR_HOURS = 6            # 服务看近几个小时的出错
ERR = re.compile(r"：(env_error|timeout|content_error)：|出错：|Traceback")   # act 失败（observe、form 的日志行）、循环出错
CHECK_TIMEOUT = 300      # 检查命令最多跑几秒
# 宿主自己在开场给了的，不再给（2026-10-20）：Claude Code 自带 git 状态和最近 5 次提交
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
    since = (datetime.now() - timedelta(days=ACTIVE_DAYS)).strftime("%Y-%m-%d")
    lines = []
    names = sorted(p.name + ("/" if p.is_dir() else "") for p in repo.iterdir() if not p.name.startswith("."))
    names = [n for n in names if n in notes or n.rstrip("/") in {x.rstrip("/") for x in notes}] + \
            [n for n in names if n not in notes and n.endswith("/")]
    for n in dict.fromkeys(names):
        tags = []
        if subprocess.run(["git", "check-ignore", "-q", n], cwd=repo).returncode == 0:
            tags.append("不进 git")
            if n == "tmp/":
                subs = sorted((p for p in (repo / "tmp").iterdir() if p.is_dir()), key=lambda p: -p.stat().st_mtime)
                if subs:
                    tags.append(f"{len(subs)} 个；最近：" + "、".join(f"{p.name}（{datetime.fromtimestamp(p.stat().st_mtime):%m-%d}）"
                                                               for p in subs[:RECENT_TMP]))
        else:
            k = len(_run(["git", "log", f"--since={since}", "--format=%h", "--", n], repo).splitlines())
            if k:
                tags.append(f"活跃，近 {ACTIVE_DAYS} 天 {k} 次提交")
            else:
                last = _run(["git", "log", "-1", "--format=%cs", "--", n], repo)
                tags.append(f"{last[5:]} 以后没动" if last else "没进过提交")
        note = notes.get(n) or notes.get(n.rstrip("/")) or ""
        lines.append(f"- {n} [{'；'.join(tags)}]" + (f" {note}" if note else ""))
    return lines


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
    out = []
    for s in names:
        st = _run(["systemctl", "--user", "show", s, "-p", "ActiveState", "-p", "ActiveEnterTimestamp", "--value"]).splitlines()
        if not st:
            continue
        state, since = (st + ["", ""])[:2]
        last = _run(["journalctl", "--user", "-u", s, "-n", "1", "-o", "cat", "--no-pager"])
        errs = [x for x in _run(["journalctl", "--user", "-u", s, "--since", f"-{ERR_HOURS}h", "-o", "short-iso",
                                 "--no-pager"]).splitlines() if ERR.search(x)]
        out.append(f"- {s}：{'在跑' if state == 'active' else state}" + (f"（{since.split(' ')[1][5:]} {since.split(' ')[2][:5]} 起）"
                                                                          if state == "active" and len(since.split(" ")) > 2 else "")
                   + (f"；最近：{_cut(last, 50)}" if last else "") + _errors(errs))
    return out


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
    parts = ["### 目录（[活跃] = 近 %d 天有提交）" % ACTIVE_DAYS, *_dirs(repo, cfg["notes"])]
    wts = _worktrees(repo)
    if wts:
        parts.append("- 别处的 worktree：" + "；".join(wts))
    log = _run(["git", "log", f"-{COMMITS}", "--format=%h %cd %s", "--date=format:%m-%d %H:%M"], repo).splitlines()
    if log:
        parts += ["", "### 最近的提交", *[f"- {_cut(x)}" for x in log]]
    dirty = [x[3:] for x in _run(["git", "status", "--porcelain"], repo).splitlines() if x.strip()]
    parts += ["", "### 没提交的改动"] + ([f"- {len(dirty)} 个文件（可能是别的会话做到一半）：" + "、".join(dirty[:DIRTY])
                                         + ("……" if len(dirty) > DIRTY else "")] if dirty else ["- 没有"])
    svc = _services(cfg["services"])
    if svc:
        parts += ["", "### 服务", *svc]
    check = _read_stamped((home or _out()) / f"{ws}.check") if cfg["check"] else None
    if check:
        at, body = check
        parts += ["", f"### 检查（`{cfg['check']}`，{at:%m-%d %H:%M} 跑）", *(x for x in body.splitlines() if x.strip())]
    return "\n".join(parts)


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
    (d / f"{ws}.check").write_text(f"<!-- 编于 {datetime.now(timezone.utc).isoformat(timespec='seconds')} -->\n{text}\n")
    return True


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
    """开场推送用：编好的项目现状，带编好的时刻，去掉这个宿主自己给了的节（HOST_GIVES）；没有就返回 ""。"""
    got = _read_stamped((home or _out()) / f"{ws}.md")
    if not got:
        return ""
    at, body = got
    drop = HOST_GIVES.get(host, ())
    if drop:
        body = "\n\n".join(p for p in re.split(r"\n\n(?=### )", body)
                           if not any(p.startswith(f"### {h}") for h in drop)).strip()
    return f"## 项目现状（{ws}，{at:%m-%d %H:%M} 编）\n\n{body}" if at else f"## 项目现状（{ws}）\n\n{body}"


def workspaces():
    """有手写部分的工作区。"""
    return sorted(p.stem for p in SOURCES.glob("*.md"))
