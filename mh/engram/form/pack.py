"""① pack：一个会话的 E → 会话记录（design/engram/form/设计.md“① pack”）。输入的排法和输出的解析，纯程序。

输入：环境，再按轮排 E（编号、说话人、mode、出处）；写了文件的轮次在事件后面加一行“写进的文件：”（没有 E 的轮次只写这一行）。
输出：开头“名字：”“主题：”，再是“### 陈述N”，每条下一行“来源：”；被合并的细节“#### 引用：名字” + “指向：”，
落进文件的“#### 文件：路径” + “指向：”。程序算出没保留也没引用的 E、来源里不在这次输入里的编号。
实验：tmp/logical-session/pack.py（pack-v0.6）。
"""
import re
from collections import defaultdict

from ..observe.label import PLATFORMS

_BLOCK = re.compile(r"^###\s*陈述\s*(\d+)\s*$", re.M)


def e_line(e):
    seg = "、".join(e["segments"]) if isinstance(e["segments"], list) else str(e["segments"])
    sp = e["speaker"] if not str(e["speaker"]).startswith("?") else "说话人未知"
    md = e["mode"] if not str(e["mode"]).startswith("?") else "mode 未知"
    return f"[E{e['id']}]（{sp}·{md}·{seg}）{e['text']}"


def env(session, model_desc):
    """session：{"source", "cwd", "model", "begin", "end"}。"""
    return "\n".join(["## 环境",
                      f"对话平台：{PLATFORMS.get(session['source'], session['source'])}，工作区 {session['cwd']}",
                      f"时间：{session['begin'][:16]} 至 {session['end'][:16]}（UTC）",
                      f"当时的“我”：{session['model']}",
                      f"现在回看的“我”：{model_desc}"])


def e_block(events, written):
    """events：这次要整理的 E（带 ordinal）；written：{ordinal: [说明…]}，只含这次范围里的轮次。"""
    by = defaultdict(list)
    for e in events:
        by[e["ordinal"]].append(e)
    out = []
    for n in sorted(set(by) | set(written)):
        part = [f"## 第 {n + 1} 轮"] + [e_line(e) for e in by.get(n, [])]
        if written.get(n):
            part.append("写进的文件：" + "；".join(written[n]))
        out.append("\n".join(part))
    return "\n\n".join(out)


def labels(spec):
    """“E3、E5-E7” → [3, 5, 6, 7]。"""
    out = []
    for a, b in re.findall(r"E(\d+)(?:\s*[-–~至]\s*E?(\d+))?", spec or ""):
        a = int(a)
        b = int(b) if b else a
        if b - a > 200:
            b = a
        out += list(range(a, b + 1))
    return out


def parse(text, events):
    """→ (名字, 主题, 陈述, 没保留也没引用的 E, 来源里不在输入里的编号)。
    陈述：[{"n", "text", "basis": [E…], "quotes": [{"name", "to": [E…]}], "files": [{"path", "to": [E…]}]}]。
    输出只写“无”时陈述为空。"""
    head = {k: (m.group(1).strip() if (m := re.search(rf"^{k}[:：]\s*(.*)$", text, re.M)) else "") for k in ("名字", "主题")}
    parts = _BLOCK.split(text)
    items = []
    for i in range(1, len(parts) - 1, 2):
        item = {"n": int(parts[i]), "text": [], "basis": [], "quotes": [], "files": []}
        block = None
        for line in parts[i + 1].strip().splitlines():
            s = line.strip()
            if not s:
                continue
            m = re.match(r"^####\s*(引用|文件)[:：]\s*(.*)$", s)
            if m:
                block = {"name": m.group(2).strip(), "to": []} if m.group(1) == "引用" else {"path": m.group(2).strip(), "to": []}
                item["quotes" if m.group(1) == "引用" else "files"].append(block)
                continue
            m = re.match(r"^(来源|指向)[:：]\s*(.*)$", s)
            if m:
                if m.group(1) == "指向" and block is not None:
                    block["to"] = labels(m.group(2))
                elif m.group(1) == "来源":
                    item["basis"] = labels(m.group(2))
                continue
            if block is None:
                item["text"].append(s)
        item["text"] = " ".join(item["text"])
        items.append(item)
    ids = {e["id"] for e in events}
    cited = set()
    for it in items:
        cited |= set(it["basis"]) | {x for q in it["quotes"] + it["files"] for x in q["to"]}
    return head["名字"], head["主题"], items, sorted(ids - cited), sorted(cited - ids)


def refs(item):
    """一条陈述依据的全部 E（来源、引用、文件的指向），去重保序。"""
    out = []
    for x in item["basis"] + [x for q in item["quotes"] + item["files"] for x in q["to"]]:
        if x not in out:
            out.append(x)
    return out
