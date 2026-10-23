"""Engram 这一层的测试：检查（三条不变量、快照、按接口查覆盖）和阅读（回溯、阅读页）。各过程自己的做法在各自的测试里。

python3 -m unittest mh.engram.test
"""
import io
import json
import tempfile
import unittest
from pathlib import Path

from . import check, observe, show
from . import store as st
from .observe import collect
from .observe.sources.turn import Session, Turn
from .store.instance import USER_NAME as U


class Source:
    def __init__(self, path, session, turns):
        self.path, self.session, self.turns = path, session, turns

    def files(self):
        return [self.path]

    def parse(self, path):
        return self.session, self.turns


class Model:
    model, platform, isolation = "fake-model", "fake", "fake-iso"

    def __init__(self, outputs):
        self.outputs = list(outputs)

    def __call__(self, prompt):
        return self.outputs.pop(0), {}


class EngramTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.db = self.dir / "engram.db"
        self.conn = st.connect(self.db)
        f = self.dir / "s.jsonl"
        f.write_text("x")
        turns = [Turn("先做一版看看", "好的", closed=True), Turn("结果呢", "结果在报告里", closed=True), Turn("再说", "嗯", closed=True)]
        sess = Session("dsh", "s1", str(f), started="2026-01-01T00:00:00Z", cwd="/w", title="一段会话")
        collect.run(self.conn, sources=("dsh",), force=True, log=io.StringIO(), modules={"dsh": Source(f, sess, turns)})
        models = {"preview": Model(["### 第 1–2 轮\n类型：讨论\n读法：粗读\n"]), "deep": Model([]),
                  "rough": Model([f"### 事件1\n{U}提出先做一版。\n来源：1:U1\n\n### 事件2\n我说明结果在报告里。\n来源：2:A1"])}
        observe.run(self.conn, workers=1, models=models, log=io.StringIO())
        self.e1, self.e2 = [r[0] for r in self.conn.execute("SELECT id FROM event ORDER BY id")]

    def _form(self, events, judgments=(("判断一", []),)):
        """一次 form：读这些 E，写几条 J（关系 [{kind, to}]）。返回 J 编号。"""
        op = st.operator(self.conn, "form-test", "form", "former")
        act = st.occurrence(self.conn, "act", op, outcome="ok", inputs={"event": list(events)})
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (act, "form", "judge", "{}", "", "", "{}"))
        out = []
        for i, (text, rels) in enumerate(judgments):
            cur = self.conn.execute("INSERT INTO judgment(born, idx, text, holders, basis, basis_labels, relations, pattern, slots, "
                                    "unresolved) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                    (act, i, text, "[]", st.dumps({"event": list(events)}), "[]", st.dumps(rels),
                                     st.NOT_DONE, st.NOT_DONE, "[]"))
            out.append(cur.lastrowid)
        self.conn.commit()
        return out

    def test_check(self):
        report = check.check(self.conn, self.db)
        self.assertNotIn("来源：", report.split("覆盖（")[0])
        self.assertNotIn("取代：", report)
        cover = report.split("覆盖（")[1]
        self.assertIn("observe：待观测（还没预览）：1", cover)          # 第 3 轮
        self.assertIn("form：交给 form 的 E 还没被读过（条数）：2", cover)
        self._form([self.e1, self.e2])
        cover = check.check(self.conn, self.db).split("覆盖（")[1]
        self.assertNotIn("还没被读过", cover)
        # 绕过触发器改一条 event：下一次检查按快照认出来
        self.conn.execute("DROP TRIGGER event_no_update")
        self.conn.execute("UPDATE event SET text='改过' WHERE id=?", (self.e1,))
        self.assertIn("覆盖：event 里上次有的行被改过", check.check(self.conn, self.db))

    def test_check_relations(self):
        j1, = self._form([self.e1])
        j2, = self._form([self.e2], [("判断二", [{"kind": "supersedes", "to": j1}])])
        self.assertNotIn("取代：", check.check(self.conn, self.db))
        self._form([self.e2], [("判断三", [{"kind": "supersedes", "to": j2 + 5}])])     # 指向不存在的判断
        self.assertIn("取代：关系另一端的判断不存在", check.check(self.conn, self.db))

    def test_check_cognition_refs(self):
        """C 的成员和表述要解析得到（D1a，2026-10-21）：绕过 cognize.write 直接写一条坏引用，检查认出来。"""
        op = st.operator(self.conn, "cognize-test", "cognize", "cognizer")
        act = st.occurrence(self.conn, "act", op, outcome="ok")
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (act, "cognize", "write", "{}", "", "", "{}"))
        bad = st.dumps([{"kind": "form_record", "id": 999, "rel": "part"}])
        self.conn.execute('INSERT INTO cognition(born, kind, name, statements, members, "from", state, window) VALUES(?,?,?,?,?,?,?,?)',
                          (act, "concrete", "甲", st.dumps([{"text": "甲。", "refs": []}]), bad, "[]", st.NA, st.NA))
        self.conn.commit()
        self.assertIn("引用：C 的成员或表述解析不到", check.check(self.conn, self.db))

    def test_check_event_relations(self):
        """E 的取代（点名申请，2026-10-21）：两端存在、新的晚于旧的、旧 E 的出处在新 E 读的那几轮里；
        申请只写给相邻的一层，认领它的 act 属于接收的那一层。"""
        t1, t2 = [r[0] for r in self.conn.execute("SELECT occurrence FROM turn_detail ORDER BY ordinal LIMIT 2")]
        me = st.main_operator(self.conn, "m")
        rid = st.request(self.conn, st.MAIN, "observe", {"event": [self.e1]}, "这条说反了", me, cause="manual")
        op = st.operator(self.conn, "reread-test", "observe", "observer")

        def reread(turns, rels):
            act = st.occurrence(self.conn, "act", op, outcome="ok", inputs={"turn": turns, "request": [rid]}, cause="request")
            self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (act, "observe", "rough", st.dumps({"turns": turns}), "", "", "{}"))
            return self.conn.execute("INSERT INTO event(born, idx, turn, text, mode, qualifier, speaker, segments, unresolved, "
                                     "relations) VALUES(?,0,?,'新的','?unknown','','?unknown','[]','[]',?)",
                                     (act, turns[0], st.dumps(rels))).lastrowid
        e3 = reread([t1], [{"kind": "supersedes", "to": self.e1}])
        self.conn.commit()
        report = check.check(self.conn, self.db)
        self.assertNotIn("取代：", report)
        self.assertNotIn("申请：", report)
        self.assertIn(f"按申请被取代的 E", report)
        self.assertNotIn(self.e1, [e["id"] for e in observe.frame(self.conn)["events"]])
        new, old = show.trace(self.conn, f"E{e3}"), show.trace(self.conn, f"E{self.e1}")      # 回溯（C2）：取代、申请、去向
        self.assertIn(f"取代：E{self.e1}", new)
        self.assertIn(f"申请 {rid}", new)
        self.assertIn("这条说反了", new)
        self.assertIn(f"被取代：纠正写了 E{e3}", old)
        reread([t2], [{"kind": "supersedes", "to": self.e1}, {"kind": "supersedes", "to": e3 + 50}])
        bad = st.occurrence(self.conn, "act", st.operator(self.conn, "form-test", "form", "former"), outcome="ok",
                            inputs={"request": [rid]})
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (bad, "form", "pack", "{}", "", "", "{}"))
        self.conn.commit()
        report = check.check(self.conn, self.db)
        self.assertIn("取代：旧 E 的出处不在新 E 读的那几轮里", report)
        self.assertIn("取代：关系另一端的 E 不存在", report)
        self.assertIn("申请：认领它的 act 不属于接收的那一层", report)

    def test_show(self):
        j, = self._form([self.e1, self.e2])
        text = show.trace(self.conn, f"E{self.e1}")
        self.assertIn(f"{U}提出先做一版", text)
        self.assertIn("observe · 粗读", text)
        page = show.page(self.conn, "dsh", "s1")
        self.assertIn(f"{U}提出先做一版", page)
        self.assertIn("第 1–2 轮", page)


if __name__ == "__main__":
    unittest.main()
