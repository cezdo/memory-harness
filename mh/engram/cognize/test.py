"""cognize 的测试（D1a、D1a′）：写入时的检查、现状与身份链、候选与已验证、主会话确认、按名字和别名查、骨架节点当成员，
引用的身份加钉住的版本（对得上才解析得到；下一级变了不再新鲜，按身份找到现在的版本）。
不调模型。python3 -m unittest mh.engram.cognize.test
"""
import json
import tempfile
import unittest
import subprocess
from unittest.mock import patch
from pathlib import Path

from .. import cognize
from .. import store as st
from ..form import skeleton
from ..store.instance import USER_NAME as U


class CognizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = st.connect(Path(self.tmp.name) / "engram.db")
        op = st.operator(self.conn, "cognize-test", "cognize", "cognizer")
        self.act = st.occurrence(self.conn, "act", op, cause="manual", outcome="ok")
        self.me = st.main_operator(self.conn, "test")
        self.pack = st.occurrence(self.conn, "act", op, cause="manual", outcome="ok")
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                          (self.pack, "form", "pack", json.dumps({"unit": ["claude", "s1", 1]}), "", "", "{}"))
        self.recs = [self.record(i) for i in range(3)]
        self.entry = self.conn.execute(
            "INSERT INTO session_entry(born, record, source, session, name, topic, stops, words, unresolved) VALUES(?,?,?,?,?,?,?,?,?)",
            (self.act, self.pack, "claude", "s1", "话题", "", "[]", "[]", "[]")).lastrowid

    def record(self, i, relations="[]", born=None):
        return self.conn.execute(
            "INSERT INTO form_record(born, idx, kind, text, refs, labels, structures, unresolved, relations) VALUES(?,?,?,?,?,?,?,?,?)",
            (born or self.pack, i, "session", f"陈述{i}", "{}", "[]", "{}", "[]", relations)).lastrowid

    def line(self, from_=()):
        return self.conn.execute('INSERT INTO topic_line(born, name, workspace, node, parent, "from") VALUES(?,?,?,?,?,?)',
                                 (self.act, "线", "memory-harness", "", st.NA, json.dumps(list(from_)))).lastrowid

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def ref(self, i):
        return cognize.pin(self.conn, "form_record", self.recs[i])

    def write(self, name, members=None, from_=(), kind="concrete"):
        members = members or [dict(self.ref(0), rel="part"), dict(self.ref(1), rel="part")]
        return cognize.write(self.conn, self.act, kind, name, [{"text": f"{name}是这样。", "refs": [self.ref(0)]}], members,
                             from_=from_)

    def test_checks(self):
        bad = [("要有成员", dict(members=[])),
               ("成员要带种类", dict(members=[self.ref(0)])),
               ("解析不到", dict(members=[{"kind": "form_record", "id": 999, "rel": "part"}])),
               ("解析不到", dict(members=[{"kind": "skeleton", "id": "k-没有", "rel": "part"}])),
               ("解析不到", dict(members=[{"kind": "form_record", "id": self.recs[0], "rel": "part"}]))]   # D1a 的写法：没有身份
        for msg, kw in bad:
            with self.assertRaisesRegex(ValueError, msg):
                cognize.write(self.conn, self.act, "concrete", "甲", [{"text": "甲。", "refs": [self.ref(0)]}], **kw)
        with self.assertRaisesRegex(ValueError, "至少要两个"):
            self.write("类", members=[dict(self.ref(0), rel="instance")], kind="abstract")
        with self.assertRaisesRegex(ValueError, "带引用"):
            cognize.write(self.conn, self.act, "concrete", "甲", [{"text": "甲。", "refs": []}], [dict(self.ref(0), rel="part")])

    def test_successor_and_names(self):
        a = self.write("DSH 升级")
        with self.assertRaisesRegex(ValueError, "重名"):
            self.write("dsh  升级")
        b = self.write("DSH 升级", from_=[a])          # 接替：名字可以沿用
        self.assertEqual(cognize.current(self.conn), [b])
        self.assertEqual(cognize.chain(self.conn, b), [a, b])
        with self.assertRaisesRegex(ValueError, "现状"):
            self.write("另一件", from_=[a])            # 已经是历史
        c = self.write("拍照")
        cognize.alias(self.conn, self.act, c, "摄影")
        self.assertEqual(cognize.lookup(self.conn, "摄影"), c)
        with self.assertRaisesRegex(ValueError, "重名"):
            cognize.alias(self.conn, self.act, b, "摄影")
        m = self.write("拍照", from_=[b, c])           # 合并：别名跟着身份走
        self.assertEqual(cognize.lookup(self.conn, "摄影"), m)
        self.assertEqual(cognize.chain(self.conn, m), sorted([a, b, c, m]))

    def test_candidate_and_verified(self):
        a = self.write("观察的开关")
        self.assertFalse(cognize.verified(self.conn, a))
        cognize.alias(self.conn, self.act, a, "no-observe")
        with self.assertRaisesRegex(ValueError, "主会话"):
            cognize.confirm(self.conn, st.operator(self.conn, "cognize-test", "cognize", "cognizer"), a, "对")
        b = cognize.confirm(self.conn, self.me, a, f"{U}确认", name="不观察的开关")
        self.assertTrue(cognize.verified(self.conn, b))
        self.assertEqual(cognize.get(self.conn, b)["from"], [a])
        self.assertEqual(cognize.get(self.conn, b)["members"], cognize.get(self.conn, a)["members"])
        self.assertEqual(cognize.lookup(self.conn, "no-observe"), b)
        c = self.write("不观察的开关", from_=[b])       # cognize 再接替：又是候选
        self.assertFalse(cognize.verified(self.conn, c))

    def test_skeleton_member(self):
        root = Path(self.tmp.name)
        development = root / "design"
        (development / "engram/cognize").mkdir(parents=True)
        (development / "engram/cognize/设计.md").write_text("# Synthetic skeleton\n")
        for args in (["init"], ["add", "design"], ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "synthetic"]):
            subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
        self.enterContext(patch.dict(skeleton.WORKSPACES, {"memory-harness": (development, "Memory Harness")}))
        kid = skeleton.nodes("memory-harness")["engram/cognize"]["id"]
        ref = cognize.pin(self.conn, "skeleton", kid)
        self.assertEqual((ref["workspace"], ref["path"], ref["id"]),
                         ("memory-harness", "engram/cognize", skeleton.version("memory-harness")))
        cid = self.write("cognize 的方向", members=[dict(ref, rel="part"), dict(self.ref(2), rel="part")])
        member = cognize.get(self.conn, cid)["members"][0]
        self.assertTrue(cognize.resolves(self.conn, member))
        self.assertTrue(cognize.fresh(self.conn, member))
        for bad in (dict(ref, id="0" * 40), dict(ref, path="没有这个节点"), dict(ref, id=12345)):
            self.assertFalse(cognize.resolves(self.conn, bad), bad)

    def test_statement_pinned(self):
        ref = self.ref(1)
        self.assertEqual((ref["session"], ref["seq"]), (["claude", "s1"], 2))
        self.assertTrue(cognize.fresh(self.conn, ref))
        self.assertFalse(cognize.resolves(self.conn, dict(ref, seq=3)))          # 身份和钉住的版本对不上
        self.assertFalse(cognize.resolves(self.conn, dict(ref, session=["claude", "s2"])))
        new = self.record(9, relations=json.dumps([{"kind": "supersedes", "to": self.recs[1]}]))   # 修订提示改写：沿用 S2
        self.assertTrue(cognize.resolves(self.conn, ref))                         # 钉住的旧版照样解析得到
        self.assertFalse(cognize.fresh(self.conn, ref))
        self.assertEqual(cognize.now(self.conn, ref), [new])
        self.assertEqual(cognize.pin(self.conn, "form_record", new)["seq"], 2)
        op = st.operator(self.conn, "restate-test", "form", "restater")
        withdraw = st.occurrence(self.conn, "act", op, cause="manual", outcome="ok")
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                          (withdraw, "form", "restate", "{}", "", "", json.dumps({"withdrawn": {str(self.recs[0]): "不对"}})))
        self.assertEqual(cognize.now(self.conn, self.ref(0)), [])                # 撤下了

    def test_entry_line_cognition_pinned(self):
        t = cognize.pin(self.conn, "session_entry", self.entry)
        self.assertEqual(t["session"], ["claude", "s1"])
        self.assertTrue(cognize.fresh(self.conn, t))
        t2 = self.conn.execute(
            "INSERT INTO session_entry(born, record, source, session, name, topic, stops, words, unresolved) VALUES(?,?,?,?,?,?,?,?,?)",
            (self.act, self.pack, "claude", "s1", "改了名", "", "[]", "[]", "[]")).lastrowid
        self.assertEqual(cognize.now(self.conn, t), [t2])
        self.assertFalse(cognize.resolves(self.conn, dict(t, session=["claude", "s2"])))
        a = self.line()
        ref = cognize.pin(self.conn, "topic_line", a)
        self.assertEqual(ref, {"kind": "topic_line", "id": a})
        b = self.line(from_=[a])
        self.assertEqual(cognize.now(self.conn, ref), [b])
        c1 = self.write("甲")
        cref = cognize.pin(self.conn, "cognition", c1)
        self.assertTrue(cognize.fresh(self.conn, cref))
        c2 = self.write("甲", from_=[c1])
        self.assertEqual(cognize.now(self.conn, cref), [c2])


if __name__ == "__main__":
    unittest.main()
