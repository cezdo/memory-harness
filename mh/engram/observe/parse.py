"""解析观察者写下的事件：正文、说话人、行为（mode）、限定、来源。纯程序；解析不了的照实记下，不丢。"""
import re

from ..store import NA, UNKNOWN


def _label(raw):
    """引用名归一：旧写法“引用结构N”，新写法“引用：有意义的概括”（2026-10-06 起）。"""
    raw = re.sub(r"\s+", "", raw) if raw.startswith("引用结构") else raw
    return re.sub(r"^引用\s*[:：]\s*", "引用：", raw).strip()


def _is_ref(name):
    return bool(re.fullmatch(r"引用结构\d+", name) or name.startswith("引用：") or re.match(r"引用\s*[:：]", name))


_SOURCE = re.compile(r"^\s*#*\s*来源\s*[:：]?\s*(.*)$", re.M)
_REF = re.compile(r"([UAFRS])(\d+)(?:\s*[-–~至到]\s*[UAFRS]?(\d+))?")


def refs(spec, table):
    """把“A3、A5-A9”或“F4-F9”这类编号解析成表里的键；表里没有的记为 missing。"""
    found, missing = [], []
    for kind, start, end in _REF.findall(spec):
        for n in range(int(start), int(end or start) + 1):
            key = f"{kind}{n}"
            (found if key in table else missing).append(key)
    return found, missing

_EVENT = re.compile(r"^#{2,4}\s*(?:事件|陈述)\s*(\d+)\s*$", re.M)
_FIELD = re.compile(r"^\s*(说话人|行为|限定|来源)\s*[:：]\s*(.*?)\s*$", re.M)
FIELDS = {"说话人": "speaker", "行为": "mode", "限定": "qualifier", "来源": "source"}
NONE_WORDS = ("无", "没有", "（无）", "-", "—")


def _value(field, raw):
    raw = raw.strip().strip("。")
    if not raw:
        return UNKNOWN
    if field == "qualifier" and raw in NONE_WORDS:
        return NA                    # 原话里没有限定：这一项不适用
    if field in ("mode", "speaker") and raw in ("未知", "不清楚", "分不清"):
        return UNKNOWN
    return raw


def parse(output, table):
    """返回 (事件列表, 是否明确写了“无”)。每个事件：text、speaker、mode、qualifier、segments（解析到的段落编号）、
    unresolved（写了却解析不到的编号）。没写的项记为 UNKNOWN。"""
    text = output.strip()
    if not _EVENT.search(text):
        return [], text.strip("。. \n") in ("无", "")
    parts = _EVENT.split(text)[1:]
    events = []
    for _, body in zip(parts[::2], parts[1::2]):
        fields = {f: UNKNOWN for f in FIELDS.values()}
        for name, raw in _FIELD.findall(body):
            fields[FIELDS[name]] = _value(FIELDS[name], raw)
        main = _FIELD.sub("", body).strip()
        spec = "" if fields["source"] == UNKNOWN else fields["source"]
        found, missing = refs(spec, table)
        events.append({"text": main, "speaker": fields["speaker"], "mode": fields["mode"],
                       "qualifier": fields["qualifier"], "segments": list(dict.fromkeys(found)),
                       "unresolved": missing + ([] if spec else ["（没写来源）"])})
    return events, False


_SOURCE = re.compile(r"来源\s*[:：]\s*(.*)$", re.M)       # 和正文写在同一行也认
_LABEL = re.compile(r"(\d+)\s*[:：]\s*([UA])\s*(\d+)(?:\s*[-–~至到]\s*(?:(\d+)\s*[:：]\s*)?([UA])?\s*(\d+))?")


def parse_turns(output, tables):
    """一轮或几轮一起的观察：tables = {轮号: 这一轮的段落表}。返回 (事件列表, 是否明确写了“无”)。每个事件：text、
    turn（来源里第一个解析到的轮号；都没解析到为 None）、segments（“轮:段落”）、unresolved。
    说话人、行为、限定不让模型写，读的时候由 modes.read 从正文读出。"""
    text = output.strip()
    if not _EVENT.search(text):
        return [], text.strip("。. \n") in ("无", "")
    parts = _EVENT.split(text)[1:]
    events = []
    for _, body in zip(parts[::2], parts[1::2]):
        m = _SOURCE.search(body)
        spec = m.group(1) if m else ""
        found, missing = [], []
        for n, kind, a, n2, kind2, b in _LABEL.findall(spec):
            if n2 and n2 != n:                     # 跨轮的区间（3:A5-4:A2）：两头各算一个
                pairs = [(n, kind, a, a), (n2, kind2 or kind, b, b)]
            else:
                pairs = [(n, kind, a, b or a)]
            for turn, k, lo, hi in pairs:
                for i in range(int(lo), int(hi) + 1):
                    label = f"{k}{i}"
                    (found if label in tables.get(int(turn), {}) else missing).append(f"{turn}:{label}")
        events.append({"text": _SOURCE.sub("", body).strip(), "turn": int(found[0].split(":")[0]) if found else None,
                       "segments": list(dict.fromkeys(found)), "unresolved": missing + ([] if spec else ["（没写来源）"])})
    return events, False
