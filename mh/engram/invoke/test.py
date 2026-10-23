"""调用接口的测试：隔离标记怎样随构造参数变、白名单怎样认工具调用、call 记下耗时。不发真的模型请求。

python3 -m unittest mh.engram.invoke.test
"""
import json
import tempfile
import unittest
from pathlib import Path

from . import Claude, Codex, DshWeb, call


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


if __name__ == "__main__":
    unittest.main()
