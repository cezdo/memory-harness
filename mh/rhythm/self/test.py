"""self 的程序部分（design/rhythm/self/设计.md）：读便签、判到点、挑哪张叫醒、醒来眼前的东西、核查自己做了什么、手记一节。
不起 claude。"""
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

from . import REPO, ROOT, TO_USER, awaken, due, hosts, notes, own_actions, pick, section, thread, turns, wakes
from ...engram import store
from .hosts import claude


class SelfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "self"
        self.db = Path(self.tmp.name) / "engram.db"          # 不建：section 读不到库就不列轮次
        (self.root / "便签" / "撕了").mkdir(parents=True)

    def note(self, name, text):
        (self.root / "便签" / f"{name}.md").write_text(text)

    def test_notes(self):
        self.note("命名影子", "到：2026-11-22\n---\n我想看命名影子攒了几次。\n上次看到 4 次。\n")
        self.note("探索", "每：7 天\n\n---\n看看 mh 的现状。")
        self.note("怪", "下周二\n到: 2026-11-30\n---\n正文")
        self.note("没有头", "只有正文")
        self.note("别的宿主", "谁：codex\n谁：claude\n---\n还没接入的宿主认不出")
        (self.root / "便签" / "撕了" / "旧.md").write_text("到：2026-11-01\n---\n撕掉的不算")
        got = {n["name"]: n for n in notes(self.root)}
        self.assertEqual(sorted(got), ["别的宿主", "命名影子", "怪", "探索", "没有头"])
        self.assertEqual((got["别的宿主"]["bad"], got["别的宿主"]["host"]), (["谁：codex"], "claude"))   # codex 还没接入
        self.assertEqual(got["探索"]["host"], hosts.DEFAULT)
        self.assertEqual(got["命名影子"]["at"], date(2026, 11, 22))
        self.assertEqual(got["命名影子"]["body"], "我想看命名影子攒了几次。\n上次看到 4 次。")
        self.assertEqual(got["探索"]["every"], 7)
        self.assertEqual((got["怪"]["bad"], got["怪"]["at"]), (["下周二"], date(2026, 11, 30)))
        self.assertEqual((got["没有头"]["at"], got["没有头"]["every"], got["没有头"]["body"]), (None, None, "只有正文"))

    def test_due(self):
        at = {"at": date(2026, 11, 22), "every": None}
        self.assertFalse(due(at, datetime(2026, 11, 21, 23)))
        self.assertTrue(due(at, datetime(2026, 11, 22, 0, 5)))
        self.assertFalse(due(at, datetime(2026, 11, 23), woke=datetime(2026, 11, 22, 9)))      # 醒过就不再算
        every = {"at": None, "every": 7}
        self.assertTrue(due(every, datetime(2026, 11, 8)))                                     # 没醒过
        self.assertFalse(due(every, datetime(2026, 11, 14), woke=datetime(2026, 11, 8)))
        self.assertTrue(due(every, datetime(2026, 11, 15), woke=datetime(2026, 11, 8)))
        self.assertFalse(due({"at": None, "every": None}, datetime(2026, 11, 8)))

    def test_pick(self):
        free, now = (lambda: False), datetime(2026, 11, 22, 10)
        self.assertEqual(pick(now, {}, self.root, free), (None, ""))                           # 没有便签
        self.note("晚", "到：2026-11-21\n---\n晚到的")
        self.note("早", "到：2026-11-09\n---\n早到的")
        self.note("周", "每：7 天\n---\n每周")
        st = {"woke": {"周": "2026-11-20T10:00:00"}}
        self.assertEqual(pick(now, st, self.root, free)[0]["name"], "早")                      # 到点最早的先叫
        self.assertIsNone(pick(now, {**st, "run": {"note": "x"}}, self.root, free)[0])          # 有一次醒来还没收
        self.assertIsNone(pick(now, st, self.root, lambda: True)[0])                           # 额度吃紧
        full = {**st, "log": ["2026-11-22T01:00:00", "2026-11-22T04:00:00", "2026-11-22T07:00:00"]}
        self.assertIn("醒了 3 次", pick(now, full, self.root, free)[1])                         # 自我交流要少
        st2 = {**st, "retry": {"早": "2026-11-23T00:00:00"}}
        self.assertEqual(pick(now, st2, self.root, free)[0]["name"], "晚")                     # 没正常结束的隔一天再叫

    def test_thread(self):
        class Host:
            NAME, ok = "claude", True
            new_session = staticmethod(lambda: "new")
            resumable = lambda self, s: self.ok
        h = Host()
        self.assertEqual(thread({}, "探索", h), ("new", False, None))
        self.assertEqual(thread({"threads": {"探索": "abc"}}, "探索", h), ("abc", True, "abc"))          # 旧的存法照旧认
        self.assertEqual(thread({"threads": {"探索": {"host": "claude", "session": "abc"}}}, "探索", h), ("abc", True, "abc"))
        self.assertEqual(thread({"threads": {"探索": {"host": "codex", "session": "abc"}}}, "探索", h), ("new", False, "abc"))  # 换了宿主
        h.ok = False
        self.assertEqual(thread({"threads": {"探索": "abc"}}, "探索", h), ("new", False, "abc"))       # 太长了另开一段

    def test_claude_resumable(self):
        h = Path(self.tmp.name) / "home"
        (h / "projects" / "p").mkdir(parents=True)
        self.assertFalse(claude.resumable("abc", h))
        (h / "projects" / "p" / "abc.jsonl").write_text("{}\n")
        self.assertTrue(claude.resumable("abc", h))
        (h / "projects" / "p" / "abc.jsonl").write_text("x" * claude.RESUME_BYTES)
        self.assertFalse(claude.resumable("abc", h))

    def test_awaken(self):
        n = {"name": "探索", "at": None, "every": 7, "body": "看看 mh 的现状。"}
        got = awaken(n, datetime(2026, 11, 15, 10), self.root / "2026-11-15-探索", False, "abc",
                     datetime(2026, 11, 8, 1), self.root / "草稿")
        self.assertTrue(got.startswith("看看 mh 的现状。"))                                      # 便签在前，是我自己的话
        self.assertIn("每：7 天，上次醒在 11-08 01:00", got)
        self.assertIn("另开一段", got)
        self.assertIn("草稿纸", got)
        self.assertIn("main 在", got)                                                          # 世界的现状由程序填
        self.assertNotIn("不会被采进记忆", got)                                                 # 那句只属于旧的方向探索
        from ...engram.observe.label import wake_note
        self.assertEqual(wake_note(got), "探索")                                               # form 按它把会话挂到便签那条线

    def test_own_actions(self):
        scratch = Path(self.tmp.name) / "草稿"
        acts = [("write", str(self.root / "2026-11-15-探索" / "结果.md")), ("write", str(scratch / "mh" / "x.py")),
                ("write", str(REPO / "AGENTS.md")), ("run", "git -C ~/memory-harness log -3"), ("run", "git commit -qm x"),
                ("run", "systemctl --user restart memory-harness-form")]
        outside, risky = own_actions(acts, self.root, scratch)
        self.assertEqual(outside, [str(REPO / "AGENTS.md")])
        self.assertEqual(len(risky), 2)

    def test_claude_log(self):
        """Claude Code 的运行记录（stream-json）→ 自己做了什么、怎么结束的。"""
        logs = Path(self.tmp.name) / "logs"
        logs.mkdir()
        use = lambda name, **inp: json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": inp}]}})
        (logs / "run.jsonl").write_text("\n".join([
            use("Write", file_path="/a/b.md"), use("Edit", file_path="/a/c.py"), use("Read", file_path="/a/d"),
            use("Bash", command="ls"), "not json",
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": " 留下了 x ", "session_id": "s1"})]))
        self.assertEqual(claude.actions(logs), [("write", "/a/b.md"), ("write", "/a/c.py"), ("run", "ls")])
        self.assertEqual(claude.outcome(logs), (True, "留下了 x", "s1"))
        self.assertEqual(claude.outcome(Path(self.tmp.name)), (False, "", ""))

    def test_hooks_home(self):
        h = claude.home(Path(self.tmp.name) / "home")
        s = json.loads((h / "settings.json").read_text())
        cmds = [x["command"] for v in s["hooks"].values() for g in v for x in g["hooks"]]
        self.assertTrue(all(str(REPO) in c for c in cmds))                                     # 只装 mh 自己的钩子
        self.assertFalse(s["autoMemoryEnabled"])
        self.assertIn("mh", json.loads((h / "mcp.json").read_text())["mcpServers"])
        self.assertTrue((h / ".credentials.json").is_symlink())

    def test_hosts_contract(self):
        """每个接入的宿主都实现了接入标准里的那组函数。"""
        for name, m in hosts.HOSTS.items():
            self.assertEqual(m.NAME, name)
            for f in ("ready", "new_session", "resumable", "launch", "outcome", "actions"):
                self.assertTrue(callable(getattr(m, f, None)), f"{name}.{f}")
        self.assertIn(hosts.DEFAULT, hosts.HOSTS)

    def test_section(self):
        now = datetime(2026, 11, 22, 10)
        self.assertEqual(section("/elsewhere", now, self.root, {}, {}), [])                    # 只给 memory-harness
        self.note("命名影子", "到：2026-11-22\n---\n我想看命名影子攒了几次。")
        self.note("还早", "到：2026-12-01\n---\n还没到")
        self.note("怪", "下周二\n---\n正文")
        w = self.root / "2026-11-15-探索"
        w.mkdir()
        (w / TO_USER).write_text("x")
        (w / "核查.md").write_text("- 越界：**有**\n")
        (self.root / "2026-10-01-太早").mkdir()
        (self.root / "2026-11-15-探索-2").mkdir()
        (self.root / "不是醒来").mkdir()
        self.assertEqual([n for _, n, _ in wakes(self.root)], ["太早", "探索", "探索"])
        got = "\n".join(section(REPO, now, self.root, {}, {}, self.db))
        self.assertIn("到点的便签", got)
        self.assertIn("命名影子攒了几次", got)
        self.assertNotIn("还没到", got)
        self.assertIn("认不出的条件：下周二", got)
        self.assertIn("11-15 醒来（探索）", got)
        self.assertIn(TO_USER, got)
        self.assertIn("核查：越界", got)
        self.assertNotIn("太早", got)
        self.assertNotIn("命名影子攒了几次", "\n".join(section(REPO, now, self.root, {"命名影子": datetime(2026, 11, 22, 9)}, {}, self.db)))
        busy = {"run": {"note": "命名影子", "started": "2026-11-22T09:00:00", "dir": str(self.root / "2026-11-22-命名影子")}}
        got = "\n".join(section(REPO, now, self.root, {}, busy, self.db))
        self.assertIn("醒着：命名影子", got)
        self.assertNotIn("命名影子.md", got)                                                    # 醒着的那张不再列成到点

    def turn_rows(self, rows):
        """往临时库里放轮次：[(会话, cwd, 开始时刻)]。"""
        conn = store.connect(self.db)
        for i, (ses, cwd, started) in enumerate(rows, 1):
            conn.execute("INSERT OR IGNORE INTO session_detail VALUES (?, 'claude', ?, '', 'main', '', ?, '', '')", (1000 + i, ses, cwd))
            conn.execute("INSERT INTO occurrence VALUES (?, 'turn', ?, ?, 'turn', 0, '{}', '?', 0, 0, 'ok', '', '{}')", (i, started, started))
            conn.execute("INSERT INTO turn_detail(occurrence, text, source, session, ordinal, closed, ending, prev, address, origin, host_kind)"
                         " VALUES (?, '', 'claude', ?, ?, 1, '', 0, '', 'human', '')", (i, ses, i))
        conn.commit()
        conn.close()

    def test_turns(self):
        now = datetime(2026, 11, 22, 10)
        self.assertEqual(turns(now, self.db), (0, 0))                                          # 没有库
        self.turn_rows([("外", "/home/u/proj", "2026-11-20T01:00:00Z")])
        self.assertEqual(turns(now, self.db), (0, 1))
        self.assertNotIn("轮次", "\n".join(section(REPO, now, self.root, {}, {}, self.db)))      # self 一轮都没有：不列
        self.assertEqual(section(REPO, now, self.root, {}, {}, self.db), [])                   # 没别的事，仍是空
        self.db.unlink()
        self.turn_rows([("外", "/home/u/proj", "2026-11-20T01:00:00Z"),
                        ("外", "/home/u/proj", "2026-11-21T01:00:00.123Z"),
                        ("醒", str(ROOT), "2026-11-21T02:00:00Z"),
                        ("醒", str(ROOT), "2026-11-01T02:00:00Z"),                            # 窗口外
                        ("醒", str(ROOT), "?unknown")])                                       # 时刻不知道的不算
        self.assertEqual(turns(now, self.db), (1, 2))
        self.assertIn("- 最近 7 天的轮次：self 1，别的工作区 2", section(REPO, now, self.root, {}, {}, self.db))


if __name__ == "__main__":
    unittest.main()
