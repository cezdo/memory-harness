"""检索：一组查词 → 入口池里的候选（design/synapse/设计.md“检索”）。纯程序。

2026-10-18：form 挂线（第 ③ 步）查已有的文件夹和话题线，和对话里的 search 是同一类逻辑，代码放在 Synapse，form 调用它。
实验：tmp/retrieval-format/keys.py（索引词的匹配）、tmp/form-attach/retrieve.py（两路、大词、强弱）。

入口（entry）：{"name", "topic", "words": {"lists": [[叫法…], …], "constraints": [...]}, "stops": [停在…]}。
- 索引词（words）：一组是同一个东西的几种叫法；组之间默认没有约束。约束三种：order（同一个查词里按顺序出现）、
  adjacent（相邻且按顺序）、needs_other（不单独：同一个入口里还有别的组命中时才计分）。
- 两路，各取前几名再合并，不混成一个分数：
  索引词匹配：查词归一（小写、去空白）后逐字对上；索引词里没有的词按最长匹配切成索引词里有的片段（“丝印对比”→“丝印”）；
  BM25：名字、主题、索引词、停在拼成文档，分词用 ASCII 词加汉字二元组，不用外部分词器。语义向量还没接。
- 大词：一个词在查询方工作区出现在 BIG 以上比例的会话中（按库里全部 E 算），单独命中不计分（当作“不单独”）。
  查询时才算，不改写索引词；比例随库长大自己变。
- 强弱：大词一律不单独时还能命中的是强命中；只靠入口名字里的大词命中的是弱命中，只在没有任何强命中时给出，说明里标“（弱）”。
"""
import itertools
import math
import re
from collections import Counter, defaultdict

KEY_TOP = 6
BM25_TOP = 6
BIG = 0.2
UBIQUITOUS = 0.75     # 几乎处处都有的词（用户、要求、设计……）：BM25 不拿它查（2026-10-20，按真实查词定的初值：0.75 以上的都是泛词，
                      # 足迹 0.22、联想 0.27、Synapse 0.58 这类领域词留着）
K1, B = 1.5, 0.75


# ---- 索引词的匹配 ----

def norm(s):
    return re.sub(r"\s", "", s.lower())


def phrases(words, vocab=None):
    """查词 → 词组列表，每个词组是归一后的词序列。给了词表（全部索引词）时，词表里没有的词按最长匹配切开，
    只换成词表里有的片段（切不出就原样留着）。不是模糊匹配：片段必须逐字是词表里的词。
    空格不挡匹配（2026-10-18）：相邻几段连起来（不管中间几个空格）是词表里的词，再加一个只有这个词的词组，
    「X 升级」「X  升级」都命中索引词「X 升级」；原来按段切出的词组照旧留着。"""
    out = []
    for w in words:
        parts = [norm(t) for t in w.split() if t.strip()]
        ph = []
        for t in parts:
            if vocab is None or t in vocab:
                ph.append(t)
                continue
            i, pieces = 0, []
            while i < len(t):
                hit = next((t[i:j] for j in range(len(t), i + 1, -1) if t[i:j] in vocab), None)
                if hit:
                    pieces.append(hit)
                    i += len(hit)
                else:
                    i += 1
            ph += pieces or [t]
        out.append(ph)
        if vocab is not None:
            out += [[x] for x in {"".join(parts[a:b]) for a in range(len(parts)) for b in range(a + 2, len(parts) + 1)}
                    if x in vocab]
    return out


def _order(words, c, phs):
    sets = [{norm(a) for a in words["lists"][i]} for i in c["lists"]]
    for ph in phs:
        pos = 0
        for s in sets:
            while pos < len(ph) and ph[pos] not in s:
                pos += 1
            if pos == len(ph):
                break
            pos += 1
        else:
            return True
    return False


def _adjacent(words, c, phs):
    sets = [{norm(a) for a in words["lists"][i]} for i in c["lists"]]
    n = len(sets)
    return any(all(ph[p + k] in sets[k] for k in range(n)) for ph in phs for p in range(len(ph) - n + 1))


CHECKS = {"order": _order, "adjacent": _adjacent}


def score(words, phs):
    """(分数, 命中了哪些组的说明)。先算绑在一起的组（顺序、相邻）和自由的组，“不单独”的最后看有没有伴。分数 = 命中的组数。"""
    tokens = {t for ph in phs for t in ph}
    groups = [c for c in words["constraints"] if c["type"] != "needs_other"]
    lonely = {i for c in words["constraints"] if c["type"] == "needs_other" for i in c["lists"]}
    bound = {i for c in groups for i in c["lists"]}
    hit, why = 0, []
    for c in groups:
        if CHECKS[c["type"]](words, c, phs):
            hit += len(c["lists"])
            why.append((" ~ " if c["type"] == "adjacent" else " > ").join(words["lists"][i][0] for i in c["lists"]))
    later = []
    for i, items in enumerate(words["lists"]):
        if i in bound:
            continue
        got = [a for a in items if norm(a) in tokens]
        if not got:
            continue
        if i in lonely:
            later.append(got[0])
            continue
        hit += 1
        why.append(got[0])
    if hit:
        hit += len(later)
        why += [w + "（有伴）" for w in later]
    return hit, why


# ---- BM25 ----

def tokens(text):
    text = text.lower()
    out = re.findall(r"[a-z0-9][a-z0-9._\-]*", text)
    for run in re.findall(r"[一-鿿]+", text):
        out += [run[i:i + 2] for i in range(len(run) - 1)] or [run]
    return out


def doc(entry):
    return " ".join([entry["name"], entry.get("topic", "")] + [a for items in entry["words"]["lists"] for a in items]
                    + list(entry.get("stops", [])))


def bm25(query, pool):
    docs = {eid: tokens(doc(e)) for eid, e in pool.items()}
    n = len(docs)
    avg = sum(map(len, docs.values())) / max(1, n)
    df = Counter(t for d in docs.values() for t in set(d))
    q = set(tokens(query))
    out = {}
    for eid, d in docs.items():
        tf = Counter(d)
        s = sum(math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) * tf[t] * (K1 + 1) /
                (tf[t] + K1 * (1 - B + B * len(d) / avg)) for t in q if t in tf)
        if s > 0:
            out[eid] = s
    return out


# ---- 大词 ----

class Corpus:
    """查询方工作区里每个会话的全部 E（小写拼起来），算大词用。按库建一次，查询时复用。"""

    def __init__(self, conn):
        from ...engram.store import session_workspace          # 和会话入口的工作区同一种算法（含按会话的更正）
        cwd = {(r[0], r[1]): session_workspace(r[0], r[1], r[2]) for r in conn.execute("SELECT source, session, cwd FROM session_detail")}
        self.texts = defaultdict(list)
        for src, s, t in conn.execute("SELECT d.source, d.session, group_concat(e.text, ' ') FROM event e "
                                      "JOIN turn_detail d ON d.occurrence=e.turn GROUP BY d.source, d.session"):
            self.texts[cwd.get((src, s), "")].append((t or "").lower())
        self._big = {}

    def share(self, word, ws):
        """这个词在工作区 ws 里出现在多大比例的会话中。"""
        k = (norm(word), ws)
        if k not in self._big:
            texts = self.texts.get(ws) or []
            w = word.lower()
            pat = re.compile((r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])") if w.isascii() else re.escape(w))
            self._big[k] = sum(1 for t in texts if pat.search(t)) / len(texts) if texts else 0
        return self._big[k]


def with_big(entry, ws, corpus, exempt_name=True, hit=None):
    """入口的索引词加上大词的“不单独”（exempt_name：名字里有的词除外）。返回 (索引词, 标出来的大词)。
    hit：查词归一后的词集合。给了就只看查词碰得到的自由组：没碰到的组、绑在顺序 / 相邻里的组加不加“不单独”，score 的结果都一样，
    不用去算比例（算比例要扫一遍工作区全部 E，一千多个索引词逐个算要半分钟，2026-10-19）。"""
    words = entry["words"]
    if not ws or corpus is None:
        return words, []
    name = norm(entry["name"])
    out = {"lists": words["lists"], "constraints": list(words["constraints"])}
    marked = []
    lonely = {i for c in out["constraints"] if c["type"] == "needs_other" for i in c["lists"]}
    bound = {i for c in out["constraints"] if c["type"] != "needs_other" for i in c["lists"]}
    for i, items in enumerate(out["lists"]):
        if i in lonely or exempt_name and any(norm(a) in name for a in items):
            continue
        if hit is not None and (i in bound or not any(norm(a) in hit for a in items)):
            continue
        if any(corpus.share(a, ws) >= BIG for a in items):
            out["constraints"].append({"type": "needs_other", "lists": [i]})
            marked.append(items[0])
    return out, marked


def word_hits(words, pool, ws="", corpus=None):
    """只走索引词匹配：{入口: (分, 命中说明)}。强命中优先；没有强命中时才给弱命中（说明里标“（弱）”）。"""
    vocab = {norm(a) for e in pool.values() for items in e["words"]["lists"] for a in items}
    phs = phrases(words, vocab=vocab)
    hit = {t for ph in phs for t in ph}
    strong, loose = {}, {}
    for eid, e in pool.items():
        w, _ = with_big(e, ws, corpus, exempt_name=False, hit=hit)
        s, why = score(w, phs)
        if s:
            strong[eid] = (s, why)
            continue
        w, _ = with_big(e, ws, corpus, hit=hit)
        s, why = score(w, phs)
        if s:
            loose[eid] = (s, why + ["（弱）"])
    return strong or loose


def search(words, text, pool, ws="", corpus=None, deep=False):
    """words：查词；text：拿去做 BM25 的整段文字；ws：查询方的工作区；corpus：算大词用（Corpus，不给就不认大词）。
    返回 [(入口, {"words": 分, "why": …, "bm25": 分})]，索引词匹配前 KEY_TOP 个在前，BM25 前 BM25_TOP 个补在后面。"""
    by_words = word_hits(words, pool, ws, corpus)
    by_bm = bm25(text, pool)
    picked = sorted(by_words, key=lambda x: -by_words[x][0])[:KEY_TOP]
    picked += [x for x in sorted(by_bm, key=lambda x: -by_bm[x])[:BM25_TOP] if x not in picked]
    if deep:            # evoke 翻页（v0.14）：前面照旧，后面两路交替接着排，排到底
        seen = set(picked)
        rest = itertools.zip_longest(sorted(by_words, key=lambda x: -by_words[x][0]), sorted(by_bm, key=lambda x: -by_bm[x]))
        for x in itertools.chain.from_iterable(rest):
            if x is not None and x not in seen:
                seen.add(x)
                picked.append(x)
    return [(eid, {"words": by_words.get(eid, (0, []))[0], "why": by_words.get(eid, (0, []))[1],
                   "bm25": round(by_bm.get(eid, 0), 2)}) for eid in picked]
