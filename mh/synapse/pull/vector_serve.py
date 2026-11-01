"""检索面的向量一路：服务端（v0.33，2026-11-02：计算和 bge-m3 在一台机器上）。design/synapse/设计.md“检索”。

跑在提供向量服务的机器上（装在工具目录 `mh-tools/vec/`，venv 里有 numpy；见那里的 README），launchd 常驻。
客户端机器上的 Synapse 只经 HTTP 用它（pull/vector.py），自己不碰 numpy。存的是从陈述、话题算出来的索引，随时可以重算，不是 Claim。

- 条目：编号（`s:<陈述>`、`n:<话题或线>`）→ 文本的哈希 + 向量（bge-m3，1024 维，归一化）；文本由客户端给，这里只算向量；
- 在场：客户端每次读视图重建时报一次现在的条目，只在这些里面找（被取代的陈述、清空的线都不在场，向量留着）；
- 接口（POST JSON，带 `Authorization: Bearer <key>`，key 和本机 llama 服务同一个）：
  /missing {items: {编号: 哈希}} → {missing: [没有或哈希变了的]}；/upsert {items: [{id, hash, text}]} → {n}；
  /active {ids: [...]} → {active, unknown}；/search {text, k, prefix?} → {hits: [[编号, 分数], ...]}；GET /health。
用法：python vector_serve.py <数据目录> <端口> <key 文件> [向量服务地址]"""
import hashlib, json, os, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

DIM, CAP, BATCH = 1024, 480, 32      # bge-m3 单条最多 512 token（physical batch），中文约一字一 token，截到 480 字


class Store:
    def __init__(self, data, embed_url, key):
        self.dir, self.embed_url, self.key = Path(data), embed_url, key
        self.lock = threading.Lock()
        self.ids, self.hashes, self.mat, self.active = [], {}, np.zeros((0, DIM), np.float32), None
        idx, vec = self.dir / "index.json", self.dir / "vectors.npy"
        if idx.exists() and vec.exists():
            d = json.loads(idx.read_text())
            self.ids, self.hashes = d["ids"], d["hashes"]
            self.mat = np.load(vec).astype(np.float32)
        self.row = {k: i for i, k in enumerate(self.ids)}

    def save(self):
        tmp = self.dir / "vectors.tmp.npy"
        np.save(tmp, self.mat.astype(np.float16))
        os.replace(tmp, self.dir / "vectors.npy")
        (self.dir / "index.tmp.json").write_text(json.dumps({"ids": self.ids, "hashes": self.hashes}))
        os.replace(self.dir / "index.tmp.json", self.dir / "index.json")

    def embed(self, texts):
        out = []
        for i in range(0, len(texts), BATCH):
            body = json.dumps({"model": "bge-m3", "input": [t[:CAP] or "（空）" for t in texts[i:i + BATCH]]}, ensure_ascii=False).encode()
            req = urllib.request.Request(self.embed_url, body, {"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
            d = json.load(urllib.request.urlopen(req, timeout=300))
            out += [x["embedding"] for x in sorted(d["data"], key=lambda x: x["index"])]
        m = np.asarray(out, np.float32).reshape(-1, DIM)
        return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)

    def missing(self, items):
        with self.lock:
            return [k for k, h in items.items() if self.hashes.get(k) != h]

    def upsert(self, items):
        vecs = self.embed([it["text"] for it in items])
        with self.lock:
            new = []
            for it, v in zip(items, vecs):
                i = self.row.get(it["id"])
                if i is None:
                    self.row[it["id"]] = len(self.ids)
                    new.append(v)
                    self.ids.append(it["id"])
                else:
                    self.mat[i] = v
                self.hashes[it["id"]] = it["hash"]
            if new:
                self.mat = np.vstack([self.mat, np.asarray(new, np.float32)])     # 新条目要等下一次 /active 才在场
            self.save()
        return len(items)

    def set_active(self, ids):
        with self.lock:
            rows = [self.row[k] for k in ids if k in self.row]
            self.active = np.asarray(sorted(rows), np.int64)
            return len(rows), len(ids) - len(rows)

    def search(self, text, k, prefix=None):
        q = self.embed([text])[0]
        with self.lock:
            rows = self.active if self.active is not None else np.arange(len(self.ids))
            if prefix:
                rows = np.asarray([r for r in rows if self.ids[r].startswith(prefix)], np.int64)
            if not len(rows):
                return []
            sims = self.mat[rows] @ q
            top = np.argsort(-sims)[:k]
            return [[self.ids[rows[i]], round(float(sims[i]), 4)] for i in top]


def handler(store, key):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            return self.headers.get("Authorization") == f"Bearer {key}"

        def do_GET(self):
            if not self._authed():
                return self._send(401, {"error": "key"})
            if self.path == "/health":
                return self._send(200, {"n": len(store.ids), "active": None if store.active is None else len(store.active)})
            self._send(404, {"error": "path"})

        def do_POST(self):
            if not self._authed():
                return self._send(401, {"error": "key"})
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                t = time.time()
                if self.path == "/missing":
                    out = {"missing": store.missing(d["items"])}
                elif self.path == "/upsert":
                    out = {"n": store.upsert(d["items"])}
                elif self.path == "/active":
                    a, u = store.set_active(d["ids"])
                    out = {"active": a, "unknown": u}
                elif self.path == "/search":
                    out = {"hits": store.search(d["text"], int(d.get("k", 200)), d.get("prefix"))}
                else:
                    return self._send(404, {"error": "path"})
                out["sec"] = round(time.time() - t, 3)
                self._send(200, out)
            except Exception as e:                                  # noqa: BLE001
                self._send(500, {"error": repr(e)})

        def log_message(self, fmt, *args):
            sys.stderr.write(f"{time.strftime('%m-%d %H:%M:%S')} {self.command} {self.path} {args[1] if len(args) > 1 else ''}\n")
    return H


def main():
    data, port, keyfile = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    embed_url = sys.argv[4] if len(sys.argv) > 4 else "http://127.0.0.1:28081/v1/embeddings"
    key = Path(keyfile).read_text().strip()
    Path(data).mkdir(parents=True, exist_ok=True)
    store = Store(data, embed_url, key)
    print(f"向量服务：{len(store.ids)} 条，听 0.0.0.0:{port}", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), handler(store, key)).serve_forever()


if __name__ == "__main__":
    main()
