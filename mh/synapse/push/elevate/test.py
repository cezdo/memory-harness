"""内核态：认命令、开关窗口、拦写入、比对记日志、安装。python3 -m unittest mh.synapse.push.elevate.test"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from . import HERE, WINDOW, Kernel, handle, install, request


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / "repo/design/synapse/core.md"
        self.src.parent.mkdir(parents=True)
        self.src.write_text("# core\n\n- 你：称呼示例用户、示例别名或 demo-user，任选。\n")
        self.k = Kernel(self.src, root / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def log(self):
        return [json.loads(l) for l in self.k.log.read_text().splitlines()] if self.k.log.exists() else []

    def ev(self, event, host="claude", session="s1", **d):
        return handle(self.k, host, event, dict(session_id=session, cwd=str(self.src.parents[2]), **d))


class Request(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(request("/mhcore 接下来优先把称呼改成示例用户"), "接下来优先把称呼改成示例用户")
        self.assertEqual(request("  $mhcore 改称呼\n第二行  "), "改称呼\n第二行")
        self.assertEqual(request("/mhcore"), "")
        self.assertEqual(request("<command-message>mhcore</command-message>\n<command-name>/mhcore</command-name>\n"
                                 "<command-args>改称呼</command-args>"), "改称呼")

    def test_not_command(self):
        for s in ("讨论一下 /mhcore 怎么做", "/mhcorex 改", "/mhcore改称呼", "", None, "/no-observe"):
            self.assertIsNone(request(s), s)


class Flow(Base):
    def edit(self, session="s1", host="claude", tool="Edit"):
        return self.ev("pre", host, session, tool_name=tool, tool_input={"file_path": str(self.src), "old_string": "a"})

    def test_user_mode_denies_edit(self):
        out, _ = self.edit()
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_other_files_and_reads_pass(self):
        other = self.src.with_name("设计.md")
        self.assertEqual(self.ev("pre", tool_name="Edit", tool_input={"file_path": str(other)}), (None, 0))
        self.assertEqual(self.ev("pre", tool_name="Read", tool_input={"file_path": str(self.src)}), (None, 0))
        self.assertEqual(self.ev("pre", tool_name="Bash", tool_input={"command": f"cat {self.src}"}), (None, 0))

    def test_codex_patch_relative_path_denied(self):
        patch = "*** Begin Patch\n*** Update File: design/synapse/core.md\n@@\n-a\n+b\n*** End Patch"
        out, _ = self.ev("pre", "codex", tool_name="apply_patch", tool_input={"command": patch})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_grant_edit_close(self):
        self.ev("post", tool_name="Read")                                 # 第一次见：记基线
        out, code = self.ev("prompt", prompt="/mhcore 接下来优先把称呼改成示例用户")
        self.assertIn("内核态", out["hookSpecificOutput"]["additionalContext"])
        self.assertIn(str(self.src), out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.edit(), (None, 0))                          # 窗口里放行
        self.assertIsNotNone(self.edit(session="s2")[0])                  # 别的会话仍是用户态
        self.src.write_text("# core\n\n- 你：称呼示例用户。\n")
        self.ev("post", tool_name="Edit")
        self.ev("stop")
        self.assertIsNotNone(self.edit()[0])                              # 这一轮结束回到用户态
        kinds = [r["kind"] for r in self.log()]
        self.assertEqual(kinds, ["baseline", "grant", "edit", "close"])
        grant, edit = self.log()[1], self.log()[2]
        self.assertEqual(grant["prompt"], "接下来优先把称呼改成示例用户")
        self.assertIn("+- 你：称呼示例用户。", edit["diff"])
        self.assertIn("-- 你：称呼示例用户、示例别名或 demo-user，任选。", edit["diff"])

    def test_unlogged_change_found_by_any_session(self):
        self.ev("post", tool_name="Read")
        self.src.write_text("手改\n")                                      # Bash、编辑器、别的宿主改的
        self.ev("post", "codex", "s9", tool_name="shell")
        r = self.log()[-1]
        self.assertEqual((r["kind"], r["host"], r["session"]), ("unlogged", "", ""))
        self.assertEqual((r["found_by"]["host"], r["found_by"]["session"], r["found_by"]["tool"]), ("codex", "s9", "shell"))
        self.ev("post", tool_name="Read")
        self.assertEqual(len(self.log()), 2)                              # 没再变就不记

    def test_change_before_grant_not_counted_as_edit(self):
        self.ev("post", tool_name="Read")
        self.src.write_text("手改\n")
        self.ev("prompt", prompt="/mhcore 改称呼")
        self.assertEqual([r["kind"] for r in self.log()], ["baseline", "unlogged", "grant"])

    def test_window_closes_on_next_prompt_and_expires(self):
        self.ev("prompt", prompt="/mhcore 改称呼")
        self.ev("prompt", prompt="接着聊")
        self.assertIsNotNone(self.edit()[0])
        self.ev("prompt", prompt="/mhcore 改称呼")
        with mock.patch("time.time", return_value=__import__("time").time() + WINDOW + 1):
            self.assertIsNotNone(self.edit()[0])

    def test_empty_request_blocked(self):
        out, _ = self.ev("prompt", prompt="/mhcore")
        self.assertEqual(out["decision"], "block")
        self.assertFalse(self.k.opened("claude", "s1"))


class Hook(unittest.TestCase):
    """真的跑一遍钩子脚本：-I -S 能起来、出错放行、codex 的拦法。"""

    def run_hook(self, host, event, payload, home):
        env = dict(os.environ, MH_HOME=str(home))
        return subprocess.run([sys.executable, "-I", "-S", str(HERE / "hook.py"), host, event],
                              input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)

    def test_script(self):
        with tempfile.TemporaryDirectory() as t:
            p = self.run_hook("claude", "prompt", {"session_id": "x", "prompt": "随便说说"}, t)
            self.assertEqual((p.returncode, p.stdout), (0, ""))
            p = self.run_hook("codex", "prompt", {"session_id": "x", "prompt": "$mhcore"}, t)
            self.assertEqual(p.returncode, 2)
            self.assertIn("$mhcore", p.stderr)
            p = self.run_hook("claude", "post", "不是 JSON", t)                 # 坏输入：放行
            self.assertEqual(p.returncode, 0)


class Install(unittest.TestCase):
    def test_preview_then_apply(self):
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            (home / ".claude").mkdir()
            (home / ".claude/settings.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "x"}]}]}}))
            plan = install(home, apply=False)
            self.assertEqual(len(plan), 4)
            self.assertFalse((home / ".claude/commands/mhcore.md").exists())
            install(home, apply=True)
            d = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(len(d["hooks"]["Stop"]), 2)                      # 原有的钩子留着
            self.assertEqual(d["hooks"]["PreToolUse"][0]["matcher"], "Edit|Write|MultiEdit|NotebookEdit")
            self.assertNotIn("matcher", json.loads((home / ".codex/hooks.json").read_text())["hooks"]["PreToolUse"][0])
            self.assertTrue((home / ".claude/commands/mhcore.md").is_symlink())
            self.assertEqual(install(home, apply=True), [])                  # 装过就不动


if __name__ == "__main__":
    unittest.main()
