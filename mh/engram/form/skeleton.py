"""骨架：一个工作区里人工定下的层级（2026-10-18：层级人工写清楚，模型不判谁从属于谁、直接拿来用）。纯程序。

- memory-harness 照 design 的目录（用户：这部分目录结构是经过很多讨论设计得到的）：design/ 下每个有“设计.md”的目录是一个节点，
  design/ 本身是根（Memory Harness）；名字按词典的全名。节点在库里写 design/ 下的相对路径（根是 ""），给模型看时写 k-<路径>。
- 写进的文件落到节点：design/<路径>/…、mh/<路径>/… 取最深的、存在的节点（mh/engram/observer/ 这类旧目录落到 Engram）；
  tmp/、家目录下的文件不算。
- 文件骨架：显式文本树与模块导览，读取快照 → 主会话申请 → form 采用；存在库的 act 里，各工作区的节点句柄分开。
- 别的工作区：没有登记的先不用（nodes 返回 None）。
实验：tmp/form-attach/skeleton.py（第八版）。
"""
import hashlib
import json
import re
import subprocess
from pathlib import Path

from . import glossary
from .. import store as st

REPO = Path(__file__).resolve().parents[3]
WORKSPACES = {"memory-harness": (REPO / "design", "Memory Harness")} if (REPO / "design").is_dir() else {}


def adopted(conn):
    """库里各工作区最近采用的文件骨架；只追加 act，读者每次按库重建。"""
    out = {}
    if conn is not None:
        for aid, meta in conn.execute("SELECT a.occurrence, a.meta FROM act_detail a JOIN occurrence o ON o.id=a.occurrence "
                                      "WHERE a.process='form' AND a.step='adopt_skeleton' AND o.outcome='ok' ORDER BY o.id"):
            m = json.loads(meta)
            out[m["workspace"]] = dict(m, act=aid)
    return out


def section(text, name):
    """唯一的 Markdown 章节（含下级章节）；重复或缺少就拒绝。"""
    headings = list(re.finditer(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", text, re.M))
    matches = [i for i, m in enumerate(headings) if m[2] == name]
    if len(matches) != 1:
        raise ValueError(f"章节 {name} 应恰好有一处，找到 {len(matches)} 处")
    i = matches[0]
    end = next((m.start() for m in headings[i + 1:] if len(m[1]) <= len(headings[i][1])), len(text))
    return text[headings[i].end():end], text[:headings[i].start()].count("\n") + 1


def file_kid(ws, path):
    """文件骨架按工作区和节点路径定句柄；名称变化会成为新节点。"""
    return "k-w" + hashlib.sha256(ws.encode()).hexdigest()[:12] + "-" + (
        hashlib.sha256(path.encode()).hexdigest()[:16] if path else "root")


def read_file(ws, root, filename, heading, overview=None):
    """mentor README 首例：只读取显式文本树与模块表；不推断层级。返回来源快照和节点。"""
    root = Path(root).resolve()
    file = (root / filename).resolve()
    relative = file.relative_to(root).as_posix()
    raw = file.read_bytes()
    text = raw.decode("utf-8")
    body, start = section(text, heading)
    blocks = list(re.finditer(r"^```(?:text)?[ \t]*\n(.*?)^```[ \t]*$", body, re.M | re.S))
    if len(blocks) != 1:
        raise ValueError("骨架章节应恰好有一棵 text 文本树")
    block = blocks[0]
    ns, stack = {}, []
    for i, line in enumerate(block[1].splitlines()):
        if not line.strip():
            continue
        if not ns:
            if line != line.strip() or re.search(r"[│├└─]", line):
                raise ValueError("文本树第一行要给根节点的名字")
            name, depth = line.strip(), 0
        else:
            m = re.fullmatch(r"((?:│  |   )*)(?:├─|└─)[ \t]+([^/]+?)", line)
            if not m:
                raise ValueError(f"认不出文本树这一行：{line}")
            name, depth = m[2].strip(), len(m[1]) // 3 + 1
            if depth > len(stack):
                raise ValueError(f"文本树层级跳过父节点：{line}")
        if not name or "/" in name:
            raise ValueError(f"节点名不明确：{line}")
        parent = stack[depth - 1] if depth else None
        path = (parent + "/" if parent else "") + name if depth else ""
        if path in ns:
            raise ValueError(f"文本树有重复节点：{path}")
        ns[path] = {"path": path, "id": file_kid(ws, path), "name": name, "parent": parent,
                    "topic": "", "files": [], "line": start + body[:block.start(1)].count("\n") + i}
        stack[depth:] = [path]
    if len(ns) < 2:
        raise ValueError("文本树至少要有根和一个子节点")
    if overview:
        table, _ = section(text, overview)
        for line in table.splitlines():
            if not line.startswith("|"):
                continue
            cells = [v.strip() for v in line.strip().strip("|").split("|")]
            matches = [n for n in ns.values() if n["name"] == cells[0]]
            if len(matches) > 1:
                raise ValueError(f"模块导览的名称对应多个节点：{cells[0]}")
            if len(cells) != 3 or not matches:
                continue
            n = matches[0]
            n["topic"] = cells[1]
            for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", cells[2]):
                link = link.split("#", 1)[0]
                if not link or ":" in link:
                    continue
                target = (file.parent / link).resolve()
                try:
                    rel = target.relative_to(root).as_posix()
                except ValueError:
                    continue
                n["files"].append(rel)
    return {"workspace": ws, "root": str(root), "file": relative, "section": heading, "overview": overview or st.NA,
            "sha256": hashlib.sha256(raw).hexdigest(), "text": text, "nodes": ns}


def placements(conn, source, assignments):
    """核对本工作区全部已有线的落点；支持节点路径或句柄，拒绝遗漏、跨工作区和失效节点。"""
    existing = {f"L{r['id']}": dict(r) for r in conn.execute("SELECT * FROM topic_line WHERE workspace=?", (source["workspace"],))}
    if set(assignments) != set(existing):
        raise ValueError(f"线的归属要覆盖 {source['workspace']} 全部已有线；缺少 {sorted(set(existing) - set(assignments))}，"
                         f"多出 {sorted(set(assignments) - set(existing))}")
    ns, out = source["nodes"], {}
    for lid, target in assignments.items():
        path = target if target in ns else next((p for p, n in ns.items() if n["id"] == target), None)
        if path is None:
            raise ValueError(f"{lid} 的节点 {target} 不在骨架里")
        out[lid] = path
    return out, existing


def preview(conn, source, assignments):
    paths, lines = placements(conn, source, assignments)
    by = {}
    for lid, path in paths.items():
        by.setdefault(path, []).append((lid, lines[lid]["name"], "沿用已有话题"))
    return f"{source['workspace']} · {source['file']} · {source['sha256']}\n" + tree(source["nodes"], by)


def request_file(conn, source, assignments, reason, operator_id):
    """主会话提交来源与采用申请。调用方在同一事务里 adopt；不自行提交。"""
    placements(conn, source, assignments)
    program = st.operator(conn, "form/read-skeleton-v0.1", "form", "program", model=st.NA, prompt=st.NA,
                          prompt_hash=st.NA, isolation=st.NA, window="workspace", params={"reader": "text-tree-and-module-table"})
    aid = st.occurrence(conn, "act", program, started=st.now(), ended=st.now(), scale="workspace", cause="manual", outcome="ok")
    meta = {k: v for k, v in source.items() if k != "text"}
    conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (aid, "form", "read_skeleton", st.dumps({"workspace": source["workspace"]}),
                 source["text"], tree(source["nodes"]), st.dumps(meta)))
    rid = st.request(conn, st.MAIN, "form", {"act": [aid]}, reason, operator_id, cause="manual")
    return rid


def adopt(conn, rid, assignments):
    """form 认领文件骨架申请并采用；原线只追加归属记录，编号和挂在下面的话题沿用。"""
    row = conn.execute("SELECT o.inputs, d.from_process, d.to_process FROM occurrence o JOIN request_detail d ON d.occurrence=o.id "
                       "WHERE o.id=?", (rid,)).fetchone()
    if row is None or row["from_process"] != st.MAIN or row["to_process"] != "form":
        raise ValueError("需要一张主会话写给 form 的文件骨架申请")
    if st.claimed(conn, rid):
        raise ValueError("这张骨架申请已经认领过")
    refs = json.loads(row["inputs"]).get("act", [])
    if len(refs) != 1:
        raise ValueError("骨架申请要指定一次读取 act")
    source = conn.execute("SELECT a.meta, o.outcome FROM act_detail a JOIN occurrence o ON o.id=a.occurrence "
                          "WHERE a.occurrence=? AND a.process='form' AND a.step='read_skeleton'", refs).fetchone()
    if source is None or source["outcome"] != "ok":
        raise ValueError("骨架申请的来源需要是成功的读取")
    m = json.loads(source["meta"])
    paths, lines = placements(conn, m, assignments)
    previous = adopted(conn).get(m["workspace"])
    inputs = {"request": [rid], "act": sorted(set(refs + [r["born"] for r in lines.values()]))}
    op = st.operator(conn, "form/adopt-skeleton-v0.1", "form", "program", model=st.NA, prompt=st.NA,
                     prompt_hash=st.NA, isolation=st.NA, window="workspace", params={"placements": "explicit"})
    aid = st.occurrence(conn, "act", op, started=st.now(), ended=st.now(), scale="workspace", inputs=inputs,
                        cause="request", predecessor=previous["act"] if previous else st.NA, outcome="ok")
    meta = dict(m, source_act=refs[0], placements=paths)
    conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (aid, "form", "adopt_skeleton", st.dumps({"workspace": m["workspace"]}),
                 st.dumps(assignments), preview(conn, m, assignments), st.dumps(meta)))
    return aid


def kid(path):
    """节点路径 → 给模型看的编号。"""
    return "k-" + path.replace("/", "-") if path else "k-root"


def _name(part):
    names = glossary.aliases(part)
    return names[0] if names else part


def nodes(ws, conn=None, registry=None):
    """{节点路径: {"path", "id", "name", "parent"}}；没有骨架的工作区返回 None。"""
    registered = (registry if registry is not None else adopted(conn)).get(ws)
    if registered:
        return registered["nodes"]
    if ws not in WORKSPACES:
        return None
    root, root_name = WORKSPACES[ws]
    out = {"": {"path": "", "id": kid(""), "name": _name(root_name), "parent": None}}
    for f in sorted(root.rglob("设计.md")):
        rel = f.parent.relative_to(root)
        if not rel.parts:
            continue
        p = "/".join(rel.parts)
        out[p] = {"path": p, "id": kid(p), "name": _name(rel.parts[-1]), "parent": "/".join(rel.parts[:-1])}
    return out


STRUCTURE = ":(glob)**/设计.md"     # design 目录的骨架只看这些文件的增删、改名


def _git(*args):
    r = subprocess.run(["git", "-C", str(WORKSPACES["memory-harness"][0]), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def version(ws, conn=None, registry=None):
    """一个工作区骨架现在的版本（cognize 的引用钉住它，2026-10-22）：采用过文件骨架的是最近那次采用的 act；照 design 目录的
    是最近一次增删、改名“设计.md”的 git 提交（正文改了不算，节点没变）。没有骨架返回 None。"""
    registered = (registry if registry is not None else adopted(conn)).get(ws)
    if registered:
        return registered["act"]
    if ws not in WORKSPACES:
        return None
    return _git("log", "-1", "--format=%H", "--diff-filter=ADR", "--", STRUCTURE)


def had(ws, path, ver, conn=None):
    """骨架的这个版本里有没有这个节点（路径）。"""
    if isinstance(ver, int):
        if conn is None:
            return False
        row = conn.execute("SELECT a.meta FROM act_detail a WHERE a.occurrence=? AND a.step='adopt_skeleton'", (ver,)).fetchone()
        return bool(row) and json.loads(row[0]).get("workspace") == ws and path in json.loads(row[0]).get("nodes", {})
    if ws not in WORKSPACES or not isinstance(ver, str) or not re.fullmatch(r"[0-9a-f]{40}", ver):
        return False
    return _git("cat-file", "-e", f"{ver}:design/{path}/设计.md" if path else f"{ver}:design") is not None


def node_of(path, ws, ns=None):
    """一个写进的文件（相对工作区的路径）落到哪个节点；不落到任何节点返回 None。"""
    ns = ns if ns is not None else nodes(ws)
    if not ns:
        return None
    if any("files" in n for n in ns.values()):
        hits = [p for p, n in ns.items() if path in n.get("files", [])]
        if not hits:
            return None
        common = hits[0].split("/") if hits[0] else []
        for hit in hits[1:]:
            parts = hit.split("/") if hit else []
            common = common[:next((i for i, (a, b) in enumerate(zip(common, parts)) if a != b), min(len(common), len(parts)))]
        return "/".join(common)
    m = re.match(r"^(?:design|mh)/(.+)$", path)
    if not m:
        return None
    parts = m.group(1).split("/")[:-1]
    best = ""
    for i in range(1, len(parts) + 1):
        p = "/".join(parts[:i])
        if p in ns:
            best = p
    return best


def tree(ns, lines=None):
    """骨架排成缩进的树；lines：{节点路径: [(线编号, 线名, 最近一个文件夹的名字)]}，挂在节点下面一并给出。"""
    kids = {}
    for n in ns.values():
        if n["parent"] is not None:
            kids.setdefault(n["parent"], []).append(n["path"])
    out = []

    def walk(p, depth):
        n = ns[p]
        out.append("  " * depth + f"[{n['id']}] {n['name']}" + (f"：{n['topic']}" if n.get("topic") else ""))
        for lid, lname, last in (lines or {}).get(p, []):
            out.append("  " * (depth + 1) + f"· 线 [{lid}] {lname}（最近一段：{last}）")
        for k in kids.get(p, []):
            walk(k, depth + 1)
    walk("", 0)
    return "\n".join(out)
