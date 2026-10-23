"""纠正：按点名申请重新观察那几轮（design/engram/设计.md“申请与修正”，C1b 2026-10-21）。

模型看这几轮的全文、申请的理由和被点名的旧 E（带编号），只回应申请：先逐条写核对的结论（保留、改正、不成立，带依据），
改正的事件写“取代：E编号”，申请说漏了的写成新的事件；别的不重写（整组重读会把没被点名的 E 再写一遍，交给 form 就重复了）。
逐条写结论是 2026-10-21 第一次真跑以后加的：只说“不用改就写无”时，Luna 对三条明显写反说话人的 E 都回了“无”。
模型和读法沿用旧 E 出生时那一步（精读、粗读），提示词换成 correct-v0.1，所以是另一个 Operator；取代不论版本，按申请判。
这里放这一步的输入和解析；跑一批、写库在 observe/__init__.py 的 correct()。
"""
import re

from . import parse

PROMPT = "correct-v0.1"
_CHECK = re.compile(r"^#{2,4}\s*核对\s*$", re.M)
_REPLACES = re.compile(r"^\s*取代\s*[:：]\s*(.*?)\s*$", re.M)
_ID = re.compile(r"E\s*(\d+)")
_LINE = re.compile(r"^\s*[-*]?\s*E\s*(\d+)\s*[:：]\s*(保留|改正|不成立)\s*[。，,.：:]?\s*(.*?)\s*$")
VERDICTS = ("保留", "改正", "不成立")


def section(reason, olds):
    """给模型的“申请”一节：理由和被点名的旧 E。olds：[(编号, 正文, 出处段落)]。"""
    lines = [f"## 申请\n理由：{reason}\n\n被点名的事件："]
    for eid, text, segments in olds:
        lines.append(f"- E{eid}：{text}" + (f"（来源：{'、'.join(segments)}）" if segments else ""))
    return "\n".join(lines)


def parse_output(output, tables, named):
    """模型的输出 → (事件, 核对, 是否什么都没写)。
    核对：{编号: {"verdict": 保留 | 改正 | 不成立, "why": 依据}}，只认被点名的；别的编号照实记在 {"unresolved": [...]}，
    被点名却没写结论的记在 {"missing": [...]}。
    事件同 parse.parse_turns，另带 supersedes（取代的编号，只认被点名的）；取代里写了不是被点名的编号，记进这条的 unresolved。"""
    text = output.strip()
    m = _CHECK.search(text)
    check, stray = {}, []
    if m:
        nxt = re.search(r"^#{2,4}\s", text[m.end():], re.M)
        block = text[m.end():m.end() + nxt.start()] if nxt else text[m.end():]
        for line in block.splitlines():
            x = _LINE.match(line)
            if not x:
                continue
            eid = int(x.group(1))
            if eid in named:
                check[eid] = {"verdict": x.group(2), "why": x.group(3)}
            else:
                stray.append(f"E{eid}")
        text = text[:m.start()] + (text[m.end() + nxt.start():] if nxt else "")
    events, _ = parse.parse_turns(text, tables)
    for e in events:
        r = _REPLACES.search(e["text"])
        e["supersedes"] = []
        if r:
            for x in _ID.findall(r.group(1)):
                (e["supersedes"].append(int(x)) if int(x) in named else e["unresolved"].append(f"取代 E{x}"))
            e["text"] = _REPLACES.sub("", e["text"]).strip()
    if stray:
        check["unresolved"] = stray
    missing = sorted(set(named) - set(check))
    if missing:
        check["missing"] = missing
    return events, check, not events and not m


def invalid_of(check):
    """核对里写了不成立的编号。"""
    return [k for k, v in check.items() if isinstance(k, int) and v["verdict"] == "不成立"]
