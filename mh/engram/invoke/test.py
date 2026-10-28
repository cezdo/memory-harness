"""调用接口的测试：隔离标记怎样随构造参数变、白名单怎样认工具调用、call 记下耗时。不发真的模型请求。

python3 -m unittest mh.engram.invoke.test
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from . import Claude, Codex, DshWeb, call, catalog


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

    def test_allowed(self):
        w = DshWeb("m", port=39005, box=self.a.parent / "box", allow=lambda c: c.startswith("count "))
        bash = lambda cmd: {"name": "bash", "arguments": json.dumps({"command": cmd})}
        self.assertTrue(w._allowed(bash("count 字")))
        self.assertFalse(w._allowed(bash("ls /")))
        self.assertFalse(w._allowed({"name": "read", "arguments": "{}"}))
        self.assertFalse(DshWeb("m", port=39006, box=self.a.parent / "box")._allowed(bash("count 字")))    # 没给白名单：都不放行

    def test_call(self):
        text, meta = call(lambda p: (p.upper(), {"model": "fake"}), "abc")
        self.assertEqual(text, "ABC")
        self.assertEqual(meta["model"], "fake")
        self.assertIsInstance(meta["seconds"], float)

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


if __name__ == "__main__":
    unittest.main()
