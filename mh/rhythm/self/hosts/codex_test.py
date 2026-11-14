"""Codex self 接入的结束、续接与动作归属；不调用模型。"""
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from . import codex


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.logs = self.base / "logs"
        self.logs.mkdir()
        self.home = self.base / "home"

    def write(self, path, events):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n")

    def stream(self, events):
        self.write(self.logs / "run.jsonl", events)

    def test_home(self):
        codex.home(self.home, self.base / "self")
        config = tomllib.loads((self.home / "config.toml").read_text())
        self.assertEqual(config["approval_policy"], "never")
        self.assertFalse(config["features"]["memories"])
        self.assertFalse(config["memories"]["generate_memories"])
        self.assertEqual(set(config["mcp_servers"]), {"mh"})
        hooks = json.loads((self.home / "hooks.json").read_text())["hooks"]
        commands = [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]
        self.assertTrue(all(str(codex.REPO) in c for c in commands))
        self.assertNotIn("notify", config)
        self.assertTrue((self.home / "auth.json").is_symlink())

    def test_outcome(self):
        start = {"type": "thread.started", "thread_id": "s1"}
        msg = {"type": "item.completed", "item": {"type": "agent_message", "text": " 完成 "}}
        self.stream([start, msg, {"type": "turn.completed"}])
        self.assertEqual(codex.outcome(self.logs), (True, "完成", "s1"))
        self.stream([start, msg, {"type": "turn.failed"}])
        self.assertEqual(codex.outcome(self.logs), (False, "完成", "s1"))
        self.assertEqual(codex.outcome(self.base), (False, "", ""))

    def test_actions_current_and_descendants(self):
        self.stream([{"type": "thread.started", "thread_id": "main"},
                     {"type": "item.completed", "item": {"type": "command_execution", "command": "pwd"}}])
        def record(sid, parent, command):
            f = self.home / "sessions" / f"rollout-{sid}.jsonl"
            self.write(f, [{"type": "session_meta", "payload": {"id": sid, "parent_thread_id": parent}},
                           {"type": "event_msg", "payload": {"type": "item_completed", "item": {
                               "type": "CommandExecution", "command": command}}}])
            return f
        main = record("main", None, "old")
        offsets = {str(main): main.stat().st_size}
        with main.open("a") as f:
            f.write(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": {
                "type": "FileChange", "changes": {"a.md": {"type": "add"}}}}}) + "\n")
        record("child", "main", "child command")
        record("grandchild", "child", "grandchild command")
        record("other", None, "unrelated")
        (self.logs / "codex.json").write_text(json.dumps({"home": str(self.home), "root": "/self", "offsets": offsets}))
        acts = codex.actions(self.logs)
        self.assertIn(("write", "/self/a.md"), acts)
        self.assertIn(("run", "child command"), acts)
        self.assertIn(("run", "grandchild command"), acts)
        self.assertNotIn(("run", "old"), acts)
        self.assertNotIn(("run", "unrelated"), acts)
        # 后来同一会话又续接，回看本次核查不能把后轮补丁算进来。
        with main.open("a") as f:
            f.write(json.dumps({"timestamp": "2026-11-08T11:00:00Z", "type": "event_msg",
                                "payload": {"type": "task_complete"}}) + "\n")
            f.write(json.dumps({"timestamp": "2026-11-08T12:00:00Z", "type": "event_msg", "payload": {
                "type": "item_completed", "item": {"type": "FileChange", "changes": {"future.md": {}}}}}) + "\n")
        self.assertNotIn(("write", "/self/future.md"), codex.actions(self.logs))

    def test_native_events(self):
        self.assertEqual(codex._actions({"type": "CommandExecution", "command": ["/bin/bash", "-lc", "pwd"]}, "/self"),
                         [("run", "/bin/bash -lc pwd")])
        self.assertEqual(codex._actions({"type": "FileChange", "changes": {"a.md": {"type": "add"}}}, "/self"),
                         [("write", "/self/a.md")])

    def test_resumable(self):
        self.assertFalse(codex.resumable("", self.home))
        f = self.home / "sessions" / "rollout-abc.jsonl"
        self.write(f, [{}])
        self.assertTrue(codex.resumable("abc", self.home))
        with patch.object(codex, "RESUME_BYTES", 1):
            self.assertFalse(codex.resumable("abc", self.home))

    @unittest.skipUnless(codex.importlib.util.find_spec("mh.rhythm.quota"), "公开包不带 quota")
    def test_ready(self):
        from ... import quota
        with patch.object(codex.shutil, "which", return_value="/bin/codex"), \
             patch.object(Path, "exists", return_value=True), patch.object(quota, "read") as read:
            read.return_value = [{"ok": True, "windows": [{"name": "5h", "used": 10}]}]
            self.assertEqual(codex.ready(), "")
            read.return_value = [{"ok": False, "windows": []}]
            self.assertIn("吃紧", codex.ready())
            read.return_value = [{"error": "offline"}]
            self.assertIn("读不到", codex.ready())


if __name__ == "__main__":
    unittest.main()
