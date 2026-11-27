"""挂线树导出（只读）：按工作区打印节点 → 线 → 话题（日期、名字、主题前 70 字），供定期整体整理使用。

    python3 -m mh tree <工作区> [--short]
    python3 -m mh tree                        # 列出全部工作区及话题数
"""
import collections


def workspaces(pool):
    """统计每个工作区当前活跃线下的话题数：[(工作区, 线数, 话题数)]，按话题数降序。"""
    lines_by_ws = collections.Counter()
    entries_by_ws = collections.Counter()
    for e in pool.values():
        if e.get("retired"):
            continue
        if e["kind"] == "line" and e.get("ws"):
            lines_by_ws[e["ws"]] += 1
        elif e["kind"] == "entry":
            ws = pool.get(e["parent"], {}).get("ws")
            if ws:
                entries_by_ws[ws] += 1
    names = sorted(set(lines_by_ws) | set(entries_by_ws), key=lambda w: (-entries_by_ws[w], w))
    return [(w, lines_by_ws[w], entries_by_ws[w]) for w in names]


def lines(pool, ws, short=False):
    """按工作区把挂线池排成文本行（节点 → 线 → 话题；跳过已退役的线/节点）。"""
    kids = collections.defaultdict(list)
    for k, e in pool.items():
        if e["kind"] != "entry" and e.get("ws") == ws:
            kids[e["parent"]].append(k)
        elif e["kind"] == "entry" and pool.get(e["parent"], {}).get("ws") == ws:
            kids[e["parent"]].append(k)

    out = []

    def show(k, depth):
        e = pool[k]
        if e["kind"] == "entry":
            date = (e.get("date") or "")[5:10]
            tail = "" if short else f" | {(e.get('topic') or '')[:70]}"
            out.append("  " * depth + f"{k} {date} {e['name']}{tail}")
            return
        if e.get("retired"):
            return
        n = sum(1 for x in kids[k] if pool[x]["kind"] == "entry")
        count = f" ({n})" if e["kind"] == "line" else ""
        out.append("  " * depth + f"{k} 【{e['name']}】{count}")
        for c in sorted(kids[k], key=lambda x: (pool[x]["kind"] != "node", pool[x]["kind"] != "line", pool[x].get("date", ""))):
            show(c, depth + 1)

    roots = [k for k in kids[None] if pool[k]["kind"] in ("node", "line")]
    for r in sorted(roots, key=lambda x: (pool[x]["kind"] != "node", pool[x].get("date", ""), x)):
        show(r, 0)
    return out


def render(conn, ws=None, short=False):
    """导出挂线树文本；不给工作区时列出各工作区汇总。"""
    from . import pool as load_pool
    p = load_pool(conn)
    if not ws:
        return "\n".join(f"{w}  {n_lines} 条线，{n_entries} 个话题" for w, n_lines, n_entries in workspaces(p))
    return "\n".join(lines(p, ws, short=short))
