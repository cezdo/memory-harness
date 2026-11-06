"""检索面的向量一路：客户端（v0.33，2026-11-02；design/synapse/设计.md“检索”）。纯标准库：计算在服务端（vector_serve.py）。

- 条目：现在的陈述 `s:<编号>`，前面带线名、话题名、日期（contextual retrieval：陈述单看缺背景）；话题和线 `n:<编号>`，
  名字、主题、索引词、停在。哈希变了（改名、补了索引词）就重算。
- 同步（sync）：读视图重建以后第一次用到时做一次——问服务缺哪些，补上（evoke 里一次最多 SYNC_MAX 条，多了留给
  `python3 -m mh.synapse.pull.vector sync`），再报在场名单。
- 查（topics）：查词拼成一句给服务，拿回最像的条目，陈述归到它所在的话题，话题的分数取它自己和它下面陈述里最高的。
- 没配 `[vector]`、服务连不上、超时，一律返回 None，evoke 照旧只有字面一路。
- 只对正式库开（store.DEFAULT_DB）：测试的临时库、实验的库拷贝陈述编号和正式库同一套，同步上去会覆盖正式的向量、改乱在场名单。"""
import hashlib, json, sys, time, urllib.request
from pathlib import Path

from ...engram.store import instance
from . import retrieve

CONF = getattr(instance, "VECTOR", {}) or {}
URL = CONF.get("url", "").rstrip("/")
KEY_FILE = Path(str(CONF.get("key_file", ""))).expanduser() if CONF.get("key_file") else None
TIMEOUT, SYNC_MAX, K = 4, 300, 300
ENABLED = bool(URL and KEY_FILE)
_STATE = {"down_until": 0.0}


def _post(path, obj, timeout=TIMEOUT):
    key = KEY_FILE.read_text().strip()
    req = urllib.request.Request(URL + path, json.dumps(obj, ensure_ascii=False).encode(),
                                 {"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def texts(L):
    """编号 → 给向量的文本。陈述前面带“线名｜话题名｜日期”。"""
    out = {}
    for sid, s in L.stmts.items():
        n = L.nodes.get(s["entry"], {})
        line = L.nodes.get(n.get("parent"), {}) if n.get("parent") else {}
        pre = "｜".join(x for x in (line.get("name", ""), n.get("name", ""), (n.get("date") or "")[:10]) if x)
        out[f"s:{sid}"] = f"{pre}｜{s['text']}" if pre else s["text"]
    for nid, n in L.nodes.items():
        if n["kind"] in ("entry", "line") and not n.get("retired"):
            out[f"n:{nid}"] = retrieve.doc(n)
    return out


def _hash(t):
    return hashlib.sha1(t.encode()).hexdigest()[:12]


def sync(L, limit=SYNC_MAX, timeout=TIMEOUT, log=None):
    """补上缺的、报在场名单。返回 (补了几条, 还缺几条)；连不上抛异常。"""
    tx = texts(L)
    hashes = {k: _hash(t) for k, t in tx.items()}
    keys = list(hashes)
    miss = []
    for i in range(0, len(keys), 5000):
        miss += _post("/missing", {"items": {k: hashes[k] for k in keys[i:i + 5000]}}, timeout)["missing"]
    todo = miss if limit is None else miss[:limit]
    for i in range(0, len(todo), 256):
        part = todo[i:i + 256]
        _post("/upsert", {"items": [{"id": k, "hash": hashes[k], "text": tx[k]} for k in part]}, max(timeout, 300))
        if log:
            print(f"补了 {i + len(part)} / {len(todo)}", file=log, flush=True)
    _post("/active", {"ids": keys}, timeout)
    return len(todo), len(miss) - len(todo)


def _main_db(conn):
    from ...engram.store import DEFAULT_DB
    try:
        f = conn.execute("PRAGMA database_list").fetchone()[2]
        return bool(f) and Path(f).resolve() == Path(DEFAULT_DB).resolve()
    except Exception:                                               # noqa: BLE001
        return False


def _ready(L):
    if not ENABLED or time.time() < _STATE["down_until"] or not _main_db(L.conn):
        return False
    if getattr(L, "_vector_synced", False):
        return True
    try:
        sync(L)
        L._vector_synced = True
        return True
    except Exception as e:                                          # noqa: BLE001
        _down(e)
        return False


def _down(e):
    _STATE["down_until"] = time.time() + 300                         # 连不上就五分钟内不再试，evoke 不被拖慢
    print(f"[mh] 向量一路不可用，只用字面：{e!r}"[:300], file=sys.stderr)


def align(conn, clauses, items, timeout=TIMEOUT):
    """每个事件对到最像的分句（relive 的来源按分句排，2026-11-05）。→ [分句序号…]，用不了返回 None（照旧按 form 写下的顺序）。"""
    if not ENABLED or len(clauses) < 2 or not items or time.time() < _STATE["down_until"] or not _main_db(conn):
        return None
    try:
        best = _post("/align", {"clauses": clauses, "items": items}, timeout)["best"]
        return best if len(best) == len(items) else None
    except Exception as e:                                          # noqa: BLE001
        _down(e)
        return None


def search(conn, text, k=50, prefix="s:", timeout=1.5):
    """只查、不同步（联想的钩子用：每一轮都要等它，同步交给整理循环）。返回 [(编号, 分数)…]，用不了返回 None。"""
    if not ENABLED or not text.strip() or time.time() < _STATE["down_until"] or not _main_db(conn):
        return None
    try:
        return [(k_, s) for k_, s in _post("/search", {"text": text, "k": k, "prefix": prefix}, timeout)["hits"]]
    except Exception as e:                                          # noqa: BLE001
        _down(e)
        return None


def topics(L, text, kinds=("entry",)):
    """一句话 → 话题（或线）按向量相近排好的列表；用不了返回 None。"""
    if not text.strip() or not _ready(L):
        return None
    try:
        hits = _post("/search", {"text": text, "k": K})["hits"]
    except Exception as e:                                          # noqa: BLE001
        _down(e)
        return None
    best = {}
    for k, score in hits:
        kind, x = k.split(":", 1)
        t = (L.stmts[int(x)]["entry"] if int(x) in L.stmts else None) if kind == "s" and x.isdigit() else x
        if t in L.nodes and L.nodes[t]["kind"] in kinds and not L.nodes[t].get("retired") and score > best.get(t, -1):
            best[t] = score
    return sorted(best, key=lambda t: -best[t])


def main():
    """python3 -m mh.synapse.pull.vector sync：全量补齐（第一次建、换模型、改了前缀以后）。平时由整理循环每 10 分钟补一次。"""
    from ...engram import store
    from . import layers
    if sys.argv[1:] != ["sync"]:
        print(main.__doc__)
        return
    if not ENABLED:
        print("实例配置里没有 [vector]（url、key_file）")
        return
    conn = store.connect(readonly=True)
    if not _main_db(conn):
        print("只对正式库同步")
        return
    L = layers(conn)
    t = time.time()
    done, left = sync(L, limit=None, timeout=60, log=sys.stdout)
    print(f"补了 {done} 条，用时 {time.time() - t:.0f} 秒")


if __name__ == "__main__":
    main()
