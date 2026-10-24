"""联想（UserPromptSubmit；送达时的展示名 souhatsu，2026-10-22）：用户说一句话，程序找出相关的记忆，只是线索。重构 E1a（2026-10-19：联想和拉取同时换库）起在新库上：
候选是现在的陈述（L3:S1）和旧系统导入的 J，分组的主题名换成话题线的名字，足迹是 use（channel = push，mode = associate）。
送达照旧暂停（DELIVER 为空，只记录），档位、门槛、词段的做法不变：纯程序的部分（词性名词段、回想说法、日常词、组合与名单变体、
读宿主上下文）从旧的 mh/synapse/associate.py 搬到这里（旧库上的做法 E4 删了，来历见 git 历史里那个文件开头的说明）；E2（2026-10-22）
读宿主上下文、认宿主注入的部分搬进 hosts/。宿主的联想钩子直接调 `python3 -m mh.synapse.push.associate`。
"""
import hashlib
import json
import math
import os
import pickle
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from ...engram.store import HOME
from ...engram.store.instance import QUIET, USER_ALIASES, USER_NAME

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
_CONCEPT = re.compile(r"「([^」]+)」")
CONCEPT_WEIGHT = 3.0
K1, B = 1.5, 0.75


PHRASE_DF, COMMON_MSGS, SHOW_MAX, HEAD = 20, 10, 5, 40
GROUPS = 5                 # 线索式联想最多列几天（另加最早的一天）
DELIVER = set()                        # 送达的档（2026-10-15 定为 {"专名","强回想","组合"}；2026-10-16 清空：推送暂停，改走纯 get/search）
SHADOW_TIERS = {"专名", "强回想", "组合"}  # 影子里仍按这套档位算「本来会推什么」，与 10-15/10-16 已送达的那批同一口径
# 2026-10-09 起一直只记录、不推送（SHADOW）；10-15 起按档送达，送达和只记录的写在同一条足迹里，标档位，两边一直能对照；
# 10-16 起 DELIVER 清空 → 一律只记录：识别、门槛、候选、展示的 K 照旧落足迹和推送流水，只是不再注入会话
CUE_DF = {"强": 60, "弱": 40}     # 有回想的说法时，名词段的稀有门槛放宽到多少（没有时是 PHRASE_DF）
CUES = {"强": ("来着", "还记得", "记得吗", "我记得", "当时", "那时候", "那会儿", "上次", "上回", "以前", "印象里", "印象中",
               "没记错", "怎么来的", "为什么决定"),
        "弱": ("之前", "是什么", "是指什么", "指的是", "是谁", "什么意思", "怎么回事")}
NOUN = {"n", "nh", "ni", "nl", "ns", "nz", "j", "ws"}      # LTP 的名词类：普通、人名、机构、处所、地名、其他专名、简称、外文
PROPER = NOUN - {"n", "nl"}                               # 专名：单独一个也可以触发（nl 处所名词“心里”“附近”不算）
STOP_POS = {"u", "p", "c", "d", "r", "m", "q", "wp", "e", "o", "x", "g", "nd"}   # 不进名词段的：助词、介词、连词、副词、代词、数词、量词、标点、方位词……
DETERMINERS = {"所有", "全部", "一些", "各种", "其他", "别的", "每个"}
NAMES = set(USER_ALIASES)                           # 用户的称呼几乎出现在每条认识里（实例配置）
CUT_FREQ = 1000            # 切坏的词：跨过它边界的常用词至少这么常见（高通 1.7 万、二字、错的；初值，按出错的案例调）
RARE_FREQ = 60000          # 单独一个词时，雾凇拼音词频不低于它的是日常词（时候 50 万、情况 50 万、门槛 17 万），不单独触发
LEXICON = os.environ.get("MH_LEXICON", "/usr/local/mh-tools/lexicon")
COVER, COVER_N = 0.6, 6    # 认识正文的 6 字片段有六成以上在上下文里逐字出现过，算已经在上下文里
PROMPT_KEEP = 500          # 足迹里留原话的前多少字
_CJK = re.compile(r"[㐀-鿿]+")
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f]")   # 平假名、片假名（含半角）：LTP 是中文模型，日语标不准



def _norm(s):
    return re.sub(r"\s+", "", s).lower()


def lexicon():
    """词 → 日常词频（雾凇拼音 base、ext），专名（THUOCL 地名、历史名人）记 0。读不到就是空表（退回不分词的做法）。"""
    fq = {}
    try:
        for name in ("base", "ext"):
            with open(f"{LEXICON}/rime-ice/cn_dicts/{name}.dict.yaml", encoding="utf-8") as f:
                body = False
                for line in f:
                    if not body:
                        body = line.startswith("...")
                        continue
                    p = line.rstrip("\n").split("\t")
                    if len(p) >= 3 and p[2].isdigit() and 2 <= len(p[0]) <= 6:
                        fq[p[0]] = max(fq.get(p[0], 0), int(p[2]))
        for name in ("diming", "lishimingren"):
            with open(f"{LEXICON}/thuocl/data/THUOCL_{name}.txt", encoding="utf-8-sig") as f:
                for line in f.read().splitlines():
                    w = line.split("\t")[0].strip()
                    for v in (w, w[:-1] if w[-1:] in "市省县区" else ""):   # 地名表收的是“哈尔滨市”，不是“哈尔滨”
                        if 2 <= len(v) <= 6:
                            fq[v] = 0
    except OSError:
        pass
    return fq


def cue(text):
    """句子里的回想说法 → (强/弱/None, 命中的说法)。"""
    for level in ("强", "弱"):
        got = [c for c in CUES[level] if c in text]
        if got:
            return level, got
    return None, []


def _text(seg):
    s = ""
    for w, *_ in seg:      # 中文之间不加空格，拉丁串之间加
        s += (" " if s and s[-1].isascii() and s[-1].isalnum() and w[:1].isascii() and w[:1].isalnum() else "") + w
    return s


def _long_enough(s):
    cjk = sum(len(r) for r in _CJK.findall(s))
    latin = re.sub(r"[^A-Za-z0-9]", "", s)
    return cjk >= 2 or len(latin) >= 3 or (len(latin) >= 2 and re.search(r"\d", latin) and re.search(r"[A-Za-z]", latin))


def docs_of(data, s):
    """词段逐字出现在哪些认识里（先用索引的特征求交，再逐字核对正文与来源）。"""
    feats = [t for t in terms(s) if not t.startswith("「")]
    if not feats:
        return set()
    post = data["idx"].post
    got = None
    for t in sorted(feats, key=lambda t: len(post.get(t, ()))):
        got = set(post.get(t, ())) if got is None else got & set(post.get(t, ()))
        if not got:
            return set()
    key = _norm(s)
    return {k for k in got if key in data["body"].get(k, "")}


def phrases(toks):
    """定中短语（依存句法）：一串连着的词，除最后一个外都以 ATT/ADV 挂在串里靠右的词上（“会话→记录”“简→模式”
    “决定性→瞬间”），动宾（“判断 内容”）、主谓（“工具 试试”）不算。里面要有名词，不含功能词、代词、数量词、
    限定词和用户的称呼。返回所有 (起, 止)，连同后缀（“dsh极简模式”也给出“简模式”“模式”，按认识里有没有再取）。"""
    ok = [p not in STOP_POS and w not in DETERMINERS and not any(n in w for n in NAMES) for w, p, _, _ in toks]
    out = set()
    for j in range(len(toks)):
        if not ok[j]:
            continue
        i = j
        while i > 0 and ok[i - 1] and toks[i - 1][3] in ("ATT", "ADV") and i <= toks[i - 1][2] - 1 <= j:
            i -= 1
        for a in range(i, j + 1):
            if any(p in NOUN for _, p, _, _ in toks[a:j + 1]):
                out.add((a, j + 1))
    return out


def spans(data, toks, text):
    """名词段 → [{t, pos, df, cue, ok, why}]，和每个通过门槛的段的认识集合。
    从最长的定中短语开始取，在认识里逐字出现过才算，互不重叠。门槛：单独一个日常词不算；出现在不超过 PHRASE_DF 条认识里
    （有回想的说法、或“那张”“那个”这样的远指挨着时放宽到 CUE_DF）；用户在 COMMON_MSGS 条以上的消息里说过的不算（有回想的说法时算）。"""
    level, said = cue(text)
    distal = [i for i, (w, p, _, _) in enumerate(toks) if p == "r" and w.startswith("那")]
    cands = [(i, j, _text(toks[i:j])) for i, j in phrases(toks)]
    cands = [c for c in cands if _long_enough(c[2])]
    cands.sort(key=lambda x: (-len(_norm(x[2])), x[0]))
    used, out, hits, docs = set(), [], {}, {}
    for i, j, s in cands:
        if used & set(range(i, j)):
            continue
        w, p = toks[i][0], toks[i][1]
        if j - i == 1 and p not in PROPER and data["lex"].get(w, 0) >= RARE_FREQ:
            d = docs_of(data, s)               # 日常词不单独触发，也不占位置；条数留给组合变体缩小交集
            out.append({"t": s, "pos": p, "df": len(d), "cue": None, "ok": False, "why": "日常词"})
            docs[s] = d
            continue
        d = docs_of(data, s)
        docs[s] = d
        if not d:
            continue
        used |= set(range(i, j))
        near = any(i - 3 < x < i or j <= x < j + 2 for x in distal)       # “哈尔滨那张照片”：那 在段前两个词以内或紧跟在后
        lv = "强" if near else level
        limit = CUE_DF[lv] if lv else PHRASE_DF
        common = sum(s in m for m in data["msgs"])
        why = ("太常见（%d 条认识，门槛 %d）" % (len(d), limit) if len(d) > limit else
               f"{USER_NAME}常说（%d 条消息）" % common if common >= COMMON_MSGS and not lv else "")
        daily = all(data["lex"].get(x[0], 0) >= RARE_FREQ for x in toks[i:j])
        alone = len(d) <= PHRASE_DF and common < COMMON_MSGS        # 没有回想的说法也能过门槛
        tier = (("强回想" if lv == "强" else "弱回想") if not alone else "日常" if daily else "专名") if not why else None
        out.append({"t": s, "pos": "/".join(p for _, p, _, _ in toks[i:j]), "df": len(d), "cue": lv, "ok": not why,
                    "why": why, "all_daily": daily, "tier": tier})
        # all_daily：每个词都是日常词（“组合能力”“处理方向”，也有“交接文档”“观察任务”）。先只记，按数据再定（2026-10-10）
        if not why:
            hits[s] = d
    return out, hits, {"level": level, "said": said, "distal": [toks[x][0] for x in distal]}, docs


def combo(terms, docs, limit):
    """组合变体：单个都太常见的词段（机器名 191、vps 327）放在一起取交集（机器名+vps+代理 18）。
    从条数少的“太常见”词段开始交，还多于门槛就再交日常词；交集落在 1..limit 才算。返回 (用到的词段, 认识集合) 或 None。"""
    common = sorted({t["t"] for t in terms if t["why"].startswith("太常见")}, key=lambda t: len(docs[t]))
    daily = sorted({t["t"] for t in terms if t["why"] == "日常词" and docs.get(t["t"])}, key=lambda t: len(docs[t]))
    if not common or len(common) + len(daily) < 2:
        return None
    used, got = [], None
    for t in common + daily:
        nxt = docs[t] if got is None else got & docs[t]
        if not nxt:
            continue                       # 交空了就跳过这个词
        used, got = used + [t], nxt
        if len(used) >= 2 and len(got) <= limit:
            return used, got
    return None


def listed(data, text):
    """名单变体：原话里出现的旧节点名（英文按整词，中文按片段），不设条数门槛。返回 {名字: 认识集合}。"""
    low, out = text.lower(), {}
    for n in data["names"]:
        if (re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", low) if n.isascii() else n in text):
            d = docs_of(data, n)
            if d:
                out[n] = d
    return out


def in_context(ctx_grams, text):
    """正文的 6 字片段六成以上在上下文里逐字出现过。只出现编号不算：一串成员编号打印进上下文时，模型并不知道它们说了什么
    （2026-10-10 一次 Atlas 查询把上千个编号带进了会话，机器名+vps+代理的 18 条全被当成看过）。"""
    t = _norm(text)
    grams = {t[i:i + COVER_N] for i in range(len(t) - COVER_N + 1)}
    return bool(grams) and len(grams & ctx_grams) / len(grams) >= COVER


def _around(text, words, before=12, width=HEAD):
    """词段第一次出现的位置附近的一段（正文里没有就取开头）：比开头更能看出这条为什么被带上来。"""
    low = text.lower()
    at = min((low.find(w.lower()) for w in words if w.lower() in low), default=-1)
    if at < 0:
        return text[:width] + ("……" if len(text) > width else "")
    a = max(0, at - before)
    return ("……" if a else "") + text[a:a + width] + ("……" if a + width < len(text) else "")



def terms(text):
    """文本 → 特征计数（和旧的 mh/index.py 同一种：汉字二元组、ASCII 词、「」概念），词段先用它在倒排表里求交。"""
    out = Counter()
    for run in _CJK.findall(text):
        out.update(run[i:i + 2] for i in range(len(run) - 1)) if len(run) > 1 else out.update([run])
    out.update(w.lower() for w in _WORD.findall(text) if len(w) > 1)
    out.update("「" + c + "」" for c in _CONCEPT.findall(text))
    return out


# ---- 新库上的联想（E1a）----

CACHE_V = 4                # 缓存格式；改了缓存里放的东西就加一（2：加上查不到的词要用的陈述和节点正文；3：加上常用词；4：旧 J 退役，不再收）
NAMES_FILE = HOME / "names.json"   # 名单变体：旧记忆系统迁来的节点名（用户整理过），换库时从旧库冻结下来；是用户的数据，放数据目录（重构 H）


class Docs:
    """联想的文档：现在的陈述（旧 J 2026-10-23 退役，不再收）。键：陈述是 ("s", form_record 编号)。
    k[键] = {text, anchor, holder, workspace, topic, h}；倒排表 post 和打分 match 照旧索引（BM25，「」概念加权）。"""

    def __init__(self, L):
        self.k = {}
        for sid, s in L.stmts.items():
            e = L.nodes[s["entry"]]
            self.k[("s", sid)] = {"text": s["text"], "anchor": e["date"], "holder": "", "workspace": e["ws"],
                                  "topic": L.nodes[e["parent"]]["name"] if e["parent"] else e["name"], "h": s["h"]}
        self.post, self.len = defaultdict(dict), {}
        for key, d in self.k.items():
            tf = terms(d["text"])
            self.len[key] = sum(tf.values())
            for t, n in tf.items():
                self.post[t][key] = n
        self.avg = sum(self.len.values()) / max(1, len(self.len))

    def idf(self, t):
        n = len(self.post.get(t, ()))
        return math.log(1 + (len(self.len) - n + 0.5) / (n + 0.5))

    def match(self, text):
        scores = defaultdict(float)
        for t in terms(text):
            w = self.idf(t) * (CONCEPT_WEIGHT if t.startswith("「") else 1)
            for key, tf in self.post.get(t, {}).items():
                scores[key] += w * tf * (K1 + 1) / (tf + K1 * (1 - B + B * self.len[key] / self.avg))
        return dict(scores)


def cached(conn):
    """联想用的数据，按（陈述、挂线的条数和最大编号）缓存在库文件旁边：库里只多了观察、没多陈述时不重建。"""
    home = Path(conn.execute("PRAGMA database_list").fetchone()[2]).parent
    from ..pull import layers
    sig = conn.execute("SELECT (SELECT count(*) || ':' || ifnull(max(id), 0) FROM form_record WHERE kind='session'), "
                       "(SELECT count(*) || ':' || ifnull(max(id), 0) FROM attachment), "
                       "(SELECT count(*) FROM session_entry)").fetchone()
    key = hashlib.sha256(f"{CACHE_V}|{'|'.join(map(str, sig))}".encode()).hexdigest()[:16]
    folder = home / "assoc-cache-engram"
    path = folder / f"{key}.pkl"
    if path.exists():
        with open(path, "rb") as f:
            return pickle.load(f)
    idx = Docs(layers(conn))
    msgs = [u for (u,) in conn.execute("SELECT user FROM turn_text WHERE length(user) < 2000")]
    body = {k: _norm(d["text"]) for k, d in idx.k.items()}
    lex = {w: f for w, f in lexicon().items() if all(w[i:i + 2] in idx.post for i in range(len(w) - 1))}
    try:
        names = json.loads(NAMES_FILE.read_text())
    except OSError:
        names = []
    L = layers(conn)
    stmts = "\x00".join(b for k, b in body.items() if k[0] == "s")          # 现在的陈述：get 取得到的 S
    nodes = "\x00".join(_norm(" ".join([n.get("name", ""), n.get("topic", "")] + [a for g in n["words"]["lists"] for a in g]))
                        for n in L.nodes.values())                         # 话题、线、骨架的名字、主题、索引词：search 碰得到的节点
    common = {w for w, f in lexicon().items() if f >= RARE_FREQ}            # 常用词：查不到也不列（2026-10-20：按词典过滤）
    data = {"idx": idx, "msgs": msgs, "body": body, "lex": lex, "names": names, "stmts": stmts, "nodes": nodes, "common": common}
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.pkl"):
        old.unlink()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "wb") as f:
        pickle.dump(data, f)
    tmp.replace(path)
    return data


TAG = "memory-harness"    # 对话中途注入的内容用 <memory-harness>…</memory-harness> 包起来，和开场、拉取同一个标签；core 的图例说明它不是用户说的话（2026-10-24，原来是 <Synapse>）


def recall_note(lost):
    """查不到的词送出去的那一行 → (给模型的，给用户看的)。"""
    line = "".join(f"「{w}」" for w in lost) + "没印象，记忆里还没有，当新的看。"     # 2026-10-22 从候选里挑的
    return f"<{TAG}>{line}</{TAG}>", f"memory-harness · 新词：{'、'.join(lost)}"
_CODEY = re.compile(r"[=(){}<>|$;]|^\s{2,}|^\S+:\s")       # 贴进来的命令、代码、日志行：不是用户自己写的话


def known(words, folder=None):
    """这些词在通用词库（lexicon()）里的词频 → {词: 词频}，没有的不列。全表 86 万词、读一次 1.4 秒，钩子每轮都跑，
    所以在缓存目录建一份 SQLite（词库文件变了才重建），每次只查要用的几个词。"""
    import sqlite3
    words = [w for w in dict.fromkeys(words) if 2 <= len(w) <= 6]
    if not words:
        return {}
    files = [Path(f"{LEXICON}/rime-ice/cn_dicts/{n}.dict.yaml") for n in ("base", "ext")] + \
            [Path(f"{LEXICON}/thuocl/data/THUOCL_{n}.txt") for n in ("diming", "lishimingren")]
    sig = hashlib.sha256("|".join(f"{f}:{f.stat().st_mtime_ns}" for f in files if f.exists()).encode()).hexdigest()[:12]
    folder = Path(folder or HOME / "assoc-cache-engram")
    path = folder / f"lexicon-{sig}.sqlite"
    if not path.exists():
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        c = sqlite3.connect(tmp)
        c.execute("CREATE TABLE w(word TEXT PRIMARY KEY, freq INTEGER) WITHOUT ROWID")
        c.executemany("INSERT INTO w VALUES(?,?)", lexicon().items())
        c.commit()
        c.close()
        os.replace(tmp, path)
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return dict(c.execute(f"SELECT word, freq FROM w WHERE word IN ({','.join('?' * len(words))})", words).fetchall())
    finally:
        c.close()


def _cut(text, w, lex):
    """w 在原句里是不是切坏的：一个常用词只有一个字伸进 w（“高|通滤波器”），或者 w 只是一个更长的常用词的一段（“特事特办”）。
    lex：候选片段 → 词频。"""
    i = text.find(w)
    if i < 0:
        return False
    j = i + len(w)
    for a, b in _cut_spans(text, i, j):
        if lex.get(text[a:b], 0) >= CUT_FREQ:
            return True
    return False


def _cut_spans(text, i, j):
    """跨过 [i, j) 边界、可能说明切坏了的片段：左边伸进一个字、右边伸进一个字、整个包住 w 的更长的词（最长 6 个字）。"""
    out = [(a, i + 1) for a in range(max(0, i - 4), i)] + [(j - 1, b) for b in range(j + 1, min(len(text), j + 4) + 1)]
    out += [(a, b) for a in range(max(0, j - 6), i + 1) for b in range(j, min(len(text), a + 6) + 1) if (a, b) != (i, j)]
    return [(a, b) for a, b in out if 2 <= b - a <= 6 and _CJK.fullmatch(text[a:b])]


def missing(data, toks, text, lookup=known):
    """查不到的词（2026-10-20）：这句话里的名词（只看 LTP 标成名词的词，仓库名这类英文专名也在里面），在现在的陈述正文里、
    话题 / 线 / 骨架的名字、主题、索引词里都逐字找不到的，也就是 search 碰不到、get 取不到 S 的。只在 E 或旧 J 里出现过的算查不到。
    英文和汉字粘在一起的词（“ds问”）拆开分别看，一个汉字的不算；词典里的常用词不列；贴进来的代码、日志行不看。
    日语不交给 LTP（2026-10-22）：假名比汉字多的行整行不看，带假名的词不列。
    通用词汇不列（2026-10-22）：普通名词在通用词库里有的不列，切坏的（_cut）不列，实例配置的手动名单 QUIET 不列。
    lookup：词 → 词频 的查表（默认 known，测试换成字典）。→ [词……]，按在句子里出现的先后。"""
    mine = "\n".join(l for l in text.splitlines()
                     if not _CODEY.search(l) and len(_KANA.findall(l)) <= sum(map(len, _CJK.findall(l))))
    words, plain = [], set()
    for w, p, _, _ in toks:
        if p not in NOUN or w not in mine or _KANA.search(w) or any(n in w for n in NAMES):
            continue
        parts = re.findall(r"[A-Za-z][A-Za-z0-9_.\-]*|[㐀-鿿]+", w) if re.search(r"[A-Za-z]", w) and _CJK.search(w) else [w]
        words += [x for x in parts if len(_norm(x)) >= 2]
        if p in ("n", "nl"):
            plain.add(w)
    common = data.get("common", ())
    out = []
    for w in dict.fromkeys(words):
        k = _norm(w)
        if k and w not in common and k not in QUIET and k not in data["stmts"] and k not in data["nodes"] and k not in [_norm(x) for x in out]:
            out.append(w)
    if not out:
        return out
    cjk = [w for w in out if _CJK.fullmatch(w)]
    lex = lookup([*cjk, *(mine[a:b] for w in cjk if (i := mine.find(w)) >= 0 for a, b in _cut_spans(mine, i, i + len(w)))])
    return [w for w in out if not (w in plain and w in lex) and not (w in cjk and _cut(mine, w, lex))]


def clues(idx, cand, words, groups=GROUPS):
    """线索式的联想（2026-10-10）：按日期分组、最近的在前，每组带话题线的名字、句柄和一条的开头；最早的一组单独列出。"""
    from ..pull import VERSION
    by = {}
    for k in cand:
        by.setdefault((idx.k[k]["anchor"] or "")[:10], []).append(k)
    days = sorted(by, reverse=True)
    pick = days[:groups] + ([days[-1]] if len(days) > groups else [])
    lines = [f"souhatsu（联想，Synapse {VERSION}，程序按{USER_NAME}这句话里的「{'」「'.join(words[:5])}」找出，只是线索，不一定相关）："
             f"{len(cand)} 条，{len(days)} 天，最近的在前："]
    for d in pick:
        ks = by[d]
        tops = list(dict.fromkeys(idx.k[k]["topic"] for k in ks if idx.k[k]["topic"]))
        head = _around(idx.k[ks[0]]["text"], words)
        lines.append(f"- {'最早 ' if d == days[-1] and len(days) > groups else ''}{d[5:]}"
                     f"{' · ' + '、'.join(tops[:2]) if tops else ''} · {' '.join(idx.k[k]['h'] for k in ks[:3])}"
                     f"{f' 等 {len(ks)} 条' if len(ks) > 3 else ''}：{head}")
    if len(days) > len(pick):
        lines.append(f"（中间还有 {len(days) - len(pick)} 天没列出）")
    lines.append("不相关就不用管；要看前后文：`mcp__mh__relive <句柄> [句柄……]`，或 `mcp__mh__evoke <词>`。")
    return "\n".join(lines)


def _seen(conn, session):
    """同一段对话里已经给过的：拉取取到的、推送送达的（只记录的联想不算，模型没看到）。"""
    out = set()
    if not session:
        return out
    for inputs, mode, query in conn.execute(
            "SELECT o.inputs, d.mode, d.query FROM use_detail d JOIN occurrence o ON o.id=d.occurrence "
            "WHERE d.session=? AND d.purpose IN ('interactive', 'prefetch')", (session,)):
        if mode == "associate" and json.loads(query).get("associate", {}).get("shadow", True):
            continue
        ins = json.loads(inputs)
        out |= {("j", x) for x in ins.get("judgment", [])} | {("s", x) for x in ins.get("form_record", [])}
    return out


def _sent_words(conn, session):
    """会话里实际注入过的词，包含旧版每轮送出的 missing；影子候选不算。"""
    if not session:
        return set()
    out = set()
    for (query,) in conn.execute("SELECT query FROM use_detail WHERE session=? AND channel='push' "
                                 "AND mode='associate'", (session,)):
        rec = json.loads(query).get("associate", {})
        words = rec.get("sent_words")
        if words is None:
            words = list(rec.get("missing", []))
            if not rec.get("shadow", True):
                words += rec.get("used", [])
        out.update(_norm(w) for w in words)
    return out


def associate(conn, prompt, session="", cwd="", transcript=None, host="claude", host_kind=None):
    """返回 (给模型的附加上下文, 给用户看的一行)；没命中返回 None。每一轮都留足迹（一条 use）。"""
    from .. import hosts, track
    from ..hosts import claude, codex, dsh_log
    from ..track import pushes
    from . import pos
    from ..pull import VERSION
    from ...engram.store import workspace
    rec = {"prompt": (prompt or "")[:PROMPT_KEEP], "shadow": True, "skip": None, "harness": host,
           "sent_words": []}

    def done(skip, shown=(), **kw):
        rec.update(kw, skip=skip)
        hs = [idx_k[k]["h"] for k in shown] if shown else []
        track.use(conn, VERSION, "associate", {"associate": rec, "omitted": kw.get("omitted", 0)}, kw.get("text") or "",
                  {"judgment": [k[1] for k in shown if k[0] == "j"], "form_record": [k[1] for k in shown if k[0] == "s"]},
                  session=session, purpose="prefetch", cwd=cwd, channel="push", meta={"shown": hs, "host": host})
        if rec.get("candidates") is not None:
            try:      # 流水出了任何错都不影响这一轮（pushes.py 的纪律）
                tier = rec.get("tier") or []
                delivered = (not rec.get("shadow")) and bool(shown)
                gone = (rec.get("n_in_context") or 0) + (rec.get("seen") or 0)
                pushes.note("associate", host, session, cwd, delivered,
                            summary=(f"souhatsu：{'、'.join(hs)}（{'、'.join(tier)}）" if shown
                                     else f"souhatsu：{gone} 条候选都已在上下文里或这段对话里出现过"),
                            version=VERSION, tier=tier, used=rec.get("used"),
                            terms=[{k: t.get(k) for k in ("t", "df", "tier")} for t in rec.get("terms") or []],
                            shown=hs, candidates=rec.get("n_candidates"), in_context=rec.get("n_in_context"),
                            seen=rec.get("seen"), omitted=kw.get("omitted", 0), workspace=rec.get("workspace"),
                            skipped=skip, text=kw.get("text") or kw.get("text_list"))
            except Exception:
                pass
        return (note, shown_line) if note else None

    idx_k = {}
    note = shown_line = None
    text = (prompt or "").strip()
    if not text:
        return done("空")
    if hosts.COMMAND.match(text):
        return done("命令", host=hosts.host_event(text, "斜杠命令" if text.startswith("/") else "shell"))
    if host_kind:
        return done("宿主注入", host=hosts.host_event(text, host_kind))
    for prefix, kind in hosts.HOST:
        if text.startswith(prefix):
            return done("宿主注入", host=hosts.host_event(text, kind))
    if host == "dsh":
        ctx, injected, found = dsh_log.read_context(session, text)
    elif host == "codex":
        ctx, injected, found = codex.read_context(transcript, text)
    else:
        ctx, injected, found = claude.read_context(transcript, text) if transcript else ("", None, False)
    rec["in_transcript"] = found
    if injected:
        return done("宿主注入", host=hosts.host_event(text, injected))
    toks = pos.tag([text])
    if toks is None:
        return done("词性未就绪")
    data = cached(conn)
    idx = data["idx"]
    idx_k.update(idx.k)
    lost = missing(data, toks[0], text) if "stmts" in data else []
    sent = _sent_words(conn, session)
    fresh = [w for w in lost if _norm(w) not in sent]
    note, shown_line = recall_note(fresh) if fresh else (None, None)
    rec["missing"] = lost
    rec["missing_sent"] = fresh
    rec["sent_words"] = list(fresh)
    found_terms, hits, cues, docs = spans(data, toks[0], text)
    rec.update(terms=found_terms, cue=cues)
    variants = {}
    c = combo(found_terms, docs, CUE_DF[cues["level"]] if cues["level"] else PHRASE_DF)
    if c:
        variants["组合"] = ({"+".join(c[0]): c[1]}, {"terms": c[0], "df": len(c[1])})
    named = listed(data, text)
    if named:
        variants["名单"] = (named, {"terms": sorted(named), "df": {n: len(d) for n, d in named.items()}})
    if not hits and not variants:
        return done("没有词段")
    seen = _seen(conn, session)
    score = idx.match(text)
    nc = _norm(ctx)
    grams = {nc[i:i + COVER_N] for i in range(len(nc) - COVER_N + 1)} if nc else set()
    here = workspace(cwd) if cwd else None

    def same(cand):
        return [idx.k[k]["h"] for k in cand[:20] if here and idx.k[k]["workspace"] == here]

    def pick(hit):
        allk = set().union(*hit.values())
        own = {k: sum(_norm(t) in _norm(idx.k[k]["text"]) for t in hit) for k in allk}
        pool = sorted(allk - seen, key=lambda k: (-own[k], -score.get(k, 0), idx.k[k]["anchor"] or ""), reverse=False)
        cand, there = [], []
        for k in pool:
            (there if grams and in_context(grams, idx.k[k]["text"]) else cand).append(k)
        return cand, there, len(allk & seen)

    def count(cand):
        return min(SHOW_MAX, max(3, math.ceil(len(cand) / 3)))

    def hs(ks):
        return [idx.k[k]["h"] for k in ks]

    if variants:
        rec["variants"] = {}
        for name, (hit, info) in variants.items():
            cand, there, nseen = pick(hit)
            rec["variants"][name] = {**info, "shown": hs(cand[:count(cand)]), "candidates": hs(cand[:20]),
                                     "in_context": hs(there[:20]), "seen": nseen, "same_workspace": same(cand)}
    send = {t["t"]: hits[t["t"]] for t in found_terms if t["ok"] and t.get("tier") in SHADOW_TIERS}
    if send:
        use, words = send, [t["t"] for t in found_terms if t["t"] in send]
        tier = [t["tier"] for t in found_terms if t["t"] in send]
    elif "组合" in SHADOW_TIERS and "组合" in variants:
        use, words, tier = variants["组合"][0], variants["组合"][1]["terms"], ["组合"]
    elif hits:
        use, words = hits, [t["t"] for t in found_terms if t["ok"]]
        tier = [t["tier"] for t in found_terms if t["ok"]]
    else:
        return done("没有词段")
    tier = list(dict.fromkeys(tier))
    delivered = any(x in DELIVER for x in tier)
    if delivered:
        words = [w for w in words if _norm(w) not in sent]
        if not words:
            return done("同词已送达")
        use = {w: ks for w, ks in use.items() if _norm(w) not in sent}
    rec.update(tier=tier, shadow=not delivered)
    cand, there, nseen = pick(use)
    rec.update(candidates=hs(cand[:20]), in_context=hs(there[:20]), seen=nseen, workspace=here, same_workspace=same(cand),
               n_candidates=len(cand), n_in_context=len(there), used=words)
    if not cand:
        return done("都已出现过")
    n = count(cand)
    shown, rest = cand[:n], len(cand) - n
    first = idx.k[shown[0]]
    listing = [f"souhatsu（联想，程序按{USER_NAME}这句话里的「{'」「'.join(words[:5])}」找出，只是线索）：",
               f"- [{first['h']}]（{first['anchor'][:10]}{'，' + first['holder'] if first['holder'] else ''}）{first['text']}"]
    listing += [f"- [{idx.k[k]['h']}]（{idx.k[k]['anchor'][:10]}）{idx.k[k]['text'][:HEAD]}……" for k in shown[1:]]
    out = clues(idx, cand, words)
    if delivered:
        rec["sent_words"] += words
    done(None, shown, text=out, text_list="\n".join(listing), omitted=max(rest, 0))
    msg = f"souhatsu：{'、'.join(hs(shown))}（{'、'.join(words[:3])}；{'、'.join(tier)}）"
    ctx = "\n\n".join(x for x in (note, out if delivered else None) if x) or None
    return ctx, (msg if delivered else msg + "（只记录，未推送）") + (f"；{shown_line}" if shown_line else "")


def hook(stdin=sys.stdin, stdout=sys.stdout):
    """UserPromptSubmit：读宿主 JSON，命中就输出附加上下文和一行显示；没命中或出错都什么也不输出（出错记进足迹）。"""
    from .. import library
    d = {}
    conn = None
    try:
        d = json.load(stdin)
        conn = library()
        got = associate(conn, d.get("prompt", ""), d.get("session_id", ""), d.get("cwd", ""),
                        d.get("transcript_path"), d.get("host") or "claude", d.get("host_kind"))
        if got:
            ctx, msg = got
            out = {"systemMessage": msg}
            if ctx:
                out["hookSpecificOutput"] = {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}
            json.dump(out, stdout, ensure_ascii=False)
    except Exception as e:
        try:
            from .. import track
            from ..pull import VERSION
            conn = conn or library()
            track.use(conn, VERSION, "associate", {"associate": {"prompt": str(d.get("prompt", ""))[:PROMPT_KEEP],
                                                                 "skip": "出错", "error": repr(e)}}, "", {},
                      session=d.get("session_id", ""), purpose="prefetch", cwd=d.get("cwd", ""), channel="push")
        except Exception:
            pass


if __name__ == "__main__":
    from mh.synapse.push import associate      # 按包路径调：缓存里 pickle 的类要记成包里的，不能记成 __main__
    associate.hook()
