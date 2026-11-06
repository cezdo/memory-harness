"""清理空会话：宿主里打开了却没开始对话的会话（用户 2026-11-06：Orca 里占着位置，影响观感），删掉宿主的原文件。纯程序。

空会话：没有一轮是用户的话（命令、宿主注入、脚本不算），也没有一句“我”的回答；只看主会话（kind = user），
子代理、审查不动。用户说了话、没等到回答的不算空，不删（那是被打断的对话，要不要留由用户定）。
候选从库里找（采集已经解析过），删之前把原文件重新解析一遍：库里不收末尾没结束的一轮，只看库会漏掉用户刚说的话。
codex 只删 rollout 文件，它自己的线程表（state_5.sqlite）不碰：那是宿主的库，codex 本来就容忍 rollout 不在。最近 AGE 内动过的不删。宿主还开着（dsh 拿着会话锁、Orca 的标签还在）也照删（用户 11-06：直接删）：Linux 下删掉被打开的文件，
原句柄照样能用；最坏是在那个空标签里再开口时宿主重建文件或报错，刷新即可。删掉的写进数据目录 cleaned.jsonl；库里的会话行不动
（它记的是“有过这么一个会话”），文件以后又被宿主写出来，采集照常收。
"""
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import label
from .sources import SOURCES
from .. import store as st

AGE = 6 * 3600          # 最近这么多秒内动过的不删：可能是刚打开、还没开口的会话
HOSTS = ("claude", "codex", "dsh")


def candidates(conn):
    """库里的空会话：[(source, session, file)]。"""
    return [tuple(r) for r in conn.execute(
        "SELECT s.source, s.session, s.file FROM session_detail s WHERE s.kind='user' AND s.source IN (%s) "
        "AND NOT EXISTS (SELECT 1 FROM turn_detail d JOIN turn_text t ON t.id=d.text "
        "  WHERE d.source=s.source AND d.session=s.session AND (d.origin='human' OR t.assistant!=''))"
        % ",".join("?" * len(HOSTS)), HOSTS)]


def empty(turns):
    """解析出来的轮次（含末尾没结束的）里没有用户的话、也没有回答。"""
    return not any(t.assistant or label.kind(t.user) is None and not t.user.lstrip().startswith("（定时任务") for t in turns)


def actual(file):
    """宿主原文件现在在哪：codex 11-03 起把旧 rollout 压成 .jsonl.zst（rollout-migrations），库里记的还是压缩前的路径。"""
    p = Path(file)
    if p.is_file():
        return p
    z = p.with_name(p.name + ".zst")
    return z if z.is_file() else None


def raw(path):
    """原文件的文本；.zst、.zstd 先解压。"""
    data = subprocess.run(["zstd", "-dcq", str(path)], capture_output=True, check=True).stdout \
        if path.suffix in (".zst", ".zstd") else path.read_bytes()
    return data.decode("utf-8", errors="replace")


def parse(source, path):
    """解析原文件；codex 的 .zst 先解压到临时文件（codex 的解析只读文本；dsh 的解析自己读 .zstd）。"""
    if path.suffix != ".zst":
        return SOURCES[source].parse(path)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / path.stem
        tmp.write_text(raw(path), encoding="utf-8")
        return SOURCES[source].parse(tmp)


REPLY = re.compile(r'"(?:role|type)"\s*:\s*"assistant"')


def replied(path):
    """原文里有没有 assistant 的消息，不经解析器：解析器漏认用户的话时（11-06：codex goal 模式的目标包在
    <codex_internal_context> 里被当成宿主注入，整段一轮都没解析出来），有回答的会话也不会被当成空的删掉。"""
    return bool(REPLY.search(raw(path)))


def target(source, file):
    """要删的东西：dsh 是整个会话目录（锁、旧版本日志都在里面）；Claude Code 连同同名目录（工具输出、子代理）。"""
    p = Path(file)
    if source == "dsh":
        return [p.parent]
    side = p.with_suffix("")
    return [p] + ([side] if source == "claude" and side.is_dir() else [])


def newest(paths):
    def mtime(p):
        if p.is_dir():
            return max([p.stat().st_mtime] + [q.stat().st_mtime for q in p.rglob("*")])
        return p.stat().st_mtime
    return max(mtime(p) for p in paths)


def clean(conn, apply=False, now=None, age=AGE, record=None):
    """找出并（apply 时）删掉空会话。返回 [(source, session, [路径])]。"""
    now = time.time() if now is None else now
    record = record or (st.HOME / "cleaned.jsonl")
    out = []
    for source, session, file in candidates(conn):
        path = actual(file)
        if path is None:
            continue
        try:
            got = parse(source, path)
        except Exception:
            continue
        if not empty(got[1]) or replied(path):
            continue
        paths = target(source, str(path))
        if now - newest(paths) < age:
            continue
        out.append((source, session, [str(p) for p in paths]))
        if apply:
            for p in paths:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
            with open(record, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": st.now(), "source": source, "session": session, "paths": [str(p) for p in paths]},
                                   ensure_ascii=False) + "\n")
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="清理空会话（打开了却没开始对话的）：默认只列出，--apply 才删")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    conn = st.connect(readonly=not a.apply)
    for source, session, paths in clean(conn, apply=a.apply):
        print(f"{'删了' if a.apply else '会删'} {source}:{session}  {'、'.join(paths)}")


if __name__ == "__main__":
    main()
