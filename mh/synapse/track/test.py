"""track 的访问树：一步给看到了什么、按连通挂上一步、自带、往上合并、只看新版本（旧足迹和 J 不进树）。数据照 pull 的测试造。
python3 -m unittest mh.synapse.track.test
"""
import json
from datetime import datetime, timezone

from ...engram import store as st
from ..pull import test as pt
from .. import pull
from . import trees

FAR = datetime(2100, 1, 1, tzinfo=timezone.utc)      # 回填时会话早已告一段落


class TreesTest(pt.PullTest):             # 借 pull 测试造的数据（会话 s1 → 话题 T1、线 L1、陈述 L1:T1:S1/S2）
    def _legacy_use(self, query):
        """照 track.backfill 的写法记一条补进来的旧足迹（Operator 是 legacy/synapse-…）。"""
        op = st.operator(self.conn, "legacy/synapse-v0.7", "synapse", "synapse", model=st.NA, prompt=st.NA, prompt_hash=st.NA,
                         isolation=st.NA, window=st.NA, params={"version": "v0.7"}, legacy=True)
        oid = st.occurrence(self.conn, "use", op, started=st.now(), scale="turn", outcome="ok")
        self.conn.execute("INSERT INTO use_detail VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (oid, "pull", "get", "interactive", "dsh", "s1", "/w", st.NA, json.dumps(query), st.NA, st.NA, "{}"))
        self.conn.commit()

    def test_trees(self):
        kw = dict(session="s1", purpose="interactive", cwd="/w")
        pull.evoke(self.conn, ["X 升级"], **kw)                         # 根：给看到 T1、L1
        pull.relive(self.conn, ["T1"], **kw)              # 挂在查词下面：给看到 L1:T1:S1、S2
        pull.relive(self.conn, ["T1:S1", "L1:T1:S2"], **kw)                 # 两个句柄都来自上一步，不写线也认
        pull.relive(self.conn, [f"E{self.e[0]}"], **kw)                     # 此前没给过：自带，另起一份
        self._legacy_use({"get": {"handles": [f"K{self._legacy_j()[0]}"]}})   # 换库前补进来的旧足迹：只有旧 J，不进树
        L = pull.layers(self.conn)
        st = trees.steps(self.conn, L, "dsh", "s1")
        b = trees.build(st)
        self.assertEqual([len(r) for r in b["records"]], [3, 1])
        self.assertFalse(any(c[0] == "J" for x in st for c in x["shows"] | set(x["wants"])))   # J 是扁平的，不进树
        f = trees.features(st, b, b["records"][0])
        self.assertEqual((f["steps"], f["depth"], f["kinds"], f["own"]), (3, 3, ["查词", "relive", "relive"], 0))
        self.assertEqual(f["followed"], 3)                               # T1、两条陈述都是给看到以后往下取的
        self.assertEqual(trees.features(st, b, b["records"][1])["own"], 1)
        s1 = L.by_h["L1:T1:S1"]
        self.assertEqual(trees.up(L, ("S", s1), "T"), {("T", 1)})
        self.assertEqual(trees.up(L, ("S", s1), "L"), {("L", 1)})
        self.assertEqual(trees.up(L, ("E", self.e[0]), "S"), {("S", s1)})  # E 合并到引它的陈述
        self.assertEqual(trees.up(L, ("L", 1), "T"), set())               # 比要合并到的层高，不往下拆
        self.assertEqual(trees.sessions(self.conn), [("dsh", "s1")])





class CiteTest(pt.PullTest):              # 〖〗回填（2026-10-22）：数据照 pull 的测试造，Jev 换成假的
    def _reply(self, sid, reply, start="2099-01-01T00:00:00Z"):
        """采进一段会话，一轮，“我”的回复是 reply；时刻放在后面，排在此前的拉取之后。"""
        import io
        from ...engram.observe import collect
        from ...engram.observe.sources.turn import Session, Turn
        from ...engram.observe.test import FakeSource
        f = self.dir / f"{sid}.jsonl"
        f.write_text("x")
        collect.run(self.conn, sources=("dsh",), force=True, fresh=0, log=io.StringIO(),       # 时刻在未来：不等它“够旧”
                    modules={"dsh": FakeSource(f, Session("dsh", sid, str(f), started=start, cwd="/w"),
                                               [Turn("接着做吧", reply, closed=True, started=start)])})

    def test_cite(self):
        from . import cite
        calls = []

        def judge(state, instructions, criteria):
            calls.append(state)
            return {"value": "A", "probabilities": {"A": 0.9, "B": 0.05, "无": 0.05}}

        pull.relive(self.conn, ["T1"], session="s2", purpose="interactive", cwd="/w")   # s2 里给看到 S1、S2
        self._reply("s2", "照你之前说的，〖甲服务的步骤写进了 docs/x.md〗，我接着补回退。〖短〗不算。")
        self.assertEqual(cite.spans("〖甲服务的步骤〗和〖短〗"), ["甲服务的步骤"])
        self.assertEqual(cite.backfill(self.conn, judge=judge, now=FAR), (1, 0))
        q, inputs = self.conn.execute("SELECT u.query, o.inputs FROM use_detail u JOIN occurrence o ON o.id=u.occurrence "
                                      "WHERE u.mode='cite'").fetchone()
        rec = json.loads(q)["cite"][0]
        s2 = pull.layers(self.conn).by_h["L1:T1:S2"]
        self.assertEqual(rec["pick"], ["form_record", s2])               # 字面第一的是写进 docs/x.md 的那条
        self.assertEqual(json.loads(inputs)["form_record"], [s2])
        self.assertIn("『文件：docs/x.md』", calls[0])                    # 候选的正文是读回的写法
        self.assertEqual(cite.backfill(self.conn, judge=judge, now=FAR), (0, 0))  # 已经回填过的轮次不再做

        def broken(*_):
            raise RuntimeError("网络")
        self._reply("s3", "〖甲服务升到新版本〗")                           # s3 里什么都没给看过：没有候选，不调 Jev
        self._reply("s4", "〖甲服务升到新版本〗")
        pull.relive(self.conn, ["T1"], session="s4", purpose="dev", cwd="/w")   # dev 的拉取不算看到
        self.assertEqual(cite.backfill(self.conn, judge=broken, now=FAR), (2, 0))
        recs = [json.loads(q)["cite"][0] for (q,) in self.conn.execute("SELECT query FROM use_detail WHERE mode='cite' ORDER BY occurrence")]
        self.assertEqual([r.get("skip") for r in recs[1:]], ["字面没有候选", "字面没有候选"])

    def test_cite_same_turn(self):
        """同一轮里先拉取、再在回复里引用（审查 08，2026-10-28）：曝光截到下一轮开始，这次拉取算候选；还没有下一轮、会话也没告一段落的先不回填。"""
        import io
        from . import cite
        from ...engram.observe import collect
        from ...engram.observe.sources.turn import Session, Turn
        from ...engram.observe.test import FakeSource
        judge = lambda *a: {"probabilities": {"A": 0.9, "无": 0.1}}                  # noqa: E731
        f = self.dir / "s7.jsonl"
        f.write_text("x")

        def turns(*ts):
            collect.run(self.conn, sources=("dsh",), force=True, fresh=0, log=io.StringIO(),
                        modules={"dsh": FakeSource(f, Session("dsh", "s7", str(f), started="2000-01-01T00:00:00Z", cwd="/w"), list(ts))})
        first = Turn("接着做吧", "〖甲服务的步骤写进了 docs/x.md〗", closed=True, started="2000-01-01T00:00:00Z")   # 拉取在这一轮开始之后
        turns(first)
        pull.relive(self.conn, ["T1"], session="s7", purpose="interactive", cwd="/w")
        self.assertEqual(cite.backfill(self.conn, judge=judge, now=datetime(2000, 1, 1, 0, 30, tzinfo=timezone.utc)), (0, 0))
        turns(first, Turn("好", "嗯", closed=True, started="2099-01-01T00:00:00Z"))
        self.assertEqual(cite.backfill(self.conn, judge=judge, now=datetime(2099, 1, 1, tzinfo=timezone.utc)), (1, 0))
        rec = json.loads(self.conn.execute("SELECT query FROM use_detail WHERE mode='cite'").fetchone()[0])["cite"][0]
        self.assertTrue(rec["linked"])

    def test_cite_retry(self):
        from . import cite

        def broken(*_):
            raise RuntimeError("网络")
        pull.relive(self.conn, ["T1"], session="s2", purpose="interactive", cwd="/w")
        self._reply("s2", "〖甲服务升到新版本〗")
        self.assertEqual(cite.backfill(self.conn, judge=broken, now=FAR), (0, 1))   # Jev 出错：这次不写，下次再试
        self.assertEqual(cite.backfill(self.conn, judge=lambda *a: {"probabilities": {"无": 0.8, "A": 0.2}}, now=FAR), (1, 0))
        rec = json.loads(self.conn.execute("SELECT query FROM use_detail WHERE mode='cite'").fetchone()[0])["cite"][0]
        self.assertEqual(rec["linked"], [])                               # 选了“都不是”：不连

    def test_cite_handles(self):
        """〖〗里直接写的句柄（2026-10-27）：程序解析后直接连，不看曝光、不问 Jev；剩下的正文够长再认；v0.1 没认句柄的轮次重做一次。"""
        from . import cite
        calls = []

        def judge(state, *_):
            calls.append(state)
            return {"probabilities": {"A": 0.9, "无": 0.1}}
        s2 = pull.layers(self.conn).by_h["L1:T1:S2"]
        self.assertEqual(cite.HANDLE.findall("L7:T461、dsh-integration；T123（10-19）；pack-v0.3、E5"), ["L7:T461", "T123"])
        self._reply("s5", "〖L1:T1:S2〗；〖L1:T1 那次甲服务的步骤〗")
        old = st.operator(self.conn, "synapse/cite-v0.1", "synapse", "program", model="jev", prompt=st.NA, prompt_hash=st.NA,
                          isolation=st.NA, window="turn", params={})
        tid = self.conn.execute("SELECT d.occurrence FROM turn_detail d WHERE d.session='s5'").fetchone()[0]
        oid = st.occurrence(self.conn, "use", old, started=st.now(), scale="turn", inputs={"turn": [tid]}, outcome="ok")
        self.conn.execute("INSERT INTO use_detail VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (oid, "track", "cite", "interactive", st.UNKNOWN, "s5",
                          st.NA, st.NA, json.dumps({"cite": [{"quote": "L1:T1:S2", "cands": [], "linked": []}]}), st.NA, st.NA, "{}"))
        self.conn.commit()
        self.assertEqual(cite.backfill(self.conn, judge=judge, now=FAR), (1, 0))   # v0.1 的记录片段里有句柄：重做
        q, inputs, meta = self.conn.execute("SELECT u.query, o.inputs, u.meta FROM use_detail u JOIN occurrence o ON o.id=u.occurrence "
                                            "WHERE u.mode='cite' ORDER BY o.id DESC").fetchone()
        recs, inputs = json.loads(q)["cite"], json.loads(inputs)
        self.assertEqual(recs[0]["linked"], [["form_record", s2]])
        self.assertEqual((recs[1]["handles"], recs[1]["linked"], recs[1].get("skip")), (["L1:T1"], [["session_entry", 1]], "字面没有候选"))
        self.assertEqual((inputs["form_record"], inputs["use"]), ([s2], [oid]))
        self.assertEqual(json.loads(meta)["handles"], {"session_entry": [1]})
        self.assertEqual(calls, [])                                       # s5 什么都没看过：剩下的正文没有候选，不调 Jev
        self.assertEqual(cite.backfill(self.conn, judge=judge, now=FAR), (0, 0))

    def test_rebind(self):
        """/clear 后记错会话的拉取（2026-10-27）：写一条 rebind 更正，cite 的曝光、联想去重、访问树都按更正认。"""
        from . import cite, rebind, rebound, trees as tr
        from ..push import associate
        pull.relive(self.conn, ["T1"], session="s1", purpose="interactive", cwd="/w")    # 实际是 s6 发的，记成了 s1
        uid = self.conn.execute("SELECT max(occurrence) FROM use_detail WHERE mode='relive'").fetchone()[0]
        self._reply("s6", "〖甲服务的步骤〗")
        texts = cite.texts_of(self.conn)
        self.assertEqual(cite.exposed(self.conn, "s6", "2100-01-01", texts), [])
        judge = lambda *a: {"probabilities": {"A": 0.9, "无": 0.1}}                  # noqa: E731
        self.assertEqual(cite.backfill(self.conn, judge=judge, now=FAR), (1, 0))           # 当时没有候选
        rid = rebind(self.conn, pull.VERSION, [uid], "s6", "测试", evidence=["toolu_x"])
        self.assertEqual(rebound(self.conn), {uid: "s6"})
        self.assertEqual(len(cite.exposed(self.conn, "s6", "2100-01-01", texts)), 2)
        self.assertEqual(cite.exposed(self.conn, "s1", "2100-01-01", texts), [])
        self.assertTrue(associate._seen(self.conn, "s6"))
        self.assertIn(uid, [x["id"] for x in tr.steps(self.conn, pull.layers(self.conn), "dsh", "s6")])
        self.assertIn(("dsh", "s6"), tr.sessions(self.conn))
        q = json.loads(self.conn.execute("SELECT query FROM use_detail WHERE occurrence=?", (rid,)).fetchone()[0])
        self.assertEqual(q["rebind"]["from"], ["s1"])
        self.assertEqual(cite.backfill(self.conn, judge=judge, now=FAR), (1, 0))           # 更正以后候选变了：重做，取代旧的那条
        rec, inputs = self.conn.execute("SELECT u.query, o.inputs FROM use_detail u JOIN occurrence o ON o.id=u.occurrence "
                                        "WHERE u.mode='cite' ORDER BY o.id DESC").fetchone()
        self.assertTrue(json.loads(rec)["cite"][0]["linked"])
        self.assertEqual(len(json.loads(inputs)["use"]), 1)
        self.assertEqual(cite.backfill(self.conn, judge=judge, now=FAR), (0, 0))


for _name in [n for n in dir(pt.PullTest) if n.startswith("test_")]:
    setattr(TreesTest, _name, None)       # pull、form 自己的测试不在这里重跑
    setattr(CiteTest, _name, None)


import hashlib
from pathlib import Path
import tempfile
import unittest
from .. import track


class ExpressionDeliveryTest(unittest.TestCase):
    def test_delivery_evidence_and_presentation_feedback(self):
        with tempfile.TemporaryDirectory() as d:
            c = st.connect(Path(d) / 'db.sqlite')
            try:
                op = st.operator(c, 'synthetic-source', 'observe', 'observer')
                act = st.occurrence(c, 'act', op)
                eid = c.execute("INSERT INTO event(born,idx,turn,text,mode,qualifier,speaker,segments,unresolved) VALUES(?,0,?,'原记录',?,?,?,'[]','[]')",
                                (act, st.UNKNOWN, st.UNKNOWN, st.UNKNOWN, st.UNKNOWN)).lastrowid
                before = tuple(c.execute('SELECT * FROM event WHERE id=?', (eid,)).fetchone())
                text = '[E1] 青禾说“我喜欢这个名字”。'
                delivered = track.deliver(c, 'test', 'relive', {}, text, {'event': [eid]}, session='synthetic', raw=True)
                r = c.execute('SELECT * FROM use_detail ORDER BY occurrence DESC LIMIT 1').fetchone()
                self.assertEqual(delivered, r['shown'])
                self.assertIn(text, delivered)
                self.assertTrue(delivered.startswith(f'<memory-harness use="{r["occurrence"]}" source="原话">'))
                meta = json.loads(r['meta'])['expression']
                self.assertEqual(meta['text_hash'], hashlib.sha256(delivered.encode()).hexdigest())
                self.assertTrue(meta['source_raw'])
                feedback = track.presentation_feedback(c, 'test', r['occurrence'], '称呼应保留原说话人', session='synthetic')
                last = c.execute('SELECT * FROM use_detail ORDER BY occurrence DESC LIMIT 1').fetchone()
                self.assertEqual(last['shown'], feedback)
                self.assertEqual(json.loads(last['meta'])['feedback']['target_use'], r['occurrence'])
                self.assertEqual(tuple(c.execute('SELECT * FROM event WHERE id=?', (eid,)).fetchone()), before)
                with self.assertRaises(ValueError):
                    track.presentation_feedback(c, 'test', 99999, 'invalid')
                self.assertEqual(c.execute('SELECT shown FROM use_detail WHERE occurrence=?', (r['occurrence'],)).fetchone()[0], delivered)
                # 对别的会话的送达作反馈，不应构成本会话的新曝光或访问。
                fid = c.execute("INSERT INTO form_record(born,idx,kind,text,refs,labels,structures,unresolved) VALUES(?,0,'session','合成陈述','{}','[]','{}','[]')", (act,)).lastrowid
                other = track.use(c, 'test', 'relive', {}, '合成陈述', {'form_record': [fid]}, session='other')
                track.presentation_feedback(c, 'test', other, '视角需要调整', session='feedback-only')
                from . import cite
                self.assertEqual(cite.exposed(c, 'feedback-only', st.now(), {('form_record', fid): '合成陈述'}), [])
                self.assertEqual(trees.steps(c, None, 'dsh', 'feedback-only'), [])
            finally:
                c.close()
