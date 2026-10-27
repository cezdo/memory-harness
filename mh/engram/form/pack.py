"""① pack：一个会话的 E → 会话记录（design/engram/form/设计.md“① pack”）。输入的排法和输出的解析，纯程序。

输入：环境，再按轮排 E（编号、说话人、mode、出处）；换了语言的轮次在事件后面加一行说明（observe.languages）；
写了文件的轮次在事件后面加一行“写进的文件：”（没有 E 的轮次只写这一行）。
输出：开头“名字：”“主题：”，再是“### 陈述N”，每条下一行“来源：”；被合并的细节“#### 引用：名字” + “指向：”，
落进文件的“#### 文件：路径” + “指向：”。程序算出没保留也没引用的 E、来源里不在这次输入里的编号；
check 查一条陈述的来源合不合规（v0.4：不写范围、在输入里、一页以内，两页要是同一段，超过两页拆开）。
实验：tmp/logical-session/pack.py（pack-v0.6）。
"""
import re
from collections import defaultdict

from ..observe.label import PLATFORMS
from ..store.instance import USER_NAME

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


def e_block(events, written, languages=None):
    """events：这次要整理的 E（带 ordinal）；written：{ordinal: [说明…]}，只含这次范围里的轮次。
    languages：{ordinal: 一句说明}，换了语言的轮次（observe.languages），附在那一轮的 E 后面，不单独成轮。"""
    languages = languages or {}
    by = defaultdict(list)
    for e in events:
        by[e["ordinal"]].append(e)
    out = []
    for n in sorted(set(by) | set(written)):
        part = [f"## 第 {n + 1} 轮"] + [e_line(e) for e in by.get(n, [])]
        if languages.get(n) and by.get(n):
            part.append(languages[n])
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


RANGE = re.compile(r"E\d+\s*[-–~至]\s*E?\d+")
LIMIT = 8                # 一条陈述的来源（连同引用、文件的指向）通常不超过一页（2026-10-27，用户：页对齐）
HARD = 16                # 超过两页就拆开；两页以内要是同一段连着的来回（预览的同一组）
DROP_SHARE = 0.1         # 略去的 E 超过这个比例就重写（2026-10-27：v0.3 的 8 个单位略去 51/3386；v0.4 限了条数后涨到 594，
                         # v0.5 提示词说“装不下就多写一条”降到 301，长会话里仍略去用户的要求和“我”的判断）


def check(items, events, groups):
    """pack-v0.4 的程序检查（2026-10-27，sol 排查、用户定的标准）：写了编号范围、来源不在这次输入里、超过 HARD 条、
    超过 LIMIT 条却跨了预览的组。groups：{轮次: 组}（observe.groups）。→ [(陈述序号, 说明)]，没有问题是空的。"""
    by = {e["id"]: e for e in events}
    out = []
    for it in items:
        ids = refs(it)
        if it.get("ranges"):
            out.append((it["n"], "编号写成了范围（如 E3-E7）：E 的编号不按对话顺序，范围会夹进别的事件，逐个列出"))
        unknown = [x for x in ids if x not in by]
        if unknown:
            out.append((it["n"], f"{'、'.join(f'E{x}' for x in unknown[:8])} 不在这次的事件里，去掉或换成对的编号"))
        if len(ids) > HARD:
            out.append((it["n"], f"来源连同引用、文件的指向共 {len(ids)} 条，超过 {HARD} 条：这条讲的不止一件事，拆成几条陈述"))
        elif len(ids) > LIMIT:
            spans = {}
            for x in ids:
                if x in by:
                    g = groups.get(by[x]["turn"])
                    spans.setdefault(g[:3] if g else ("轮", by[x]["ordinal"]), set()).add(by[x]["ordinal"])
            if len(spans) > 1:
                parts = [f"第 {min(v) + 1}–{max(v) + 1} 轮" if min(v) != max(v) else f"第 {min(v) + 1} 轮" for v in spans.values()]
                out.append((it["n"], f"来源连同指向共 {len(ids)} 条，跨了{'、'.join(parts)}这几段：拆开，或只留这句话直接依据的"))
    if items:                    # 覆盖：用户的话不略去，略去的不超过 DROP_SHARE（陈述序号 0 = 整份记录）
        cited = {x for it in items for x in refs(it)}
        dropped = [e for e in events if e["id"] not in cited]
        user = [e for e in dropped if e.get("speaker") == USER_NAME]
        if user:
            out.append((0, f"{USER_NAME}说的 {'、'.join(f'E{e["id"]}' for e in user[:20])} 没有落在任何陈述上：{USER_NAME}的输入总要保留"))
        if len(dropped) > DROP_SHARE * len(events):
            out.append((0, f"有 {len(dropped)} 条事件没有落在任何陈述上（{'、'.join(f'E{e["id"]}' for e in dropped[:80])}"
                           f"{'……' if len(dropped) > 80 else ''}）：除了寒暄、重复的进度、纯粹的执行过程，都挂到所属的陈述上，装不下就多写陈述"))
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
                if RANGE.search(m.group(2)):
                    item["ranges"] = True
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
