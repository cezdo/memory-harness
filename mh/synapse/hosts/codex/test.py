"""仅验证 Codex 接口边界：python3 -m unittest mh.synapse.hosts.codex.test。"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from . import read_context


class ContextTest(unittest.TestCase):
    def test_compaction_uses_replacement_and_ignores_reasoning(self):
        def msg(role, text):
            return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}

        events = [
            {"type": "response_item", "payload": msg("assistant", "已被压缩的旧正文")},
            {"type": "compacted", "payload": {"replacement_history": [msg("user", "压缩后保留的信息")]}},
            {"type": "response_item", "payload": {"type": "reasoning", "text": "推理内容"}},
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "工具返回的正文"}},
            {"type": "response_item", "payload": msg("developer", "hook 给出的正文")},
            {"type": "response_item", "payload": msg("user", "当前问题")},
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rollout.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in events) + "\n{partial")
            ctx, host, found = read_context(path, "当前问题")
        self.assertEqual(ctx, "压缩后保留的信息\n工具返回的正文\nhook 给出的正文")
        self.assertIsNone(host)
        self.assertTrue(found)

    def test_unavailable_transcript(self):
        self.assertEqual(read_context(None, "问题"), ("", None, False))

    def test_bind_overrides_supplied_session_and_preserves_arguments(self):
        for args in ({"words": ["摄影"], "session": "wrong"}, json.dumps({"words": ["摄影"]})):
            result = subprocess.run([sys.executable, "-m", "mh.synapse.hosts.codex", "bind"],
                                    input=json.dumps({"session_id": "current", "cwd": "/workspace",
                                                      "tool_input": args}), text=True, capture_output=True, check=True)
            out = json.loads(result.stdout)["hookSpecificOutput"]
            self.assertEqual(out["hookEventName"], "PreToolUse")
            self.assertEqual(out["permissionDecision"], "allow")
            self.assertEqual(out["updatedInput"], {"words": ["摄影"], "session": "current", "cwd": "/workspace"})


if __name__ == "__main__":
    unittest.main()
