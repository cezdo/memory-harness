"""拔掉 mh 起宿主：python3 -m unittest mh.synapse.hosts.unplug.test。只用假的日常配置，不碰真的 home。"""
import json
import tempfile
import unittest
from pathlib import Path

from . import REPO, claude_home, codex_home, dsh_home, strip_hooks

MH = f"/usr/bin/python3 -I -S {REPO}/mh/engram/elevate/hook.py claude prompt"
UI = "/home/x/tui/bin/agent-signal clear"


def hook(cmd):
    return {"type": "command", "command": cmd}


class StripTest(unittest.TestCase):
    def test_drops_mh_commands_and_empty_groups(self):
        hooks = {"SessionStart": [{"hooks": [hook(f"cd {REPO} && python3 -m mh inject")]}, {"hooks": [hook(UI)]}],
                 "Stop": [{"hooks": [hook(MH), hook(UI)]}],
                 "PreToolUse": [{"matcher": "^mcp__mh__", "hooks": [hook(f"env PYTHONPATH={REPO} python3 -m mh.synapse.hosts.codex bind")]}]}
        out, moved = strip_hooks(hooks)
        self.assertEqual(out, {"SessionStart": [{"hooks": [hook(UI)]}], "Stop": [{"hooks": [hook(UI)]}]})
        self.assertEqual(moved, {("SessionStart", 1, 0): (0, 0), ("Stop", 0, 1): (0, 0)})


class HomeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_claude(self):
        src = self.root / ".claude"
        (src / "skills/no-observe").mkdir(parents=True)
        (src / "skills/ppt").mkdir()
        (src / "commands").mkdir()
        (src / "commands/mhcore.md").write_text("x")
        (src / "commands/other.md").write_text("x")
        (src / ".credentials.json").write_text("{}")
        (src / "settings.json").write_text(json.dumps({"statusLine": {"x": 1}, "hooks": {"UserPromptSubmit": [{"hooks": [hook(MH), hook(UI)]}]}}))
        state = self.root / ".claude.json"
        state.write_text(json.dumps({"hasCompletedOnboarding": True, "mcpServers": {"mh": {}}, "projects": {"/a": {}}}))
        h = claude_home(self.root / "out", src, state)
        settings = json.loads((h / "settings.json").read_text())
        self.assertEqual(settings["hooks"], {"UserPromptSubmit": [{"hooks": [hook(UI)]}]})
        self.assertEqual(settings["statusLine"], {"x": 1})
        self.assertEqual(json.loads((h / ".claude.json").read_text()), {"hasCompletedOnboarding": True})
        self.assertEqual(sorted(p.name for p in (h / "skills").iterdir()), ["ppt"])
        self.assertEqual(sorted(p.name for p in (h / "commands").iterdir()), ["other.md"])
        self.assertTrue((h / ".credentials.json").is_symlink())
        # 宿主自己写的状态保留，mh 的 MCP 再去一次
        (h / ".claude.json").write_text(json.dumps({"numStartups": 3, "mcpServers": {"mh": {}, "x": {}}}))
        claude_home(self.root / "out", src, state)
        self.assertEqual(json.loads((h / ".claude.json").read_text()), {"numStartups": 3, "mcpServers": {"x": {}}})

    def test_codex(self):
        src = self.root / ".codex"
        src.mkdir()
        (src / "auth.json").write_text("{}")
        (src / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [hook(UI)]}, {"hooks": [hook(MH)]}],
                                                             "UserPromptSubmit": [{"hooks": [hook(MH)]}, {"hooks": [hook(UI)]}]}}))
        old = src / "hooks.json"
        (src / "config.toml").write_text(
            'model = "m"\n[projects."/a"]\ntrust_level = "trusted"\n'
            '[mcp_servers.mh]\ncommand = "py"\n[mcp_servers.mh.env]\nPYTHONPATH = "r"\n'
            '[mcp_servers.other]\ncommand = "o"\n[hooks.state]\n\n'
            f'[hooks.state."{old}:stop:0:0"]\ntrusted_hash = "a"\n'
            f'[hooks.state."{old}:stop:1:0"]\ntrusted_hash = "b"\n'
            f'[hooks.state."{old}:user_prompt_submit:1:0"]\ntrusted_hash = "c"\n')
        h = codex_home(self.root / "out", src)
        cfg = (h / "config.toml").read_text()
        new = h / "hooks.json"
        self.assertNotIn("mcp_servers.mh", cfg)
        self.assertIn("[mcp_servers.other]", cfg)
        self.assertIn(f'[hooks.state."{new}:stop:0:0"]\ntrusted_hash = "a"', cfg)
        self.assertIn(f'[hooks.state."{new}:user_prompt_submit:0:0"]\ntrusted_hash = "c"', cfg)
        self.assertNotIn('"b"', cfg)
        self.assertNotIn(str(old), cfg)
        self.assertEqual(json.loads(new.read_text())["hooks"], {"Stop": [{"hooks": [hook(UI)]}], "UserPromptSubmit": [{"hooks": [hook(UI)]}]})

    def test_dsh_copies_profile_without_mh_plugins(self):
        src = self.root / ".dsh"
        web = src / "profiles/web"
        (web / "node_modules/mh-synapse").mkdir(parents=True)
        (web / ".plugin-manager").mkdir()
        (web / "cordis.patch.yml").write_text("a: 1\n")
        (web / "package.json.bak-x").write_text("{}")
        (web / "package.json").write_text(json.dumps({"dsh": {"profile": {"bundles": ["base", "mh-synapse", "ctx"]}},
                                                      "dependencies": {"mh-synapse": f"link:{REPO}/mh/synapse/hosts/dsh", "ctx": "^1"}}))
        (src / ".credentials.yaml").write_text("k")
        h = dsh_home(self.root / "out", src)
        pkg = json.loads((h / "profiles/web/package.json").read_text())
        self.assertEqual(pkg["dsh"]["profile"]["bundles"], ["base", "ctx"])
        self.assertEqual(pkg["dependencies"], {"ctx": "^1"})
        out = h / "profiles/web"
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["cordis.patch.yml", "node_modules", "package.json"])
        self.assertFalse((out / "node_modules").is_symlink())          # 拷贝：插件管理修剪时动不到日常那份
        self.assertTrue((h / ".credentials.yaml").is_symlink())


if __name__ == "__main__":
    unittest.main()
