"""Engram 的原语与存储：Occurrence / Claim / Operator 三原语，放在新库 engram.db 里。设计见 design/engram/设计.md。

- 不用 SQL 的 NULL。字段没有值时写三种标记之一：UNKNOWN（未知）、NOT_DONE（没做）、NA（不适用）；三者都以 "?" 开头，
  程序用 absent() 认，不默认成某个值（公理 IV）。引用别的行的列（operator、parent、predecessor）存编号或这三种标记。
- 不可变：Claim、原文、发生记录、Operator 登记由触发器挡住 UPDATE / DELETE（原文只追加、只取代不覆盖）。
  可以改的只有采集的更正（turn_detail 只从无到有）、会话的元数据和采集缓存。删除（用户要求时）另走 deletion，留一条记录。
- 句柄：E = event.id，J = judgment.id，C = cognition.id。form 的内部数据（逐轮记录、会话记录）在 form_record：有编号，没有对外句柄。
  form 的产物会话入口（session_entry）、话题线（topic_line）和挂线（attachment）是抽象 J（B2，2026-10-18）；对外句柄等 Synapse 换库（第 6 步）再定。
- inputs（看了什么）是 JSON：{"<种类>": [编号…]}，种类是 Occurrence 的角色（turn、act、use……）或 Claim 的种类（event、judgment、
  cognition、form_record）。
"""
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os

HOME = Path(os.environ.get("MH_HOME", Path.home() / ".local/share/memory-harness"))   # 数据目录：只在这里定义
DEFAULT_DB = HOME / "engram.db"

# 环境错误：额度、余额、订阅到期、限流、连接、服务未就绪。和这一次的内容无关，过后会自己恢复，
# 照常记录，但不计入“失败几次就不再重试”的上限（执行状态不等于认识状态）。超时不在此列：长轮次可能每次都超时；
# 两种都沾的（超时的字样加上额度、限流）按环境错误算。
ENVIRONMENTAL = re.compile(r"quota|insufficient|balance|rate.?limit|usage limit|too many requests|\b40[23]\b|\b429\b|\b50[23]\b"
                           r"|overloaded|connection (refused|reset|error|aborted)|ECONN|ENOTFOUND|EAI_AGAIN|network"
                           r"|waiting for service|did not activate|not supported when using Codex with a ChatGPT account|额度|余额|欠费|限流|网络|订阅", re.I)
# 内容过滤：服务商按内容拦下（如 ark 的 finish_reason: content_filter）。重试不会变，这一份就不要了（2026-10-17）
FILTERED = re.compile(r"content_filter|content.?(policy|moderation)|内容过滤|内容安全|敏感内容", re.I)


# 结构标记（2026-10-22，词典“〖〗；『』”）：陈述里程序要认的结构写『引用：…』『文件：…』，名字、说法、原话照旧用「」。
# 之前写的是「引用：…」「文件：…」，库不改，读出来时换成『』（restyle），各个读者看到同一种写法。
OLD_STRUCTURE = re.compile(r"「((?:引用|文件)\s*[:：][^」]*)」")


def restyle(text):
    """旧写法的结构标记「引用：…」「文件：…」换成『』；别的「」不动。"""
    return OLD_STRUCTURE.sub(r"『\1』", text) if text and "「" in text else text


def environmental(error):
    return bool(error) and bool(ENVIRONMENTAL.search(error))


def filtered(error):
    return bool(error) and bool(FILTERED.search(error))


UNKNOWN, NOT_DONE, NA = "?unknown", "?not_done", "?n/a"
MARKERS = (UNKNOWN, NOT_DONE, NA)

ROLES = ("session", "turn", "act", "use", "request", "deletion")
PROCESSES = ("observe", "form", "cognize", "synapse")
LAYERS = ("observe", "form", "cognize")      # 层的上下：E 在下、J 在上、C 更上；申请只写给直接相邻的一层
MAIN = "main"                                # 主会话：用记忆的那个对话。它报的问题落在被纠正的那一层，不受相邻的限制（2026-10-18）
OUTCOMES = ("ok", "env_error", "timeout", "content_error", "filtered")   # 环境错误、超时、内容错误分开计；被内容过滤拦下的不要了
CAUSES = ("new_text", "schedule", "sleep", "request", "manual", "conversation", "process")
RELATIONS = {"相同": "same", "取代": "supersedes", "细化": "refines", "矛盾": "contradicts",
             "拆分自": "split_from", "合并自": "merged_from"}
REPLACING = ("supersedes", "split_from", "merged_from")         # 旧 J 不再是当前的
HANDLES = {"E": "event", "J": "judgment", "C": "cognition"}
CLAIM_KINDS = ("event", "form_record", "judgment", "cognition", "alias")

SCHEMA = """
CREATE TABLE IF NOT EXISTS operator(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  process TEXT NOT NULL,          -- observe | form | cognize | synapse
  role TEXT NOT NULL,             -- collector | observer | former | cognizer | synapse | program（过程里的程序步骤）
  model TEXT NOT NULL,
  prompt TEXT NOT NULL,           -- 提示词版本名
  prompt_hash TEXT NOT NULL,
  isolation TEXT NOT NULL,
  window TEXT NOT NULL,           -- turn | turns（一轮或多轮）| session | free；2026-10-17 上线时写过 segment，旧行照留
  params TEXT NOT NULL,           -- JSON：影响输入的程序参数
  legacy INTEGER NOT NULL,        -- 1 = 旧系统的做法，导入时登记
  registered TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS occurrence(
  id INTEGER PRIMARY KEY,
  role TEXT NOT NULL CHECK(role IN ('session','turn','act','use','request','deletion')),
  started TEXT NOT NULL,
  ended TEXT NOT NULL,
  scale TEXT NOT NULL,            -- 窗口：turn | turns（一轮或多轮）| session | free；旧行里有 segment
  operator INTEGER NOT NULL,      -- 按什么做法（operator.id）
  inputs TEXT NOT NULL,           -- 看了什么：JSON {"<种类>": [编号…]}
  cause TEXT NOT NULL,            -- 因为什么：CAUSES；处理申请的 act 是 request，申请编号写在 inputs 的 request 里
  predecessor INTEGER NOT NULL,   -- 对同一份输入的上一次同类发生
  parent INTEGER NOT NULL,        -- turn → session；use → 所在的 session 或 act；relate → judge
  outcome TEXT NOT NULL,
  error TEXT NOT NULL,
  cost TEXT NOT NULL);            -- JSON {"seconds": …, "tokens": {…}}，取不到的写标记
CREATE INDEX IF NOT EXISTS occurrence_role ON occurrence(role, parent);

CREATE TABLE IF NOT EXISTS session_detail(
  occurrence INTEGER PRIMARY KEY, source TEXT NOT NULL, session TEXT NOT NULL, file TEXT NOT NULL,
  kind TEXT NOT NULL, parent TEXT NOT NULL, cwd TEXT NOT NULL, model TEXT NOT NULL, title TEXT NOT NULL,
  UNIQUE(source, session));
CREATE TABLE IF NOT EXISTS turn_text(
  id TEXT PRIMARY KEY, user TEXT NOT NULL, assistant TEXT NOT NULL,
  commentary TEXT NOT NULL, reasoning TEXT NOT NULL, tools TEXT NOT NULL, collected TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turn_detail(
  occurrence INTEGER PRIMARY KEY, text TEXT NOT NULL, source TEXT NOT NULL, session TEXT NOT NULL,
  ordinal INTEGER NOT NULL, closed INTEGER NOT NULL, ending TEXT NOT NULL, prev INTEGER NOT NULL,
  address TEXT NOT NULL,
  origin TEXT NOT NULL,           -- human | host | script：这一轮开头是谁
  host_kind TEXT NOT NULL,        -- notification | interrupt | command | compaction（origin = host 时）
  UNIQUE(source, session, ordinal));
CREATE INDEX IF NOT EXISTS turn_detail_text ON turn_detail(text);
CREATE TABLE IF NOT EXISTS collect_file(path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime REAL NOT NULL);

CREATE TABLE IF NOT EXISTS act_detail(
  occurrence INTEGER PRIMARY KEY,
  process TEXT NOT NULL,
  step TEXT NOT NULL,             -- observe: preview | deep | rough | withdraw / restore（开关在 observe → form 边界上的记录；旧版本逐轮：split）；form: pack | label | attach | import（B2 起；import 是导入实验结果的程序 act；旧做法 organize | pack | judge | relate）；cognize: …
  window TEXT NOT NULL,           -- JSON：这次看的窗口（哪一轮、哪段会话的哪几轮）
  input TEXT NOT NULL,            -- 实际发给模型的输入
  output TEXT NOT NULL,           -- 原始输出
  meta TEXT NOT NULL);            -- JSON：其余的调用信息
CREATE TABLE IF NOT EXISTS use_detail(
  occurrence INTEGER PRIMARY KEY,
  channel TEXT NOT NULL,          -- push | pull
  mode TEXT NOT NULL,             -- push: assist | shadow；pull: search | get | map | recall
  purpose TEXT NOT NULL,          -- interactive | prefetch | replay | dev | form | cognize
  host TEXT NOT NULL, session TEXT NOT NULL, cwd TEXT NOT NULL,
  view TEXT NOT NULL,             -- 读的是哪一版（operator.name）
  query TEXT NOT NULL,            -- JSON
  shown TEXT NOT NULL,            -- 当时给看到的文本
  note TEXT NOT NULL,
  meta TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS request_detail(
  occurrence INTEGER PRIMARY KEY, from_process TEXT NOT NULL, to_process TEXT NOT NULL,
  reason TEXT NOT NULL);          -- 被质疑的 Claim 在 occurrence.inputs；处理没有：由 inputs 里带这张申请的 act 推出，不另存
CREATE TABLE IF NOT EXISTS deletion_detail(
  occurrence INTEGER PRIMARY KEY, target TEXT NOT NULL, reason TEXT NOT NULL, requested_by TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS event(
  id INTEGER PRIMARY KEY,
  born INTEGER NOT NULL,          -- 出生于哪次 act
  idx INTEGER NOT NULL,
  turn INTEGER NOT NULL,          -- 原文出处：哪一轮（turn 的 occurrence；几轮一起观察时取来源里第一个解析到的轮次）
  text TEXT NOT NULL,
  mode TEXT NOT NULL,             -- 这次表达是什么行为：候选表或自由写的词
  qualifier TEXT NOT NULL,        -- 原话里的限定：暂定、可能……
  speaker TEXT NOT NULL,
  segments TEXT NOT NULL,         -- JSON：出处的原文段落编号（几轮一起观察写“轮:段落”，如 3:A2；旧版本逐轮写 U1、A2）
  unresolved TEXT NOT NULL,       -- JSON：输出里写了、解析不到的编号
  relations TEXT NOT NULL DEFAULT '[]');   -- JSON [{"kind": "supersedes", "to": E 编号}]：点名申请重新观察时，新 E 取代哪些旧 E
                                  -- （C1a 2026-10-21 加；范围申请的取代按轮次推出，不写在这里）
CREATE INDEX IF NOT EXISTS event_born ON event(born, idx);
CREATE TABLE IF NOT EXISTS form_record(
  id INTEGER PRIMARY KEY, born INTEGER NOT NULL, idx INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('turn','session')),    -- 逐轮记录、会话记录
  text TEXT NOT NULL,
  refs TEXT NOT NULL,             -- JSON {"event": […]} 或 {"form_record": […]}
  labels TEXT NOT NULL,           -- JSON：输出里写的编号（F1、R2……）
  structures TEXT NOT NULL,       -- JSON：「引用：概括」→ 指向
  unresolved TEXT NOT NULL,
  relations TEXT NOT NULL DEFAULT '[]');   -- JSON [{"kind": "supersedes", "to": 陈述编号}]：修订提示改写的陈述取代哪条（2026-10-21 加）
CREATE INDEX IF NOT EXISTS form_record_born ON form_record(born, idx);
CREATE TABLE IF NOT EXISTS judgment(
  id INTEGER PRIMARY KEY, born INTEGER NOT NULL, idx INTEGER NOT NULL,
  text TEXT NOT NULL,
  holders TEXT NOT NULL,          -- JSON [{"who": …, "role": …}]
  basis TEXT NOT NULL,            -- JSON {"event": […]} 或 {"form_record": […]}
  basis_labels TEXT NOT NULL,
  relations TEXT NOT NULL,        -- JSON [{"kind": same|supersedes|…, "to": J 编号}]
  pattern TEXT NOT NULL,          -- 格式化：句式
  slots TEXT NOT NULL,            -- 格式化：槽位
  unresolved TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS judgment_born ON judgment(born, idx);
CREATE TABLE IF NOT EXISTS session_entry(     -- 会话入口：一份会话记录的入口（form 的 label 写，B2）
  id INTEGER PRIMARY KEY,
  born INTEGER NOT NULL,          -- label 的 act
  record INTEGER NOT NULL,        -- 会话记录：pack 的 act（陈述是它出生的 form_record）
  source TEXT NOT NULL, session TEXT NOT NULL,
  name TEXT NOT NULL,
  topic TEXT NOT NULL,            -- 主题：pack 写的
  stops TEXT NOT NULL,            -- JSON [{"text": …, "basis": [form_record 编号…]}]：停在
  words TEXT NOT NULL,            -- JSON [[叫法…], …]：模型写的索引词；程序补的词、别名是缓存，读时从 act 重算
  unresolved TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topic_line(        -- 话题线 / 线：为同一个目标做事的几个会话入口（form 的 attach 新起，B2）
  id INTEGER PRIMARY KEY,
  born INTEGER NOT NULL,          -- 新起它的那次 attach 的 act
  name TEXT NOT NULL,
  workspace TEXT NOT NULL,
  node TEXT NOT NULL,             -- 挂在骨架的哪个节点（design/ 下的相对路径，根是 ""）；工作区没有骨架写 ?n/a
  parent INTEGER NOT NULL,        -- 没有骨架时的父线（topic_line 编号）；没有写 ?n/a
  "from" TEXT NOT NULL);          -- JSON [线编号…]：空 = 新起；以后改名、合并时写前身
CREATE TABLE IF NOT EXISTS attachment(         -- 挂线：哪个会话入口挂到哪条线（attach 写）；同一个会话入口以最后一次为准
  id INTEGER PRIMARY KEY,
  born INTEGER NOT NULL,          -- attach 的 act
  entry INTEGER NOT NULL,       -- session_entry 的编号
  line INTEGER NOT NULL,
  why TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cognition(
  id INTEGER PRIMARY KEY, born INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('concrete','abstract')),
  name TEXT NOT NULL,
  statements TEXT NOT NULL,       -- JSON [{"text": …, "refs": [句柄…]}]
  members TEXT NOT NULL,          -- JSON [{"claim": "J12" | "C3", "rel": part | instance}]
  "from" TEXT NOT NULL,           -- JSON [C 编号…]：空 = 新生，一个 = 接替，多个 = 合并
  state TEXT NOT NULL,
  window TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alias(
  id INTEGER PRIMARY KEY, born INTEGER NOT NULL, cognition INTEGER NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS expression(       -- 再次表达：同一个说法又出现一次（“相同”只记次数）
  claim TEXT NOT NULL, occurrence INTEGER NOT NULL, PRIMARY KEY(claim, occurrence));

CREATE TABLE IF NOT EXISTS legacy(key TEXT PRIMARY KEY, id TEXT NOT NULL);   -- 旧库的哪一行导成了哪一行
"""

IMMUTABLE = ("operator", "occurrence", "turn_text", "act_detail", "use_detail", "request_detail", "deletion_detail",
             "event", "form_record", "judgment", "session_entry", "topic_line", "attachment", "cognition", "alias", "expression", "legacy")


def _triggers():
    out = []
    for t in IMMUTABLE:
        for op in ("UPDATE", "DELETE"):
            out.append(f"CREATE TRIGGER IF NOT EXISTS {t}_no_{op.lower()} BEFORE {op} ON {t} "
                       f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;")
    return "\n".join(out)


def connect(db=DEFAULT_DB, readonly=False):
    if readonly:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    else:
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        _migrate(conn)
        conn.executescript(SCHEMA + _triggers())
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn):
    """一次性的迁移（CREATE IF NOT EXISTS 不改已有的表）。C1a（2026-10-19）：request_detail 撤掉 target 列；库里一张申请都没有，
    删表重建。有数据就停下，不悄悄丢掉。C1a（2026-10-21）：event 加 relations 列，旧行是 []（没有被点名重新观察过）。"""
    for table in ("event", "form_record"):        # form_record 的 relations 同日稍后加（修订提示只改写那几条陈述）
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if cols and "relations" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN relations TEXT NOT NULL DEFAULT '[]'")
            conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(request_detail)")]
    if "target" in cols:
        if conn.execute("SELECT COUNT(*) FROM request_detail").fetchone()[0]:
            raise RuntimeError("request_detail 里已经有申请，不能删表重建：先写迁移")
        conn.execute("DROP TABLE request_detail")
        conn.commit()


def absent(value):
    """是不是三种标记之一（未知、没做、不适用）。"""
    return isinstance(value, str) and value in MARKERS


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso(text, minus_seconds=None):
    """旧库的 'YYYY-MM-DD HH:MM:SS'（UTC）或 ISO 文本 → ISO（UTC，Z 结尾）；认不出返回 UNKNOWN。"""
    if not text:
        return UNKNOWN
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return UNKNOWN
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    try:
        t -= timedelta(seconds=float(minus_seconds or 0))
    except (TypeError, ValueError):
        pass
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + (
        f".{t.microsecond // 1000:03d}Z" if t.microsecond else "Z")


# 按目录更正工作区（最长前缀；其余取 cwd 的最后一节）和管理性质的工作区（工作区下面还有工作区，会话可以挂到目录下任何一个工作区的线）：
# 是这台机器的设定，在实例配置里（重构 H）。
from .instance import ENGRAM_DIRS as WORKSPACE_DIRS, MANAGING, SESSIONS as SESSION_WORKSPACE


def workspace(cwd):
    """工作区名：先按 WORKSPACE_DIRS（最长前缀），否则取会话 cwd 的最后一节；worktree（…/.claude/worktrees/<名>）算回它所在的仓库。
    没有 cwd 返回 ""。"""
    c = re.sub(r"/\.claude/worktrees/[^/]+/?$", "", (cwd or "").rstrip("/"))
    for d in sorted(WORKSPACE_DIRS, key=len, reverse=True):
        if c == d or c.startswith(d + "/"):
            return WORKSPACE_DIRS[d]
    return c.split("/")[-1] if c else ""


def canonical(ws):
    """按名字的旧写法（库里存着的线的工作区，如 mobile-ui）换成映射后的名字。"""
    return {d.rsplit("/", 1)[-1]: w for d, w in WORKSPACE_DIRS.items()}.get(ws, ws)


def reachable(conn, ws):
    """这个工作区的会话能挂到哪些工作区的线：本工作区；管理性质的工作区再加上目录下会话认出的全部工作区。"""
    if ws not in MANAGING:
        return {ws}
    d = MANAGING[ws]
    return {ws} | {session_workspace(src, ses, cwd) for src, ses, cwd in conn.execute("SELECT source, session, cwd FROM session_detail")
                   if cwd and (cwd == d or cwd.startswith(d + "/"))}


# 按会话更正工作区（SESSION_WORKSPACE，实例配置）：会话从别的目录启动、实际在另一个仓库里干活的，手工更正。
# 按写进的文件自动判断有反例（在 dsh 里接 Synapse 的会话写的是 dsh 的文件），规则等案例多了再定；见案例日志 10-20。


def session_workspace(source, session, cwd):
    """一段会话的工作区：先查按会话的更正，否则按 cwd（workspace）。"""
    return SESSION_WORKSPACE.get((source, session)) or workspace(cwd)


def handle(kind, id_):
    """Claim 的句柄：event 5 → E5。form_record 没有对外句柄。"""
    letter = {v: k for k, v in HANDLES.items()}.get(kind)
    return f"{letter}{id_}" if letter else f"{kind}:{id_}"


def operator(conn, name, process, role, model=UNKNOWN, prompt=UNKNOWN, prompt_hash=UNKNOWN, isolation=UNKNOWN,
             window=UNKNOWN, params=None, legacy=False):
    """登记一个 Operator（同名的已登记就直接返回编号）。"""
    row = conn.execute("SELECT id FROM operator WHERE name=?", (name,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute(
        "INSERT INTO operator(name, process, role, model, prompt, prompt_hash, isolation, window, params, legacy, registered)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (name, process, role, model, prompt, prompt_hash, isolation, window, dumps(params or {}), int(legacy), now()))
    return cur.lastrowid


def occurrence(conn, role, operator, started=UNKNOWN, ended=UNKNOWN, scale=NA, inputs=None, cause=UNKNOWN,
               predecessor=NA, parent=NA, outcome=NA, error=NA, cost=NA):
    cur = conn.execute(
        "INSERT INTO occurrence(role, started, ended, scale, operator, inputs, cause, predecessor, parent, outcome, error, cost)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (role, started, ended, scale, operator, dumps(inputs or {}), cause, predecessor, parent, outcome, error,
         cost if isinstance(cost, str) else dumps(cost)))
    return cur.lastrowid


def main_operator(conn, model):
    """主会话的 Operator：模型照实记当时主会话的模型（申请、反馈由它直写，cause = manual）。"""
    return operator(conn, f"{MAIN}/{model}", MAIN, "main-session", model=model, prompt=NA, prompt_hash=NA, isolation=NA,
                    window=NA)


def request(conn, from_process, to_process, inputs, reason, operator_id, parent=NA, cause="process", at=None):
    """写一张申请：role = request 的 Occurrence，inputs 写被质疑或者变了的 Claim，parent 指向发现它的那次 act。
    inputs 里有具体 Claim 的是点名申请，只给范围（session、turn）的是范围申请（scope 推出，不存）。
    只能写给直接相邻的一层或同一个过程里更高的一级（adjacent；主会话除外：它报的问题落在被纠正的那一层）。at：到达时刻（补写时沿用原来的），默认现在。
    返回申请的编号。"""
    if to_process not in LAYERS:
        raise ValueError(f"申请只能写给 {LAYERS}：{to_process}")
    if not adjacent(from_process, to_process):
        raise ValueError(f"申请只写给直接相邻的一层：{from_process} → {to_process}")
    if not reason.strip() or not any(inputs.values()):
        raise ValueError("申请要写明被质疑的 Claim 和理由")
    rid = occurrence(conn, "request", operator_id, started=at or now(), ended=now(), inputs=inputs, cause=cause, parent=parent)
    conn.execute("INSERT INTO request_detail VALUES(?,?,?,?)", (rid, from_process, to_process, reason))
    return rid


def adjacent(from_process, to_process):
    """申请写得到吗：主会话写给哪一层都行；不同过程之间只写给直接相邻的一层；同一个过程里写给更高的一级也行（2026-10-21：
    认为一次决策会动摇更高一级，就另写那一级的申请，陈述、话题、线都是 form 的）。"""
    if to_process not in LAYERS:
        return False
    return from_process == MAIN or from_process == to_process or (
        from_process in LAYERS and abs(LAYERS.index(from_process) - LAYERS.index(to_process)) == 1)


def scope(inputs):
    """申请是哪一种：inputs 里有具体 Claim 的是点名申请（named），只给会话、轮次的是范围申请（range）。"""
    return "named" if any(k in CLAIM_KINDS and v for k, v in inputs.items()) else "range"


def claimed(conn, request_id):
    """处理过这张申请的 act：[(act, outcome)]，按先后。处理没有另存，从 inputs 里带这张申请的 act 推出。
    补写的申请（2026-10-21：再观察原来是一条标记，parent 指向它）也认 inputs 的 act 里带着那条标记的。第一批（标记 23344）
    是补记的：预览早于标记、没带它（上线时写在代码常量 REOBSERVE 里），那时旧版本观察过的轮次只有这一批会被预览，所以
    没有预览带着标记的，认标记之前这几段会话的预览。"""
    out = [(r[0], r[1]) for r in conn.execute(
        "SELECT o.id, o.outcome FROM occurrence o, json_each(o.inputs, '$.request') j WHERE o.role='act' AND j.value=? "
        "ORDER BY o.id", (request_id,))]
    mark = conn.execute("SELECT o.parent, o.inputs FROM occurrence o JOIN act_detail a ON a.occurrence=o.parent "
                        "WHERE o.id=? AND a.step='reobserve'", (request_id,)).fetchone()
    if not mark:
        return out
    got = [(r[0], r[1]) for r in conn.execute("SELECT o.id, o.outcome FROM occurrence o, json_each(o.inputs, '$.act') j "
                                              "WHERE o.role='act' AND j.value=? ORDER BY o.id", (mark[0],))]
    if not got:
        ids = json.loads(mark[1])["session"]
        turns = {r[0] for r in conn.execute(f"SELECT t.id FROM occurrence t WHERE t.role='turn' AND t.parent IN "
                                            f"({','.join('?' * len(ids))})", ids)}
        got = [(oid, res) for oid, res, inputs in conn.execute(
            "SELECT o.id, o.outcome, o.inputs FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
            "WHERE a.process='observe' AND a.step='preview' AND o.id<? ORDER BY o.id", (mark[0],))
            if turns & set(json.loads(inputs).get("turn", []))]
    return sorted(set(out + got))


def requests(conn, to_process, state="open"):
    """给这个过程的申请：[{id, from, to, reason, inputs, parent, acts}]。state：open（还没有成功的 act 认领）、handled（有）、
    failed（只有失败的）、all。成功包括被内容过滤拦下的（不要了，算处理过）。一张申请分几次做时，还剩什么由接收的那一层自己判断
    （observe 的纠正：correction_targets；form 认领时从 pack 起整段重做，act 的 inputs 不带被点名的记录）。"""
    out = []
    for r in conn.execute("SELECT o.id, o.inputs, o.parent, d.from_process, d.to_process, d.reason FROM request_detail d "
                          "JOIN occurrence o ON o.id=d.occurrence WHERE d.to_process=? ORDER BY o.id", (to_process,)):
        acts = claimed(conn, r["id"])
        inputs = json.loads(r["inputs"])
        ok = any(x in ("ok", "filtered") for _, x in acts)
        st = "handled" if ok else ("failed" if acts else "open")
        if state in ("all", st) or (state == "open" and st == "failed"):
            out.append({"id": r["id"], "from": r["from_process"], "to": r["to_process"], "reason": r["reason"],
                        "inputs": inputs, "parent": r["parent"], "acts": acts, "state": st})
    return out


_TIMEOUT = re.compile(r"timeout|timed out|超时", re.I)


def consumed(conn, process, kind="event"):
    """某个过程成功的 act 读过哪些东西：它们 inputs 里的 kind 编号（form 读过哪些 E：observe 的开关、check 的覆盖都要问）。
    原来是 observe 的 formed，读的是 form 的 act、依赖方向反了，2026-10-20 移到这里（用户）。"""
    out = set()
    for (inputs,) in conn.execute("SELECT o.inputs FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
                                  "WHERE a.process=? AND o.outcome='ok' AND o.inputs LIKE ?", (process, f'%"{kind}"%')):
        out.update(json.loads(inputs).get(kind, []))
    return out


def outcome(error):
    """一次调用的结果：(outcome, error)。超时、环境错误（额度、限流、连接……）、内容错误分开，环境错误不算失败次数。
    服务商按内容拦下（filtered）的不重试、不进待重做清单，这一份就不要了（2026-10-17）。"""
    if error is None:
        return "ok", NA
    if filtered(error):
        return "filtered", error
    if environmental(error):   # 先于超时：codex 额度用完时 stderr 先报“刷新模型列表 timed out”（2026-10-21 af3a#7 因此满次数）
        return "env_error", error
    if _TIMEOUT.search(error):
        return "timeout", error
    return "content_error", error


def cost(seconds, usage):
    """调用的 seconds、usage（dsh 的 inputTokens 那种列表）→ {"seconds", "tokens"}；没记 token 的写 UNKNOWN。
    cache_write 只在来源给了的时候有（claude -p 的缓存写入，2026-10-19 起记；之前的 claude 调用输入只记了没进缓存的那一点）。"""
    c = {"seconds": float(seconds) if seconds not in (None, "") else UNKNOWN}
    if isinstance(usage, list) and usage:
        tok = {"input": 0, "output": 0, "cache_read": 0}
        for u in usage:
            for k, name in (("inputTokens", "input"), ("outputTokens", "output"), ("cacheReadTokens", "cache_read"),
                            ("cacheWriteTokens", "cache_write")):
                if k in u or name != "cache_write":
                    tok[name] = tok.get(name, 0) + int(u.get(k) or 0)
        c["tokens"] = tok
    else:
        c["tokens"] = UNKNOWN
    return c
