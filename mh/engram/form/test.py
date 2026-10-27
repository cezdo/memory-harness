"""form 的测试：单位怎么挑（告一段落、门槛、只认新做法的 E）、三步的解析与写入、挂线（有骨架、没骨架）、失败计数与不挡后面、
程序补词与线的索引词。模型换成假的，不调模型。python3 -m unittest mh.engram.form.test
"""
import io
import subprocess
import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import check, form, observe
from .. import store as st
from ..observe import collect
from ..observe.sources.turn import Session, Turn
from ..observe.test import FakeModel, FakeSource
from . import attach, label, pack, skeleton
from ..store.instance import USER_NAME as U

PLAN = "### 第 1–2 轮\n类型：讨论\n读法：精读\n"
READ = f"### 事件1\n{U}想把 X 升到新版本。\n来源：1:U1\n\n### 事件2\n我把步骤写进了 docs/x.md。\n来源：2:A1\n"

PACK = """名字：X 升级
主题：讨论 X 怎么升级
### 陈述1
{U}想把「甲服务」升到新版本，这次对话由此开始。
来源：E{a}
### 陈述2
我把和「甲服务」步骤相关的内容写进了「文件：docs/x.md」。
来源：E{b}
#### 文件：docs/x.md
指向：E{b}
""".replace("{U}", U)
LABEL = """名字：X 升级
停在：定下按 b 做法升级（陈述1、陈述2）
索引词：
- X 升级｜升级 X
- docs/x.md
"""


class FormTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.conn = st.connect(self.dir / "engram.db")
        self.now = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

    def _session(self, sid, start, cwd="/w"):
        """采进一个两轮的会话，并让 observe 读出两条新做法的 E。返回这两条 E 的编号。"""
        f = self.dir / f"{sid}.jsonl"
        f.write_text("x")
        sess = Session("dsh", sid, str(f), started=start, cwd=cwd)
        turns = [Turn(f"把 X 升级一下（{sid}）", "好", closed=True, started=start),     # 原话不同：同一份原文只观察一次
                 Turn(f"写下来（{sid}）", "写进 docs/x.md 了", closed=True, started=start)]
        collect.run(self.conn, sources=("dsh",), force=True, log=io.StringIO(), modules={"dsh": FakeSource(f, sess, turns)})
        fakes = {"preview": FakeModel([PLAN]), "deep": FakeModel([READ]), "rough": FakeModel([READ])}   # 精读超了字数上限会改成粗读
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO(), session=sid, now=self.now)
        return [r[0] for r in self.conn.execute("SELECT e.id FROM event e JOIN turn_detail d ON d.occurrence=e.turn "
                                                "WHERE d.session=? ORDER BY e.id", (sid,))]

    def _more(self, sid, start, cwd="/w"):
        """给会话 sid 续上第 3 轮，让 observe 读出一条 E。返回它的编号。"""
        f = self.dir / f"{sid}.jsonl"
        sess = Session("dsh", sid, str(f), started=start, cwd=cwd)
        turns = [Turn(f"把 X 升级一下（{sid}）", "好", closed=True, started=start),
                 Turn(f"写下来（{sid}）", "写进 docs/x.md 了", closed=True, started=start),
                 Turn(f"再加个回退（{sid}）", "加了", closed=True, started="2026-10-17T10:00:00Z")]
        collect.run(self.conn, sources=("dsh",), force=True, log=io.StringIO(), modules={"dsh": FakeSource(f, sess, turns)})
        fakes = {"preview": FakeModel(["### 第 3–3 轮\n类型：讨论\n读法：精读\n"]),
                 "deep": FakeModel([f"### 事件1\n{U}要加回退。\n来源：3:U1\n"]), "rough": FakeModel([f"### 事件1\n{U}要加回退。\n来源：3:U1\n"])}
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO(), session=sid, now=self.now)
        return [r[0] for r in self.conn.execute("SELECT e.id FROM event e JOIN turn_detail d ON d.occurrence=e.turn "
                                                "WHERE d.session=? AND d.ordinal=2", (sid,))]

    def _late(self, eid):
        """晚到的 E：照着已有的一条复制一条（同一轮、同一次读），模拟后来补进整理过的范围。"""
        r = self.conn.execute("SELECT * FROM event WHERE id=?", (eid,)).fetchone()
        return self.conn.execute("INSERT INTO event(born, idx, turn, text, mode, qualifier, speaker, segments, unresolved) "
                                 "VALUES(?,?,?,?,?,?,?,?,?)", (r["born"], r["idx"] + 100, r["turn"], f"{U}还提到要先备份。", r["mode"],
                                                              r["qualifier"], r["speaker"], r["segments"], r["unresolved"])).lastrowid

    def test_pack_languages(self):
        from .pack import e_block
        evs = [{"id": 1, "ordinal": 0, "speaker": "我", "mode": "告知", "segments": ["A1"], "text": "我说明已经停了。"}]
        out = e_block(evs, {1: ["a.md"]}, {0: "这一轮我换成英文回复（上一轮用中文）。", 1: "这一轮我换成中文回复（上一轮用英文）。"})
        self.assertIn("我说明已经停了。\n这一轮我换成英文回复", out)
        self.assertNotIn("换成中文", out)                  # 没有 E 的轮次不单独写语言

    def test_continue_and_insert(self):
        a = self._session("s1", "2026-10-17T08:00:00Z")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))
        # 续上：新单位，label 看得到原来的入口，挂线由程序沿用（不调 attach），一段会话只剩一个入口
        c = self._more("s1", "2026-10-17T08:00:00Z")
        self.assertEqual(len(c), 1)
        units = form.units(self.conn, self.now)
        self.assertEqual([(u["unit"][2], u["redo"]) for u in units], [(c[0], False)])
        pack2 = "名字：X 升级\n主题：加回退\n### 陈述1\n{U}要加「回退」。\n来源：E{c}\n".replace("{U}", U).format(c=c[0])
        label2 = "名字：X 升级\n停在：回退也加了（陈述1）\n索引词：\n- 回退｜rollback\n- X 升级｜X 的升级\n"
        fakes = self._fakes([pack2], [label2], [])
        self._run(fakes)
        text = self.conn.execute("SELECT input FROM act_detail WHERE step='label' ORDER BY occurrence DESC").fetchone()[0]
        self.assertIn("## 这段会话原来的会话入口", text)
        self.assertIn("（续上：", text)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM act_detail WHERE step='attach'").fetchone()[0], 1)
        rows = self.conn.execute("SELECT entry, line, why FROM attachment ORDER BY id").fetchall()
        self.assertEqual([(r["entry"], r["line"]) for r in rows], [(1, 1), (2, 1)])
        self.assertTrue(rows[1]["why"].startswith("续上："))
        es = form.entries(self.conn)
        self.assertEqual(list(es), ["T2"])                                   # 一段会话一个入口
        self.assertIn(["X 升级", "升级 X", "X 的升级"], es["T2"]["words"]["lists"])   # 索引词和原来的合并
        self.assertIn(["回退", "rollback"], es["T2"]["words"]["lists"])
        self.assertEqual(form.units(self.conn, self.now), [])
        # 中间插入：晚到的 E 落进第一个单位的范围 → 那个单位整个重新 pack，入口重写，挂线沿用、标待审视
        late = self._late(a[0])
        units = form.units(self.conn, self.now)
        self.assertEqual([(u["unit"][2], u["redo"]) for u in units], [(a[0], True)])
        self.assertEqual([e["id"] for e in units[0]["events"]], [a[0], late, a[1]])
        pack3 = PACK.format(a=a[0], b=a[1]).replace("来源：E{}\n".format(a[0]), "来源：E{}、E{}\n".format(a[0], late), 1)
        self._run(self._fakes([pack3], [LABEL], []))
        rows = self.conn.execute("SELECT entry, line, why FROM attachment ORDER BY id").fetchall()
        self.assertEqual((rows[-1]["entry"], rows[-1]["line"]), (3, 1))
        self.assertTrue(rows[-1]["why"].startswith("待审视：") and f"E{late}" in rows[-1]["why"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM act_detail WHERE step='attach'").fetchone()[0], 1)
        self.assertEqual(form.units(self.conn, self.now), [])
        self.assertTrue(form.entries(self.conn)["T3"]["review"].startswith("待审视："))          # 读的一侧看得到（J6）
        me = st.main_operator(self.conn, "m")                                                   # 主会话看过：改挂到同一条线，标记就清掉
        form.feedback_attach(self.conn, me, 3, "带着新内容看过了，挂得对", line="L1")
        self.assertEqual(form.entries(self.conn)["T3"]["review"], "")
        text = self.conn.execute("SELECT input FROM act_detail WHERE step='label' ORDER BY occurrence DESC").fetchone()[0]
        self.assertIn("（重新整理：", text)
        self.assertIn(["回退", "rollback"], form.entries(self.conn)["T3"]["words"]["lists"])   # 续上部分的词没丢

    def test_fork(self):
        """分叉（2026-10-19：fork 和阶段性做完、开新对话接着做本质上一样，不回头重做）：共用的轮次只观察、只整理一次，
        先告一段落的那段整段整理；后来的那段只整理自己分开以后的部分，起名时附上共用那几轮的陈述，挂线照常。"""
        shared = [Turn("把 X 升级一下", "好", closed=True, started="2026-10-17T08:00:00Z"),
                  Turn("写下来", "写进 docs/x.md 了", closed=True, started="2026-10-17T08:00:00Z")]
        for sid, turns in (("s1", shared), ("s2", shared + [Turn("再加个回退", "加了", closed=True, started="2026-10-17T09:00:00Z")])):
            f = self.dir / f"{sid}.jsonl"
            f.write_text("x")
            collect.run(self.conn, sources=("dsh",), force=True, log=io.StringIO(),
                        modules={"dsh": FakeSource(f, Session("dsh", sid, str(f), started="2026-10-17T08:00:00Z", cwd="/w"), turns)})
        fakes = {"preview": FakeModel([PLAN, "### 第 3 轮\n类型：讨论\n读法：精读\n"]),
                 "deep": FakeModel([READ, f"### 事件1\n{U}要加回退。\n来源：3:U1\n"]), "rough": FakeModel([READ, f"### 事件1\n{U}要加回退。\n来源：3:U1\n"])}
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO(), now=self.now)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM act_detail WHERE step='preview'").fetchone()[0], 2)   # 共用的只预览一次
        a = [e["id"] for e in observe.frame(self.conn, "dsh", "s1")["events"]]
        units = form.units(self.conn, self.now)
        self.assertEqual([u["unit"][1] for u in units], ["s1"])              # s2 结束得晚，等着；共用的归 s1
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))
        (u,) = form.units(self.conn, self.now)
        self.assertEqual((u["unit"][1], [e["ordinal"] for e in u["events"]]), ("s2", [2]))     # s2 只整理自己的第 3 轮
        c = u["events"][0]["id"]
        self._run(self._fakes(["名字：X 加回退\n主题：加回退\n### 陈述1\n{U}要加「回退」。\n来源：E{}\n".replace("{U}", U).format(c)],
                              ["名字：X 加回退\n停在：回退加了（陈述1）\n索引词：\n- 回退\n"], ["挂到：L1｜为什么：同一个目标"]))
        text = self.conn.execute("SELECT input FROM act_detail WHERE step='label' ORDER BY occurrence DESC").fetchone()[0]
        self.assertIn("## 这段对话的前 2 轮", text)
        self.assertIn(f"{U}想把「甲服务」升到新版本", text)                  # s1 记录里关于共用轮次的陈述
        self.assertEqual(self.conn.execute("SELECT count(*) FROM act_detail WHERE step='attach'").fetchone()[0], 2)   # 挂线照常
        self.assertEqual(form.units(self.conn, self.now), [])

    def test_request_redo(self):
        """申请重做（C1，2026-10-19）：一段会话被切成两份会话记录，主会话写一张申请指向这两份的陈述；form 认领，整段从 pack 起
        合成一个单位重做，起名不接原来的入口，挂线重新挂；原来的整理、起名、挂线不再算数，act 只追加。"""
        a = self._session("s1", "2026-10-17T08:00:00Z")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))
        c = self._more("s1", "2026-10-17T08:00:00Z")
        pack2 = "名字：X 升级\n主题：加回退\n### 陈述1\n{U}要加「回退」。\n来源：E{c}\n".replace("{U}", U).format(c=c[0])
        self._run(self._fakes([pack2], ["名字：X 升级\n停在：回退也加了（陈述1）\n索引词：\n- 回退\n"], []))
        recs = [r[0] for r in self.conn.execute("SELECT id FROM form_record ORDER BY id")]
        old_packs = [r[0] for r in self.conn.execute("SELECT occurrence FROM act_detail WHERE step='pack' ORDER BY occurrence")]
        self.assertEqual(len(old_packs), 2)                                   # 两份会话记录
        me = st.main_operator(self.conn, "m")
        rid = st.request(self.conn, st.MAIN, "form", {"form_record": recs}, "一段会话被切成两份会话记录", me, cause="manual")
        self.conn.commit()
        units = form.units(self.conn, self.now)
        self.assertEqual([(u["unit"], u["request"]) for u in units], [(("dsh", "s1", a[0]), [rid])])
        self.assertEqual([e["id"] for e in units[0]["events"]], [a[0], a[1], c[0]])
        merged = PACK.format(a=a[0], b=a[1]) + "### 陈述3\n{U}要加「回退」。\n来源：E{c}\n".replace("{U}", U).format(c=c[0])
        self._run(self._fakes([merged], [LABEL], ["挂到：L1｜为什么：同一个目标"]))
        pk = self.conn.execute("SELECT o.* FROM occurrence o JOIN act_detail a ON a.occurrence=o.id WHERE a.step='pack' "
                               "ORDER BY o.id DESC").fetchone()
        self.assertEqual((pk["cause"], json.loads(pk["inputs"])["request"], pk["predecessor"]), ("request", [rid], old_packs[0]))
        self.assertEqual(st.requests(self.conn, "form"), [])                  # 认领过了
        lab = self.conn.execute("SELECT input FROM act_detail WHERE step='label' ORDER BY occurrence DESC").fetchone()[0]
        self.assertNotIn("## 这段会话原来的会话入口", lab)                      # 下游跟着重做，不接原来的入口
        self.assertEqual(self.conn.execute("SELECT count(*) FROM act_detail WHERE step='attach'").fetchone()[0], 2)
        (e,) = form.entries(self.conn).values()                               # 一段会话一个入口，挂在原来那条线上
        record = self.conn.execute("SELECT record FROM session_entry WHERE id=?", (e["row"],)).fetchone()[0]
        self.assertEqual((e["parent"], record, len(form.record(self.conn, record)[2])), ("L1", pk["id"], 3))
        texts = [it["text"] for it in form.record(self.conn, record)[2]]     # 旧写法的结构标记读回时换成『』，名字的「」不动（2026-10-22）
        self.assertTrue(any("『文件：docs/x.md』" in t and "「甲服务」" in t for t in texts), texts)
        self.assertEqual(form.units(self.conn, self.now), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM form_record").fetchone()[0], len(recs) + 3)   # 旧的照留
        # observe 写来的修订提示是 lazy 的（2026-10-21）：写下时不触发整段重做；form 内部的拉取不算，
        # 主会话的拉取取到了依据这条 E 的陈述，才认领、从 pack 起整段重做
        obs = st.operator(self.conn, "observe-test", "observe", "observer")
        rev = st.request(self.conn, "observe", "form", {"event": [a[0]]}, "E 被取代", obs)
        self.assertEqual(form.units(self.conn, self.now), [])
        stmt, = [r[0] for r in self.conn.execute("SELECT f.id FROM form_record f, json_each(f.refs, '$.event') j "
                                                 "WHERE j.value=? AND f.born=?", (a[0], pk["id"]))]
        syn = st.operator(self.conn, "synapse-test", "synapse", "synapse")
        def use(purpose):
            u = st.occurrence(self.conn, "use", syn, inputs={"form_record": [stmt]})
            self.conn.execute("INSERT INTO use_detail VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                              (u, "pull", "get", purpose, "claude", "s", "/w", st.NA, "{}", "", st.NA, "{}"))
        use("form")
        self.assertEqual(form.units(self.conn, self.now), [])
        use("interactive")
        self.assertEqual(form.units(self.conn, self.now), [])                  # 默认 statements：不走整段重做
        form.REVISE = "pack"                                                    # 选项 pack：从 pack 起整段重做
        self.addCleanup(setattr, form, "REVISE", "statements")
        self.assertEqual([u["request"] for u in form.units(self.conn, self.now)], [[rev]])
        # 认领以后从 pack 起重做，但接原来的入口、沿用挂线（2026-10-21：L 一级要有明确的接续）
        lines_before = {x["parent"] for x in form.entries(self.conn).values()}
        self._run(self._fakes([merged], [LABEL], []))                          # 不调 attach
        lab = self.conn.execute("SELECT input FROM act_detail WHERE step='label' ORDER BY occurrence DESC").fetchone()[0]
        self.assertIn("## 这段会话原来的会话入口", lab)
        self.assertEqual({x["parent"] for x in form.entries(self.conn).values()}, lines_before)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM act_detail WHERE step='attach'").fetchone()[0], 2)
        self.assertEqual(st.requests(self.conn, "form", "handled")[-1]["id"], rev)

    def test_restate(self):
        """修订提示默认只改写依据那几条 E 的陈述（2026-10-21）：被用到才做；改写的取代原来那条，沿用位置和编号；
        撤下的去掉；起名、挂线不动；没受影响的陈述照旧。"""
        a = self._session("s1", "2026-10-17T08:00:00Z")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))
        s1, s2 = [r[0] for r in self.conn.execute("SELECT id FROM form_record ORDER BY id")]
        before = form.entries(self.conn)
        # observe 纠正了 a[0]（新 E 取代它）、核对 a[1] 不成立，给 form 写修订提示
        obs = st.operator(self.conn, "observe-test", "observe", "observer")
        t = self.conn.execute("SELECT turn FROM event WHERE id=?", (a[0],)).fetchone()[0]
        act = st.occurrence(self.conn, "act", obs, outcome="ok", inputs={"turn": [t], "event": [a[0], a[1]]})
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (act, "observe", "correct", st.dumps({"turns": [t]}), "", "",
                                                                           st.dumps({"invalid": [a[1]]})))
        new = self.conn.execute("INSERT INTO event(born, idx, turn, text, mode, qualifier, speaker, segments, unresolved, relations) "
                                "VALUES(?,0,?,'我想把「甲服务」升到新版本。','?unknown','','?unknown','[]','[]',?)",
                                (act, t, st.dumps([{"kind": "supersedes", "to": a[0]}]))).lastrowid
        split = self.conn.execute("INSERT INTO event(born, idx, turn, text, mode, qualifier, speaker, segments, unresolved, relations) "
                                  f"VALUES(?,1,?,'{U}同意了。','?unknown','','?unknown','[]','[]',?)",
                                  (act, t, st.dumps([{"kind": "supersedes", "to": a[0]}]))).lastrowid   # 一条旧 E 拆成两条新 E
        rev = st.request(self.conn, "observe", "form", {"event": [a[0], a[1]]}, "E 被取代、不成立", obs, parent=act)
        self.conn.commit()
        model = FakeModel([f"### 陈述1\n我想把「甲服务」升到新版本，这次对话由此开始。\n来源：E{new}\n\n### 陈述2\n撤下\n理由：依据的事件不成立"])
        self.assertEqual(form.units(self.conn, self.now), [])                  # 纠正写的 E 不当新 E，不整段重做
        self.assertEqual(form.restate(self.conn, models={"restate": model}, log=io.StringIO()), (0, 0))   # 还没被用到
        syn = st.operator(self.conn, "synapse-test", "synapse", "synapse")
        u = st.occurrence(self.conn, "use", syn, inputs={"form_record": [s1]})
        self.conn.execute("INSERT INTO use_detail VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (u, "pull", "get", "interactive", "claude", "x", "/w", st.NA, "{}", "", st.NA, "{}"))
        self.conn.commit()
        log = io.StringIO()
        self.assertEqual(form.restate(self.conn, models={"restate": model}, log=log), (1, 0))
        self.assertIn("改写 1 条、撤下 1 条", log.getvalue())
        self.assertIn("## 变化", model.prompts[0])
        self.assertIn(f"E{a[1]}（不成立）", model.prompts[0])
        self.assertIn(f"→ E{split}（新）：{U}同意了。", model.prompts[0])          # 拆成的几条新 E 都给
        after = form.entries(self.conn)
        (k,) = after
        items = after[k]["items"]
        self.assertEqual([(it["n"], it["was"], it["text"]) for it in items], [(1, s1, "我想把「甲服务」升到新版本，这次对话由此开始。")])
        rel = json.loads(self.conn.execute("SELECT relations FROM form_record WHERE id=?", (items[0]["id"],)).fetchone()[0])
        self.assertEqual(rel, [{"kind": "supersedes", "to": s1}])
        self.assertEqual((after[k]["name"], after[k]["parent"]), (before[k]["name"], before[k]["parent"]))   # 起名、挂线不动
        self.assertEqual(st.requests(self.conn, "form", "handled")[0]["id"], rev)
        self.assertEqual(form.units(self.conn, self.now), [])
        self.assertNotIn("取代：", check.check(self.conn, self.dir / "engram.db"))

    def test_feedback_entry(self):
        """会话入口的反馈（2026-10-19）：主会话给出改好的名字，写一个新入口取代原来的，挂线沿用，form 不重判；重名、对已被取代的
        入口、只说不对不给内容的都拒绝。之后续上，起名看到的原来的入口是反馈写的那个。"""
        a = self._session("s1", "2026-10-17T08:00:00Z")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))
        b = self._session("s2", "2026-10-17T09:00:00Z")
        self._run(self._fakes([PACK.format(a=b[0], b=b[1])], [LABEL], ["挂到：L1｜为什么：同一个目标"]))
        me = st.main_operator(self.conn, "m")
        names = sorted(e["name"] for e in form.entries(self.conn).values())
        self.assertEqual(names[0].split("（")[0], "X 升级")                 # 起名查重给第二个加了日期
        with self.assertRaises(ValueError):
            form.feedback_entry(self.conn, me, 1, "两个入口要分得开")       # 没给内容：该写申请
        with self.assertRaises(ValueError):
            form.feedback_entry(self.conn, me, 1, "改名", name=names[1])     # 和另一个入口重名
        new = form.feedback_entry(self.conn, me, 1, "两个入口要分得开", name="X 升级：先定做法")
        es = form.entries(self.conn)
        self.assertEqual((es[f"T{new}"]["name"], es[f"T{new}"]["parent"]), ("X 升级：先定做法", "L1"))
        self.assertNotIn("T1", es)
        fb = self.conn.execute("SELECT o.cause, o.inputs, o.operator, a.meta FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
                               "WHERE a.step='feedback'").fetchone()
        born = self.conn.execute("SELECT born FROM session_entry WHERE id=1").fetchone()[0]
        self.assertEqual((fb["cause"], json.loads(fb["inputs"]), fb["operator"]), ("manual", {"act": [born]}, me))
        self.assertEqual(json.loads(fb["meta"])["supersedes"], "T1")
        with self.assertRaises(ValueError):
            form.feedback_entry(self.conn, me, 1, "再改", name="别的")       # T1 已被取代
        self.assertEqual(form.units(self.conn, self.now), [])                # form 不重判
        c = self._more("s1", "2026-10-17T08:00:00Z")
        pack2 = "名字：X 升级\n主题：加回退\n### 陈述1\n{U}要加「回退」。\n来源：E{c}\n".replace("{U}", U).format(c=c[0])
        self._run(self._fakes([pack2], ["名字：X 升级：先定做法\n停在：回退也加了（陈述1）\n索引词：\n- 回退\n"], []))
        text = self.conn.execute("SELECT input FROM act_detail WHERE step='label' ORDER BY occurrence DESC").fetchone()[0]
        self.assertIn("X 升级：先定做法", text)                               # 续上看到的是反馈写的入口
        msg = form.feedback(self.conn, me, "s-" + max(form.entries(self.conn), key=lambda k: int(k[1:]))[1:], "停在写偏了", stops=["定下按 b 做法升级（陈述1、陈述2）"])   # 工具的入口
        self.assertTrue(msg.startswith("反馈已写：T"))                       # 旧写法 s-… 也认，写回 T…
        latest = max(form.entries(self.conn), key=lambda k: int(k[1:]))
        self.assertEqual(json.loads(self.conn.execute("SELECT stops FROM session_entry WHERE id=?", (int(latest[1:]),)).fetchone()[0]),
                         [{"text": "定下按 b 做法升级", "basis": [r[0] for r in self.conn.execute(
                             "SELECT id FROM form_record WHERE born=(SELECT record FROM session_entry WHERE id=?) ORDER BY idx",
                             (int(latest[1:]),))]}])
        msg = form.feedback(self.conn, me, latest, "这段整理得不对，说不清哪里")           # 没给内容：转成申请
        self.assertIn("转成申请", msg)
        self.assertEqual(len(st.requests(self.conn, "form")), 1)
        with self.assertRaises(ValueError):
            form.feedback(self.conn, me, "K12", "旧认识")                    # 现在只接会话入口

    def _fakes(self, packs=(), labels=(), attaches=()):
        return {"pack": FakeModel(list(packs)), "label": FakeModel(list(labels)),
                "attach": FakeModel(list(attaches)), "attach_lines": FakeModel(list(attaches))}

    def _run(self, fakes, **kw):
        return form.run(self.conn, workers=1, models=fakes, log=io.StringIO(), now=self.now, **kw)

    def test_pack_check(self):
        """pack-v0.4 的检查（2026-10-27）：范围、不在输入里、超过两页、两页跨了预览的组。"""
        evs = [{"id": i, "turn": 100 + i // 10, "ordinal": i // 10} for i in range(1, 30)]
        groups = {100: ("p", 1, 1, "a"), 101: ("p", 2, 2, "b"), 102: ("p", 3, 3, "c")}
        it = lambda n, basis, **kw: {"n": n, "text": "", "basis": basis, "quotes": [], "files": [], **kw}
        got = dict(pack.check([it(1, [1, 2], ranges=True), it(2, [1, 99]), it(3, list(range(1, 20))),
                               it(4, list(range(1, 11))), it(5, list(range(10, 20))), it(6, [1, 2, 3])], evs, groups))
        self.assertIn("范围", got[1])
        self.assertIn("E99", got[2])
        self.assertIn("拆成几条", got[3])
        self.assertIn("跨了第 1 轮、第 2 轮", got[4])                                       # 9–16 条跨了两组
        self.assertNotIn(5, got)                                                           # 9–16 条在同一组（第 2 轮）
        self.assertNotIn(6, got)
        few = [dict(e, speaker=U if e["id"] == 2 else "我") for e in evs[:3]]                  # 覆盖（检查 v2）
        cov = pack.check([it(1, [1])], few, groups)
        self.assertTrue(any(n == 0 and "E2" in why and "总要保留" in why for n, why in cov))
        self.assertTrue(any(n == 0 and "2 条事件没有落在" in why for n, why in cov))
        self.assertEqual(pack.check([it(1, [1, 2, 3])], few, groups), [])
        _, _, items, _, _ = pack.parse("名字：x\n主题：y\n### 陈述1\nz\n来源：E1-E3\n", evs)
        self.assertTrue(items[0].get("ranges"))

    def test_pack_rewrite(self):
        """检查出不合规的陈述，带着问题当场重写一次；重写后的入库，第一次的输出和问题留在 meta。"""
        e = self._session("s1", "2026-10-17T08:00:00Z")
        bad = PACK.format(a=e[0], b=e[1]).replace(f"来源：E{e[0]}\n", f"来源：E{e[0]}-E{e[1]}\n", 1)
        fakes = self._fakes([bad, PACK.format(a=e[0], b=e[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"])
        self._run(fakes)
        meta = json.loads(self.conn.execute("SELECT meta FROM act_detail WHERE step='pack' ORDER BY occurrence DESC").fetchone()[0])
        self.assertEqual(len(meta["rewrite"]), 1)
        self.assertIn("范围", meta["rewrite"][0]["problems"][0][1])
        self.assertNotIn("violations", meta)
        basis = json.loads(self.conn.execute("SELECT structures FROM form_record ORDER BY id LIMIT 1").fetchone()[0])["basis"]
        self.assertEqual(basis, [e[0]])

    def test_repack_keeps_entry(self):
        """只重做陈述（2026-10-27，方案一）：form 写给自己的申请点名陈述，从 pack 起重做，入口（含手动改过的名字）和挂线由程序沿用，
        不调模型起名、挂线；停在按 E 的重合换到新陈述。"""
        a = self._session("s1", "2026-10-17T08:00:00Z")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))
        me = st.main_operator(self.conn, "m")
        (t,) = form.entries(self.conn)
        form.feedback_entry(self.conn, me, int(str(t).lstrip("T")), "名字太泛", name="X 升级与回退")
        line = self.conn.execute("SELECT line FROM attachment ORDER BY id DESC").fetchone()[0]
        recs = [r[0] for r in self.conn.execute("SELECT id FROM form_record ORDER BY id")]
        rid = form.request_repack(self.conn, recs[:1], "来源超过两页")
        self.conn.commit()
        self.assertEqual([u["request"] for u in form.units(self.conn, self.now)], [[rid]])
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [], []))           # 起名、挂线的假模型是空的：调用就会出错
        (e,) = form.entries(self.conn).values()
        self.assertEqual(e["name"], "X 升级与回退")
        self.assertEqual(self.conn.execute("SELECT line FROM attachment ORDER BY id DESC").fetchone()[0], line)
        new = [r[0] for r in self.conn.execute("SELECT id FROM form_record ORDER BY id")][len(recs):]
        stops = json.loads(self.conn.execute("SELECT stops FROM session_entry ORDER BY id DESC").fetchone()[0])
        self.assertTrue(stops and set(stops[0]["basis"]) <= set(new) and stops[0]["basis"])
        self.assertEqual(st.requests(self.conn, "form"), [])                  # 认领过了

    def test_auto_repack_capped(self):
        """页对齐检查重写后仍不合规的，form 自动给自己写一张重做申请（沿用入口和挂线）；重做以后还不合规就不再写（J4，防环）。"""
        e = self._session("s1", "2026-10-17T08:00:00Z")
        bad = PACK.format(a=e[0], b=e[1]).replace(f"来源：E{e[0]}\n", f"来源：E{e[0]}-E{e[1]}\n", 1)
        self._run(self._fakes([bad, bad], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))       # 重写一次仍违规，入库
        n_rec = self.conn.execute("SELECT count(*) FROM form_record").fetchone()[0]
        self.assertEqual(form.auto_repack(self.conn, log=io.StringIO()), 1)
        self.assertEqual(form.auto_repack(self.conn, log=io.StringIO()), 0)                     # 还没认领：不重复写
        self.conn.commit()
        self._run(self._fakes([bad, bad], [], []))                                               # 重做，仍违规；入口、挂线沿用
        self.assertGreater(self.conn.execute("SELECT count(*) FROM form_record").fetchone()[0], n_rec)
        self.assertEqual(form.auto_repack(self.conn, log=io.StringIO()), 0)                     # 已写过 REPACK_MAX 张：不再自动重做
        reqs = [r for r in st.requests(self.conn, "form", "all") if r["from"] == "form"]
        self.assertEqual(len(reqs), form.REPACK_MAX)

    def test_relabel_sees_old_entry(self):
        """看着指定的旧入口重新起名（2026-10-27）：主会话申请重做后另起了线，relabel 拿旧入口重新起名，挂回旧入口那时的线。"""
        a = self._session("s1", "2026-10-17T08:00:00Z")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))
        (t_old,) = form.entries(self.conn)
        old_line = self.conn.execute("SELECT line FROM attachment ORDER BY id DESC").fetchone()[0]
        me = st.main_operator(self.conn, "m")
        recs = [r[0] for r in self.conn.execute("SELECT id FROM form_record ORDER BY id")]
        st.request(self.conn, st.MAIN, "form", {"form_record": recs[:1]}, "来源超过两页", me, cause="manual")
        self.conn.commit()
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], ["名字：X 的另一种说法\n停在：升级（陈述1）\n索引词：\n- X\n"],
                              ["挂到：新线 X 升级（二）｜为什么：新的目标"]))
        self.assertNotEqual(self.conn.execute("SELECT line FROM attachment ORDER BY id DESC").fetchone()[0], old_line)
        state = form.relabel(self.conn, "dsh", "s1", int(str(t_old).lstrip("T")), model=FakeModel([LABEL]), log=io.StringIO())
        self.assertIn("X 升级", state)
        lab = self.conn.execute("SELECT input FROM act_detail WHERE step='label' ORDER BY occurrence DESC").fetchone()[0]
        self.assertIn("## 这段会话原来的会话入口", lab)                           # 模型看得到旧入口
        self.assertEqual(self.conn.execute("SELECT line FROM attachment ORDER BY id DESC").fetchone()[0], old_line)

    def test_main_request_level(self):
        """申请按级别定下游（J2，2026-10-27）：主会话写的 level = statement，沿用入口（含手动改的名字）和挂线，不调模型起名；
        level = unit（或以前没写级别的主会话申请）才从起名、挂线从头来。"""
        a = self._session("s1", "2026-10-17T08:00:00Z")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))
        me = st.main_operator(self.conn, "m")
        (t,) = form.entries(self.conn)
        form.feedback_entry(self.conn, me, int(str(t).lstrip("T")), "名字太泛", name="X 升级与回退")
        line = self.conn.execute("SELECT line FROM attachment ORDER BY id DESC").fetchone()[0]
        recs = [r[0] for r in self.conn.execute("SELECT id FROM form_record ORDER BY id")]
        rid = st.request(self.conn, st.MAIN, "form", {"form_record": recs[:1], "level": ["statement"]}, "来源超过两页", me, cause="manual")
        self.conn.commit()
        u = form.units(self.conn, self.now)[0]
        self.assertTrue(form._keep_entry(self.conn, u))
        self.assertFalse(form._restart(self.conn, u))
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [], []))           # 起名、挂线的假模型是空的：调用就会出错
        (e,) = form.entries(self.conn).values()
        self.assertEqual(e["name"], "X 升级与回退")
        self.assertEqual(self.conn.execute("SELECT line FROM attachment ORDER BY id DESC").fetchone()[0], line)
        # 单位级、以前没写级别的主会话申请：从头来
        recs = [r[0] for r in self.conn.execute("SELECT id FROM form_record ORDER BY id")]
        st.request(self.conn, st.MAIN, "form", {"form_record": recs[-1:], "level": ["unit"]}, "会话被切成两份", me, cause="manual")
        self.conn.commit()
        u = form.units(self.conn, self.now)[0]
        self.assertTrue(form._restart(self.conn, u) and not form._keep_entry(self.conn, u))
        self.assertEqual(form._levels(self.conn, [rid]), ["statement"])

    def _formed(self):
        a = self._session("s1", "2026-10-17T08:00:00Z")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))
        recs = [r[0] for r in self.conn.execute("SELECT id FROM form_record ORDER BY id")]
        line = self.conn.execute("SELECT line FROM attachment ORDER BY id DESC").fetchone()[0]
        return st.main_operator(self.conn, "m"), recs, line

    def _acts(self, step):
        return self.conn.execute("SELECT count(*) FROM act_detail WHERE step=?", (step,)).fetchone()[0]

    def test_entry_level_redo(self):
        """入口级的申请（J2，2026-10-27）：只重新起名，陈述留着（不再 pack）、挂线沿用（不再 attach）；模型看得到被指出的问题；
        起名认领这张申请。"""
        me, recs, line = self._formed()
        rid = st.request(self.conn, st.MAIN, "form", {"form_record": recs[:1], "level": ["entry"]}, "名字太泛，要带上回退", me, cause="manual")
        self.conn.commit()
        u = form.units(self.conn, self.now)[0]
        self.assertEqual(u["request"], [rid])
        self.assertFalse(form._restart(self.conn, u) or form._keep_entry(self.conn, u))
        new = "名字：X 升级与回退演练\n停在：定下按 b 做法升级（陈述1、陈述2）\n索引词：\n- X 升级｜升级 X\n"
        self._run(self._fakes([], [new], []))                       # pack、attach 的假模型是空的：调用就会出错
        (e,) = form.entries(self.conn).values()
        self.assertEqual(e["name"], "X 升级与回退演练")
        self.assertEqual(self.conn.execute("SELECT line FROM attachment ORDER BY id DESC").fetchone()[0], line)
        self.assertEqual((self._acts("pack"), self._acts("label"), self._acts("attach")), (1, 2, 1))
        lab = self.conn.execute("SELECT input FROM act_detail WHERE step='label' ORDER BY occurrence DESC").fetchone()[0]
        self.assertIn("原来的入口被指出的问题", lab)
        self.assertIn("名字太泛", lab)
        self.assertEqual(st.requests(self.conn, "form", "open"), [])       # 认领了
        self.assertEqual(form.units(self.conn, self.now), [])

    def test_attach_level_redo(self):
        """挂线级的申请（J2，2026-10-27）：只重新挂线，陈述和入口留着（不再 pack、起名）；模型看得到原来的挂线和被指出的问题。"""
        me, recs, line = self._formed()
        rid = st.request(self.conn, st.MAIN, "form", {"form_record": recs[:1], "level": ["attach"]}, "线名起窄了，应当另起一条", me, cause="manual")
        self.conn.commit()
        u = form.units(self.conn, self.now)[0]
        self.assertEqual(u["request"], [rid])
        self._run(self._fakes([], [], ["挂到：新线 X 升级的另一条线｜为什么：目标不同"]))
        self.assertEqual(form.entries(self.conn)["T1"]["name"], "X 升级")
        self.assertNotEqual(self.conn.execute("SELECT line FROM attachment ORDER BY id DESC").fetchone()[0], line)
        self.assertEqual((self._acts("pack"), self._acts("label"), self._acts("attach")), (1, 1, 2))
        att = self.conn.execute("SELECT input FROM act_detail WHERE step='attach_lines' OR step='attach' ORDER BY occurrence DESC").fetchone()[0]
        self.assertIn("有人指出不对：线名起窄了", att)
        self.assertIn(f"L{line}", att)
        self.assertEqual(st.requests(self.conn, "form", "open"), [])
        self.assertEqual(form.units(self.conn, self.now), [])

    def test_entry_and_attach_levels_together(self):
        """同时有入口级和挂线级的申请：起名认领入口级、不沿用挂线，挂线认领挂线级。"""
        me, recs, line = self._formed()
        st.request(self.conn, st.MAIN, "form", {"form_record": recs[:1], "level": ["entry"]}, "名字太泛", me, cause="manual")
        st.request(self.conn, st.MAIN, "form", {"form_record": recs[:1], "level": ["attach"]}, "线挂错了", me, cause="manual")
        self.conn.commit()
        new = "名字：X 升级与回退演练\n停在：定下按 b 做法升级（陈述1、陈述2）\n索引词：\n- X 升级｜升级 X\n"
        self._run(self._fakes([], [new], ["挂到：新线 另一条｜为什么：换了"]))
        self.assertEqual((self._acts("pack"), self._acts("label"), self._acts("attach")), (1, 2, 2))
        self.assertEqual(st.requests(self.conn, "form", "open"), [])
        self.assertNotEqual(self.conn.execute("SELECT line FROM attachment ORDER BY id DESC").fetchone()[0], line)

    def test_parse(self):
        name, topic, items, dropped, unknown = pack.parse(PACK.format(a=1, b=2), [{"id": 1}, {"id": 2}, {"id": 3}])
        self.assertEqual((name, topic, len(items)), ("X 升级", "讨论 X 怎么升级", 2))
        self.assertEqual(items[1]["files"], [{"path": "docs/x.md", "to": [2]}])
        self.assertEqual((dropped, unknown), ([3], []))
        self.assertEqual(pack.labels("E3、E5-E7"), [3, 5, 6, 7])
        n, stops, lists = label.parse(LABEL)
        self.assertEqual((n, stops, lists), ("X 升级", [("定下按 b 做法升级", [1, 2])], [["X 升级", "升级 X"], ["docs/x.md"]]))
        self.assertEqual(label.parse("名字：a\nkey：\n- b｜c\n")[2], [["b", "c"]])          # 旧写法 key 也认
        self.assertEqual(attach.parse("挂到：新线 Y（父：L3）｜为什么：w", False)[1:3], ("新线 Y", "L3"))
        self.assertEqual([form.handle_of(x) for x in ("l-3", "s-5", "t5", "L3", "k-engram")], ["L3", "T5", "T5", "L3", "k-engram"])
        self.assertEqual(label.dedup("x 升级", ["X 升级"], "2026-10-17T08:00:00Z"), ("x 升级（10-17）", "X 升级"))
        self.assertEqual(label.dedup("X 升级", ["X 升级", "X 升级（10-17）"], "2026-10-17T08:00:00Z"),
                         ("X 升级（10-17 08:00）", "X 升级"))
        self.assertEqual(label.dedup("Y", ["X 升级"], "2026-10-17"), ("Y", None))
        items = [{"text": "用「Foo 服务」和 hook 做"}, {"text": "「Foo 服务」又出现；AI 和 GB 太短"}, {"text": "「只一次」"}]
        self.assertEqual(label.terms(items, [["hook"]]), ["Foo 服务"])          # 中文要两条陈述、ASCII 太短不补、已有不补

    def test_flow(self):
        a = self._session("s1", "2026-10-17T08:00:00Z")
        b = self._session("s2", "2026-10-17T09:00:00Z")
        units = form.units(self.conn, self.now)
        self.assertEqual([u["unit"][1] for u in units], ["s1", "s2"])
        fakes = self._fakes([PACK.format(a=a[0], b=a[1]), PACK.format(a=b[0], b=b[1])], [LABEL, LABEL],
                            ["挂到：新线 X 升级｜为什么：新的目标", "挂到：L1｜为什么：同一个目标"])
        self._run(fakes)
        steps = [r[0] for r in self.conn.execute("SELECT a.step FROM act_detail a WHERE a.process='form' ORDER BY a.occurrence")]
        self.assertEqual(steps, ["pack", "pack", "label", "label", "attach", "attach"])
        import hashlib
        for r in self.conn.execute("SELECT input,meta FROM act_detail WHERE process='form'"):
            ex = json.loads(r['meta'])['expression']
            self.assertEqual(ex['text_hash'], hashlib.sha256(r['input'].encode()).hexdigest())
            self.assertEqual(ex['user_name'], U)
            self.assertIn('## 表达规范', r['input'])
        # 起名查相近的话题、挂线查候选，各是一次内部拉取：一条 use，parent 是那次 act
        uses = self.conn.execute("SELECT o.parent, o.inputs, d.query, d.meta, a.step FROM use_detail d "
                                 "JOIN occurrence o ON o.id=d.occurrence JOIN act_detail a ON a.occurrence=o.parent "
                                 "WHERE d.purpose='form' ORDER BY o.id").fetchall()
        self.assertEqual([r["step"] for r in uses], ["label", "label", "attach", "attach"])
        self.assertEqual(json.loads(uses[3]["meta"])["handles"]["session_entry"], [1])     # 第二次挂线看到了 T1
        self.assertIn("act", json.loads(uses[3]["inputs"]))                                # 记成写出它的 act
        from ...synapse import track
        self.assertEqual(track.backfill_internal(self.conn), 0)                            # 已经记过的不再补
        self.assertEqual(self.conn.execute("SELECT count(*) FROM form_record WHERE kind='session'").fetchone()[0], 4)
        lines = self.conn.execute("SELECT * FROM topic_line").fetchall()
        self.assertEqual([(r["name"], r["workspace"], r["node"]) for r in lines], [("X 升级", "w", st.NA)])
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT entry, line FROM attachment ORDER BY id")], [(1, 1), (2, 1)])
        self.assertEqual(form.units(self.conn, self.now), [])               # 都做完了
        names = [r[0] for r in self.conn.execute("SELECT name FROM session_entry ORDER BY id")]
        self.assertEqual(names, ["X 升级", "X 升级（10-17）"])                # 程序查重：重名的加日期
        # 同一批并行起名时互相看不到，靠上面的程序查重；之后起名的看得到名字查得到的相近会话入口（带日期、主题）
        pa = self.conn.execute("SELECT record FROM session_entry WHERE session='s2'").fetchone()[0]
        text, near = form.build_label(self.conn, {"unit": ("dsh", "s2", 0), "session": form._session(self.conn, "dsh", "s2")}, pa)
        self.assertEqual(near, ["T1"])
        self.assertIn("- X 升级（2026-10-17）：讨论 X 怎么升级", text)
        self.assertNotIn("全部", text)                                         # 不推全部名字
        others = form._others(self.conn, {"unit": ("dsh", "s2", 0), "session": form._session(self.conn, "dsh", "s2")})
        self.assertEqual(label.similar("X   升级", others, "w"), ["T1"])          # 空格不挡匹配，多几个也行
        self.assertEqual(label.similar("Y 迁移", form._others(self.conn, {"unit": ("dsh", "s2", 0),
                         "session": form._session(self.conn, "dsh", "s2")}), "w"), [])      # 不相关的不推
        p = form.pool(self.conn)
        self.assertEqual(p["T1"]["parent"], "L1")
        self.assertEqual(p["T1"]["stops"], ["定下按 b 做法升级（陈述1、陈述2）"])
        self.assertIn(["X 升级", "升级 X"], p["L1"]["words"]["lists"])     # 两个子会话入口都有的组进了线的索引词
        self.assertIn(["甲服务"], p["T1"]["words"]["lists"])                    # 程序补的「」词（两条陈述里都有）
        # 写进的文件附在第 2 轮后面（dsh 的假记录没有写文件，这里只看输入里有事件）
        first = self.conn.execute("SELECT input FROM act_detail WHERE step='pack' ORDER BY occurrence").fetchone()[0]
        self.assertIn(f"[E{a[0]}]", first)
        self.assertIn("## 第 2 轮", first)

    def test_line_words_share(self):
        """线的索引词要够多子入口都有：15 个子入口要 3 个（LINE_SHARE 两成），只有两个的不收；成员少时两个就够。"""
        def kid(i, *groups):
            return f"T{i}", {"id": f"T{i}", "kind": "entry", "parent": "L1", "words": {"lists": [list(g) for g in groups]}}
        pool = {"L1": {"id": "L1", "kind": "line", "name": "线", "parent": None}}
        pool.update(kid(i, ["甲"] if i < 2 else ["丙"], ["乙"] if i < 3 else ["丁"]) for i in range(15))
        got = form.line_words(pool, "L1")["lists"]
        self.assertNotIn(["甲"], got)
        self.assertIn(["乙"], got)
        small = {k: v for k, v in pool.items() if k in ("L1", "T0", "T1")}
        self.assertIn(["甲"], form.line_words(small, "L1")["lists"])

    def test_feedback_attach(self):
        """挂线的反馈：改挂到新线或已有的线，新写一条挂线，原来的留着；按会话更正工作区以后，入口和新线都算到更正后的工作区。"""
        from unittest import mock
        a = self._session("s1", "2026-10-17T08:00:00Z")
        b = self._session("s2", "2026-10-17T09:00:00Z")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1]), PACK.format(a=b[0], b=b[1])], [LABEL, LABEL],
                              ["挂到：新线 X 升级｜为什么：新的目标", "挂到：L1｜为什么：同一个目标"]))
        op = st.main_operator(self.conn, "test-model")
        with self.assertRaises(ValueError):
            form.feedback_attach(self.conn, op, 2, "理由", line=None, new_line=None)       # 只给一个去处
        with self.assertRaises(ValueError):
            form.feedback_attach(self.conn, op, 2, "理由", new_line="X 升级")             # 和已有的线重名
        with mock.patch.dict(st.SESSION_WORKSPACE, {("dsh", "s2"): "v"}):
            lid = form.feedback_attach(self.conn, op, 2, "其实是另一个目标", new_line="Y 迁移")
            e = form.entries(self.conn)["T2"]
            self.assertEqual((e["parent"], e["ws"]), (f"L{lid}", "v"))
            self.assertEqual(self.conn.execute("SELECT workspace FROM topic_line WHERE id=?", (lid,)).fetchone()[0], "v")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM attachment WHERE entry=2").fetchone()[0], 2)   # 原来的留着
        form.feedback_attach(self.conn, op, 2, "还是同一个目标", line="L1")
        self.assertEqual(form.entries(self.conn)["T2"]["parent"], "L1")

    def test_feedback_attach_move(self):
        """搬线和中间层（2026-10-24，L44 按索引粗分）：新线接替旧线（from，重名不拦），旧线清空后标 retired、不进候选；
        新线可以挂在一条线下面（中间层），只有子线的线不算清空；新线可以建在别的工作区。"""
        a = self._session("s1", "2026-10-17T08:00:00Z")
        b = self._session("s2", "2026-10-17T09:00:00Z")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1]), PACK.format(a=b[0], b=b[1])], [LABEL, LABEL],
                              ["挂到：新线 X 升级｜为什么：新的目标", "挂到：L1｜为什么：同一个目标"]))
        op = st.main_operator(self.conn, "test-model")
        top = form.feedback_attach(self.conn, op, 1, "搬线", new_line="X 升级", from_=["L1"])          # 和 L1 同名，接替它
        self.assertEqual(json.loads(self.conn.execute('SELECT "from" FROM topic_line WHERE id=?', (top,)).fetchone()[0]), [1])
        self.assertEqual(set(form.line_heads(self.conn, 1)), {top})                  # 旧句柄顺着 from 找到现在的线
        sub = form.feedback_attach(self.conn, op, 2, "分到中间层下", new_line="X 升级的回滚", parent_line=f"L{top}")
        p = form.pool(self.conn)
        self.assertTrue(p["L1"].get("retired"))                                  # 两个话题都改挂走了
        self.assertEqual((p[f"L{sub}"]["parent"], p["T2"]["parent"]), (f"L{top}", f"L{sub}"))
        self.assertFalse(p[f"L{top}"].get("retired"))                            # 下面有子线里的话题
        with self.assertRaises(ValueError):
            form.feedback_attach(self.conn, op, 2, "重名", new_line="X 升级")       # 和现在的线重名照样拦
        mid = form.feedback_line(self.conn, op, "X 的整体", "中间层", "w", parent_line=f"L{top}")
        self.assertTrue(form.pool(self.conn)[f"L{mid}"].get("retired"))         # 还没有子线、话题：先不列
        form.feedback_attach(self.conn, op, 2, "放进中间层", new_line="X 升级的回滚 2", parent_line=f"L{mid}")
        self.assertFalse(form.pool(self.conn)[f"L{mid}"].get("retired"))
        other = form.feedback_attach(self.conn, op, 2, "其实是别的项目的事", new_line="Z 维护", workspace="z")
        self.assertEqual(self.conn.execute("SELECT workspace FROM topic_line WHERE id=?", (other,)).fetchone()[0], "z")

    def test_root_to_global(self):
        """根下不挂线（2026-10-24）：模型选了骨架的根，程序改挂 global。"""
        design = self.dir / "design"
        for d in ("", "global", "engram"):
            (design / d).mkdir(parents=True, exist_ok=True)
            if d:
                (design / d / "设计.md").write_text("x")
        self.enterContext(patch.dict(skeleton.WORKSPACES, {"proj": (design, "Proj")}, clear=True))
        root, glob = skeleton.nodes("proj")[""]["id"], skeleton.nodes("proj")["global"]["id"]
        a = self._session("s1", "2026-10-17T08:00:00Z", cwd="/home/u/proj")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], [f"节点：{root}｜挂到：新线 X 升级｜为什么：跨了几个模块"]))
        line = self.conn.execute("SELECT * FROM topic_line").fetchone()
        self.assertEqual(line["node"], "global")
        self.assertEqual(form.pool(self.conn)["L1"]["parent"], glob)

    def test_managing_workspace(self):
        """管理性质的工作区（2026-10-21）：目录下别的工作区的线也能挂；不是管理性质的，挂别的工作区的线由程序拒掉，候选里也没有。"""
        from unittest import mock
        a = self._session("s1", "2026-10-17T08:00:00Z", cwd="/m/a")
        b = self._session("s2", "2026-10-17T09:00:00Z", cwd="/m")
        c = self._session("s3", "2026-10-17T10:00:00Z", cwd="/z")
        with mock.patch.dict(st.MANAGING, {"m": "/m"}):
            self.assertEqual(st.reachable(self.conn, "m"), {"m", "a"})
            self.assertEqual(st.reachable(self.conn, "z"), {"z"})
            self._run(self._fakes([PACK.format(a=x[0], b=x[1]) for x in (a, b, c)], [LABEL] * 3,
                                  ["挂到：新线 X 升级｜为什么：新的目标", "挂到：L1｜为什么：同一个目标"] + ["挂到：L1｜为什么：同一个目标"] * 3))
            e = form.entries(self.conn)
            self.assertEqual((e["T1"]["ws"], e["T2"]["ws"], e["T2"]["parent"]), ("a", "m", "L1"))
            rows = self.conn.execute("SELECT o.error, d.meta FROM occurrence o JOIN act_detail d ON d.occurrence=o.id "
                                     "WHERE d.step='attach' AND json_extract(d.window,'$.unit[1]')='s3'").fetchall()
            self.assertTrue(rows and all("属于别的工作区" in r[0] for r in rows))
            self.assertTrue(all("L1" not in json.loads(r[1])["picks"] for r in rows))
            # 本工作区的入口挂在别的工作区的线下（以前的反馈、迁移留下的）：按它所在的线算，也不进候选
            form.feedback_attach(self.conn, st.main_operator(self.conn, "test-model"), 3, "改挂", line="L1")
            d = self._session("s4", "2026-10-17T10:30:00Z", cwd="/z")
            self._run(self._fakes([PACK.format(a=d[0], b=d[1])], [LABEL], ["挂到：L1｜为什么：同一个目标"] * 6))
            for _ in range(2):                                    # s3 没失败满次数前挡在前面，多跑两轮
                self._run(self._fakes(attaches=["挂到：L1｜为什么：同一个目标"] * 6))
            rows = self.conn.execute("SELECT d.meta FROM occurrence o JOIN act_detail d ON d.occurrence=o.id "
                                     "WHERE d.step='attach' AND json_extract(d.window,'$.unit[1]')='s4'").fetchall()
            self.assertTrue(rows and all(not {"L1", "T3"} & set(json.loads(r[0])["picks"]) for r in rows))

    def test_backfill_internal(self):
        """10-20 以前的 act 没记内部拉取的 use：从 meta 的 near、picks 补，可以重复跑；meta 里没有的不补。"""
        from unittest import mock
        from ...synapse import track
        a = self._session("s1", "2026-10-17T08:00:00Z")
        b = self._session("s2", "2026-10-17T09:00:00Z")
        fakes = self._fakes([PACK.format(a=a[0], b=a[1]), PACK.format(a=b[0], b=b[1])], [LABEL, LABEL],
                            ["挂到：新线 X 升级｜为什么：新的目标", "挂到：L1｜为什么：同一个目标"])
        with mock.patch.object(track, "internal", lambda *x, **k: None):
            self._run(fakes)
        count = "SELECT count(*) FROM use_detail WHERE purpose='form'"
        self.assertEqual(self.conn.execute(count).fetchone()[0], 0)
        self.assertEqual(track.backfill_internal(self.conn), 4)
        self.assertEqual(track.backfill_internal(self.conn), 0)
        r = self.conn.execute("SELECT o.started, o.parent, d.session, d.meta FROM use_detail d JOIN occurrence o ON o.id=d.occurrence "
                              "WHERE d.purpose='form' ORDER BY o.id DESC").fetchone()
        act = self.conn.execute("SELECT started FROM occurrence WHERE id=?", (r["parent"],)).fetchone()
        self.assertEqual((r["started"], r["session"]), (act["started"], "s2"))        # 时刻、会话照那次 act
        self.assertEqual(json.loads(r["meta"])["handles"]["session_entry"], [1])

    def test_wait_and_gate(self):
        self._session("s1", "2026-10-17T08:00:00Z")
        self.assertEqual(form.units(self.conn, datetime(2026, 10, 17, 8, 30, tzinfo=timezone.utc)), [])   # 还没告一段落
        # 门槛：更早的会话里还有没预览的轮次 → 后面的会话等着
        f = self.dir / "s0.jsonl"
        f.write_text("x")
        sess = Session("dsh", "s0", str(f), started="2026-10-17T07:00:00Z", cwd="/w")
        collect.run(self.conn, sources=("dsh",), force=True, log=io.StringIO(),
                    modules={"dsh": FakeSource(f, sess, [Turn("还没看", "嗯", closed=True, started="2026-10-17T07:00:00Z")])})
        self.assertEqual(form.units(self.conn, self.now), [])

    def test_active_session_does_not_gate(self):
        """还在进行的会话（没到 IDLE）的未预览轮不挡门槛（2026-10-27：另一段会话的未预览轮从更早的时间钉住门槛两个多小时）；
        告一段落了还没预览的才挡。"""
        self._session("s1", "2026-10-17T08:00:00Z")
        f = self.dir / "s0.jsonl"
        f.write_text("x")
        sess = Session("dsh", "s0", str(f), started="2026-10-17T07:00:00Z", cwd="/w")
        began = (self.now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        collect.run(self.conn, sources=("dsh",), force=True, log=io.StringIO(),
                    modules={"dsh": FakeSource(f, sess, [Turn("早先的", "嗯", closed=True, started="2026-10-17T07:00:00Z"),
                                                         Turn("还在做", "嗯", closed=True, started=began)])})
        self.assertEqual([u["unit"][1] for u in form.units(self.conn, self.now)], ["s1"])

    def test_switched_off_session_unreadable(self):
        """/no-observe 在 form 读过之后（J5，2026-10-27）：会话入口读不到（pool、entries 都不列，线跟着清空），/do-observe 后回来。"""
        a = self._session("s1", "2026-10-17T08:00:00Z")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))
        self.assertEqual(len(form.entries(self.conn)), 1)

        def say(command, when):
            f = self.dir / "s1.jsonl"
            sess = Session("dsh", "s1", str(f), started="2026-10-17T08:00:00Z", cwd="/w")
            turns = [Turn("把 X 升级一下（s1）", "好", closed=True, started="2026-10-17T08:00:00Z"),
                     Turn("写下来（s1）", "写进 docs/x.md 了", closed=True, started="2026-10-17T08:00:00Z")]
            turns += [Turn(c, "", closed=True, started=t) for c, t in self.said + [(command, when)]]
            self.said.append((command, when))
            collect.run(self.conn, sources=("dsh",), force=True, log=io.StringIO(), modules={"dsh": FakeSource(f, sess, turns)})
        self.said = []
        say("/no-observe", "2026-10-17T09:00:00Z")
        self.assertEqual(form.entries(self.conn), {})
        got = form.pool(self.conn).values()
        self.assertFalse([e for e in got if e["kind"] == "entry"])
        self.assertTrue(all(e.get("retired") for e in got if e["kind"] == "line"))
        say("/do-observe", "2026-10-17T10:00:00Z")
        self.assertEqual(len(form.entries(self.conn)), 1)

    def test_wait_follows_observe_idle(self):
        """form 等会话告一段落按 observe 的 IDLE（dsh 3 小时），不是自己的 60 分钟：J1 以后还在进行的会话不进门槛，
        只靠 60 分钟的话，快道读过的 E 会先被整理，预览读完的再变成续上或中间插入（J8，2026-10-27）。"""
        self._session("s1", "2026-10-17T08:00:00Z")
        for hours, want in ((1.5, []), (2.5, []), (3.5, ["s1"])):
            now = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc) + timedelta(hours=hours)
            self.assertEqual([u["unit"][1] for u in form.units(self.conn, now)], want, hours)

    def test_failures(self):
        a = self._session("s1", "2026-10-17T08:00:00Z")
        b = self._session("s2", "2026-10-17T09:00:00Z")
        fakes = self._fakes([TimeoutError("超时"), PACK.format(a=b[0], b=b[1])] + [TimeoutError("超时")] * 2, [LABEL],
                            ["挂到：新线 X 升级｜为什么：新的目标"])
        self._run(fakes)              # s1 超时一次：挡住 s2 的挂线
        self.assertEqual(self.conn.execute("SELECT count(*) FROM attachment").fetchone()[0], 0)
        self._run(fakes)
        self._run(fakes)              # s1 失败满三次：进待重做清单，不再挡后面
        self.assertEqual([r[0] for r in form.redo(self.conn)], ["pack"])
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT entry, line FROM attachment")], [(1, 1)])

    def test_capacity_retries_then_recovers(self):
        a = self._session("s1", "2026-10-17T08:00:00Z")
        busy = RuntimeError("Selected model is at capacity. Please try a different model.")
        fakes = self._fakes([busy] * 3 + [PACK.format(a=a[0], b=a[1])], [LABEL],
                            ["挂到：新线 X 升级｜为什么：新的目标"])
        for _ in range(3):
            self._run(fakes)
        rows = self.conn.execute("SELECT o.outcome FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
                                 "WHERE a.process='form' AND a.step='pack' ORDER BY o.id").fetchall()
        self.assertEqual([r[0] for r in rows], ["env_error"] * 3)
        self.assertEqual(form.redo(self.conn), [])
        self._run(fakes)
        self.assertEqual(len(form.history(self.conn).packed), 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM attachment").fetchone()[0], 1)

    def test_bad_attach(self):
        a = self._session("s1", "2026-10-17T08:00:00Z")
        fakes = self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：L9｜为什么：没有这条线", "挂到：新线 X 升级｜为什么：新的"])
        self._run(fakes)
        self._run(fakes)
        rows = self.conn.execute("SELECT o.outcome FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
                                 "WHERE a.step='attach' ORDER BY o.id").fetchall()
        self.assertEqual([r[0] for r in rows], ["content_error", "ok"])

    def test_cross_workspace_attach(self):
        """在 proj 开、碰过 other 3 处以上的会话，other 的线进候选、挂得上，输入里写明工作区线索；碰得少的挂上去算错（2026-10-26）。"""
        from unittest import mock
        a = self._session("s1", "2026-10-17T08:00:00Z", cwd="/home/u/other")
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL], ["挂到：新线 X 升级｜为什么：新的"]))
        b = self._session("s2", "2026-10-17T09:00:00Z", cwd="/home/u/proj")
        c = self._session("s3", "2026-10-17T10:00:00Z", cwd="/home/u/proj")
        touched = {"s2": {"other": 5, "proj": 2}, "s3": {"other": 1}}
        with mock.patch.object(form, "crossed", lambda conn, u: touched[u["unit"][1]]):
            self._run(self._fakes([PACK.format(a=b[0], b=b[1]), PACK.format(a=c[0], b=c[1])], [LABEL, LABEL],
                                  ["挂到：L1｜为什么：同一次升级", "挂到：L1｜为什么：同一次升级", "挂到：新线 Y｜为什么：新的"]))
        rows = self.conn.execute("SELECT o.outcome, a.input FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
                                 "WHERE a.step='attach' ORDER BY o.id").fetchall()
        self.assertEqual([r[0] for r in rows], ["ok", "ok", "content_error"])           # 出错的下一批再试
        self.assertIn("工作区：在 proj 开的；工具调用里碰过的路径（程序数的，不分读写）：other 5 处、proj 2 处", rows[1][1])
        self.assertNotIn("工作区：", rows[2][1])                                   # other 只碰过 1 处：不进范围，也不写线索
        self.assertEqual(self.conn.execute("SELECT line FROM attachment ORDER BY id").fetchall()[1][0], 1)

    def test_skeleton(self):
        design = self.dir / "design"
        for d in ("", "engram", "engram/form"):
            (design / d).mkdir(parents=True, exist_ok=True)
            if d:
                (design / d / "设计.md").write_text("x")
        saved = dict(skeleton.WORKSPACES)
        skeleton.WORKSPACES.clear()
        skeleton.WORKSPACES["proj"] = (design, "Proj")
        try:
            ns = skeleton.nodes("proj")
            self.assertEqual(sorted(ns), ["", "engram", "engram/form"])
            self.assertEqual(skeleton.node_of("mh/engram/form/x.py", "proj"), "engram/form")
            self.assertEqual(skeleton.node_of("mh/engram/observer/x.py", "proj"), "engram")
            self.assertIsNone(skeleton.node_of("tmp/x.py", "proj"))
            a = self._session("s1", "2026-10-17T08:00:00Z", cwd="/home/u/proj")
            fakes = self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL],
                                ["节点：k-engram-form｜挂到：新线 X 升级｜为什么：在做 form 的事"])
            self._run(fakes)
            line = self.conn.execute("SELECT * FROM topic_line").fetchone()
            self.assertEqual((line["workspace"], line["node"]), ("proj", "engram/form"))
            text = self.conn.execute("SELECT input FROM act_detail WHERE step='attach'").fetchone()[0]
            self.assertIn("[k-engram-form] form", text)
            self.assertEqual(form.pool(self.conn)["L1"]["parent"], "k-engram-form")
        finally:
            skeleton.WORKSPACES.clear()
            skeleton.WORKSPACES.update(saved)

    def test_registered_skeleton_attach(self):
        """登记后的文件骨架用于下一次挂线，act 留下采用版本；既有目录骨架仍在。"""
        source = self.dir / "README.md"
        source.write_text("## 功能划分\n\n```text\n示例项目\n├─ 处理\n└─ 交付\n```\n")
        development = self.dir / "design"
        development.mkdir()
        self.enterContext(patch.dict(skeleton.WORKSPACES, {"memory-harness": (development, "Memory Harness")}))
        spec = skeleton.read_file("file-project", self.dir, "README.md", "功能划分")
        with self.conn:
            rid = skeleton.request_file(self.conn, spec, {}, "采用已核对的结构", st.main_operator(self.conn, "test"))
            aid = skeleton.adopt(self.conn, rid, {})
        a = self._session("s1", "2026-10-17T08:00:00Z", cwd="/home/u/file-project")
        nid = spec["nodes"]["处理"]["id"]
        self._run(self._fakes([PACK.format(a=a[0], b=a[1])], [LABEL],
                             [f"节点：{nid}｜挂到：新线 X 升级｜为什么：这次在做处理模块的事"]))
        pool = form.pool(self.conn)
        self.assertIn("k-root", pool)
        self.assertEqual(pool["L1"]["parent"], nid)
        row = self.conn.execute("SELECT o.inputs,a.meta FROM act_detail a JOIN occurrence o ON o.id=a.occurrence "
                                "WHERE a.step='attach'").fetchone()
        self.assertIn(aid, json.loads(row["inputs"])["act"])
        self.assertEqual(json.loads(row["meta"])["skeleton_act"], aid)
        self.assertEqual(form.redo_requests(self.conn), {})

    def test_written(self):
        from ..observe import written_files as written
        self.assertEqual(written.rel("/r/w/.claude/worktrees/b/mh/x.py", "/r/w"), "mh/x.py")
        self.assertEqual(written.rel(str(Path.home() / "a.md"), "/r/w"), "~/a.md")
        log = self.dir / "c.jsonl"
        log.write_text("\n".join(json.dumps(x) for x in [
            {"timestamp": "2026-10-17T08:00:05Z", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "/r/w/docs/x.md"}}]}},
            {"message": {"content": [{"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "/r/w/a.py"}}]}},
        ]))
        got = written.written("claude", str(log), "/r/w", ["2026-10-17T08:00:00Z", "2026-10-17T09:00:00Z"])
        self.assertEqual(got, {0: ["写了 docs/x.md", "写了 a.py"]})            # 不带时间的行沿用前一条
        repo = self.dir / "repo"
        repo.mkdir()
        git = lambda *a, when=None: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, env={
            "PATH": "/usr/bin:/bin", "HOME": str(self.dir), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            **({"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when} if when else {})})
        git("init", "-q")
        for msg, when in (("先", "2026-10-17T08:10:00Z"), ("后", "2026-10-17T08:20:00Z")):
            git("commit", "-q", "--allow-empty", "-m", msg, when=when)
        log.write_text(json.dumps({"timestamp": "2026-10-17T08:05:00Z", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git commit -m x"}}]}}))
        got = written.written("claude", str(log), str(repo), ["2026-10-17T08:00:00Z"])
        self.assertEqual([x.split("「")[1][0] for x in got[0]], ["先", "后"])   # 同一轮几次提交，早的在前


class FileSkeletonTest(unittest.TestCase):
    TEXT = """# 项目
## 功能划分
```text
项目
├─ 检查
│  ├─ 规则
│  └─ 报告
└─ 交付
```
## 模块导览
| 模块 | 功能范围 | 入口 |
|---|---|---|
| 规则 | 核对规则 | [规则](docs/rules.md)、[共同入口](docs/common.md) |
| 报告 | 展示结果 | [共同入口](docs/common.md) |
| 交付 | 导出产物 | [导出](docs/export.md) |
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "README.md"
        self.file.write_text(self.TEXT)
        self.conn = st.connect(self.root / "engram.db")
        self.addCleanup(self.conn.close)
        self.op = st.main_operator(self.conn, "test")

    def read(self, ws="mentor"):
        return skeleton.read_file(ws, self.root, "README.md", "功能划分", "模块导览")

    def line(self, ws="mentor"):
        aid = st.occurrence(self.conn, "act", self.op, started=st.now(), ended=st.now(), outcome="ok")
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (aid, "form", "feedback", "{}", "", "", "{}"))
        return self.conn.execute('INSERT INTO topic_line(born,name,workspace,node,parent,"from") VALUES(?,?,?,?,?,?)',
                                 (aid, "检查工具", ws, st.NA, st.NA, "[]")).lastrowid

    def test_parse_and_file_hints(self):
        spec = self.read()
        ns = spec["nodes"]
        self.assertEqual(len(ns), 5)
        self.assertEqual(ns["检查/报告"]["parent"], "检查")
        self.assertEqual(ns["检查/规则"]["topic"], "核对规则")
        self.assertEqual(self.TEXT.splitlines()[ns["检查/规则"]["line"] - 1], "│  ├─ 规则")
        self.assertEqual(skeleton.node_of("docs/rules.md", "mentor", ns), "检查/规则")
        self.assertEqual(skeleton.node_of("docs/common.md", "mentor", ns), "检查")
        self.assertIsNone(skeleton.node_of("lib/shared.vbs", "mentor", ns))
        self.assertEqual(ns["检查/规则"]["id"], self.read()["nodes"]["检查/规则"]["id"])

    def test_adopt_snapshot_namespace_and_existing_lines(self):
        """两个工作区同名节点并存；同一只读连接的地图缓存能看到新采用，源文件改变不改历史。"""
        from ...synapse import pull
        development = self.root / "design"
        development.mkdir()
        self.enterContext(patch.dict(skeleton.WORKSPACES, {"memory-harness": (development, "Memory Harness")}))
        lid = self.line()
        self.conn.commit()
        cached = pull.layers(self.conn)
        spec = self.read()
        mapping = {f"L{lid}": "检查/规则"}
        before = tuple(self.conn.execute("SELECT * FROM topic_line WHERE id=?", (lid,)).fetchone())
        with self.conn:
            rid = skeleton.request_file(self.conn, spec, mapping, "按文件采用", self.op)
            self.file.write_text(self.TEXT.replace("├─ 检查", "├─ 检验"))
            aid = skeleton.adopt(self.conn, rid, mapping)
        self.assertEqual(st.claimed(self.conn, rid), [(aid, "ok")])
        self.assertIsNot(pull.layers(self.conn), cached)
        self.assertEqual(tuple(self.conn.execute("SELECT * FROM topic_line WHERE id=?", (lid,)).fetchone()), before)
        pool = form.pool(self.conn)
        self.assertEqual(pool[f"L{lid}"]["parent"], spec["nodes"]["检查/规则"]["id"])
        self.assertEqual(skeleton.nodes("mentor", self.conn), spec["nodes"])
        with self.assertRaises(ValueError):
            skeleton.adopt(self.conn, rid, mapping)
        self.file.write_text(self.TEXT)
        second = self.read("other-project")
        with self.conn:
            rid2 = skeleton.request_file(self.conn, second, {}, "另一个工作区", self.op)
            skeleton.adopt(self.conn, rid2, {})
        pool = form.pool(self.conn)
        self.assertEqual(len([n for n in pool.values() if n["kind"] == "node" and n["ws"] in ("mentor", "other-project")]), 10)
        self.assertNotEqual(spec["nodes"][""]["id"], second["nodes"][""]["id"])
        self.assertIn("k-root", pool)
        readonly = st.connect(self.root / "engram.db", readonly=True)
        try:
            self.assertEqual(form.pool(readonly)[f"L{lid}"]["parent"], spec["nodes"]["检查/规则"]["id"])
        finally:
            readonly.close()

    def test_missing_cross_workspace_and_invalid_placement(self):
        lid = self.line()
        other = self.line("other")
        spec = self.read()
        for mapping in ({}, {f"L{other}": "检查"}, {f"L{lid}": "不存在"}):
            with self.assertRaises(ValueError):
                skeleton.preview(self.conn, spec, mapping)
        self.assertNotIn("mentor", skeleton.adopted(self.conn))

    def test_update_replaces_active_view_with_history(self):
        spec = self.read()
        with self.conn:
            rid = skeleton.request_file(self.conn, spec, {}, "第一版", self.op)
            aid = skeleton.adopt(self.conn, rid, {})
        self.file.write_text(self.TEXT.replace("├─ 检查", "├─ 检验"))
        with self.conn:
            rid2 = skeleton.request_file(self.conn, self.read(), {}, "新版本", self.op)
            aid2 = skeleton.adopt(self.conn, rid2, {})
        self.assertIn("检验/规则", skeleton.nodes("mentor", self.conn))
        self.assertNotIn("检查/规则", skeleton.nodes("mentor", self.conn))
        self.assertEqual(self.conn.execute("SELECT predecessor FROM occurrence WHERE id=?", (aid2,)).fetchone()[0], aid)
        self.assertEqual(json.loads(self.conn.execute("SELECT meta FROM act_detail WHERE occurrence=?", (aid,)).fetchone()[0])["nodes"], spec["nodes"])

    def test_malformed_sources_and_rollback(self):
        for text in (self.TEXT + "\n## 功能划分\nx\n", self.TEXT.replace("├─ 检查", "│  ├─ 检查"),
                     self.TEXT.replace("└─ 交付", "└─ 检查")):
            self.file.write_text(text)
            with self.assertRaises(ValueError):
                self.read()
        self.file.write_text(self.TEXT)
        with self.assertRaises(ValueError):
            with self.conn:
                rid = skeleton.request_file(self.conn, self.read(), {}, "", self.op)
                skeleton.adopt(self.conn, rid, {})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM act_detail WHERE step='read_skeleton'").fetchone()[0], 0)


class GlossaryIndexTest(unittest.TestCase):
    def test_index_in_sync(self):
        """词典开头的词头索引和词头一致（加了词条要跑 glossary.write_index()）。"""
        from . import glossary
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "glossary.md"
            path.write_text("# 词典\n\n## 怎么维护\n\n## 过程\n\n**形成 / form**\n- 形成会话记录。\n")
            glossary.write_index(path)
            self.assertIn(glossary.index(path), path.read_text())
            before = path.read_text()
            glossary.write_index(path)
            self.assertEqual(path.read_text(), before)


if __name__ == "__main__":
    unittest.main()


from ..store import instance


from . import glossary


class PrivateNamesTest(unittest.TestCase):
    def test_private_alias_supply_does_not_merge_role_or_edit_source(self):
        with tempfile.TemporaryDirectory() as d, patch.object(instance, 'USER_ALIASES', ('青禾', '松风', 'studio', '用户', 'user')):
            p = Path(d) / 'glossary.md'
            p.write_text('**用户 / user**\n**工具 / tool**\n')
            source = 'studio 是机器名；青禾说“我的名字有多种叫法”。'
            self.assertEqual(glossary.aliases('青禾', p), ['青禾', '松风', 'studio'])
            self.assertEqual(glossary.aliases('用户', p), ['用户', 'user'])
            hint = glossary.text(source, p)
            self.assertIn('本实例的称呼线索', hint)
            self.assertIn('按上下文判断', hint)
            self.assertEqual(source, 'studio 是机器名；青禾说“我的名字有多种叫法”。')
            self.assertIn('松风', glossary.aliases('studio', p))  # 检索候选扩大，未建立实体关系
            self.assertEqual(glossary.aliases('tool', p), ['工具', 'tool'])
            self.assertEqual(glossary.text('完全无关的一段', p), '')

class ExpressionOperatorTest(unittest.TestCase):
    def test_rules_version_and_instance_names(self):
        from unittest.mock import patch
        from ... import express
        module = form
        step = 'pack'
        with patch.object(module, 'register', return_value=1) as register:
            with patch.object(module, 'USER_NAME', '青禾'):
                a = module.operator(None, step)[1]
            with patch.object(module, 'USER_NAME', '松风'):
                self.assertEqual(a, module.operator(None, step)[1])
            self.assertEqual(register.call_args.kwargs['params']['expression'], express.signature())
            with patch('mh.express.VERSION', 'test-next'):
                self.assertNotEqual(a, module.operator(None, step)[1])
