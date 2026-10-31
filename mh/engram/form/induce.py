"""归纳：一段会话的陈述 → 陈述之上的陈述（design/engram/form/设计.md“归纳”，G6，2026-10-29）。输入的排法、输出的解析、
检查和程序补的数字，纯程序。

输入：pack 写出的陈述（修订提示改写过的用现在的那条），每条标编号和它在对话里的轮次。
输出：“### 归纳N”，一句话；“来源：陈述…”“转折：陈述…”。现在只归纳排查。
被归纳的陈述降半级：不在会话记录的主列里，句柄不变、可以直达（column）。轮数、用时、转折次数由程序算，补在句末（tail）。
"""
import re
from datetime import datetime

_BLOCK = re.compile(r"^###\s*归纳\s*(\d+)\s*$(.*?)(?=^###\s|\Z)", re.M | re.S)
LIMIT = 8                # 一条归纳通常不超过几条陈述（同 pack.LIMIT：一页读得完）
HARD = 16                # 超过就拆；LIMIT 到 HARD 之间要是连着的一段


def block(items):
    """items：[{"n", "text", "ords"}]，ords 是这条陈述的来源出自哪几轮（ordinal，从 0 起）。"""
    out = []
    for it in items:
        o = it["ords"]
        where = (f"第 {o[0] + 1} 轮" if o[0] == o[-1] else f"第 {o[0] + 1}–{o[-1] + 1} 轮") if o else "轮次未知"
        out.append(f"### 陈述{it['n']}（{where}）\n{it['text']}")
    return "\n\n".join(out)


def parse(text):
    """→ [{"n", "text", "from": [陈述序号], "pivots": [陈述序号]}]；只写“无”的是空的。"""
    got = []
    for m in _BLOCK.finditer(text or ""):
        body = m.group(2)

        def nums(k):
            line = re.search(rf"^{k}[:：]\s*(.*)$", body, re.M)
            return [int(x) for x in re.findall(r"陈述\s*(\d+)", line.group(1))] if line else []
        words = [l.strip() for l in body.strip().splitlines() if l.strip() and not re.match(r"^(来源|转折)[:：]", l.strip())]
        got.append({"n": int(m.group(1)), "text": " ".join(words), "from": nums("来源"), "pivots": nums("转折")})
    return got


def check(found, known):
    """不合规的归纳：来源不存在、转折不在来源里、超过 HARD、LIMIT 以上不连着、一条陈述被归纳两次、没有来源。
    known：这次输入的陈述序号。→ [(归纳序号, 说明)]。不合规的那条不入库（只丢那一条）。"""
    out, seen = [], {}
    for g in found:
        k = g["from"]
        if not k:
            out.append((g["n"], "没有来源"))
        elif any(n not in known for n in k):
            out.append((g["n"], "来源有不存在的陈述"))
        elif len(k) > HARD:
            out.append((g["n"], f"来源 {len(k)} 条，超过 {HARD} 条"))
        elif len(k) > LIMIT and sorted(k) != list(range(min(k), max(k) + 1)):
            out.append((g["n"], f"来源 {len(k)} 条，不是连着的一段"))
        elif any(p not in k for p in g["pivots"]):
            out.append((g["n"], "转折不在来源里"))
        elif any(n in seen for n in k):
            out.append((g["n"], f"陈述{next(n for n in k if n in seen)} 已经归纳进归纳{seen[next(n for n in k if n in seen)]}"))
        else:
            for n in k:
                seen[n] = g["n"]
    return out


def measure(ords, starts, end):
    """ords：被归纳的陈述出自的轮次；starts：{ordinal: 开始时刻 ISO}；end：会话结束的时刻（最后一轮之后没有下一轮时用）。
    → {"turns", "minutes"}：跨几轮，用时 = 第一轮开始到最后一轮的下一轮开始（包括等用户的时间）。"""
    ords = sorted(set(ords))
    if not ords:
        return {"turns": 0, "minutes": None}
    a, b = starts.get(ords[0]), starts.get(ords[-1] + 1) or end
    try:
        iso = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
        minutes = max(0, round((iso(b) - iso(a)).total_seconds() / 60))
    except (TypeError, ValueError, AttributeError):
        minutes = None
    return {"turns": len(ords), "minutes": minutes}


def tail(m, pivots):
    """补在句末的数字：（排查 N 轮、约 M 分钟、转折 K 次）。"""
    parts = [f"排查 {m['turns']} 轮"] + ([f"约 {m['minutes']} 分钟"] if m.get("minutes") is not None else []) + [f"转折 {pivots} 次"]
    return "（" + "、".join(parts) + "）"


def column(items, induced):
    """会话记录的主列：items 是 pack 的陈述 [{"id", …}]（按先后），induced 是归纳 [{"id", "text", "from": [陈述 id]}]。
    被归纳的陈述去掉，归纳放在它第一条的位置。→ (主列, {被归纳的陈述 id: 归纳 id})。
    修订提示改写过的陈述（id 换了，was 是原来那条）两个编号都认。"""
    marks = {x: g["id"] for g in induced for x in g["from"]}
    hit = lambda it: marks.get(it["id"]) or marks.get(it.get("was"))
    first = {}
    for g in induced:
        pos = [i for i, it in enumerate(items) if hit(it) == g["id"]]
        if pos:
            first[min(pos)] = g
    out, under = [], {}
    for i, it in enumerate(items):
        if i in first:
            g = first[i]
            out.append({"n": None, "text": g["text"], "id": g["id"], "was": g["id"], "from": g["from"]})
        if hit(it):
            under[it["id"]] = hit(it)
        else:
            out.append(it)
    return out, under
