"""runbook 跟上（design/synapse/设计.md 项目现状“runbook”，用户 2026-10-29）：runbook 靠交互会话做完一项顺手打勾、移走经过，
常常落后。两半：程序认出哪里落后了（编近况时算，进“runbook”一节，只推异常）；模型每天写一份更新稿（改哪几处、依据哪个提交），
放进数据目录 status/<工作区>.runbook.md，不改仓库：用户在场的会话里由“我”看过再改、提交（用户 2026-10-29 选的）。
runbook 一改，稿就算处理过了，不再提示。

工作区要用，在项目现状的手写部分写一行“- runbook：<仓库内路径>”。"""
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from ...engram.store.instance import USER_ALIASES, USER_NAME

LONG = 1500             # 一项超过几个字算经过写长了（起步值：10-29 清理时移出的几项是 3000–8000 字，留着的多在 1000 以内）
WAIT_DAYS = 3           # “等用户……”挂了几天没动算久
LOG_CUT = 20000         # 给模型的提交说明最多几个字
PROMPT = Path(__file__).parent / "prompts" / "runbook-v0.2.md"
ITEM = re.compile(r"^(\s*)- \[([ x])\] (?:~~)?([A-Z]\d+[a-z]?′?)?")
ID = re.compile(r"(?<![A-Za-z0-9])([A-Z]\d+[a-z]?′?)(?![A-Za-z0-9])")
LINK = re.compile(r"\]\(([^)\s#]*)(?:#([^)\s]+))?\)")
REF = re.compile(r"runbook ?([A-Z]\d+[a-z]?′?)(?![A-Za-z0-9])")
HISTORY = ("cases", "已做.md")   # 只追加的历史：链接和编号停在写下时，不核
SHOW = 5                        # 对不上的链接最多列几处


def _run(args, cwd):
    from .status import _run as run
    return run(args, cwd)


def items(text):
    """runbook 的条目 → [(第几行, 勾了没有, 编号或 None, 这一项的正文)]。正文连着下面缩进更深的子项一起算
    （10-29 清理时 I7、F4 的经过分散在十来条子项里，单看一行都不长）。"""
    lines, out = text.splitlines(), []
    for i, line in enumerate(lines):
        m = ITEM.match(line)
        if not m:
            continue
        depth, body = len(m.group(1)), [line]
        for nxt in lines[i + 1:]:
            if not nxt.strip() or len(nxt) - len(nxt.lstrip()) <= depth:
                break
            body.append(nxt)
        out.append((i + 1, m.group(2) == "x", m.group(3), "\n".join(body)))
    return out


def _blob(repo, rel):
    try:
        return hashlib.sha1((repo / rel).read_bytes()).hexdigest()
    except OSError:
        return None


def _ages(repo, rel):
    """每一行最后一次改动的时刻（git blame）→ {行号: datetime}；没提交过的行不在里面。"""
    got, line = {}, None
    for x in _run(["git", "blame", "--line-porcelain", "--", rel], repo).splitlines():
        m = re.match(r"[0-9a-f]{40} \d+ (\d+)", x)
        if m:
            line = int(m.group(1))
        elif x.startswith("author-time ") and line:
            got[line] = datetime.fromtimestamp(int(x.split()[1]), timezone.utc)
    return got


def since(repo, rel):
    """runbook 上次提交之后的提交 → (上次提交的时刻, [“短哈希 说明”])；runbook 没提交过返回 (None, [])。"""
    last = _run(["git", "log", "-1", "--format=%H %ct", "--", rel], repo).split()
    if not last:
        return None, []
    at = datetime.fromtimestamp(int(last[1]), timezone.utc)
    return at, _run(["git", "log", f"{last[0]}..HEAD", "--format=%h %s"], repo).splitlines()


def behind(repo, rel, now=None):
    """程序认出的落后之处 → 几行（“- ”开头）；都没有返回 []。判据都来自 10-29 那次清理碰到的：打了勾还留着、
    提交提到了某项而那一项没跟着改、经过写长了、等用户看的挂了好几天（其实已经看过）。"""
    try:
        text = (repo / rel).read_text()
    except OSError:
        return []
    now = now or datetime.now(timezone.utc)
    its, out = items(text), []
    done = [f"{n or f'第 {i} 行'}" for i, x, n, _ in its if x]
    if done:
        out.append(f"- 打了勾还留着：{'、'.join(done)}（做完的连同经过移进已做）")
    at, commits = since(repo, rel)
    if commits:
        open_ids = {n for _, x, n, _ in its if n and not x}
        hit = sorted({m for c in commits for m in ID.findall(c.split(" ", 1)[-1])} & open_ids)
        if hit:          # 只看提交数是噪音：修 bug 的提交本来不碰 runbook（10-29 上线当晚）；提交照样都给写稿的模型
            out.append(f"- runbook 上次改在 {at.astimezone():%m-%d %H:%M}，之后 {len(commits)} 次提交提到了 {'、'.join(hit)}")
    long = [f"{n or f'第 {i} 行'}（{len(l)} 字）" for i, x, n, l in its if not x and len(l) > LONG]
    if long:
        out.append(f"- 经过写长了：{'、'.join(long)}（经过移进已做，留现状和剩下的）")
    wait = re.compile("等(?:" + "|".join(map(re.escape, USER_ALIASES)) + ")")
    ages = _ages(repo, rel) if wait.search(text) else {}
    old = [f"{n or f'第 {i} 行'}（{(now - ages[i]).days} 天）" for i, x, n, l in its
           if not x and wait.search(l.splitlines()[0]) and i in ages and (now - ages[i]).days >= WAIT_DAYS]
    if old:
        out.append(f"- 等{USER_NAME}的挂了 {WAIT_DAYS} 天以上：{'、'.join(old)}（问一句还等不等）")
    return out + drift(repo, rel, {n for _, x, n, _ in its if n and not x})


def _anchors(text):
    """一份 md 能跳到的锚点：标题（GitHub 的写法：小写、去标点、空格换连字符，重名加 -1、-2）和 <a id>。"""
    got, seen = set(re.findall(r'<a id="([^"]+)"', text)), {}
    for line in text.splitlines():
        m = re.match(r"#{1,6} (.+)", line)
        if m:
            s = re.sub(r"[^\w\- ]", "", m.group(1).strip().lower()).replace(" ", "-")
            got.add(f"{s}-{seen[s]}" if s in seen else s)
            seen[s] = seen.get(s, 0) + 1
    return got


def drift(repo, rel, open_ids):
    """runbook 和设计对不上的地方 → 几行（用户 2026-10-30）：链接指向的文件或标题不在了；设计里写“runbook X”而 X 已经
    不在 runbook 里（做完移走了）。只核 runbook 所在的文档目录（`design/`），只追加的历史（案例正文、已做）不核。
    锚点只要是某个标题的开头就算对得上：标题后面的括号（日期、“还没实现”）常改，链接写前一段就够读者找到
    （10-29 归纳一节去掉“还没实现”，两处链接断了；10-24 起近况一节改名，“#项目现状”断了三处）。"""
    top = repo / Path(rel).parts[0] if len(Path(rel).parts) > 1 else repo
    files = [f for f in sorted(top.rglob("*.md"))
             if ".git" not in f.parts and not any(h in f.relative_to(top).parts for h in HISTORY)]
    cache, broken, refs = {}, [], {}
    for f in files:
        text = f.read_text()
        for path, anchor in LINK.findall(text):
            if path.startswith(("http:", "https:", "mailto:")):
                continue
            t = (f.parent / path).resolve() if path else f
            if not t.exists():
                broken.append(f"{f.relative_to(top)} → {path}")
            elif anchor and t.is_file() and t.suffix == ".md":
                if t not in cache:
                    cache[t] = _anchors(t.read_text())
                if not any(a.startswith(anchor.lower()) for a in cache[t]):
                    broken.append(f"{f.relative_to(top)} → {path}#{anchor}")
        if f != repo / rel:
            gone = sorted(set(REF.findall(text)) - open_ids)
            if gone:
                refs[str(f.relative_to(top))] = gone
    out = []
    if broken:
        more = f"，共 {len(broken)} 处" if len(broken) > SHOW else ""
        out.append(f"- 链接对不上：{'；'.join(broken[:SHOW])}{more}（标题改了的，链接写标题的前一段）")
    if refs:
        where = "；".join(k + " 的 " + "、".join(v) for k, v in refs.items())
        out.append(f"- 设计指向 runbook 里已经没有的项：{where}（做完的改指已做或案例）")
    return out


def _draft_path(ws, home):
    from .status import _out
    return (home or _out()) / f"{ws}.runbook.md"


def pending(ws, repo, rel, home=None):
    """有没有还没处理的更新稿：稿写下时的 runbook 和现在一样才算没处理 → 稿的路径或 None。"""
    f = _draft_path(ws, home)
    try:
        head = f.read_text().splitlines()[0]
    except (OSError, IndexError):
        return None
    m = re.search(r"runbook=(\w+)", head)
    return f if m and m.group(1) == _blob(repo, rel) else None


def section(ws, repo, rel, home=None, now=None):
    """近况里的“runbook”一节 → 几行；没有落后、也没有待处理的稿就 []。"""
    lines = behind(repo, rel, now)
    f = pending(ws, repo, rel, home)
    if f:
        body = f.read_text().splitlines()
        n = sum(1 for x in body if x.startswith("- "))
        if n:                                        # 稿写的是“无”就不提示
            lines.append(f"- 有一份没处理的更新稿（{n} 处）：`{f}`，看过再改 runbook")
    return lines


def window(ws, repo, rel, home=None):
    """写稿看哪些提交 → [“短哈希 说明”]，最新的在前：上一份稿写下时的 HEAD 之后的；还没写过稿（或那次 HEAD 已经不在库里）
    才退回 runbook 上次提交之后的。原来只看后者，runbook 一天改十几次时窗口总被清空：11-05 最后一次改在 22:39，
    每天的任务 22:40 醒来看到没有提交就跳过，v0.40 上线（F5 做完）那次提交写稿的模型一直没看到（方向探索第 0 轮，2026-11-06）。"""
    try:
        m = re.search(r"head=(\w+)", _draft_path(ws, home).read_text().splitlines()[0])
    except (OSError, IndexError):
        m = None
    if m and _run(["git", "rev-parse", "--verify", "-q", m.group(1) + "^{commit}"], repo):
        return _run(["git", "log", f"{m.group(1)}..HEAD", "--format=%h %s"], repo).splitlines()
    return since(repo, rel)[1]


def material(ws, repo, rel, home=None):
    """给模型的材料：runbook 全文、上一份稿以来的提交（说明和改了哪些文件，见 window）、程序认出的落后之处。"""
    commits = window(ws, repo, rel, home)
    log = ""
    if commits:
        first = commits[-1].split()[0]
        log = _run(["git", "log", f"{first}~1..HEAD", "--format=### %h %cd%n%B", "--date=format-local:%m-%d %H:%M", "--stat=100"], repo)
        if len(log) > LOG_CUT:
            log = log[:LOG_CUT] + "\n……（后面截掉了）"
    return {"runbook": (repo / rel).read_text(), "log": log or "（上一份稿之后没有新提交）",
            "behind": "\n".join(behind(repo, rel)) or "（没有）"}


def draft(ws, repo, rel, model, home=None):
    """写一份更新稿进数据目录。runbook 自上次写稿以来没变、也没有新提交，就不写（不花额度）。返回写了没有。"""
    from ...engram import invoke
    from ... import express
    f = _draft_path(ws, home)
    blob, head = _blob(repo, rel), _run(["git", "rev-parse", "HEAD"], repo)
    try:
        if f"runbook={blob} head={head}" in f.read_text().splitlines()[0]:
            return False
    except (OSError, IndexError):
        pass
    m = material(ws, repo, rel, home)
    if not window(ws, repo, rel, home) and m["behind"] == "（没有）":
        return False
    text = express.prompt(PROMPT.read_text(), USER_NAME)
    for k, v in m.items():
        text = text.replace("{{" + k + "}}", v)
    out, meta = invoke.call(model, text)
    now = datetime.now(timezone.utc)
    body = f"<!-- 编于 {now.isoformat(timespec='seconds')} runbook={blob} head={head} model={meta.get('model')} -->\n{out.strip()}\n"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    kept = f.parent / "runbook-drafts" / f"{ws}-{now:%Y%m%dT%H%M}.md"     # 每份留底：G8 要攒几次稿再看，覆盖就攒不起来（2026-11-07）
    kept.parent.mkdir(exist_ok=True)
    kept.write_text(body)
    return True
