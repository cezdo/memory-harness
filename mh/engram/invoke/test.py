"""调用接口的测试：隔离标记怎样随构造参数变、白名单怎样认工具调用、call 记下耗时。不发真的模型请求。

python3 -m unittest mh.engram.invoke.test
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from . import Claude, Codex, DeepSeek, DshWeb, call, catalog, relink


def datetime_at(hour):
    from datetime import datetime, timezone
    return datetime(2026, 11, 25, hour, tzinfo=timezone.utc)


class InvokeTest(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp())
        self.a, self.b = d / "a.patch.yml", d / "b.patch.yml"
        self.a.write_text("- id: x\n")
        self.b.write_text("- id: y\n")

    def test_isolation(self):
        one = DshWeb("m", port=39001, box=self.a.parent / "box", patches=(self.a,))
        two = DshWeb("m", port=39002, box=self.a.parent / "box", patches=(self.a, self.b))
        same = DshWeb("m", port=39003, box=self.a.parent / "box", patches=(self.a,))
        self.assertEqual(one.isolation, same.isolation)                 # 端口、目录是执行参数，不进隔离标记
        self.assertNotEqual(one.isolation, two.isolation)               # 补丁进隔离标记
        self.assertTrue(one.isolation.startswith("dsh-web-minimal-box-"))
        tagged = DshWeb("m", port=39004, box=self.a.parent / "box", patches=(self.a,), allow=lambda c: True, allow_tag="t")
        self.assertEqual(tagged.isolation, one.isolation + "-allow-t")
        self.assertEqual(Claude().isolation, "claude-home-v1")
        self.assertEqual(Codex("gpt", "high", isolation="codex-home-v1-high").isolation, "codex-home-v1-high")

    def test_relink(self):
        d = self.a.parent
        main, cred = d / "main.json", d / "home.json"
        put = lambda path, exp, tok: path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": exp, "accessToken": tok}}))
        put(main, 100, "old")
        relink(cred, main)                                   # 没有副本：链上
        self.assertTrue(cred.is_symlink())
        cred.unlink(); put(cred, 200, "new")                 # claude 刷新后把链接写成普通文件，副本更新：写回主凭据
        relink(cred, main)
        self.assertTrue(cred.is_symlink())
        self.assertEqual(json.loads(main.read_text())["claudeAiOauth"]["accessToken"], "new")
        cred.unlink(); put(cred, 0, "")                      # 刷新失败写回的空令牌：不覆盖主凭据
        relink(cred, main)
        self.assertEqual(json.loads(main.read_text())["claudeAiOauth"]["accessToken"], "new")
        self.assertTrue(cred.is_symlink())

    def test_allowed(self):
        w = DshWeb("m", port=39005, box=self.a.parent / "box", allow=lambda c: c.startswith("count "))
        bash = lambda cmd: {"name": "bash", "arguments": json.dumps({"command": cmd})}
        self.assertTrue(w._allowed(bash("count 字")))
        self.assertFalse(w._allowed(bash("ls /")))
        self.assertFalse(w._allowed({"name": "read", "arguments": "{}"}))
        self.assertFalse(DshWeb("m", port=39006, box=self.a.parent / "box")._allowed(bash("count 字")))    # 没给白名单：都不放行

    def test_deepseek_peak(self):
        from datetime import datetime, timezone
        at = lambda d, h, m=0: DeepSeek.wait(datetime(2026, 11, d, h, m, tzinfo=timezone.utc))
        self.assertEqual(at(25, 1).hour, 4)              # 周三 01:00 UTC 起是高峰，到 04:00
        self.assertEqual(at(25, 9, 59).hour, 10)
        self.assertIsNone(at(25, 4))                     # 04–06 谷价
        self.assertIsNone(at(25, 10))
        self.assertIsNone(at(25, 0, 59))
        self.assertIsNone(at(28, 2))                     # 周六全天谷价
        self.assertEqual(DeepSeek().isolation, "deepseek-api-v1-high")

    def test_deepseek_call(self):
        from unittest import mock
        key = self.a.parent / "deepseek.key"
        key.write_text("sk-test\n")
        reply = {"id": "x", "model": "deepseek-flash", "choices": [{"message": {"content": "### 事件1", "reasoning_content": "先想想"},
                                                                    "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 60, "completion_tokens": 30,
                           "completion_tokens_details": {"reasoning_tokens": 20}}}
        sent = []

        def urlopen(req, timeout):
            sent.append(req)
            return mock.Mock(read=lambda: json.dumps(reply).encode())
        m = DeepSeek()
        m.key = key
        with mock.patch.object(DeepSeek, "wait", return_value=None), mock.patch("urllib.request.urlopen", urlopen):
            text, meta = m("提示词")
            body = json.loads(sent[0].data)
            self.assertEqual(body["messages"], [{"role": "user", "content": "提示词"}])     # 不加 system、不带工具
            self.assertNotIn("tools", body)
            self.assertEqual(sent[0].get_header("Authorization"), "Bearer sk-test")
            self.assertEqual(text, "### 事件1")
            self.assertEqual(meta["usage"], [{"inputTokens": 40, "cacheReadTokens": 60, "outputTokens": 30}])
            self.assertEqual(meta["reasoning"], "先想想")                                  # 思考全文存进 act 的 meta
            reply["choices"][0]["finish_reason"] = "length"
            with self.assertRaises(RuntimeError):
                m("提示词")
        with mock.patch.object(DeepSeek, "wait", return_value=datetime_at(4)):
            with self.assertRaisesRegex(RuntimeError, "高峰时段"):          # 跨进高峰的那一次也不发；按环境错误认
                m("提示词")
        from ..store import outcome
        self.assertEqual(outcome("RuntimeError('DeepSeek 高峰时段，不发（等谷价，04:00 UTC 起）')")[0], "env_error")

    def test_call(self):
        text, meta = call(lambda p: (p.upper(), {"model": "fake"}), "abc")
        self.assertEqual(text, "ABC")
        self.assertEqual(meta["model"], "fake")
        self.assertIsInstance(meta["seconds"], float)

    def test_cli_slots(self):
        """本机起 CLI 的调用共用名额（2026-11-26：内存稀缺）：同时不超过上限，多的排队；别的模型不排队。"""
        import threading
        import time
        from unittest import mock
        from . import _Slot
        live, peak, lock = [0], [0], threading.Lock()

        class Fake(Codex):
            def __init__(self):
                pass

            def __call__(self, prompt):
                with lock:
                    live[0] += 1
                    peak[0] = max(peak[0], live[0])
                time.sleep(0.3)
                with lock:
                    live[0] -= 1
                return prompt, {}
        with tempfile.TemporaryDirectory() as d, mock.patch(f"{__package__}.POLL", 0.05):
            ts = [threading.Thread(target=call, args=(Fake(), "x", _Slot(2, Path(d), free=lambda: None))) for _ in range(5)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(peak[0], 2)
            peak[0] = 0                                       # 内存紧（X9）：只放 0 号名额，一路一路走，不卡死
            ts = [threading.Thread(target=call, args=(Fake(), "x", _Slot(3, Path(d), free=lambda: 100, min_free=2048)))
                  for _ in range(3)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            self.assertEqual(peak[0], 1)
            with _Slot(1, Path(d)):                          # 名额占满时，不起 CLI 的照常调
                self.assertEqual(call(lambda p: (p, {}), "y")[0], "y")

    def test_catalog(self):
        """本地模型目录：没有、过期、或没有要用的模型就刷新一次；够新就不动（2026-10-28，免得每次进程启动都拉 0.66MB）。"""
        d = Path(tempfile.mkdtemp())
        path = d / "cat.json"
        made = json.dumps({"models": [{"slug": "gpt-6.1-sol"}, {"slug": "gpt-6-luna"}]})
        runs = []

        def run():
            runs.append(1)
            return made
        self.assertEqual(catalog("gpt-6.1-sol", path=path, run=run), path)          # 没有：生成
        self.assertEqual(json.loads(path.read_text())["models"][0]["slug"], "gpt-6.1-sol")
        self.assertEqual(catalog("gpt-6.1-sol", path=path, run=run), path)          # 够新、有模型：不再生成
        self.assertEqual(len(runs), 1)
        self.assertEqual(catalog("gpt-6-luna", path=path, run=run), path)           # 换个有的模型：还是不再生成
        self.assertEqual(len(runs), 1)
        self.assertEqual(catalog("new-model", path=path, run=run), path)            # 没有这个模型：刷新一次
        self.assertEqual(len(runs), 2)
        os.utime(path, (0, 0))
        catalog("gpt-6.1-sol", path=path, run=run)                                  # 过期：刷新
        self.assertEqual(len(runs), 3)

    def test_catalog_unavailable(self):
        """刷新不了（没网、版本不对）：返回 None，调用方退回在线目录，不挡调用。"""
        d = Path(tempfile.mkdtemp())

        def boom():
            raise RuntimeError("no network")
        self.assertIsNone(catalog("m", path=d / "none.json", run=boom))
        self.assertIsNone(catalog("m", path=d / "bad.json", run=lambda: "<html>"))
        self.assertIsNone(catalog("m", path=d / "empty.json", run=lambda: json.dumps({"models": []})))

    def test_catalog_off(self):
        """版本不认 model_catalog_json：记一笔之后不再带，免得每次调用都白起一个进程。"""
        d = Path(tempfile.mkdtemp())
        path = d / "cat.json"
        path.write_text(json.dumps({"models": [{"slug": "m"}]}))
        (d / "cat.json.off").write_text("0")
        runs = []
        self.assertIsNone(catalog("m", path=path, run=lambda: runs.append(1) or path.read_text()))
        self.assertEqual(runs, [])


class Fake:
    """假的模型：按提示词决定返回什么、抛什么。"""
    model, platform, isolation, timeout = "fake", "fake-platform", "iso-1", 5

    def __call__(self, prompt):
        if prompt == "slow":
            raise TimeoutError("dsh web 观察超时（会话 x）")
        if prompt == "tool":
            raise RuntimeError("观察者调用了工具 ['bash: ls']，已取消，结果作废")
        return prompt.upper(), {"model": self.model, "session": "s1"}


class ServeTest(unittest.TestCase):
    """远端调用（X2，2026-10-29）：转发后拿到的和本地一样；模型那边的错原样带回、照本地规则分类；连不上、
    Operator 不一致、token 不对算环境错误。不碰 DshWeb。"""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import ThreadingHTTPServer
        from . import serve
        cls.null = open(os.devnull, "w")
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.handler({"fake": Fake}, "t0k", log=cls.null))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.null.close()

    def remote(self, url=None, token="t0k", local=None):
        from . import serve
        return serve.remote("fake", local or Fake(), {"remote": {"fake": url or self.url}, "token": token})

    def kind(self, model, prompt):
        from ..store import outcome
        try:
            model(prompt)
        except Exception as e:
            return type(e).__name__, outcome(str(e))[0]
        return None, "ok"

    def test_pass_through(self):
        from . import serve
        r = self.remote()
        self.assertIsInstance(r, serve.Remote)
        self.assertEqual((r.model, r.platform, r.isolation), ("fake", "fake-platform", "iso-1"))   # 身份在本地算
        text, meta = r("abc")
        self.assertEqual(text, "ABC")
        self.assertEqual(meta["session"], "s1")
        self.assertEqual(meta["remote"], self.url)
        local = Fake()
        self.assertIs(serve.remote("fake", local, {"remote": {"other": self.url}}), local)       # 没配就在本地跑

    def test_errors(self):
        self.assertEqual(self.kind(self.remote(), "slow"), ("TimeoutError", "timeout"))
        self.assertEqual(self.kind(self.remote(), "tool"), ("RuntimeError", "content_error"))
        stale = Fake()
        stale.isolation = "iso-0"
        self.assertEqual(self.kind(self.remote(local=stale), "abc"), ("RuntimeError", "env_error"))   # 两边补丁不一致
        self.assertEqual(self.kind(self.remote(token="wrong"), "abc"), ("RuntimeError", "env_error"))
        import socket
        with socket.socket() as s:                   # 拿一个没人听的端口
            s.bind(("127.0.0.1", 0))
            dead = f"http://127.0.0.1:{s.getsockname()[1]}"
        self.assertEqual(self.kind(self.remote(url=dead), "abc"), ("RuntimeError", "env_error"))

    def test_forward(self):
        """代跑（X7）：配了 [invoke].forward，Codex / Claude 按构造参数交给对方代跑；对方构造的不再往外转；
        模型那边的错带回来照本地规则分；Operator 不一致拒绝。"""
        from unittest.mock import patch
        from . import INVOKE, Codex as Real
        seen = []

        class Here(Real):                      # 对方那边的 Codex：不真起 codex，记下怎么被构造、被调
            def __call__(self, prompt, images=(), keep=False):
                if getattr(self, "_local", False) is not True:
                    raise AssertionError("对方又往外转了")
                seen.append((self.model, self.effort, self.isolation, prompt, keep))
                if prompt == "boom":
                    raise RuntimeError("观察者调用了工具 ['bash: ls']，已取消，结果作废")
                return prompt[::-1], {"model": self.model, "isolation": self.isolation}

        with patch.dict(INVOKE, {"forward": self.url, "token": "t0k"}), patch(f"{__package__}.Codex", Here):
            m = Real("gpt-x", "low", isolation="codex-home-v1-low", no_tools=True)
            text, meta = m("abc", keep=True)
            self.assertEqual((text, meta["forwarded"]), ("cba", self.url))
            self.assertEqual(seen, [("gpt-x", "low", "codex-home-v1-low-notools", "abc", True)])
            self.assertEqual(self.kind(m, "boom"), ("RuntimeError", "content_error"))
            m.isolation = "codex-home-v0"                                      # 两边版本不一致
            self.assertEqual(self.kind(m, "abc"), ("RuntimeError", "env_error"))


class ForwardTest(unittest.TestCase):
    """宿主入口转给服务机（X7）：ssh 的命令行由本机的 python 命令行换解释器、仓库路径换成相对检出的拼成，带上宿主的变量。"""

    def test_command(self):
        import shlex
        from ...synapse.hosts import forward
        cfg = {"ssh": "server", "python": "/opt/homebrew/bin/python3", "device": "laptop"}
        argv = ["/usr/bin/python3", "-I", "-S", "/r/mh/engram/elevate/hook.py", "claude", "pre", "/elsewhere/x"]
        cmd = forward.command(cfg, argv, {"CLAUDE_CODE_SESSION_ID": "s 1", "SECRET": "x", "PYTHONPATH": "/r"}, repo=Path("/r"))
        self.assertEqual(cmd[0], "ssh")
        self.assertEqual(cmd[-2], "server")
        remote = cmd[-1]
        self.assertTrue(remote.startswith("cd memory-harness && "))           # 服务机自己 ~ 下的检出，不要求同路径
        self.assertNotIn(" HOME=", remote)
        self.assertNotIn("PYTHONPATH", remote)
        self.assertIn("MH_DEVICE=laptop", remote)
        self.assertIn("CLAUDE_CODE_SESSION_ID='s 1'", remote)
        self.assertNotIn("SECRET", remote)
        self.assertTrue(remote.endswith(shlex.join(["/opt/homebrew/bin/python3", "-I", "-S", "mh/engram/elevate/hook.py",
                                                    "claude", "pre", "/elsewhere/x"])))
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(forward.config(Path(d) / "没有.toml"))            # 没有转发配置：本机跑


if __name__ == "__main__":
    unittest.main()
