"""改写陈述：修订提示认领以后，只改写依据那几条 E 的陈述（design/engram/form/设计.md“修订提示”，2026-10-21：
一次决策的影响范围不超出它所在的那一级）。输入的排法和输出的解析，纯程序；跑、写库在 form/__init__.py 的 restate()。

输入：变化（旧 E → 新 E，不成立），再是受影响的陈述，按 pack 输出的格式写（“### 陈述N”、正文、来源、引用、文件），
每条后面列出它依据的 E（现在的正文）。输出：同样的格式，原来的编号；撤下的写“撤下”和“理由：”。
"""
import json
import re

from . import pack
from ..store import restyle

_BLOCK = re.compile(r"^###\s*陈述\s*(\d+)\s*$", re.M)


def statement_text(n, row):
    """一条陈述按 pack 输出的格式写出来（正文、来源、引用、文件）。"""
    st = json.loads(row["structures"])
    ids = lambda xs: "、".join(f"E{x}" for x in xs)
    out = [f"### 陈述{n}", restyle(row["text"]), f"来源：{ids(st.get('basis', []))}"]
    for q in st.get("quotes", []):
        out += [f"#### 引用：{q['name']}", f"指向：{ids(q['to'])}"]
    for f in st.get("files", []):
        out += [f"#### 文件：{f['path']}", f"指向：{ids(f['to'])}"]
    return "\n".join(out)


def build(head, changes, stmts):
    """changes：[(旧 E 行, [新 E 行…] 或 None 表示不成立)]；stmts：[(编号 N, 陈述行, [它依据的 E 行…])]。"""
    lines = ["## 变化"]
    for old, new in changes:
        if new is None:
            lines.append(f"- E{old['id']}（不成立）：{old['text']}")
        else:
            lines.append(f"- E{old['id']}（旧）：{old['text']}")
            lines += [f"  → E{x['id']}（新）：{x['text']}" for x in new]
    parts = [head, "\n".join(lines), "## 受影响的陈述"]
    for n, row, evs in stmts:
        parts.append(statement_text(n, row) + "\n\n它依据的事件：\n" + "\n".join(pack.e_line(e) for e in evs))
    return "\n\n".join(parts)


def parse(output, events):
    """→ {N: {"withdraw": 理由} 或 pack.parse 的一条陈述}。events：这次给的 E（核对来源里的编号）。"""
    out = {}
    parts = _BLOCK.split(output)
    for i in range(1, len(parts) - 1, 2):
        n, body = int(parts[i]), parts[i + 1].strip()
        if body.startswith("撤下"):
            m = re.search(r"理由[:：]\s*(.*)", body)
            out[n] = {"withdraw": m.group(1).strip() if m else ""}
            continue
        _, _, items, _, _ = pack.parse(f"### 陈述{n}\n{body}", events)
        if items:
            out[n] = items[0]
    return out
