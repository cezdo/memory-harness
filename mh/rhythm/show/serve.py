"""常驻服务：打开时现编页面（约 0.5 秒，PAGE_TTL 内复用），额度在后台每 QUOTA_EVERY 秒查一次、机器每 MEASURE_EVERY 秒量一次，写缓存。
只听实例配置 [show].listen，只放 [show].allow 里的来源进来；登录由前面的 nginx 用 auth_request 管。只读。"""
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ...engram.store import HOME
from ...engram.store.instance import SHOW
from . import page
from .collect import REPO
from .measure import MEASURE_EVERY, save

PAGE_TTL, QUOTA_EVERY = 60, 600


def quota_loop(log=sys.stderr):
    while True:
        try:
            out = subprocess.run([sys.executable, "-m", "mh", "quota"], cwd=REPO, capture_output=True, text=True, timeout=120).stdout
            if out.strip():
                (HOME / "show").mkdir(exist_ok=True)
                (HOME / "show" / "quota.txt").write_text(out)
        except Exception as e:
            print(f"额度没查到：{e}", file=log)
        time.sleep(QUOTA_EVERY)


def measure_loop(cfg, log=sys.stderr):
    while True:
        try:
            save(cfg)
        except Exception as e:
            print(f"机器没量到：{e!r}", file=log)
        time.sleep(MEASURE_EVERY)


def serve(cfg=SHOW, log=sys.stderr):
    host, port = cfg.get("listen", "127.0.0.1:3089").rsplit(":", 1)
    allow = set(cfg.get("allow", ["127.0.0.1"]))
    lock, cache = threading.Lock(), {"at": 0.0, "body": b""}

    def body():
        with lock:
            if time.time() - cache["at"] > PAGE_TTL:
                cache.update(at=time.time(), body=page().encode())
            return cache["body"]

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.client_address[0] not in allow:
                return self.reply(403, b"forbidden", "text/plain")
            if self.path.split("?")[0] not in ("/", "/index.html"):
                return self.reply(404, b"not found", "text/plain")
            try:
                return self.reply(200, body(), "text/html; charset=utf-8")
            except Exception as e:
                print(f"页面编不出来：{e!r}", file=log)
                return self.reply(500, f"页面编不出来：{e}".encode(), "text/plain; charset=utf-8")

        def reply(self, code, data, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *a):
            pass

    threading.Thread(target=quota_loop, args=(log,), daemon=True).start()
    threading.Thread(target=measure_loop, args=(cfg, log), daemon=True).start()
    print(f"听 {host}:{port}，放行 {'、'.join(sorted(allow))}", file=log)
    ThreadingHTTPServer((host, int(port)), H).serve_forever()
