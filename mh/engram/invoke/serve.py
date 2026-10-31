"""远端调用（X2，2026-10-29）：只能在某台机器上跑的模型定义（精读的 DshWeb：bwrap 沙箱、PR_SET_PDEATHSIG，只有 Linux），
由那台机器上的服务代跑，别的机器经 HTTP 调它。接口和本地一样：给提示词，拿回 (文本, 调用信息)。

- 服务端 `python3 -m mh engram serve`：只提供构造出来是 DshWeb 的定义（程序认，不另列名单）；听实例配置 [invoke].listen，
  默认只听本机。每个请求带 token（[invoke].token），模型的密钥只在服务端那台机器上。
- 客户端：模型定义照旧只有一份，用 remote(名字, 本地构造的模型) 包一层；实例配置 [invoke.remote] 里给了这个名字的地址
  才转发，否则原样返回本地的。model、platform、isolation 都是本地算的，请求带上 isolation，服务端算出的不一样就拒绝
  （两边仓库的补丁不一致，结果就不是这个 Operator 的）。
- 错误：模型那边的错原样带回，类型（超时）也带回，环境错误、超时、内容错误照本地的规则分；连不上、Operator 不一致
  记成环境错误（store.ENVIRONMENTAL），不计次数，休眠后再试。
"""
import hmac
import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..store.instance import INVOKE

DEFAULT_LISTEN = "127.0.0.1:3090"
SLACK = 120                  # 客户端比模型的超时多等几秒：超时由服务端判，带回来


class Remote:
    """转发给远端服务的模型。属性照本地构造的那个（Operator 的身份在本地算）。"""

    def __init__(self, name, local, url, token):
        self.name, self.url, self.token = name, url.rstrip("/"), token
        self.model, self.platform = local.model, local.platform
        self.isolation = getattr(local, "isolation", "")
        self.timeout = local.timeout

    def __call__(self, prompt):
        body = json.dumps({"name": self.name, "isolation": self.isolation, "prompt": prompt}).encode()
        req = urllib.request.Request(f"{self.url}/invoke", data=body, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=self.timeout + SLACK).read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"远端调用连不上（{self.url}，HTTP {e.code}）：{e.read()[:300].decode('utf-8', 'replace')}")
        except TimeoutError:
            raise TimeoutError(f"远端调用超时（{self.url}，{self.timeout + SLACK} 秒没有返回）")
        except (OSError, ValueError) as e:
            raise RuntimeError(f"远端调用连不上（{self.url}）：{e}")
        if d.get("ok"):
            return d["text"], {**d["meta"], "remote": self.url}
        raise (TimeoutError if d.get("type") == "TimeoutError" else RuntimeError)(d.get("error", "远端没给错误信息"))


def remote(name, local, config=None):
    """实例配置里给了这个定义的地址就换成 Remote，否则原样返回本地的。"""
    cfg = INVOKE if config is None else config
    url = (cfg.get("remote") or {}).get(name)
    return Remote(name, local, url, cfg.get("token", "")) if url else local


def servable(models):
    """{名字: 构造函数} 里构造出来是 DshWeb 的。"""
    from . import DshWeb
    return {n: f for n, f in models.items() if isinstance(f(), DshWeb)}


def handler(models, token, log=sys.stderr):
    """请求处理：认 token、按名字构造本地的模型、核 isolation、调用。"""
    built = {}

    class H(BaseHTTPRequestHandler):
        def _send(self, code, d):
            body = json.dumps(d, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not token or not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                return self._send(401, {"ok": False, "error": "token 不对"})
            if self.path != "/invoke":
                return self._send(404, {"ok": False, "error": f"没有 {self.path}"})
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                name, prompt = d["name"], d["prompt"]
            except (ValueError, KeyError) as e:
                return self._send(400, {"ok": False, "error": f"请求格式不对：{e!r}"})
            if name not in models:
                return self._send(404, {"ok": False, "error": f"远端调用连不上：这里不提供 {name}"})
            model = built.get(name) or built.setdefault(name, models[name]())
            if d.get("isolation") != model.isolation:
                return self._send(409, {"ok": False, "error": f"远端的 Operator 不一致：{name} 本地 {d.get('isolation')}，"
                                                              f"远端 {model.isolation}（两边仓库的版本对齐了再调）"})
            from . import call
            try:
                text, meta = call(model, prompt)
            except Exception as e:
                print(f"{name}：{type(e).__name__}：{e}", file=log, flush=True)
                return self._send(200, {"ok": False, "type": type(e).__name__, "error": str(e)})
            print(f"{name}：{meta.get('seconds')}s", file=log, flush=True)
            self._send(200, {"ok": True, "text": text, "meta": meta})

        def log_message(self, *a):           # 一次调用一行，记在上面；http.server 的访问日志不要
            pass

    return H


def serve(models, listen=None, token=None, log=sys.stderr):
    """常驻：听 listen（host:port），代跑 models 里的定义。"""
    listen = listen or INVOKE.get("listen") or DEFAULT_LISTEN
    token = token if token is not None else INVOKE.get("token", "")
    if not token:
        raise SystemExit("实例配置 [invoke].token 没写：远端调用不开放")
    host, _, port = listen.rpartition(":")
    srv = ThreadingHTTPServer((host, int(port)), handler(models, token, log))
    print(f"远端调用：听 {listen}，提供 {'、'.join(models)}", file=log, flush=True)
    srv.serve_forever()
