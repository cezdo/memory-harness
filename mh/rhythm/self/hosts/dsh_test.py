"""dsh self 接入的结束、续接与动作归属；不调用模型。"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import dsh


def _zstd(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n"
    with path.open("wb") as f:
        subprocess.run(["zstd", "-q", "-c"], input=raw.encode(), stdout=f, check=True)


class DshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.logs = self.base / "logs"
        self.logs.mkdir()
        self.home = self.base / "home"

    def stream(self, events):
        (self.logs / "run.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")

    def test_home(self):
        dsh.home(self.home)
        prof = self.home / "profiles" / "headless"
        package = json.loads((prof / "package.json").read_text())
        self.assertIn("mh-synapse", package["dsh"]["profile"]["bundles"])
        self.assertTrue((prof / "node_modules" / "mh-synapse").is_symlink())
        patch_text = (prof / "cordis.patch.yml").read_text()
        self.assertIn("provider: opencode-go", patch_text)
        self.assertIn(str(Path.home() / ".dsh/.credentials.yaml"), patch_text)
        self.assertTrue((self.home / "sessions").is_dir())

    def test_outcome(self):
        self.stream([{"type": "session", "sessionId": "session-a", "cwd": "/self"},
                     {"type": "text", "text": "做了一半"},
                     {"type": "final", "sessionId": "session-a", "reason": {"kind": "completed"}, "text": " 完成 "}])
        self.assertEqual(dsh.outcome(self.logs), (True, "完成", "session-a"))
        self.stream([{"type": "session", "sessionId": "session-a"},
                     {"type": "final", "sessionId": "session-a", "reason": {"kind": "error", "error": {"message": "x"}}, "text": "算了"}])
        self.assertEqual(dsh.outcome(self.logs), (False, "算了", "session-a"))
        self.stream([{"type": "session", "sessionId": "session-b"}])
        self.assertEqual(dsh.outcome(self.logs), (False, "", "session-b"))       # 进程被杀：没有 final
        self.assertEqual(dsh.outcome(self.base), (False, "", ""))

    def test_actions_main_and_subagents(self):
        main = self.home / "sessions" / "--self--" / "session-main" / "session.v4.jsonl.zstd"
        _zstd(main, [{"type": "session", "id": "session-main", "cwd": "/self"},
                     {"type": "tool/call", "data": {"name": "write", "arguments": json.dumps({"file_path": "old.md"})}}])
        offsets = {str(main): 2}
        with main.open("ab") as f:
            raw = "\n".join(json.dumps(e) for e in [
                {"type": "tool/call", "data": {"name": "edit", "arguments": json.dumps({"file_path": "a.md"})}},
                {"type": "tool/call", "data": {"name": "bash", "arguments": json.dumps({"command": "  ls   -la "})}},
                {"type": "tool/call", "data": {"name": "read", "arguments": json.dumps({"file_path": "r.md"})}}]) + "\n"
            comp = subprocess.run(["zstd", "-q", "-c"], input=raw.encode(), capture_output=True, check=True).stdout
            f.write(comp)
        child = self.home / "sessions" / "--self--" / "kid" / "session.v4.jsonl.zstd"
        _zstd(child, [{"type": "session", "id": "kid", "parentSession": "session-main", "cwd": "/self"},
                      {"type": "tool/call", "data": {"name": "bash", "arguments": json.dumps({"command": "pwd"})}}])
        grand = self.home / "sessions" / "--self--" / "grand" / "session.v4.jsonl.zstd"
        _zstd(grand, [{"type": "session", "id": "grand", "parentSession": "kid", "cwd": "/self"},
                      {"type": "tool/call", "data": {"name": "write", "arguments": json.dumps({"file_path": "g.md"})}}])
        other = self.home / "sessions" / "--else--" / "other" / "session.v4.jsonl.zstd"
        _zstd(other, [{"type": "session", "id": "other", "cwd": "/else"},
                      {"type": "tool/call", "data": {"name": "bash", "arguments": json.dumps({"command": "unrelated"})}}])
        self.stream([{"type": "session", "sessionId": "session-main"}, {"type": "final", "sessionId": "session-main", "text": ""}])
        (self.logs / "dsh.json").write_text(json.dumps({"home": str(self.home), "root": "/self", "offsets": offsets}))
        acts = dsh.actions(self.logs)
        self.assertIn(("write", "/self/a.md"), acts)
        self.assertIn(("run", "ls -la"), acts)
        self.assertIn(("run", "pwd"), acts)
        self.assertIn(("write", "/self/g.md"), acts)
        self.assertNotIn(("write", "/self/old.md"), acts)        # 启动前的那一行不算
        self.assertNotIn(("run", "unrelated"), acts)

    def test_resumable(self):
        self.assertFalse(dsh.resumable(""))
        f = self.home / "sessions" / "--self--" / "session-a" / "session.v4.jsonl.zstd"
        _zstd(f, [{"type": "session", "id": "session-a"}])
        self.assertTrue(dsh.resumable("session-a", self.home))
        with patch.object(dsh, "RESUME_BYTES", 1):
            self.assertFalse(dsh.resumable("session-a", self.home))

    @unittest.skipUnless(__import__("importlib").util.find_spec("mh.rhythm.quota"), "公开包不带 quota")
    def test_ready(self):
        from ... import quota
        with patch.object(dsh.shutil, "which", return_value="/bin/dsh"), \
             patch.object(Path, "exists", return_value=True), patch.object(quota, "read") as read:
            read.return_value = [{"ok": True, "windows": [{"name": "5h", "used": 10}]}]
            self.assertEqual(dsh.ready(), "")
            read.return_value = [{"ok": False, "windows": []}]
            self.assertIn("吃紧", dsh.ready())
            read.return_value = [{"error": "offline"}]
            self.assertIn("读不到", dsh.ready())


if __name__ == "__main__":
    unittest.main()
