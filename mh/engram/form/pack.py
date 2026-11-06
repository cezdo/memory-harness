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
from ..store.instance import REVIEW_URL, USER_ALIASES, USER_NAME

_BLOCK = re.compile(r"^###\s*陈述\s*(\d+)\s*$", re.M)


def unlink(text, url=None):
    """去掉发布到预览站的页面链接（带 # 页面号的那种）连同“预览为”之类的引子；Markdown 链接留下文字。
    页面链接是一次性的，给用户看完就没用了，记录里留着只占字（用户 11-04：回复只给原件路径，预览站静默发布）。
    只认实例配置的 [review].url；说预览站本身（不带页面号）的不动。"""
    url = REVIEW_URL if url is None else url
    if not url or url not in text:
        return text
    page = re.escape(url) + r"#[0-9a-f]{8,}"
    text = re.sub(r"\[([^\]]+)\]\(" + page + r"\)", r"\1", text)
    text = re.sub(r"[（(]\s*(?:预览(?:页|站)?\s*(?:为|在|是)?\s*[:：]?\s*)?" + page + r"\s*[）)]", "", text)
    text = re.sub(r"[，,；;]?\s*(?:预览(?:页|站)?\s*(?:为|在|是)?\s*[:：]?\s*)?" + page, "", text)
    return re.sub(r"[，,]\s*(?=[。；，])", "", text)


def e_line(e):
    seg = "、".join(e["segments"]) if isinstance(e["segments"], list) else str(e["segments"])
    sp = e["speaker"] if not str(e["speaker"]).startswith("?") else "说话人未知"
    md = e["mode"] if not str(e["mode"]).startswith("?") else "mode 未知"
    return f"[E{e['id']}]（{sp}·{md}·{seg}）{e['text']}"


def env(session, model_desc, events=()):
    """session：{"source", "cwd", "model", "begin", "end"}。events 里出现了用户的别的称呼（如换记录名以前观察的 E），
    加一行说明是同一个人（2026-11-04：记录名换了以后，旧 E 写着旧称呼、说话人标新名字，模型写出“新名（旧称）”“称呼旧称「新名」”）。"""
    lines = ["## 环境",
             f"对话平台：{PLATFORMS.get(session['source'], session['source'])}，工作区 {session['cwd']}",
             f"时间：{session['begin'][:16]} 至 {session['end'][:16]}（UTC）",
             f"当时的“我”：{session['model']}",
             f"现在回看的“我”：{model_desc}"]
    other = [a for a in USER_ALIASES[1:] if any(a in e["text"] for e in events)]
    if other:
        lines.append(f"称呼：事件里的{'、'.join(f'「{a}」' for a in other)}是{USER_NAME}的别的称呼，同一个人；"
                     f"记录里一律写{USER_NAME}，不并写，照引的原话不改")
    return "\n".join(lines)


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
FILE_PATH = re.compile(r"『文件[:：][^』]*』")
LONG = 450               # 正文超过这么多字，请模型确认这些字是否必要（2026-11-04，pack-v0.9.2：陈述写到能用；数只在程序里，不进提示词）
DROP_SHARE = 0.1         # 略去的 E 超过这个比例就重写（2026-10-27：v0.3 的 8 个单位略去 51/3386；v0.4 限了条数后涨到 594，
                         # v0.5 提示词说“装不下就多写一条”降到 301，长会话里仍略去用户的要求和“我”的判断）


def check(items, events, groups=None):
    """pack 的程序检查（v3，2026-11-04）：写了编号范围、来源不在这次输入里、正文超过 LONG 字；覆盖（用户的话、略去的比例）。
    v2 的来源上限（一条不超过一页、两页要同一段）去掉了：它逼写的人把一件事拆开，每句依据哪几条改由程序对（用户 11-04）。
    groups 不再用，留着参数免得调用方跟着改。→ [(陈述序号, 说明)]，没有问题是空的。"""
    by = {e["id"]: e for e in events}
    out = []
    for it in items:
        ids = refs(it)
        if it.get("ranges"):
            out.append((it["n"], "编号写成了范围（如 E3-E7）：E 的编号不按对话顺序，范围会夹进别的事件，逐个列出"))
        unknown = [x for x in ids if x not in by]
        if unknown:
            out.append((it["n"], f"{'、'.join(f'E{x}' for x in unknown[:8])} 不在这次的事件里，去掉或换成对的编号"))
        size = len(FILE_PATH.sub("『文件』", it["text"]))     # 文件路径不算字数（11-04：一句列 4 个源码路径就过了 LONG）
        if size > LONG:
            out.append((it["n"], f"正文 {size} 字，比较长：请确认这些字是不是都必要，能删改的就删改（经过、重复、执行中没改变结论的波折），"
                                 f"觉得已经合适就保持，不必为了变短而删；结论、数值、理由、{USER_NAME}的原话和现在的状态不要删，其实是两件事的可以拆成两条"))
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


def soft(why):
    """软的问题：只请模型确认，确认后还在也照样入库、不写重做申请（正文长度：满意就保持，用户 11-04）。"""
    return why.startswith("正文 ")


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
        item["text"] = unlink(" ".join(item["text"]))
        items.append(item)
    ids = {e["id"] for e in events}
    cited = set()
    for it in items:
        cited |= set(it["basis"]) | {x for q in it["quotes"] + it["files"] for x in q["to"]}
    return head["名字"], head["主题"], items, sorted(ids - cited), sorted(cited - ids)


def splice(prev, follow):
    """下一轮只交回改过的陈述（用原编号；拆开或补出的写成“陈述N.1”接在第 N 条后面）→ 拼回上一次的记录、重编号。
    下一轮回“保持”或没有陈述，就是上一次的。"""
    got = {}
    for m in re.finditer(r"^###\s*陈述\s*(\d+)(?:\.\d+)?\s*$(.*?)(?=^###\s|\Z)", follow, re.M | re.S):
        got.setdefault(int(m.group(1)), []).append(m.group(2).strip())
    if not got:
        return prev
    parts = _BLOCK.split(prev)
    blocks = []
    for i in range(1, len(parts) - 1, 2):
        blocks += got.get(int(parts[i]), [parts[i + 1].strip()])
    return parts[0].strip() + "\n\n" + "\n\n".join(f"### 陈述{i}\n{b}" for i, b in enumerate(blocks, 1)) + "\n"


def refs(item):
    """一条陈述依据的全部 E（来源、引用、文件的指向），去重保序。"""
    out = []
    for x in item["basis"] + [x for q in item["quotes"] + item["files"] for x in q["to"]]:
        if x not in out:
            out.append(x)
    return out
