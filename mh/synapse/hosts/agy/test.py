"""仅验证 agy 接口边界：python3 -m unittest mh.synapse.hosts.agy.test。"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import os

from . import bind_key, extract_user, is_acp, is_first_invocation, read_context, remember, take
from .install import install

_HOME = tempfile.TemporaryDirectory()      # bind 写的旁路、acp-prompt 记的开关都落在这里，不碰正式数据目录
ENV = dict(os.environ, MH_HOME=_HOME.name)


def run(action, payload):
    out = subprocess.run([sys.executable, "-m", "mh.synapse.hosts.agy", action], input=json.dumps(payload),
                         text=True, capture_output=True, check=True, env=ENV)
    return json.loads(out.stdout)


class AgyHostTest(unittest.TestCase):
    def test_extract_user_strips_wrapper_tags(self):
        raw = "<USER_REQUEST>\n尝试把agy接入mh\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nnow\n</ADDITIONAL_METADATA>"
        self.assertEqual(extract_user(raw), "尝试把agy接入mh")
        self.assertEqual(extract_user("  普通文本  "), "普通文本")

    def test_first_invocation_and_read_context_prefer_full_transcript(self):
        steps = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\n第一句\n</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "第一轮回答", "thinking": "内部思考",
             "tool_calls": [{"name": "read_file", "args": {"Path": "/tmp/a.py"}}]},
            {"step_index": 2, "type": "GENERIC", "content": "文件内容"},
            {"step_index": 3, "type": "USER_INPUT", "content": "<USER_REQUEST>\n当前问题\n</USER_REQUEST>"},
        ]
        with tempfile.TemporaryDirectory() as folder:
            short = Path(folder) / "transcript.jsonl"
            full = Path(folder) / "transcript_full.jsonl"
            short.write_text("{}\n")
            full.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in steps) + "\n")
            self.assertTrue(is_first_invocation(short, invocation_num=3))
            ctx, host, found = read_context(short, "<USER_REQUEST>\n当前问题\n</USER_REQUEST>")
            self.assertIn("第一句", ctx)
            self.assertIn("第一轮回答", ctx)
            self.assertIn("/tmp/a.py", ctx)
            self.assertIn("文件内容", ctx)
            self.assertNotIn("内部思考", ctx)
            self.assertNotIn("当前问题", ctx)
            self.assertIsNone(host)
            self.assertTrue(found)

            # 同轮已出现 PLANNER_RESPONSE 时不再算本轮第一次调用
            full.write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in steps + [
                {"step_index": 4, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "read_file"}]}
            ]) + "\n")
            self.assertFalse(is_first_invocation(short, invocation_num=1))

    def test_unavailable_transcript(self):
        self.assertEqual(read_context(None, "问题"), ("", None, False))

    def test_acp_db_transcript_maps_to_brain_and_ignores_binary_or_non_dict(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            db = root / "conversations/conv-acp.db"
            db.parent.mkdir(parents=True)
            db.write_bytes(b"SQLite format 3\n123\n\"str\"\n")
            # 还没有 brain transcript 时不读 .db，回退看 invocation_num
            self.assertTrue(is_first_invocation(db, invocation_num=1, session_id="conv-acp"))
            self.assertFalse(is_first_invocation(db, invocation_num=2, session_id="conv-acp"))
            self.assertEqual(read_context(db, "问题", session_id="conv-acp"), ("", None, False))
            # 写出 brain transcript（含非 dict 噪声行）后正常解析
            full = root / "brain/conv-acp/.system_generated/logs/transcript_full.jsonl"
            full.parent.mkdir(parents=True)
            full.write_text('123\n"noise"\n' + json.dumps({"type": "USER_INPUT", "content": "<USER_REQUEST>\n新问题\n</USER_REQUEST>"}) + "\n")
            self.assertTrue(is_first_invocation(db, invocation_num=5, session_id="conv-acp"))
            with full.open("a") as f:
                f.write(json.dumps({"type": "PLANNER_RESPONSE", "content": "已回复"}) + "\n")
            self.assertFalse(is_first_invocation(db, invocation_num=1, session_id="conv-acp"))
            ctx, _, found = read_context(db, "新问题", session_id="conv-acp")
            self.assertTrue(found)
            self.assertIn("已回复", ctx)

    def test_bind_overrides_supplied_session_and_preserves_arguments(self):
        for payload in (
            {"conversationId": "conv-1", "workspacePaths": ["/workspace"],
             "toolCall": {"name": "mcp_mh_evoke", "args": {"words": ["摄影"], "session": "wrong"}}},
            {"conversationId": "conv-1", "cwd": "/workspace",
             "toolCall": {"name": "mcp_mh_evoke", "args": json.dumps({"words": ["摄影"]})}},
        ):
            result = subprocess.run([sys.executable, "-m", "mh.synapse.hosts.agy", "bind"],
                                    input=json.dumps(payload), text=True, capture_output=True, check=True, env=ENV)
            out = json.loads(result.stdout)
            self.assertEqual(out["decision"], "allow")
            self.assertEqual(out["overwrite"], {"words": ["摄影"], "session": "conv-1", "cwd": "/workspace"})

    def test_bind_supports_lazy_call_mcp_tool_and_ignores_other_servers(self):
        cases = [
            ({"ServerName": "mh", "ToolName": "evoke", "Arguments": {"words": ["摄影"], "session": "wrong"}},
             {"ServerName": "mh", "ToolName": "evoke", "Arguments": {"words": ["摄影"], "session": "conv-1", "cwd": "/workspace"}}),
            ({"ServerName": "mh", "ToolName": "roam"},
             {"ServerName": "mh", "ToolName": "roam", "Arguments": {"session": "conv-1", "cwd": "/workspace"}}),
            ({"ServerName": "mh", "ToolName": "yorimichi", "Arguments": json.dumps({"width": 0})},
             {"ServerName": "mh", "ToolName": "yorimichi", "Arguments": json.dumps({"width": 0, "session": "conv-1", "cwd": "/workspace"}, ensure_ascii=False)}),
        ]
        for args, want in cases:
            payload = {"conversationId": "conv-1", "workspacePaths": ["/workspace"],
                       "toolCall": {"name": "call_mcp_tool", "args": args}}
            result = subprocess.run([sys.executable, "-m", "mh.synapse.hosts.agy", "bind"],
                                    input=json.dumps(payload), text=True, capture_output=True, check=True, env=ENV)
            out = json.loads(result.stdout)
            self.assertEqual(out["decision"], "allow")
            self.assertEqual(out["overwrite"], want)

        other = {"conversationId": "conv-1", "workspacePaths": ["/workspace"],
                 "toolCall": {"name": "call_mcp_tool", "args": {"ServerName": "other", "ToolName": "ping", "Arguments": {}}}}
        result = subprocess.run([sys.executable, "-m", "mh.synapse.hosts.agy", "bind"],
                                input=json.dumps(other), text=True, capture_output=True, check=True, env=ENV)
        self.assertEqual(json.loads(result.stdout), {})

    def test_extract_user_drops_injected_block_and_acp_path(self):
        raw = "<USER_REQUEST>\n<memory-harness>\n[Souhatsu] 旧事\n</memory-harness>\n\n真正的问题\n</USER_REQUEST>"
        self.assertEqual(extract_user(raw), "真正的问题")
        self.assertTrue(is_acp("/x/antigravity-acp/conversations/abc.db"))
        self.assertFalse(is_acp("/x/brain/abc/.system_generated/logs/transcript.jsonl"))
        self.assertFalse(is_acp(""))

    def test_acp_tool_output_steps_count_as_context(self):
        with tempfile.TemporaryDirectory() as folder:
            full = Path(folder) / "transcript_full.jsonl"
            steps = [{"type": "USER_INPUT", "content": "<USER_REQUEST>\n问\n</USER_REQUEST>"},
                     {"type": "RUN_COMMAND", "content": "命令输出"},
                     {"type": "CODE_ACTION", "content": "改动"}]
            full.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in steps) + "\n")
            ctx, _, _ = read_context(full, "下一句")
            self.assertIn("命令输出", ctx)
            self.assertIn("改动", ctx)
            self.assertFalse(is_first_invocation(full, invocation_num=1))

    def test_bind_side_record_is_taken_once_by_tool_and_args(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            remember("evoke", {"words": ["摄影"], "session": "模型乱填的"}, "conv-1", "/w", folder=d, now=100)
            self.assertEqual(take("evoke", {"words": ["别的"]}, folder=d, now=101), {})
            got = take("evoke", {"words": ["摄影"]}, folder=d, now=101)
            self.assertEqual((got["session"], got["cwd"]), ("conv-1", "/w"))
            self.assertEqual(take("evoke", {"words": ["摄影"]}, folder=d, now=101), {})
            remember("roam", {}, "conv-2", "/w", folder=d, now=100)
            self.assertEqual(take("roam", {}, folder=d, now=100 + 121), {})
            self.assertNotEqual(bind_key("evoke", {"words": ["a"]}), bind_key("relive", {"words": ["a"]}))

    def test_bind_hook_writes_side_record_for_mcp(self):
        payload = {"conversationId": "conv-9", "workspacePaths": ["/workspace"],
                   "toolCall": {"name": "call_mcp_tool",
                                "args": {"ServerName": "mh", "ToolName": "relive", "Arguments": {"handles": ["L1"]}}}}
        run("bind", payload)
        got = take("relive", {"handles": ["L1"]}, folder=Path(_HOME.name) / "run/agy-bind")
        self.assertEqual(got.get("session"), "conv-9")

    def test_bind_bare_tool_name_from_acp_only_records(self):
        # ACP 实际给的（2026-11-20 直接驱动 adapter 抓到）：裸工具名，参数还是整个外壳
        folder = Path(_HOME.name) / "run/agy-bind"
        payload = {"conversationId": "acp-7", "workspacePaths": ["/w"],
                   "toolCall": {"name": "roam", "args": {"Arguments": {"node": "zeron"}, "ServerName": "mh", "ToolName": "roam"}}}
        self.assertEqual(run("bind", payload), {"decision": "allow"})
        self.assertEqual(take("roam", {"node": "zeron"}, folder=folder).get("session"), "acp-7")
        other = {"conversationId": "acp-7", "toolCall": {"name": "roam", "args": {"Arguments": {}, "ServerName": "别的", "ToolName": "roam"}}}
        self.assertEqual(run("bind", other), {})
        bare = {"conversationId": "acp-8", "workspacePaths": ["/w"], "toolCall": {"name": "relive", "args": {"handles": ["L1"]}}}
        self.assertEqual(run("bind", bare), {"decision": "allow"})
        self.assertEqual(take("relive", {"handles": ["L1"]}, folder=folder).get("session"), "acp-8")

    def test_acp_prompt_switch_commands_stop_without_model(self):
        for prompt, log in (("/no-observe", "observe-switch.jsonl"), ("/no-souhatsu", "souhatsu-switch.jsonl")):
            out = run("acp-prompt", {"sessionId": "acp-1", "cwd": "/tmp", "prompt": prompt})
            self.assertIn("stop", out)
            rows = [json.loads(x) for x in (Path(_HOME.name) / log).read_text().splitlines()]
            self.assertEqual((rows[-1]["session"], rows[-1]["state"]), ("acp-1", "off"))
        out = run("acp-prompt", {"sessionId": "acp-1", "cwd": "/tmp", "prompt": "/mhcore"})
        self.assertIn("/mhcore", out["stop"])
        self.assertEqual(run("acp-prompt", {"sessionId": "acp-1", "cwd": "/tmp", "prompt": ""}), {})

    def test_acp_hooks_do_nothing(self):
        payload = {"conversationId": "acp-2", "workspacePaths": ["/tmp"], "invocationNum": 1,
                   "transcriptPath": "/nowhere/antigravity-acp/conversations/acp-2.db"}
        self.assertEqual(run("associate", payload), {})

    def test_install_preserves_other_hook_groups_and_writes_mcp_config(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            (home / "hooks.json").write_text(json.dumps({"orca-status": {"Stop": [{"type": "command", "command": "echo 1"}]}}))
            install(home)
            hooks = json.loads((home / "hooks.json").read_text())
            self.assertIn("orca-status", hooks)
            self.assertIn("memory-harness", hooks)
            self.assertIn("SessionStart", hooks["memory-harness"])
            self.assertIn("PreInvocation", hooks["memory-harness"])
            self.assertIn("PreToolUse", hooks["memory-harness"])
            self.assertEqual(hooks["memory-harness"]["PreToolUse"][0]["matcher"],
                             "^mcp__?mh_.*|^call_mcp_tool$|^(evoke|relive|roam|yorimichi|shiori)$")
            mcp = json.loads((home / "mcp_config.json").read_text())
            self.assertEqual(mcp["mcpServers"]["mh"]["args"], ["-m", "mh.synapse.hosts.agy", "mcp"])
            self.assertTrue((home / "skills/mh-zhengming").is_symlink())
            self.assertTrue((home / "skills/mh-describe").is_symlink())


if __name__ == "__main__":
    unittest.main()
