"""原语与存储的测试：不可变、三种标记、错误分类、原文段落编号、申请。只测这一层。

python3 -m unittest mh.engram.store.test
"""
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from . import NA, NOT_DONE, UNKNOWN, absent, connect, iso, occurrence, operator
from .. import store as st


class ResourceTest(unittest.TestCase):
    def test_explicit_development_and_external_defaults(self):
        from . import instance
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            development = root / "development.md"
            with mock.patch.object(instance, "_D", {}), mock.patch.object(instance, "PATH", root / "instance.toml"):
                self.assertEqual(instance.resource("standing", development, "standing.md"), root / "standing.md")
                development.write_text("local")
                self.assertEqual(instance.resource("standing", development, "standing.md"), development)
            with mock.patch.object(instance, "_D", {"resources": {"standing": str(root / "explicit.md")}}):
                self.assertEqual(instance.resource("standing", development, "standing.md"), root / "explicit.md")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "engram.db"
        self.conn = connect(self.db)

    def test_markers_and_time(self):
        self.assertTrue(absent(UNKNOWN) and absent(NOT_DONE) and absent(NA))
        self.assertFalse(absent("未知") or absent(0) or absent(""))
        self.assertEqual(iso("2026-10-02 00:00:05"), "2026-10-02T00:00:05Z")
        self.assertEqual(iso("2026-10-02 00:00:05", 5), "2026-10-02T00:00:00Z")
        self.assertEqual(iso(""), UNKNOWN)

    def test_workspace(self):
        """工作区：按目录的映射（最长前缀）优先，其余取 cwd 最后一节；旧名字读的时候换成映射后的（2026-10-21）。"""
        h = "/h"
        dirs = {f"{h}/p/dsh/nas": "nas", f"{h}/p/dsh/mobile-ui": "dsh", f"{h}/p/dsh/tmp": "tmp", f"{h}/.dsh": "dsh",
                f"{h}/.dsh/.memory": ".memory", f"{h}/p/memory-system": ".memory"}      # 测试自己的映射，不读这台机器的实例配置
        with mock.patch.dict(st.WORKSPACE_DIRS, dirs, clear=True):
            self.assertEqual([st.workspace(f"{h}/{p}") for p in ("p/dsh/nas", "p/dsh/mobile-ui/x", "p/dsh", ".dsh",
                                                                  ".dsh/.memory", "p/memory-system", "p/nas")],
                             ["nas", "dsh", "dsh", "dsh", ".memory", ".memory", "nas"])
            self.assertEqual((st.workspace(h), st.workspace(f"{h}/r/.claude/worktrees/w"), st.workspace("")), ("h", "r", ""))
            self.assertEqual([st.canonical(w) for w in ("mobile-ui", "memory-system", ".dsh", "dsh", "x")],
                             ["dsh", ".memory", "dsh", "dsh", "x"])

    def test_delete_sessions(self):
        """删除（2026-10-22）：只属于这段会话的发生和 Claim 删掉，混着别的会话的留下；删除留记录，触发器装回去。"""
        from .delete import deleted, delete_sessions
        c, op = self.conn, operator(self.conn, "t", "observe", "observer")
        turns = {}
        for ses in ("a", "b"):
            sid = occurrence(c, "session", op)
            c.execute("INSERT INTO session_detail VALUES(?,?,?,?,?,?,?,?,?)", (sid, "claude", ses, NA, "user", NA, "/w", NA, NA))
            c.execute("INSERT INTO turn_text VALUES(?,?,?,?,?,?,?)", (f"t{ses}", "u", "a", "[]", "[]", "[]", st.now()))
            turns[ses] = occurrence(c, "turn", op, parent=sid)
            c.execute("INSERT INTO turn_detail VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (turns[ses], f"t{ses}", "claude", ses, 0, 1, "normal", NA, "[]", "human", "chat"))
        act = occurrence(c, "act", op, inputs={"turn": [turns["a"]]}, outcome="ok")
        c.execute("INSERT INTO event(born, idx, turn, text, mode, qualifier, speaker, segments, unresolved)"
                  " VALUES(?,0,?,'x',?,?,?,'[]','[]')", (act, turns["a"], UNKNOWN, UNKNOWN, UNKNOWN))
        batch = occurrence(c, "act", op, inputs={"turn": [turns["a"], turns["b"]]}, outcome="ok")
        c.commit()
        did = delete_sessions(c, [("claude", "a")], "测试会话")
        self.assertIsNone(c.execute("SELECT 1 FROM session_detail WHERE session='a'").fetchone())
        self.assertIsNone(c.execute("SELECT 1 FROM turn_text WHERE id='ta'").fetchone())
        self.assertEqual(c.execute("SELECT COUNT(*) FROM event").fetchone()[0], 0)
        self.assertIsNone(c.execute("SELECT 1 FROM occurrence WHERE id=?", (act,)).fetchone())
        self.assertIsNotNone(c.execute("SELECT 1 FROM occurrence WHERE id=?", (batch,)).fetchone())   # 混着 b，留下
        self.assertIsNotNone(c.execute("SELECT 1 FROM session_detail WHERE session='b'").fetchone())
        gone_sessions, gone = deleted(c)
        self.assertEqual(gone_sessions, {("claude", "a")})
        self.assertIn(turns["a"], gone["occurrence"])
        self.assertEqual(c.execute("SELECT role FROM occurrence WHERE id=?", (did,)).fetchone()[0], "deletion")
        with self.assertRaises(sqlite3.IntegrityError):                # 触发器装回去了
            c.execute("DELETE FROM occurrence")

    def test_append_only(self):
        op = operator(self.conn, "t", "observe", "observer")
        act = occurrence(self.conn, "act", op, outcome="ok")
        self.conn.execute("INSERT INTO event(born, idx, turn, text, mode, qualifier, speaker, segments, unresolved)"
                          " VALUES(?,0,?,'x',?,?,?,'[]','[]')", (act, NA, UNKNOWN, UNKNOWN, UNKNOWN))
        for sql in ("UPDATE event SET text='y'", "DELETE FROM event", "UPDATE occurrence SET outcome='timeout'",
                    "UPDATE operator SET model='m'"):
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                self.conn.execute(sql)
        with self.assertRaises(sqlite3.IntegrityError):     # 不用 NULL
            self.conn.execute("INSERT INTO judgment(born, idx, text) VALUES(1, 0, 'x')")

    def test_request(self):
        """申请（C1a，2026-10-19）：只写给直接相邻的一层，主会话除外；处理由 inputs 里带它的 act 推出。"""
        from . import main_operator, request, requests
        me = main_operator(self.conn, "m")
        op = operator(self.conn, "f", "form", "former")
        with self.assertRaises(ValueError):
            request(self.conn, "observe", "cognize", {"event": [1]}, "隔层", op)          # 隔层不传
        with self.assertRaises(ValueError):
            request(self.conn, "main", "form", {"form_record": [1]}, " ", me)            # 没写理由
        rid = request(self.conn, "main", "form", {"form_record": [1, 2]}, "一段会话被切成两份", me, cause="manual")
        self.assertEqual([(r["id"], r["state"]) for r in requests(self.conn, "form")], [(rid, "open")])
        occurrence(self.conn, "act", op, outcome="timeout", inputs={"request": [rid]})
        self.assertEqual(requests(self.conn, "form")[0]["state"], "failed")              # 只失败过：仍要处理
        occurrence(self.conn, "act", op, outcome="ok", inputs={"request": [rid], "event": [3]})
        self.assertEqual(requests(self.conn, "form"), [])
        self.assertEqual(requests(self.conn, "form", "handled")[0]["inputs"], {"form_record": [1, 2]})
        self.assertEqual(requests(self.conn, "observe", "all"), [])

    def test_scope_and_migrate_event(self):
        """点名申请、范围申请按 inputs 分（2026-10-21）；旧库的 event 没有 relations 列，连上时补上，旧行是 []。"""
        from . import scope
        self.assertEqual((scope({"event": [1]}), scope({"session": [2]}), scope({"session": [2], "event": []})),
                         ("named", "range", "range"))
        db = self.dir / "old-event.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE event(id INTEGER PRIMARY KEY, born INTEGER NOT NULL, idx INTEGER NOT NULL, turn INTEGER NOT NULL, "
                  "text TEXT NOT NULL, mode TEXT NOT NULL, qualifier TEXT NOT NULL, speaker TEXT NOT NULL, "
                  "segments TEXT NOT NULL, unresolved TEXT NOT NULL)")
        c.execute("INSERT INTO event VALUES(1,1,0,1,'x','m','','s','[]','[]')")
        c.commit()
        c.close()
        conn = connect(db)
        self.assertEqual(conn.execute("SELECT relations FROM event").fetchone()[0], "[]")

    def test_migrate_request(self):
        """旧的 request_detail 带 target 列：空表删掉重建；有数据就停下。"""
        db = self.dir / "old.db"
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE request_detail(occurrence INTEGER PRIMARY KEY, from_process TEXT NOT NULL, "
                  "to_process TEXT NOT NULL, reason TEXT NOT NULL, target TEXT NOT NULL)")
        c.commit()
        c.close()
        conn = connect(db)
        self.assertNotIn("target", [r[1] for r in conn.execute("PRAGMA table_info(request_detail)")])
        conn.close()
        db2 = self.dir / "old2.db"
        c = sqlite3.connect(db2)
        c.execute("CREATE TABLE request_detail(occurrence INTEGER PRIMARY KEY, from_process TEXT NOT NULL, "
                  "to_process TEXT NOT NULL, reason TEXT NOT NULL, target TEXT NOT NULL)")
        c.execute("INSERT INTO request_detail VALUES(1,'form','observe','x','y')")
        c.commit()
        c.close()
        with self.assertRaises(RuntimeError):
            connect(db2)

    def test_outcomes_and_blocks(self):
        from . import outcome, blocks
        self.assertEqual(outcome(None)[0], "ok")
        self.assertEqual(outcome("TimeoutExpired(['x'], 9)")[0], "timeout")
        self.assertEqual(outcome("HTTP 429 Too Many Requests")[0], "env_error")
        self.assertEqual(outcome("RuntimeError('codex exec 失败（1）：Selected model is at capacity. "
                                 "Please try a different model.')")[0], "env_error")
        self.assertEqual(outcome("RuntimeError('codex exec 失败（1）：ERROR codex_models_manager::manager: failed to refresh "
                                 "available models: request timed out\\nYou’ve hit your usage limit.')")[0], "env_error")
        # 2026-10-17 ark 周额度用完（阶段 B 的实验里遇到）：不计失败，休眠后再试
        self.assertEqual(outcome("RuntimeError('dsh web 没有正常结束：{\\'kind\\': \\'error\\', \\'error\\': {\\'message\\': "
                                 "\\'429: {\"code\":\"AccountQuotaExceeded\",\"message\":\"You have exceeded the weekly usage quota.\"}"
                                 "\\', \\'code\\': \\'RATE_LIMIT\\'}}')")[0], "env_error")
        self.assertEqual(outcome("finish_reason: content_filter")[0], "filtered")
        self.assertEqual(outcome("解析失败")[0], "content_error")
        self.assertEqual(st.restyle("合并成一句「引用：三个风险」，写进「文件：docs/a.md」，名字「dsh」不动"),
                         "合并成一句『引用：三个风险』，写进『文件：docs/a.md』，名字「dsh」不动")
        self.assertEqual(st.restyle("已经是『引用：甲』"), "已经是『引用：甲』")
        table, text = blocks.number({"user": "问一句\n\n再问", "assistant": "# 标题\n答", "commentary": ["先看看"]})
        self.assertEqual(list(table), ["U1", "U2", "A1", "A2", "A3"])
        self.assertEqual((table["A1"], table["A2"]), ("先看看", "# 标题"))
        self.assertIn("A1–A1 是过程中的话", text)


if __name__ == "__main__":
    unittest.main()
