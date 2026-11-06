"""会话结构（D4，design/engram/form/设计.md“会话结构”，2026-11-05）：一个话题现在的陈述 → “问题—决定”的结构
（问题、方向、证据、判断、遗留和它们之间的边）。输入的排法、输出的解析和检查，纯程序；原型 tmp/session-structure/（v0.6）。

输入：话题的名字、主题、停在，记录里用到的词典词条，按对话先后排的陈述（陈述1、陈述2……，就是 relive 主列的顺序）。
输出：一个 JSON（nodes、edges、objects），节点的 from 是陈述编号。存在 act_detail（step = structure）的 output，
meta.items 是这次输入的陈述编号（form_record）按顺序排，Synapse 读的时候按它把编号换成句柄；陈述变了（重做、续上）就重做。
做哪些：现行 pack 写出的话题，和交互里 relive 过、还没有结构的旧话题（用户 11-05：全量补太贵，被读到的才补）。"""
import json
import math
import re

TYPES = {"Q": "问题", "D": "方向", "E": "证据", "J": "判断", "R": "遗留"}
EDGES = ("回应", "支持", "推翻", "接替", "引出")
PAGE = 8                 # 陈述超过这么多条（relive 一页，Synapse 的 SHOW）才做：一页以内 relive 直接给陈述，结构没有人读（2026-11-05）
SHORT = 10               # 陈述不超过这么多条的短对话给节点上限（v0.6：上限全给，78/100 正好写满；长对话不给数）


def block(entry, items, known=""):
    """entry：{"name", "topic", "stops": [文本…]}；items：[{"text"}]（按主列的顺序）；known：词典一节（glossary.text）。"""
    cap = f"这段对话 {len(items)} 条陈述，节点最多 {max(4, math.ceil(len(items) * 0.6))} 个。" if len(items) <= SHORT else ""
    lines = [f"话题：{entry['name']}", f"主题：{entry['topic']}",
             "停在：" + "；".join(entry["stops"]) if entry.get("stops") else "", known.strip(), cap, "", "陈述："]
    lines += [f"陈述{i}：{it['text']}" for i, it in enumerate(items, 1)]
    return "\n".join(x for x in lines if x)


def parse(output, n):
    """→ (结构, 问题)。结构：{"nodes", "edges", "objects"}，去掉 from 越界的编号、指向不存在节点的边；
    认不出 JSON、没有节点的返回 (None, 说明)。"""
    m = re.search(r"\{.*\}", output or "", re.S)
    if not m:
        return None, "没有 JSON"
    try:
        d = json.loads(m.group(0))
    except ValueError as e:
        return None, f"JSON 读不了：{e}"[:120]
    nodes = []
    for x in d.get("nodes") or []:
        if not isinstance(x, dict) or not str(x.get("id", ""))[:1] in TYPES or not x.get("text"):
            continue
        x["from"] = [i for i in x.get("from") or [] if isinstance(i, int) and 1 <= i <= n]
        for a in x.get("asked") or []:
            a["from"] = [i for i in a.get("from") or [] if isinstance(i, int) and 1 <= i <= n]
        nodes.append(x)
    if not nodes:
        return None, "没有节点"
    ids = {x["id"] for x in nodes}
    edges = [e for e in d.get("edges") or [] if isinstance(e, dict) and e.get("from") in ids and e.get("to") in ids and e.get("type") in EDGES]
    return {"nodes": nodes, "edges": edges, "objects": d.get("objects") or []}, None
