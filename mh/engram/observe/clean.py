"""清理空会话：宿主里打开了却没产生对话的会话（用户 2026-11-06：Orca 里占着位置，影响观感），删掉宿主的原文件。纯程序。

空会话 = mh 从它提取不到东西（2026-11-27）：解析出来没有一轮有“我”的最终回答（observe 只读写出了最终回答的轮次，
label.wanted）。用户说了话、没等到回答的（被打断、报错）也算空（11-06 曾经留着，11-27 改：没产生对话就没必要留）。
只看主会话（kind = user），子代理、审查不动。删之前把原文件重新解析一遍（库里不收末尾没结束的一轮），
再不经解析器看一眼原文里有没有 assistant 的正文（answered），防解析器漏认。
codex 只删 rollout 文件，它自己的线程表（state_5.sqlite）不碰：那是宿主的库，codex 本来就容忍 rollout 不在。最近 AGE 内动过的不删。宿主还开着（dsh 拿着会话锁、Orca 的标签还在）也照删（用户 11-06：直接删）：Linux 下删掉被打开的文件，
原句柄照样能用；最坏是在那个空标签里再开口时宿主重建文件或报错，刷新即可。删掉的写进数据目录 cleaned.jsonl；库里的会话行不动
（它记的是“有过这么一个会话”），文件以后又被宿主写出来，采集照常收。
"""
import json
import os
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


def pushed(cwd, homes=None):
    """客户端推来的会话（cwd 在 [deploy.clients] 的家目录下）：服务机上那份是镜像，原件在客户端，
    删了下一次推送又补回来（2026-11-27：服务机每小时删同一批客户端推来的空会话，名单越滚越长）。"""
    from ..store import instance
    homes = instance.CLIENT_HOMES if homes is None else homes
    return any(cwd == h or cwd.startswith(h + "/") for h in homes)


def candidates(conn, homes=None):
    """库里的空会话：[(source, session, file)]；客户端推来的不算（见 pushed）。"""
    return [(src, sid, f) for src, sid, f, cwd in conn.execute(
        "SELECT s.source, s.session, s.file, s.cwd FROM session_detail s WHERE s.kind='user' AND s.source IN (%s) "
        "AND NOT EXISTS (SELECT 1 FROM turn_detail d JOIN turn_text t ON t.id=d.text "
        "  WHERE d.source=s.source AND d.session=s.session AND t.assistant!='')"
        % ",".join("?" * len(HOSTS)), HOSTS) if not pushed(cwd or "", homes)]


def empty(turns):
    """解析出来的轮次（含末尾没结束的）里没有一句“我”的最终回答：observe 没有能读的（label.wanted）。"""
    return not any(label.wanted(t.user, t.assistant) for t in turns)


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


ANSWER = {   # 原文里 assistant 的正文（不是只有工具调用、思考），按宿主的格式逐行认
    "claude": (re.compile(r'"type"\s*:\s*"assistant"'), re.compile(r'"type"\s*:\s*"text"\s*,\s*"text"\s*:\s*"\s*[^"\s]')),
    "codex": (re.compile(r'"role"\s*:\s*"assistant"'), re.compile(r'"type"\s*:\s*"output_text"\s*,\s*"text"\s*:\s*"\s*[^"\s]')),
    "dsh": (re.compile(r'"type"\s*:\s*"assistant/message"'), re.compile(r'"type"\s*:\s*"text"\s*,\s*"text"\s*:\s*"\s*[^"\s]')),
}


def answered(source, path):
    """原文里有没有 assistant 的正文，不经解析器：解析器漏认时（11-06：codex goal 模式的目标包在
    <codex_internal_context> 里，整段一轮都没解析出来）有回答的会话也不会被当成空的删掉。只有工具调用、没写出正文的不算。"""
    who, text = ANSWER[source]
    return any(who.search(l) and text.search(l) for l in raw(path).splitlines())


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


def check(source, file, now, age):
    """原文件现在还是空会话、AGE 内没动过 → 要删的路径；否则 None。删之前在原件上重新解析（库里不收末尾没结束的一轮）。"""
    path = actual(file)
    if path is None:
        return None
    try:
        got = parse(source, path)
    except Exception:
        return None
    if not empty(got[1]) or answered(source, path):
        return None
    paths = target(source, str(path))
    return None if now - newest(paths) < age else paths


def _remove(paths, source, session, record):
    for p in paths:
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    with open(record, "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": st.now(), "source": source, "session": session, "paths": [str(p) for p in paths]},
                           ensure_ascii=False) + "\n")


def clean(conn, apply=False, now=None, age=AGE, record=None):
    """找出并（apply 时）删掉空会话。返回 [(source, session, [路径])]。"""
    now = time.time() if now is None else now
    record = record or (st.HOME / "cleaned.jsonl")
    out = []
    for source, session, file in candidates(conn):
        paths = check(source, file, now, age)
        if paths is None:
            continue
        out.append((source, session, [str(p) for p in paths]))
        if apply:
            _remove(paths, source, session, record)
    return out


CACHE = "client-empty-v2.json"   # 客户端本机判过的（v2：11-27 改成“没有一轮有最终回答”；判法变了就换名字）：路径 → [mtime, size, 是不是空的主会话]；文件没变就不再解析


def local_empties(roots, cache=None):
    """客户端本机（2026-11-27：反过来，在客户端就地判）：roots 下各宿主的空主会话 [(source, session, 原文件)]。
    不看年纪；判法和 check 一样（重新解析原件、不经解析器再看一眼有没有回答），按 (mtime, size) 缓存在数据目录。"""
    cache_file = Path(cache or st.HOME / CACHE)
    try:
        old = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        old = {}
    roots = [Path(r) for r in roots]
    new, out = {}, []
    for source in HOSTS:
        for f in SOURCES[source].files():
            f = Path(f)
            if not any(f.is_relative_to(r) for r in roots):
                continue
            try:
                s = f.stat()
            except OSError:
                continue
            key, sig = str(f), [s.st_mtime, s.st_size]
            hit = old.get(key)
            if hit and hit[:2] == sig:
                verdict = hit[2]
            else:
                try:
                    sess, turns = parse(source, f)
                    verdict = sess.session if sess and sess.kind == "user" and empty(turns) and not answered(source, f) else ""
                except Exception:
                    verdict = ""
            new[key] = sig + [verdict]
            if verdict:
                out.append((source, verdict, f))
    if new != old:
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(new, ensure_ascii=False))
        os.replace(tmp, cache_file)
    return out


def excludes(roots, cache=None):
    """推送时跳过的：空主会话连同它要删的那些路径（target），按 root 分组、相对 root。空会话从头到尾不到服务机。"""
    roots = [Path(r) for r in roots]
    out = {r: [] for r in roots}
    for source, _, f in local_empties(roots, cache):
        for p in target(source, str(f)):
            r = next(r for r in roots if p.is_relative_to(r))
            out[r].append(str(p.relative_to(r)))
    return out


def clean_client(roots, apply=False, now=None, age=AGE, record=None, cache=None):
    """客户端清自己的空会话：本机判、本机删，不找服务机（空会话推送时就跳过了，服务机上没有镜像）。
    返回 [(source, session, [路径])]。"""
    now = time.time() if now is None else now
    record = record or (st.HOME / "cleaned.jsonl")
    out = []
    for source, session, f in local_empties(roots, cache):
        paths = target(source, str(f))
        if now - newest(paths) < age:
            continue
        out.append((source, session, [str(p) for p in paths]))
        if apply:
            _remove(paths, source, session, record)
    return out


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="清理空会话（打开了却没开始对话的）：默认只列出，--apply 才删")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--client", action="store_true", help="客户端：本机判、本机删（推送的那些目录）")
    a = ap.parse_args(argv)
    if a.client:
        from ...deploy.migrate import source_roots
        got = clean_client(source_roots(), apply=a.apply)
    else:
        got = clean(st.connect(readonly=not a.apply), apply=a.apply)
    for source, session, paths in got:
        print(f"{'删了' if a.apply else '会删'} {source}:{session}  {'、'.join(paths)}")


if __name__ == "__main__":
    main()
