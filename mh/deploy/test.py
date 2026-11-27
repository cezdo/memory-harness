import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from . import migrate


def machine(root, *, extra=()):
    """临时目录里搭一台机器：家目录、数据目录（一个小库和几样文件）、实例配置、一处宿主原文。"""
    home = Path(root) / "home"
    data = home / ".local/share/memory-harness"
    config = home / ".config/memory-harness"
    for d in (data / "events", data / "claude-home", data / "assoc-cache-engram", config, home / ".claude/projects/p"):
        d.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data / "engram.db")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(5)])
    conn.commit()
    conn.close()
    (data / "events/2026-10-10.jsonl").write_text("{}\n")
    (data / "claude-home/.credentials.json").write_text("secret")
    (data / "assoc-cache-engram/x").write_text("cache")
    (data / "observe.lock").write_text("")
    for name in extra:
        (data / name).write_text("?")
    (config / "instance.toml").write_text(f'projects = "~/proj"\n[tools]\ncodex = "{home}/bin/codex"\nother = "/nowhere/x"\n')
    (config / "instance.toml.bak-1").write_text("old")
    (config / "forward.toml").write_text("client")
    (config / "a.key").write_text("k")
    (home / ".claude/projects/p/s.jsonl").write_text("{}\n")
    return home, data, config


class Migrate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def kw(self, home, data, config):
        return dict(home=home, data=data, config=config, roots=[home / ".claude/projects", home / ".codex/sessions"],
                    log=io.StringIO())

    def test_classify(self):
        self.assertEqual(migrate.classify("engram.db"), "take")
        self.assertEqual(migrate.classify("engram.db-wal"), "stay")       # 快照里已含 WAL
        self.assertEqual(migrate.classify("codex-self-home"), "stay")
        self.assertEqual(migrate.classify("assoc-cache-engram"), "rebuild")
        self.assertIsNone(migrate.classify("new-thing.json"))

    def test_classify_follows_writer(self):
        """写它的服务不搬就留下；点名跟着搬才带走。"""
        self.assertEqual(migrate.classify("reply-log.jsonl"), "stay")
        self.assertEqual(migrate.classify("self-runs"), "stay")
        self.assertEqual(migrate.classify("tidy-state.json"), "take")
        self.assertEqual(migrate.classify("self-runs", (*migrate.LOOPS, "self")), "take")
        self.assertEqual(migrate.classify("tend", (*migrate.LOOPS, "self")), "stay")

    def test_round_trip(self):
        old = machine(self.root / "old")
        bundle = self.root / "bundle"
        m = migrate.export(bundle, **self.kw(*old))
        self.assertEqual(m["counts"], {"t": 5})
        self.assertEqual(m["originals"], [".claude/projects"])           # 不存在的来源目录跳过
        self.assertEqual(sorted(m["config"]), ["a.key", "instance.toml"])  # 备份、客户端的 forward.toml 不带
        self.assertFalse((bundle / "data/claude-home").exists())         # 隔离 home 留在原机
        self.assertFalse((bundle / "data/assoc-cache-engram").exists())  # 缓存重建
        self.assertIn(("tools.codex", f"{old[0]}/bin/codex"), [tuple(x) for x in m["absolute_paths"]])

        new = (self.root / "new/home", self.root / "new/home/.local/share/memory-harness", self.root / "new/home/.config/memory-harness")
        log = io.StringIO()
        migrate.import_(bundle, home=new[0], data=new[1], config=new[2], log=log)
        self.assertEqual(migrate.counts(new[1] / "engram.db"), {"t": 5})
        self.assertEqual((new[0] / ".claude/projects/p/s.jsonl").read_text(), "{}\n")
        self.assertTrue((new[2] / "instance.toml").exists())
        self.assertIn("旧机器家目录下", log.getvalue())                     # codex 指在旧家目录
        self.assertIn("tools.other = /nowhere/x", log.getvalue())

        with self.assertRaises(SystemExit):                              # 再导一次：目标已有库
            migrate.import_(bundle, home=new[0], data=new[1], config=new[2], log=io.StringIO())
        migrate.import_(bundle, home=new[0], data=new[1], config=new[2], force=True, log=io.StringIO())
        self.assertTrue(list(new[1].glob("engram.db.before-import-*")))

    def test_unknown_stops(self):
        old = machine(self.root / "old", extra=["new-thing.json"])
        with self.assertRaises(SystemExit) as e:
            migrate.export(self.root / "bundle", **self.kw(*old))
        self.assertIn("new-thing.json", str(e.exception))

    def test_running_loop_stops(self):
        old = machine(self.root / "old")
        import fcntl
        with open(old[1] / "observe.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(SystemExit):
                migrate.export(self.root / "b1", **self.kw(*old))
            m = migrate.export(self.root / "b2", live=True, **self.kw(*old))
            self.assertTrue(m["live"])

    def test_wal_content_in_snapshot(self):
        old = machine(self.root / "old")
        keep = sqlite3.connect(old[1] / "engram.db")                     # 开着不关：新写的还在 WAL 里
        keep.execute("INSERT INTO t VALUES(99)")
        keep.commit()
        m = migrate.export(self.root / "bundle", **self.kw(*old))
        keep.close()
        self.assertEqual(m["counts"], {"t": 6})

    def test_conflicting_file_stops(self):
        old = machine(self.root / "old")
        bundle = self.root / "bundle"
        migrate.export(bundle, **self.kw(*old))
        new = self.root / "new/home"
        (new / ".claude/projects/p").mkdir(parents=True)
        (new / ".claude/projects/p/s.jsonl").write_text("different\n")
        with self.assertRaises(SystemExit) as e:
            migrate.import_(bundle, home=new, data=new / "d", config=new / "c", log=io.StringIO())
        self.assertIn("s.jsonl", str(e.exception))

    def test_real_sources(self):
        """来源模块的目录都在家目录下（数据目录里的随隔离 home 留下）。"""
        home = Path.home()
        for r in migrate.source_roots():
            self.assertTrue(r.is_relative_to(home))


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from . import install
        self.install = install

    def test_tools(self):
        root = self.root / "tools"
        (root / "typesafe/venv/bin").mkdir(parents=True)
        (root / "typesafe/venv/bin/python").write_text("")
        (root / "lexicon").mkdir()                                     # 空目录不算装好
        steps = {s.name: s for s in self.install.tool_steps(root)}
        self.assertEqual(steps["工具 typesafe"].state, self.install.OK)
        self.assertEqual(steps["工具 lexicon"].state, self.install.HUMAN)   # 要拷数据，没给 --from
        self.assertEqual(steps["工具 ltp"].state, self.install.HUMAN)
        steps = {s.name: s for s in self.install.tool_steps(root, source="oldhost:/usr/local/mh-tools")}
        self.assertEqual(steps["工具 ltp"].state, self.install.TODO)
        self.assertIn("pip 装 torch==", steps["工具 ltp"].detail)

    def test_relay(self):
        """relay（X10）：没编、源码比二进制新都是“缺”；没有 cargo 要人装；编好且不旧算好。"""
        import os
        root = self.root / "tools"
        s = self.install.relay_step(root, which=lambda t: "/bin/cargo")
        self.assertEqual(s.state, self.install.TODO)
        out = root / "relay/mh-relay"
        out.parent.mkdir(parents=True)
        out.write_text("")
        os.utime(out, (1, 1))                                          # 比源码旧
        self.assertEqual(self.install.relay_step(root, which=lambda t: "/bin/cargo").state, self.install.TODO)
        os.utime(out, None)
        self.assertEqual(self.install.relay_step(root, which=lambda t: None).state, self.install.OK)
        out.unlink()
        from unittest.mock import patch
        with patch.object(Path, "home", lambda: self.root):
            self.assertEqual(self.install.relay_step(root, which=lambda t: None).state, self.install.HUMAN)

    def test_tunnel(self):
        """隧道（X7）：按 [deploy.tunnel] 拼 ssh；没配就没有这一步。"""
        from . import tunnel
        self.assertIsNone(tunnel.command({}))
        self.assertIsNone(self.install.tunnel_step(cfg={}))
        cmd = tunnel.command({"ssh": "box", "forward": ["127.0.0.1:1:127.0.0.1:2"], "control": "/tmp/cm"})
        self.assertEqual(cmd[0], "/usr/bin/ssh")
        self.assertEqual(cmd[-3:], ["-L", "127.0.0.1:1:127.0.0.1:2", "box"])
        self.assertIn("ControlPath=/tmp/cm", cmd)
        self.assertIn("ControlMaster=yes", cmd)
        self.assertNotIn("ControlMaster=yes", tunnel.command({"ssh": "box"}))

    def test_cli(self):
        from unittest.mock import patch
        self.assertEqual(self.install.cli_step(lambda t: f"/bin/{t}").state, self.install.OK)
        with patch.dict("mh.engram.store.instance.INVOKE", {}, clear=True):
            s = self.install.cli_step(lambda t: None)
            self.assertEqual(s.state, self.install.HUMAN)
        with patch.dict("mh.engram.store.instance.INVOKE", {"forward": "http://laptop:3091"}, clear=True):
            s = self.install.cli_step(lambda t: None)                 # 交给别的机器代跑也算好
            self.assertEqual(s.state, self.install.OK)
            self.assertIn("laptop:3091", s.detail)

    def test_config_and_data(self):
        cfg = self.root / "instance.toml"
        s = self.install.config_steps(cfg)[0]
        self.assertEqual(s.state, self.install.TODO)
        s.do()                                                         # 全新部署：照示例抄一份
        self.assertEqual(self.install.config_steps(cfg)[0].state, self.install.OK)
        cfg.write_text('[tools]\ncodex = "/nowhere/codex"\n[show]\npaths = [{path = "/etc/x"}]\n'
                       '[deploy.clients]\nlaptop = "/home/someone"\n')
        s = self.install.config_steps(cfg)[0]
        self.assertEqual(s.state, self.install.HUMAN)
        self.assertIn("/nowhere/codex", s.detail)
        self.assertNotIn("/etc/x", s.detail)                           # 展示页的说明不算
        self.assertNotIn("/home/someone", s.detail)                   # 客户端的家目录在那台机器上，不是这台的
        db = self.root / "engram.db"
        self.assertEqual(self.install.data_step(db).state, self.install.HUMAN)
        s = self.install.data_step(db, fresh=True)
        s.do()
        self.assertEqual(self.install.data_step(db).state, self.install.OK)

    def test_test_modules(self):
        mods = self.install.test_modules()
        self.assertIn("mh.deploy.test", mods)
        self.assertIn("mh.rhythm.quota_test", mods)

    def test_forward_step(self):
        path = self.root / "forward.toml"
        s = self.install.forward_step("server", python="/opt/homebrew/bin/python3", path=path)
        self.assertEqual(s.state, self.install.TODO)
        from ..synapse.hosts import forward
        path.write_text(s.detail.split("：", 1)[1].replace("，", "\n") + "\n")
        self.assertEqual(forward.config(path)["ssh"], "server")
        self.assertEqual(self.install.forward_step("server", python="/opt/homebrew/bin/python3", path=path).state, self.install.OK)

    def test_forward_mcp_relay(self):
        from ..synapse.hosts import forward
        cfg = {"ssh": "server", "repo": "memory-harness", "python": "python3", "device": "laptop"}
        # 既不是 MCP 也不是代跑钩子的照旧直接 exec python
        other = forward.command(cfg, ["python3", "-m", "mh", "check"], env={})[-1]
        self.assertNotIn("mh-relay", other)
        # inject / associate 钩子经 --hook 走远端 relay
        inj = forward.command(cfg, ["python3", "-m", "mh", "inject"], env={})[-1]
        self.assertIn("mh-relay", inj)
        self.assertIn("--hook inject", inj)
        assoc = forward.command(cfg, ["python3", "-m", "mh.synapse.push.associate"], env={})[-1]
        self.assertIn("--hook associate", assoc)
        # MCP 入口（agy）自动探测远端 mh-relay，带 MH_HOST=agy 并去掉外层 CLAUDE_CODE_SESSION_ID
        agy = forward.command(cfg, ["python3", "-m", "mh.synapse.hosts.agy", "mcp"],
                              env={"CLAUDE_CODE_SESSION_ID": "outer"})[-1]
        self.assertIn("mh-relay", agy)
        self.assertIn("MH_HOST=agy", agy)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", agy)
        # 默认 MCP（pull.mcp）保留 CLAUDE_CODE_SESSION_ID，也走 relay
        claude = forward.command(cfg, ["python3", "-m", "mh.synapse.pull.mcp"],
                                 env={"CLAUDE_CODE_SESSION_ID": "s1"})[-1]
        self.assertIn("mh-relay", claude)
        self.assertIn("CLAUDE_CODE_SESSION_ID=s1", claude)


class Push(unittest.TestCase):
    def test_commands(self):
        from . import push
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / ".claude/projects").mkdir(parents=True)
            cmds = push.commands("server", [home / ".claude/projects", home / ".codex/sessions"], home=home)
            skipped = push.commands("server", [home / ".claude/projects"], home=home,
                                    skip={home / ".claude/projects": ["-w/b.jsonl", "-w/a.jsonl"]})
        self.assertEqual(skipped[1][1:4], ["-a", "--exclude=/-w/a.jsonl", "--exclude=/-w/b.jsonl"])   # 空会话不推（11-27）
        self.assertEqual(len(cmds), 2)                                # 本机没有的目录不推（不会把服务机那份清空）
        self.assertTrue(cmds[0][-1].endswith("mkdir -p .claude/projects"))
        self.assertEqual(cmds[1][-2:], [f"{home}/.claude/projects/", "server:.claude/projects/"])
        self.assertNotIn("--delete", cmds[1])                         # 服务机上别的文件不碰
        self.assertEqual(push.commands("server", [], home=home), [])


if __name__ == "__main__":
    unittest.main()
