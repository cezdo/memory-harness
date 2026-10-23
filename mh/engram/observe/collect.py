"""采集：observe 的接口。把本机各宿主的会话归一化为轮次，写进新库的 session、turn（design/engram/observe/采集.md）。
纯程序，不调用模型。

各宿主的解析器在 sources/（claude、codex、dsh，加上钩子写的开关记录 switch），这里写进库：
- 每一轮在采集时就定好来源：开头是用户的话（human）、宿主注入（host：通知、打断、命令、压缩）还是脚本发起（script）；
- 结束标记、分叉点、非正常结束记在 turn_detail；之后的更正只从无到有，位置对上、内容不同的不写（原文只追加）；
- 新会话开始不满 FRESH 不收，也不标记文件，下一轮再看（测试、误开的会话在窗口内删掉就不会进来）。
"""
import sys
from datetime import datetime, timezone

from . import label
from .sources import FRESH, SOURCES, switch, young
from .. import store as st
from ..store import NA, UNKNOWN, iso, occurrence, operator
from ..store.delete import deleted

SCRIPT_PREFIX = "（定时任务"          # 脚本、定时任务发起的轮次（见 synapse.hosts.HOST）
HOST_KINDS = {"通知": "notification", "打断": "interrupt", "命令": "command", "压缩": "compaction"}
COLLECTOR = "observe/collect-v2"         # v2（2026-10-17）：开关记录作为一个来源收进来


def origin(user):
    """这一轮开头是谁：(origin, host_kind)。宿主注入按 label.kind 认，脚本按约定的开头认。"""
    k = label.kind(user)
    if k:
        return "host", HOST_KINDS[k]
    if (user or "").lstrip().startswith(SCRIPT_PREFIX):
        return "script", NA
    return "human", NA


def _blank(v):
    return UNKNOWN if v in (None, "") else v


def collector(conn):
    return operator(conn, COLLECTOR, "observe", "collector", model=NA, prompt=NA, prompt_hash=NA, isolation=NA,
                    window="turn", params={"code": "mh/engram/observe/collect.py", "fresh_seconds": FRESH.total_seconds(),
                                                    "sources": sorted(SOURCES)})


def unchanged(conn, path):
    s = path.stat()
    row = conn.execute("SELECT size, mtime FROM collect_file WHERE path=?", (str(path),)).fetchone()
    return row is not None and (row[0], row[1]) == (s.st_size, s.st_mtime)


def mark(conn, path):
    s = path.stat()
    conn.execute("INSERT OR REPLACE INTO collect_file VALUES(?,?,?)", (str(path), s.st_size, s.st_mtime))


def save(conn, op, session, turns, log=None):
    """写一个会话。末尾还没结束的一轮留到下次。返回 (新轮次, 新原文, 更正)。"""
    if turns and not turns[-1].closed:
        turns = turns[:-1]
    s = session
    if (s.source, s.session) in deleted(conn)[0]:          # 按要求删掉的会话不采回来（store.delete）
        return 0, 0, 0
    row = conn.execute("SELECT occurrence FROM session_detail WHERE source=? AND session=?", (s.source, s.session)).fetchone()
    parent = (NA if s.kind == "user" else UNKNOWN) if not s.parent else s.parent
    meta = (s.file or UNKNOWN, _blank(s.kind), parent, _blank(s.cwd), _blank(s.model), _blank(s.title))
    if row is None:
        sid = occurrence(conn, "session", op, started=iso(s.started), scale="session", cause="new_text")
        conn.execute("INSERT INTO session_detail VALUES(?,?,?,?,?,?,?,?,?)", (sid, s.source, s.session, *meta))
    else:
        sid = row[0]      # 会话的元数据（标题、文件位置……）可以变，照最新的
        conn.execute("UPDATE session_detail SET file=?, kind=?, parent=?, cwd=?, model=?, title=? WHERE occurrence=?",
                     (*meta, sid))
    new = texts = fixed = 0
    for i, t in enumerate(turns):
        cur = conn.execute("INSERT OR IGNORE INTO turn_text VALUES(?,?,?,?,?,?,?)",
                           (t.id, t.user, t.assistant, st.dumps(t.commentary), st.dumps(t.reasoning), st.dumps(t.tools),
                            st.now()))
        texts += cur.rowcount
        prev = t.prev if t.prev is not None else (i - 1 if s.source == "claude" else UNKNOWN)
        ending = t.ending or ("normal" if s.source == "dsh" else UNKNOWN)
        old = conn.execute("SELECT * FROM turn_detail WHERE source=? AND session=? AND ordinal=?",
                           (s.source, s.session, i)).fetchone()
        if old is None:
            oid = occurrence(conn, "turn", op, started=iso(t.started), scale="turn", cause=NA, parent=sid)
            org, kind = ("human", "command") if s.source == switch.SOURCE else origin(t.user)     # 开关记录：用户的命令
            conn.execute("INSERT INTO turn_detail VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (oid, t.id, s.source, s.session, i, int(t.closed), ending, prev, st.dumps(t.address), org, kind))
            new += 1
            continue
        if old["text"] != t.id:
            if log:
                print(f"[{s.source}] {s.session} 第 {i} 轮：库里是 {old['text']}，重新解析是 {t.id}，不改", file=log)
            continue
        oid = old["occurrence"]
        if t.closed and not old["closed"]:
            fixed += conn.execute("UPDATE turn_detail SET closed=1 WHERE occurrence=?", (oid,)).rowcount
        if t.prev is not None and old["prev"] in (UNKNOWN, i - 1) and old["prev"] != t.prev:
            fixed += conn.execute("UPDATE turn_detail SET prev=? WHERE occurrence=?", (t.prev, oid)).rowcount
        if t.ending and old["ending"] in (UNKNOWN, "normal"):
            fixed += conn.execute("UPDATE turn_detail SET ending=? WHERE occurrence=?", (t.ending, oid)).rowcount
    return new, texts, fixed


def run(conn, sources=tuple(SOURCES), force=False, fresh=FRESH, log=sys.stderr, modules=None):
    """增量采集。modules：{来源: 解析模块}，测试时替换。返回各来源的计数。"""
    modules = modules or SOURCES
    op = collector(conn)
    now = datetime.now(timezone.utc)
    totals = {}
    for name in sources:
        mod = modules[name]
        n = {"files": 0, "skipped": 0, "fresh": 0, "failed": 0, "turns": 0, "texts": 0, "fixed": 0}
        for path in mod.files():
            if not force and unchanged(conn, path):
                n["skipped"] += 1
                continue
            try:
                many = hasattr(mod, "parse_all")             # 一个文件里有几个会话（开关记录）：有意的命令，不等 FRESH
                found = mod.parse_all(path) if many else [mod.parse(path)]
            except Exception as e:          # 单个文件坏了不影响其余
                n["failed"] += 1
                print(f"[{name}] {path}: {e!r}", file=log)
                continue
            if not many and young(found[0][0], now, fresh):
                n["fresh"] += 1
                continue
            for session, turns in found:
                t, x, f = save(conn, op, session, turns, log)
                n["turns"] += t
                n["texts"] += x
                n["fixed"] += f
            mark(conn, path)
            n["files"] += 1
        conn.commit()
        totals[name] = n
    return totals
