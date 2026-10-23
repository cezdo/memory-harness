"""预览：观察之前先看一遍用户的话，把轮次分组（每组一轮或多轮）、定读法（2026-10-17，实验 tmp/observe-e3/、tmp/observe-e4/）。

- 只给用户的话和程序统计的每轮“我”做了什么（回复多长、过程中的话多长、用了哪些工具），不给回复本身。
- 每组写类型、判断、关注、问题和读法：精读、粗读、不读三选一，按这几轮的内容以后是不是只有记忆能想起来。
- 精读是方向上的约束（2026-10-19）：大致占这次预览两三成的轮、两到四成的回复字数，先看轮、再看字数；没验证过，不是指标。
  程序只兜底：轮数超过 PRECISE_TURNS、或回复字数超过 PRECISE_SHARE 的，按预览给的“精读顺序”从后往前改成粗读，放得下的小组照样放；
  前 PRECISE_FREE 轮不受这两条限制（短窗口里分量重的一两轮照样精读）。
- 怎么分组由预览按需要定；每组一个观察对象，程序不把几组合并成一次观察。
- 每组以“### 第 a–b 轮”开头（一轮写“### 第 a 轮”）；精读顺序也写轮次。
- 后台通知、压缩总结不是用户的输入，不界定轮（2026-10-20）：库里照旧各算一轮，预览里接在前一个真实的轮下面
  （“连同第 6–7 轮”），不单独分组；解析时跟着那一轮所在的组走，预览单独写了也并回去，读的时候一起读（FOLD）。
- 宿主消息的两条线（2026-10-20，案例：Sol 把汇报实验结果的通知轮判了不读）：宿主消息后回复不超过 HOST_SKIP 字的轮，
  默认不显示：预览里不列、模型不判，程序判不读，读的时候也不带上（hidden）；一组里有宿主消息、回复超过 HOST_FLOOR 字的，
  至少粗读（预览可以往上提，不能判不读）。
"""
import collections
import json
import re

from ..invoke import Claude, Codex
from . import label
from .label import relayed

# 预览的 Operator 定义；Sol 经隔离的 codex exec，low，工具调用结果作废。
MODELS = {"opus": lambda: Claude("claude-opus-5-5", "claude -p（opus）"),
          "sonnet": lambda: Claude("claude-sonnet-5-5", "claude -p（sonnet）", effort="low", isolation="claude-home-v1-low"),
          "sol": lambda: Codex("gpt-6.1-sol", "low", isolation="codex-home-v1-low", no_tools=True)}

PRECISE_TURNS = 0.3          # 精读的轮数上限（占这次预览的轮数）：兜底，方向是两三成（2026-10-19，原来只按字数卡一半）
PRECISE_SHARE = 0.4          # 精读的回复字数上限（占这次预览的全部回复）：兜底，方向是两到四成
PRECISE_FREE = 2             # 精读的前这么多轮不受上面两条限制
USER_CAP = 1200              # 一轮用户的话最多给这么多字
FOLD = ("notification", "compaction")    # 宿主注入里不界定轮的：接在前一轮下面
HOST_SKIP = 100              # 宿主消息后的回复不超过这么多字：只是进度、等待、重试（全库 179 条通知里 101 条在这以下）
HOST_FLOOR = 1000            # 宿主消息后的回复超过这么多字：多是带判断的汇报，至少粗读
RANGE = r"第\s*(\d+)\s*(?:[-–—~至到]\s*(\d+))?\s*轮"
GROUP = re.compile(r"^#{2,4}\s*" + RANGE + r"\s*$", re.M)
HOW = re.compile(r"读法\s*[:：]\s*\**\s*(精读|粗读|不读)")
TYPE = re.compile(r"类型\s*[:：]\s*(.*)")
ORDER = re.compile(r"^#{2,4}\s*精读顺序\s*$", re.M)


def reply_chars(t):
    return len(t["assistant"]) + sum(len(x) for x in json.loads(t["commentary"]))


def _field(t, k):
    try:
        return t[k]
    except (KeyError, IndexError):            # 库里的行（sqlite3.Row）没有这一列时是 IndexError
        return None


def folds(turns):
    """{接在前面的轮号: 它跟着的真实轮号}：后台通知、压缩总结（FOLD）接在这次预览里前一个不是它们的轮下面；
    这次预览的第一轮前面没有可接的，照旧单独一轮。"""
    out, host = {}, None
    for t in turns:
        k = t["ordinal"] + 1
        if _field(t, "origin") == "host" and _field(t, "host_kind") in FOLD and host is not None:
            out[k] = host
        else:
            host = k
    return out


def _stat(t):
    tools = collections.Counter(json.loads(t["tools"]))
    proc = sum(len(x) for x in json.loads(t["commentary"]))
    stat = f"回复 {len(t['assistant'])} 字，过程中的话 {proc} 字" + (
        "，工具 " + "、".join(f"{k}×{v}" for k, v in tools.most_common(6)) if tools else "，没用工具")
    return stat + ("" if t["closed"] else "，没有正常结束")


def hidden(turns):
    """默认不显示的轮号：宿主消息后“我”的回复不超过 HOST_SKIP 字（只是进度、等待、重试）。"""
    return {t["ordinal"] + 1 for t in turns if _field(t, "origin") == "host" and len(t["assistant"]) <= HOST_SKIP}


def fold_line(t):
    """接在前一轮下面的一条：宿主转来了什么（通知只给摘要，压缩总结不给正文），我接着做了什么。"""
    what = ("上下文压缩，宿主插入了之前的“我”写的总结（内容略）" if t["host_kind"] == "compaction"
            else "宿主转来" + label.user_text(t["user"])[0])
    return f"（第 {t['ordinal'] + 1} 轮：{what}；我接着{_stat(t)}）"


def turn_line(t, folded=()):
    """一轮在预览里的样子：用户的话（转来的长文本只给开头）和“我”这一轮的统计；folded：接在它下面的通知、压缩。"""
    user = label.user_text(t["user"])[0]
    tag = {"host": f"（宿主：{t['host_kind']}）", "script": "（脚本发起）"}.get(t["origin"], "")
    relay = relayed(t["user"])
    if relay is not None:
        user = (relay + "\n" if relay else "") + f"【转来的文本，{len(user)} 字，开头：{user[len(relay):len(relay) + 150].strip()}……】"
    elif len(user) > USER_CAP:
        user = user[:USER_CAP] + f"……（共 {len(user)} 字，后面略）"
    head = f"## 第 {t['ordinal'] + 1} 轮{tag}"
    if folded:
        a, b = folded[0]["ordinal"] + 1, folded[-1]["ordinal"] + 1
        head += f"（连同第 {a} 轮）" if a == b else f"（连同第 {a}–{b} 轮）"
    return "\n".join([head, f"{label.USER_NAME}：{user}", f"我这一轮：{_stat(t)}", *map(fold_line, folded)])


JUDGE = re.compile(r"判断\s*[:：]\s*(.*)")


def previous_line(group):
    """之前预览过的一组，给续上的预览看：轮次、类型、读法和判断，不给原话。"""
    j = JUDGE.search(group.get("plan", ""))
    head = f"- 第 {group['from']}–{group['to']} 轮：{group.get('type') or '（没写类型）'}，{group['how']}"
    return head + (f"。判断：{j.group(1).strip()}" if j else "")


def build(head, env, turns, earlier, previous=()):
    """预览的输入。turns 是这次要预览的轮次；earlier：前面已经处理过几轮（会话进行中分几次预览）；
    previous：这个会话之前预览定下的分组（续上时给 Opus 接着划分，前面的原话不再给）。"""
    first = turns[0]["ordinal"] + 1
    note = f"，这次是第 {first}–{turns[-1]['ordinal'] + 1} 轮" + (f"（前面 {earlier} 轮已经处理过）" if earlier else "")
    parts = [head, env + note]
    if previous:
        parts.append("## 之前的预览\n" + "\n".join(previous_line(s) for s in previous))
    f, hid = folds(turns), hidden(turns)
    under = collections.defaultdict(list)
    for t in turns:
        if t["ordinal"] + 1 in f and t["ordinal"] + 1 not in hid:
            under[f[t["ordinal"] + 1]].append(t)
    return "\n\n".join(parts + [turn_line(t, under[t["ordinal"] + 1]) for t in turns
                                 if t["ordinal"] + 1 not in f and t["ordinal"] + 1 not in hid])


def parse(output, turns):
    """解析预览：[{from, to, type, plan, asked, how, chars}] 和 {total, precise, order, missing, outside}。
    每组的起止按会话里的轮号（第几轮）；超出这次预览范围的轮次不算；没写读法的按粗读；精读超出上限的改粗读。"""
    hid = hidden(turns)                # 默认不显示的轮：不占字数，模型分的组读的时候也不带上
    size = {t["ordinal"] + 1: (0 if t["ordinal"] + 1 in hid else reply_chars(t)) for t in turns}
    parts = ORDER.split(output, maxsplit=1)
    body, order_text = parts[0], (parts[1] if len(parts) > 1 else "")
    pieces = GROUP.split(body)
    groups, outside = [], []
    written = {}                       # 组 → 预览写的起始轮（精读顺序按它对）
    for i in range(1, len(pieces), 3):
        a, b, text = int(pieces[i]), int(pieces[i + 1] or pieces[i]), pieces[i + 2]
        keep = [k for k in range(a, b + 1) if k in size]
        outside += [k for k in range(a, b + 1) if k not in size]
        if not keep:
            continue
        m = HOW.search(text)
        t = TYPE.search(text)
        how = m.group(1) if m else "粗读"
        groups.append({"from": keep[0], "to": keep[-1], "type": t.group(1).strip() if t else "",
                       "plan": (HOW.split(text)[0] if m else text).strip(), "asked": how, "how": how,
                       "chars": sum(size[k] for k in keep)})
        written[id(groups[-1])] = a
    f = folds(turns)                   # 接在前一轮下面的通知、压缩：跟着那一轮所在的组走，预览单独写了的也并回去
    for s in list(groups):
        p = next((g for g in groups if s["from"] in f and g["from"] <= f[s["from"]] <= g["to"]), None)
        while p is not None and s["from"] <= s["to"] and s["from"] in f and p["to"] + 1 == s["from"]:
            p["to"] = s["from"]
            p["chars"] += size[s["from"]]
            s["chars"] -= size[s["from"]]
            s["from"] += 1
        if s["from"] > s["to"]:
            groups.remove(s)
    written_turns = {k for s in groups for k in range(s["from"], s["to"] + 1)}
    for s in groups:
        while s["to"] + 1 in f and s["to"] + 1 not in written_turns and s["from"] <= f[s["to"] + 1] <= s["to"]:
            s["to"] += 1
            s["chars"] += size[s["to"]]
    host = {t["ordinal"] + 1: len(t["assistant"]) for t in turns if _field(t, "origin") == "host"}
    for s in list(groups):
        s["hidden"] = [k for k in range(s["from"], s["to"] + 1) if k in hid]
        if len(s["hidden"]) == s["to"] - s["from"] + 1:
            groups.remove(s)           # 范围里全是不显示的轮：没有要读的
        elif s["how"] == "不读" and any(host.get(k, 0) > HOST_FLOOR for k in range(s["from"], s["to"] + 1)):
            s["how"] = "粗读"          # 先于精读兜底
    for k in sorted(hid):              # 不显示的轮：程序判不读，一轮一组（连着的合成一组）
        if groups and groups[-1].get("program") and groups[-1]["to"] == k - 1:
            groups[-1]["to"] = k
        else:
            groups.append({"from": k, "to": k, "type": "宿主消息", "plan": f"程序：宿主消息后的回复不超过 {HOST_SKIP} 字，不显示、不读",
                           "asked": "不读", "how": "不读", "chars": 0, "program": True, "hidden": []})
            written[id(groups[-1])] = k
    groups.sort(key=lambda s: (s["from"], bool(s.get("program"))))
    total = sum(size.values())
    order = [int(m[0]) for m in re.findall(RANGE, order_text)]
    rank = lambda s: order.index(written[id(s)]) if written[id(s)] in order else len(order) + s["from"]
    precise = sorted((s for s in groups if s["how"] == "精读"), key=rank)
    used = used_turns = 0
    for s in precise:
        n = s["to"] - s["from"] + 1 - len(s["hidden"])
        if used_turns + n <= PRECISE_FREE or (used_turns + n <= PRECISE_TURNS * (len(size) - len(hid)) and used + s["chars"] <= PRECISE_SHARE * total):
            used, used_turns = used + s["chars"], used_turns + n
        else:
            s["how"] = "粗读"
    covered = {k for s in groups for k in range(s["from"], s["to"] + 1)}
    return groups, {"total": total, "precise": used, "precise_turns": used_turns, "order": order,
                  "missing": [k for k in size if k not in covered], "outside": sorted(set(outside))}
