"""② label：会话记录 → 会话入口的名字、停在、索引词（design/engram/form/设计.md“② label”）。输入的排法、解析和程序补词，纯程序。

模型写索引词一组一行“- 词｜词”（不用行内符号）；程序再补：
- 记录里出现、模型没写的「」词（中文的要在至少两条陈述里出现）和 ASCII 名字（三个字符以上、不是已有词的一部分）；
  结构标记（『引用：』『文件：』，旧写法「引用：」「文件：」）、带标点或超过十字的原话、纯数字日期、单字不补；
- 词典里同一个东西的别的叫法（单字母不补）。
补的词是缓存：每次从模型的原始输出和会话记录重算，不存库。实验：tmp/form-attach/run.py（key-v0.5，第七版的补词规则）。
"""
import re

from . import glossary
from ...synapse.pull import retrieve
from ...synapse.pull.retrieve import norm

SIMILAR_TOP = 6          # 起名时最多附几个相近的会话入口（名字、日期、主题）


def record_text(name, topic, items):
    head = f"名字：{name}\n主题：{topic}"
    return head + "\n\n" + "\n".join(f"陈述{it['n']}：{it['text']}" for it in items)


def similar(name, others, ws, corpus=None):
    """和这次相近的已有会话入口（同一工作区）：拿 pack 写的名字去查 Synapse 的索引词，只要命中的（强命中优先，没有才给弱命中），
    BM25 不补：不相关的不推送（2026-10-18：一次性推送全部名字是 form 在给自己加工作量）。"""
    if not others or not name:
        return []
    hits = retrieve.word_hits([name], others, ws, corpus)
    return sorted(hits, key=lambda x: -hits[x][0])[:SIMILAR_TOP]


def existing_text(others, near):
    """起名时附上的相近会话入口；没有命中的就什么都不附。"""
    if not near:
        return ""
    return "\n".join(["\n\n## 相近的会话入口", ""] + [f"- {others[i]['name']}（{others[i]['date'][:10]}）：{others[i]['topic']}" for i in near])


def previous_text(name, topic, stops, lists, kind):
    """这段会话原来的会话入口（续上、重新整理时附上）。"""
    how = "这次的会话记录是后来续上的部分" if kind == "续上" else "补进晚到的事件以后，整段重新整理了一遍"
    return "\n".join(["\n\n## 这段会话原来的会话入口", "", f"（{kind}：{how}）", f"名字：{name}", f"主题：{topic}"]
                     + [f"停在：{s}" for s in stops] + ["索引词："] + ["- " + "｜".join(g) for g in lists])


def merge(old, new):
    """续上时索引词和原来的合并：新的一组和原来某组有同一个叫法（归一后），就并进那一组；没有就加在后面。"""
    out = [list(g) for g in old]
    for g in new:
        hit = next((o for o in out if {norm(a) for a in o} & {norm(a) for a in g}), None)
        if hit is None:
            out.append(list(g))
        else:
            hit += [a for a in g if norm(a) not in {norm(b) for b in hit}]
    return out


def dedup(name, taken, date):
    """程序查重：和已有的名字（归一后）一样，就在后面加日期，还一样再加时刻。返回 (名字, 撞上的那个名字或 None)。"""
    have = {norm(n): n for n in taken}
    if norm(name) not in have:
        return name, None
    hit = have[norm(name)]
    for suffix in (date[5:10], date[5:16].replace("T", " ")):
        cand = f"{name}（{suffix}）"
        if suffix and norm(cand) not in have:
            return cand, hit
    return f"{name}（{date}）", hit


def parse(output):
    """→ (名字, 停在 [(一句, [陈述编号…])], 模型写的索引词 [[叫法…], …])。"""
    m = re.search(r"^名字[:：]\s*(.+)$", output, re.M)
    name = m.group(1).strip() if m else ""
    stops = []
    for m in re.finditer(r"^停在[:：]\s*(.+)$", output, re.M):
        s = m.group(1).strip()
        basis = re.search(r"[（(]([^（）()]*陈述[^（）()]*)[)）]\s*$", s)
        nums = [int(x) for x in re.findall(r"陈述\s*(\d+)", basis.group(1))] if basis else []
        stops.append((s[:basis.start()].strip() if basis else s, nums))
    lists, on = [], False
    for line in output.splitlines():
        t = line.strip()
        if re.match(r"^(索引词|key)[:：]\s*$", t, re.I):
            on = True
            continue
        if on and t.startswith("- "):
            items = [a.strip() for a in t[2:].split("｜") if a.strip()]
            if items:
                lists.append(items)
        elif on and t:
            on = False
    return name, stops, lists


def terms(items, lists):
    """程序补的词（第七版）：会话记录里出现、模型没写的「」词和 ASCII 名字。"""
    have = {norm(a) for g in lists for a in g}
    seen, quoted = [], {}
    for it in items:
        for t in set(re.findall(r"「([^」]+)」", it["text"])):
            quoted[t] = quoted.get(t, 0) + 1
    for it in items:
        found = [t for t in re.findall(r"「([^」]+)」", it["text"])
                 if not t.startswith(("引用：", "文件：")) and len(t) <= 10 and not re.search(r"[，、。；：！？,]", t)]
        found += re.findall(r"(?<![A-Za-z0-9_./~-])(?:--)?[A-Za-z][A-Za-z0-9_.\-/]*[A-Za-z0-9]", it["text"])
        for t in found:
            n = norm(t)
            if n in have or t in seen or re.fullmatch(r"[\d.\-:/ ]+", t):
                continue
            if not t.isascii() and quoted.get(t, 0) < 2:
                continue                     # 中文的「」词要在至少两条陈述里出现（原话碎片不补）
            if len(n) < 2 or t.isascii() and (len(n) < 3 or any(n in h for h in have | {norm(x) for x in seen})):
                continue                     # ASCII 碎片：太短（AI、GB），或是已有词的一部分（append only 里的 append）
            seen.append(t)
    return seen


def expand(lists):
    """按词典补别名：组里的词是词典里某个东西的叫法，就把它的其他叫法补进这一组；单个字母不补。返回新的列表。"""
    out = []
    for items in lists:
        items = list(items)
        have = {norm(a) for a in items}
        for a in list(items):
            for b in glossary.aliases(a):
                if len(norm(b)) >= 2 and norm(b) not in have:
                    items.append(b)
                    have.add(norm(b))
        out.append(items)
    return out


def words(model_lists, items):
    """会话入口的全部索引词：模型写的 + 程序补的词，再按词典补别名。{"lists", "constraints", "origin"}。"""
    extra = terms(items, model_lists)
    lists = [list(g) for g in model_lists] + [[t] for t in extra]
    return {"lists": expand(lists), "constraints": [], "origin": ["model"] * len(model_lists) + ["program"] * len(extra)}


def show(words, sep="\n"):
    """索引词给人和模型看：一组一行“- 词｜词”。"""
    return sep.join("- " + "｜".join(items) for items in words["lists"])
