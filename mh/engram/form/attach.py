"""③ attach：新会话入口挂到哪条话题线（design/engram/form/设计.md“③ attach”）。输入的排法和输出的解析，纯程序。

池（pool）：{编号: 入口}，入口是 {"id", "kind": entry | line | node, "name", "topic", "words", "stops", "parent", "date"}；
会话入口的 parent 是线，线的 parent 是父线或骨架节点（k-…），节点的 parent 是上一级节点。
- 有骨架（attach-v0.1）：给骨架的树（节点下列出已经挂着的线）、按写进的文件算出的落点、检索挑出的会话入口（连同它们所在的线）；
  模型输出“节点：k-x｜挂到：L…（或 新线 名字）｜为什么：…”（attach-v0.2；v0.1 的例子写 l-…）。
- 没骨架（attach-lines-v0.2；v0.1 写着候选“都比新文件夹早”，再观察补进旧对话时不成立，2026-10-19 改）：给检索挑出的线和会话入口；模型输出“挂到：L…（或 新线 名字（父：L…））｜为什么：…”。
句柄 2026-10-19 起写 L3、T5（以前 l-3、s-5，模型写成旧样子也认，form.handle_of 换成现在的）。
实验：tmp/form-attach/run.py（relate-v0.5、relate-v0.6，第八版）。
"""
import re

from . import glossary, label, skeleton


def entry_text(name, topic, stops, words, date):
    return (f"名字：{name}\n主题：{topic}\n" + "\n".join(f"停在：{s}" for s in stops)
            + "\n索引词：\n" + label.show(words) + f"\n时间：{date[:10]}")


def path(pool, eid):
    out = []
    while eid:
        out.append(f"{eid} {pool[eid]['name']}")
        eid = pool[eid]["parent"]
    return " › ".join(reversed(out))


def _last(pool, line):
    kids = [x for x in pool.values() if x["parent"] == line and x["kind"] == "entry"]
    return (max(kids, key=lambda x: x["date"])["name"] if kids else "（还没有段）"), len(kids)


def candidates(pool, picks):
    """检索挑出的入口，连同它们往上的线和节点（各给一次）。"""
    ids = list(picks)
    for eid in list(ids):
        p = pool[eid]["parent"]
        while p:
            if p not in ids:
                ids.append(p)
            p = pool[p]["parent"]
    lines = []
    for eid in ids:
        e = pool[eid]
        if e["kind"] == "node":
            continue
        if e["kind"] == "line":
            last, n = _last(pool, eid)
            lines.append(f"[{eid}]〔线〕{path(pool, eid)}｜下面 {n} 段，最近一段：{last}")
        else:
            st = "；".join(e["stops"])[:300]
            lines.append(f"[{eid}]〔段〕{path(pool, eid)}｜{e['date'][:10]}" + (f"｜主题：{e['topic']}" if e["topic"] else "")
                         + (f"｜停在：{st}" if st else ""))
    return "\n".join(lines) or "（没有）"


def skeleton_tree(pool, ns):
    lines = {}
    for e in pool.values():
        if e["kind"] == "line" and e["parent"] in {n["id"] for n in ns.values()} and not e.get("retired"):
            node = next(p for p, n in ns.items() if n["id"] == e["parent"])
            lines.setdefault(node, []).append((e["id"], e["name"], _last(pool, e["id"])[0]))
    return skeleton.tree(ns, lines)


def hint(nodes_count, ns):
    if not nodes_count:
        return "这次没有写进骨架里的文件"
    return "、".join(f"{ns[p]['id']}（{c} 个文件）" for p, c in sorted(nodes_count.items(), key=lambda x: -x[1]))


def build(prompt, entry, pool, picks, ns=None, nodes_count=None):
    """ns：骨架（skeleton.nodes），没有就是没骨架的写法。"""
    if ns:
        segs = [e for e in picks if pool[e]["kind"] == "entry"]
        cand = candidates(pool, segs) if segs else "（没有）"
        body = ("\n\n## 新文件夹\n\n" + entry + "\n\n## 骨架\n\n" + skeleton_tree(pool, ns)
                + "\n\n## 程序按写进的文件算出的落点\n\n" + hint(nodes_count, ns) + "\n\n## 检索挑出的段\n\n" + cand)
    else:
        cand = candidates(pool, [e for e in picks if pool[e]["kind"] != "node"])
        body = "\n\n## 新文件夹\n\n" + entry + "\n\n## 候选\n\n" + cand
    return prompt + body + glossary.text(entry.replace("\n索引词：\n", "\n") + cand)   # 格式里的标题不算用到


def parse(output, skeleton_on):
    """→ (节点编号或 None, 挂到（线编号或“新线 名字”）, 父线编号或 None, 为什么)。认不出返回挂到 ""。"""
    out = output.strip()
    if skeleton_on:
        m = re.search(r"节点[:：]\s*(k-[\w-]+)\s*[｜|]\s*挂到[:：]\s*(新线\s+.+?|[A-Za-z][\w-]*)\s*(?:[｜|]\s*为什么[:：]\s*(.+))?$", out, re.M)
        if not m:
            return None, "", None, ""
        return m.group(1), m.group(2).strip(), None, (m.group(3) or "").strip()
    m = re.search(r"^(?:挂到[:：]\s*)?(新线\s+.+?|[A-Za-z][\w-]*)(?:\s*[｜|]\s*为什么[:：]\s*(.+))?$", out, re.M)   # 漏写“挂到：”也认
    if not m:
        return None, "", None, ""
    target, why = m.group(1).strip(), (m.group(2) or "").strip()
    parent = None
    p = re.match(r"新线\s+(.+?)(?:（父[:：]\s*(\S+?)）)?$", target)
    if p and p.group(2):
        target, parent = "新线 " + p.group(1), p.group(2)
    return None, target, parent, why


def new_line(target):
    """“新线 名字” → 名字；不是新线返回 None。"""
    m = re.match(r"新线\s+(.+)$", target)
    return m.group(1).strip() if m else None
