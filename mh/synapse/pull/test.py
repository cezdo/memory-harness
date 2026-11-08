"""Synapse 换库（E1a）的测试：新库上的 search / get / map、旧 J 和会话入口的重叠、足迹（use）与旧足迹的补录、手记推送、MCP。
数据照 form 的测试造（假模型），旧系统导入的 J 手写一条。python3 -m unittest mh.synapse.pull.test
"""
import json
import re

from ...engram import store as st
from ...engram.form import test as ft
from .. import pull, push, track
from . import mcp
from ...engram.store.instance import USER_NAME as U


def body(text):
    """句柄一致性比较来源内容；每次送达编号属于本次交付。"""
    import re
    return re.sub(r"^<memory-harness[^>]*>\n|\n</memory-harness>$", "", text)


class PullTest(ft.FormTest):              # 借 form 测试造数据的办法；form 自己的测试不在这里重跑（见文件末尾）
    def setUp(self):
        super().setUp()
        pull._CACHE.clear()
        from ..track import pushes                    # 推送流水写进临时目录，不进真的数据目录
        self._pushes = (pushes.DIR, pushes.LATEST)
        pushes.DIR, pushes.LATEST = self.dir / "events", self.dir / "events" / "synapse-latest.md"
        self.addCleanup(lambda: setattr(pushes, "DIR", self._pushes[0]) or setattr(pushes, "LATEST", self._pushes[1]))
        from ..hosts import claude                   # 钩子按宿主进程记的当前会话也写进临时目录（不能改到正在用的 claude）
        self._run_dir, claude.RUN = claude.RUN, self.dir / "run" / "claude"
        self.addCleanup(lambda: setattr(claude, "RUN", self._run_dir))
        self.e = self._session("s1", "2026-10-17T08:00:00Z")
        self._run(self._fakes([ft.PACK.format(a=self.e[0], b=self.e[1])], [ft.LABEL], ["挂到：新线 X 升级｜为什么：新的目标"]))
        self.kw = dict(session="t", purpose="dev", cwd="/w")

    def _legacy_j(self, text="当时我认为 X 应该先备份再升级。", session="s1"):
        """旧系统导入的 J：依据是旧的会话记录，旧的 pack act 的窗口里写着会话。"""
        op = st.operator(self.conn, "legacy/form-test", "form", "former", legacy=True)
        sess = self.conn.execute("SELECT occurrence FROM session_detail WHERE session=?", (session,)).fetchone()[0]
        act = st.occurrence(self.conn, "act", op, started="2026-10-18T00:00:00Z", scale="session", outcome="ok")
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                          (act, "form", "pack", json.dumps({"session": sess, "ordinals": [0, 1]}), "", "", "{}"))
        rec = self.conn.execute("INSERT INTO form_record(born, idx, kind, text, refs, labels, structures, unresolved) "
                                "VALUES(?,?,?,?,?,?,?,?)", (act, 0, "session", f"旧的陈述：{U}说先备份。", "{}", '["S1"]', "{}", "[]")).lastrowid
        jid = 900
        self.conn.execute("INSERT INTO judgment VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (jid, act, 0, text, '[{"who":"我","role":"?unknown"}]', json.dumps({"form_record": [rec]}),
                           '["S1"]', "[]", "?not_done", "?not_done", "[]"))
        self.conn.commit()
        return jid, rec

    def _uses(self):
        return self.conn.execute("SELECT o.inputs, d.* FROM use_detail d JOIN occurrence o ON o.id=d.occurrence "
                                 "WHERE d.session='t' ORDER BY o.id").fetchall()

    def test_switched_off_like_missing(self):
        """/no-observe 以后，已经形成的也读不到，和没有一样（审查 05，2026-10-28）：直接给话题、陈述（会话里的、线里的）、E 的句柄，
        得到的和不存在的句柄一样；/do-observe 以后回来。"""
        import io
        from ...engram.observe import collect
        from ...engram.observe.sources.turn import Session, Turn
        from ...engram.observe.test import FakeSource
        hs = ["T1:S1", "L1:S1", f"E{self.e[0]}"]
        rl = lambda h: pull.relive(self.conn, [h], **self.kw)
        self.assertIn("X", rl("T1:S1"))
        self.assertNotIn("没有这一条", rl(f"E{self.e[0]}"))

        def say(*commands):
            f = self.dir / "s1.jsonl"
            sess = Session("dsh", "s1", str(f), started="2026-10-17T08:00:00Z", cwd="/w")
            turns = [Turn("把 X 升级一下（s1）", "好", closed=True, started="2026-10-17T08:00:00Z"),
                     Turn("写下来（s1）", "写进 docs/x.md 了", closed=True, started="2026-10-17T08:00:00Z")]
            turns += [Turn(c, "", closed=True, started=f"2026-10-17T{9 + i:02d}:00:00Z") for i, c in enumerate(commands)]
            collect.run(self.conn, sources=("dsh",), force=True, log=io.StringIO(), modules={"dsh": FakeSource(f, sess, turns)})
            self.conn.commit()
        say("/no-observe")
        self.assertEqual(body(rl("T1:S1")), body(rl("T99:S1")).replace("T99", "T1"))
        self.assertEqual(body(rl("L1:S1")), body(rl("L1:S99")).replace("S99", "S1"))
        self.assertEqual(body(rl(f"E{self.e[0]}")), body(rl("E99999")).replace("E99999", f"E{self.e[0]}"))
        say("/no-observe", "/do-observe")
        self.assertIn("X", rl("T1:S1"))
        self.assertNotIn("没有这一条", rl(f"E{self.e[0]}"))

    def test_search_big_words(self):
        """v0.12：几乎处处都有的词（这个工作区里 UBIQUITOUS 以上的会话都出现过）不拿去 BM25 查；全是这种词又没碰上，就说没有命中。"""
        text = pull.evoke(self.conn, [f"{U}"], **self.kw)               # 测试库只有一段会话，“用户”在它的 E 里：大词
        self.assertIn(f"没有命中（查词都是几乎每段会话都有的词：{U}", text)
        self.assertEqual(json.loads(self._uses()[-1]["query"])["big"], [f"{U}"])
        self.assertIn("X 升级", pull.evoke(self.conn, [f"{U}", "甲服务"], **self.kw))   # 别的词照常查

    def test_self_marked(self):
        """v0.42：self 来的话题、陈述标“自语”（按节点的工作区认）；roam 一个 self 的节点往下不逐行再标；别处的不标。"""
        from unittest.mock import patch
        L = pull.layers(self.conn)
        ws = L.nodes["T1"]["ws"]
        self.assertNotIn("自语", pull.evoke(self.conn, ["X 升级"], **self.kw))
        with patch.object(pull, "SELF_WORKSPACE", ws):
            self.assertIn("T1 X 升级（2026-10-17，自语，2 条陈述；", pull.evoke(self.conn, ["X 升级"], **dict(self.kw, session="t8")))
            self.assertIn("[L1:T1] X 升级（2026-10-17，自语，2 条陈述）", pull.relive(self.conn, ["T1"], **self.kw))
            self.assertIn("[L1:T1:S1]（自语） ", pull.relive(self.conn, ["L1:T1:S1"], **self.kw))
            self.assertIn("- L1:T1 X 升级（2026-10-17，2 条陈述）", pull.relive(self.conn, ["L1"], **self.kw))

    def test_search_get_layers(self):
        text = pull.evoke(self.conn, ["X 升级"], **self.kw)
        self.assertIn("T1 X 升级", text)
        self.assertNotIn("- L1 X 升级（1 个话题", text)                   # v0.32：S、T 只给话题和陈述，线要 grain=L
        self.assertIn("- L1 X 升级（1 个话题", pull.evoke(self.conn, ["X 升级"], grain="L", **dict(self.kw, session="t9")))
        self.assertNotIn("往下", text)                                  # 只给主要内容，不带说明
        self.assertIn("〖〗", text)                                      # v0.31：结果末尾提醒用到就标半句，引用回填靠它连回
        u = self._uses()[-1]
        self.assertEqual((u["channel"], u["mode"]), ("pull", "evoke"))
        born = self.conn.execute("SELECT born FROM session_entry WHERE id=1").fetchone()[0]
        self.assertIn(born, json.loads(u["inputs"])["act"])            # 会话入口照 form 的写法记成写出它的 act
        self.assertIn(1, json.loads(u["meta"])["handles"]["session_entry"])
        # 一层：入口给停在和陈述，附上一级（线）的名字；线给会话入口
        s = pull.relive(self.conn, ["T1"], **self.kw)
        self.assertNotIn("上一级", s)                                  # relive 话题只给它的一组陈述，上一级的线先不给
        self.assertIn("[L1:T1] X 升级（2026-10-17，2 条陈述）", s)       # 话题带上所在的线和陈述条数（文件夹）
        self.assertIn(f"- L1:T1:S1 {U}想把「甲服务」升到新版本", s)       # 陈述带上线和话题（文件）
        self.assertIn("- L1:T1:S2 ", s)
        rid = "L1:T1:S1"
        self.assertIn("- L1:T1 X 升级（2026-10-17，2 条陈述）", pull.relive(self.conn, ["L1"], **self.kw))
        self.assertIn("[L1]", pull.relive(self.conn, ["l-1"], **self.kw))     # 旧写法 l-1 也认
        self.assertIn("[L1:T1]", pull.relive(self.conn, ["L1:T1"], **self.kw))
        both = pull.relive(self.conn, ["T1:S1-S9"], **self.kw)               # 线只是位置，不写也认
        self.assertIn("[L1:T1:S2]", both)
        self.assertIn("L1:T1:S3：没有这一条", both)
        self.assertIn("▶ L1:T1:S2", pull.relive(self.conn, ["L?:T1:S2"], near=True, **self.kw))
        self.assertIn("▶ L1:T1:S1", pull.relive(self.conn, ["L1:S1"], near=True, **self.kw))   # v0.10 按线编的旧句柄照认
        # 一条陈述默认只给它自己（v0.39）；near 给它在话题里的前后几条（v0.13）；source 给依据的 E，不再重复正文；said 一路到原话
        alone = pull.relive(self.conn, [rid], **self.kw)
        self.assertIn(f"[L1:T1:S1] {U}想把「甲服务」升到新版本", alone)
        self.assertNotIn("L1:T1:S2", alone)
        r = pull.relive(self.conn, [rid], near=True, **self.kw)
        self.assertIn(f"▶ L1:T1:S1 {U}想把「甲服务」升到新版本", r)
        self.assertIn("- L1:T1:S2 ", r)
        self.assertNotIn("上一级", r)
        self.assertNotIn(f"E{self.e[0]}", r)                           # 陈述只给正文；来源用 source 取
        src = pull.relive(self.conn, [rid], source=True, **self.kw)
        self.assertIn(f"E{self.e[0]}", src)
        self.assertTrue(body(src).startswith("[L1:T1:S1]\n来源："))
        self.assertNotIn("升到新版本，这次对话由此开始", src)            # 正文不重复
        self.assertIn("把 X 升级一下（s1）", pull.relive(self.conn, [f"E{self.e[0]}"], said=True, **self.kw))
        self.assertIn("写进 docs/x.md 了", pull.relive(self.conn, [f"E{self.e[1]}"], said=True, **self.kw))   # 2:A1：轮号是会话里的第几轮
        # 名字逐字唯一才取；同名的列出来不猜（入口和线都叫“X 升级”）
        self.assertIn("有几个同名的", pull.relive(self.conn, ["X 升级"], **self.kw))

    def test_numbers_append_only(self):
        """续上的会话：写出新的话题 T2，陈述按会话接着往后编，原来的序号不变；旧话题 T1 的句柄照样取得到。"""
        c = self._more("s1", "2026-10-17T08:00:00Z")
        pack2 = "名字：X 升级\n主题：加回退\n### 陈述1\n{U}要加「回退」。\n来源：E{c}\n".replace("{U}", U).format(c=c[0])
        label2 = "名字：X 升级\n停在：回退也加了（陈述1）\n索引词：\n- 回退｜rollback\n"
        self._run(self._fakes([pack2], [label2], []))
        pull._CACHE.clear()
        text = pull.relive(self.conn, ["T2:S1-S3"], **self.kw)
        self.assertIn(f"[L1:T2:S1] {U}想把「甲服务」升到新版本", text)
        self.assertIn(f"[L1:T2:S3] {U}要加「回退」。", text)
        self.assertEqual(body(pull.relive(self.conn, ["L1:T1:S1"], **self.kw)), body(pull.relive(self.conn, ["T2:S1"], **self.kw)))
        self.assertIn("▶ L1:T2:S3", pull.relive(self.conn, ["L1:S3"], near=True, **self.kw))           # 按线编的旧句柄
        self.assertIn(f"- L1:T2:S3 {U}要加「回退」。", pull.relive(self.conn, ["T2"], **self.kw))
        old = pull.relive(self.conn, ["T1"], **self.kw)
        self.assertTrue(body(old).startswith("T1：这段会话以前的话题，现在是 L1:T2\n[L1:T2]"))
        # 陈述的历史：写出的时间、最早收在哪个话题、现在在哪；不重复正文
        h = pull.relive(self.conn, ["L1:T2:S3"], recall="all", **self.kw)
        self.assertEqual(body(h), "[L1:T2:S3]\n历史：\n- 写于 2026-10-17，最早收在话题 T2\n- 现在在 L1:T2 里")
        self.assertIn("最早收在话题 T1", pull.relive(self.conn, ["T2:S1"], recall="all", **self.kw))

    def test_number_by_unit(self):
        """会话按 pack 窗口里的 unit 认：没写 session 的 pack 照样编号，序号不变。"""
        before = pull.relive(self.conn, ["T1:S1-S2"], **self.kw)
        self.conn.execute("DROP TRIGGER act_detail_no_update")          # 临时库：模拟以后不写 session 的 pack
        self.conn.execute("UPDATE act_detail SET window=json_remove(window, '$.session') WHERE process='form' AND step='pack'")
        self.conn.commit()
        pull._CACHE.clear()
        self.assertEqual(body(pull.relive(self.conn, ["T1:S1-S2"], **self.kw)), body(before))
        self.assertEqual(len(pull.layers(self.conn).sseq), 2)               # 是按会话编上的号，不是退回到话题里的第几条
        self.assertIn("[L1:T1:S2]", before)

    def test_topic_pages(self):
        """v0.13：话题按顺序读，一次 SHOW 条，后面还有的写出接着读的范围；一条陈述给它前后 CONTEXT 条，要的那条标 ▶。"""
        self.assertEqual(pull._span(["L2:T5:S9", "L2:T5:S10", "L2:T5:S11"]), "L2:T5:S9-S11")
        self.assertEqual(pull._span(["L2:T5:S9", "L3:T6:S1"]), "L2:T5:S9")
        L = pull.layers(self.conn)
        n = L.nodes["T1"]
        base = n["items"]
        n["items"] = [dict(base[i % 2], id=1000 + i, text=f"第{i}条") for i in range(12)]
        for i, it in enumerate(n["items"]):
            L.stmts[it["id"]] = {"text": it["text"], "entry": "T1", "h": f"L1:T1:S{i + 1}", "id": it["id"]}
        text, got = pull.show_node(L, "T1", conn=self.conn)
        self.assertEqual(text.count("\n- "), pull.SHOW)
        self.assertTrue(text.split("\n")[1].startswith("- L1:T1:S1 第0条"))      # 按原来的顺序，从头读
        self.assertTrue(text.endswith("接着读：L1:T1:S9-S12"))
        self.assertEqual(len(got["form_record"]), pull.SHOW)
        ctx, got = pull.show_context(L, 1005)
        self.assertEqual([l[:1] for l in ctx.split("\n")[1:]], ["-", "-", "-", "▶", "-", "-", "-"])
        self.assertIn("▶ L1:T1:S6 第5条", ctx)
        self.assertTrue(ctx.startswith("[L1:T1] X 升级"))
        edge, _ = pull.show_context(L, 1000)
        self.assertEqual(edge.count("\n"), 1 + pull.CONTEXT)                  # 第一条：前面没有，后面 3 条

    def test_legacy_j_and_overlap(self):
        jid, rec = self._legacy_j()
        g = pull.relive(self.conn, [f"K{jid}"], **self.kw)              # 旧 K 编号原样就是 J
        self.assertIn(f"[J{jid}]", g)
        self.assertIn(pull.RETIRED, g)                                        # 旧 J 退役（2026-10-23）：还能打开，标明已退役
        src = pull.relive(self.conn, [f"K{jid}"], source=True, **self.kw)
        self.assertIn("同一段会话现在的话题 T1 X 升级", src)
        self.assertIn(f"旧的陈述：{U}说先备份。", src)
        self.assertNotIn(f"J{jid}", pull.evoke(self.conn, ["备份"], **self.kw))     # evoke 不再给旧 J
        found = pull.evoke(self.conn, ["甲服务", "备份", "X 升级"], **self.kw)
        self.assertLessEqual(found.count("\n- ") + 1, pull.SHOW)
        self.assertNotIn("联想：", found)
        self.assertIn("v6 以前的 K 编号留在旧库", pull.relive(self.conn, ["K12345"], **self.kw))
        self.assertIn("旧地图的节点编号", pull.relive(self.conn, ["D4G2"], **self.kw))

    def test_map(self):
        self.assertIn("- w（当前）：1 条话题线，1 个话题", pull.roam(self.conn, **self.kw))
        self.assertIn("L1 X 升级", pull.roam(self.conn, "w", **self.kw))
        self.assertEqual(self._uses()[-1]["mode"], "roam")

    def test_paging(self):
        """v0.14：relive、roam 条数固定、只翻页；截断写出下一页（evoke 的一半一半 v0.32 去掉：陈述和话题按同时含几个词排在一起）。"""
        self.assertEqual(pull.paging(0), (pull.SHOW, 1))
        self.assertEqual(pull.paging("x"), (pull.SHOW, 1))
        self.assertEqual(pull.paging(3), (pull.SHOW, 3))
        tok = pull._WIN.set((2, 2))
        try:
            self.assertEqual(pull._cap(list("abcde"), 5), ["c", "d", "（还有 1 条：下一页 page=3）"])
        finally:
            pull._WIN.reset(tok)

    def test_grain_and_window_in_use(self):
        """grain=T 不给陈述，记进足迹；evoke、relive 都不认 limit（v0.17、v0.24），relive 只翻页。"""
        text = pull.evoke(self.conn, ["X 升级"], grain="T", limit=3, **self.kw)
        self.assertNotIn(":S", text)
        q = json.loads(self._uses()[-1]["query"])
        self.assertEqual(q["grain"], "T")
        self.assertNotIn("limit", q)
        self.assertIn("第 9 页没有了", pull.relive(self.conn, ["T1"], page=9, **self.kw))
        pull.relive(self.conn, ["T1"], limit=30, **self.kw)
        self.assertNotIn("limit", self._uses()[-1]["query"])

    def test_source_order(self):
        """陈述的来源按 form 写下的顺序给，和足迹记下的那一页一致（10-27：原来按编号排）。"""
        sid = next(iter(pull.layers(self.conn).stmts))
        evs = json.loads(self.conn.execute("SELECT refs FROM form_record WHERE id=?", (sid,)).fetchone()[0])["event"]
        rows = self.conn.execute("SELECT id FROM event WHERE id IN (%s)" % ",".join("?" * len(evs)), evs).fetchall()
        self.assertTrue(rows)
        text, got = pull.show_stmt(self.conn, pull.layers(self.conn), sid, "S1", source=True)
        shown = [int(x) for x in __import__("re").findall(r"^- E(\d+)", text, __import__("re").M)]
        self.assertEqual(shown, [e for e in evs if e in {r[0] for r in rows}][:len(shown)])
        self.assertEqual(shown, got["event"])

    def test_source_by_clause(self):
        """来源按分句排（2026-11-05）：每条 E 挂到最像的分句，按分句先后给；向量一路用不了照旧（返回 None）。"""
        from unittest import mock
        rs = [{"id": 1, "text": "a"}, {"id": 2, "text": "b"}, {"id": 3, "text": "c"}]
        with mock.patch.object(pull.vector, "align", return_value=[2, 0, 0]):
            got = pull._by_clause(self.conn, "甲。乙；丙。", rs)
        self.assertEqual([(c, [r["id"] for r in m]) for c, m in got], [("甲。", [2, 3]), ("丙。", [1])])
        with mock.patch.object(pull.vector, "align", return_value=None):
            self.assertIsNone(pull._by_clause(self.conn, "甲。乙。", rs))
        self.assertIsNone(pull._by_clause(self.conn, "甲。乙。", rs[:1]))

    def test_topic_sections(self):
        """v0.19：话题超窗时先给目录，按 observe 预览的分组归节、节名用“类型”，同一轮以最近一次预览为准；接着读照旧；said 在陈述上只给位置和开头。"""
        L = pull.layers(self.conn)
        nid = next(k for k, v in L.nodes.items() if v["kind"] == "entry")
        items = L.nodes[nid]["items"]
        tok = pull._WIN.set((1, 1))                                   # 窗口压成 1 条，让测试话题超窗
        try:
            self.assertIn("第 1–2 轮 讨论：", pull.show_node(L, nid, None, self.conn)[0])   # 测试数据自带一次预览
            self.assertNotIn("先给目录", pull.show_node(L, nid, None, self.conn, start=1)[0])   # 接着读照旧按顺序
            turn = self.conn.execute("SELECT turn FROM event WHERE id=?", (json.loads(self.conn.execute(
                "SELECT refs FROM form_record WHERE id=?", (items[0]["id"],)).fetchone()[0])["event"][0],)).fetchone()[0]
            op = st.operator(self.conn, "test/preview", "observe", "previewer")
            occ = st.occurrence(self.conn, "act", op, started="2026-10-17T09:00:00Z", scale="session", outcome="ok")
            meta = {"reads": [{"from": 1, "to": 2, "type": "升级方案确认。", "turns": [turn]}]}
            self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (occ, "observe", "preview", "{}", "", "", json.dumps(meta)))
            self.conn.commit()
            text = pull.show_node(L, nid, None, self.conn)[0]
            self.assertIn("先给这段对话的结构", text)                  # 有 form 写的结构：结构在前（v0.38）
            self.assertIn("J1 判断（我；S1）先迁配置再升", text)
            self.assertIn("第 1–2 轮 升级方案确认：", text)             # 目录跟在后面
            from unittest import mock
            with mock.patch.object(pull.structure, "latest", return_value=None):
                self.assertIn("先给目录", pull.show_node(L, nid, None, self.conn)[0])   # 没有结构（或对不上现在的主列）照旧给目录
        finally:
            pull._WIN.reset(tok)
        t, _ = pull.show_stmt(self.conn, L, items[0]["id"], "S1", said=True)
        self.assertNotIn("…" * 2, t)
        for line in t.splitlines():
            if line.strip().startswith("[") and "]" in line and not line.startswith("[L"):
                self.assertLessEqual(len(line.split("] ", 1)[-1]), pull.SAID_HEAD + 1)

    def test_source_sections(self):
        """v0.20：来源一页以内不分节、不提示；超出时同一轮再执行给下一节，分了节才提示，给完说一共几节；新的一轮从头。"""
        L = pull.layers(self.conn)
        from unittest import mock
        evs = [r[0] for r in self.conn.execute("SELECT id FROM event ORDER BY id DESC")]      # 倒着挂：顺序要照写下的
        self.assertGreater(len(evs), 1)
        base = self.conn.execute("SELECT born FROM form_record WHERE id=?", (next(iter(L.stmts)),)).fetchone()[0]
        sid = self.conn.execute("INSERT INTO form_record(born, idx, kind, text, refs, labels, structures, unresolved) "
                                "VALUES(?,?,?,?,?,?,?,?)", (base, 99, "session", "挂了几条 E 的陈述", json.dumps({"event": evs}),
                                                            "[]", "{}", "[]")).lastrowid
        self.conn.commit()
        h = "L1:T1:S99"
        L.stmts[sid] = {"text": "挂了几条 E 的陈述", "entry": "T1", "h": h, "id": sid}
        patch = mock.patch.object(pull, "resolve", lambda L_, hh: ("stmts", [(hh, sid)]))
        patch.start()
        self.addCleanup(patch.stop)
        self.assertEqual(pull.source_sections(self.conn, evs), [(None, evs)])
        st_ = {"basis": [evs[0]], "quotes": [], "files": [{"path": "a.md", "to": evs}]}         # v0.21：按 form 写下的结构分
        secs = pull.source_sections(self.conn, evs, size=1, structures=st_)
        self.assertEqual(secs, [(None, [evs[0]])])                                          # 『文件』指向默认不给（v0.22），剩一页不分节
        st2 = {"basis": evs[:1], "quotes": [{"name": "q", "to": evs[1:]}], "files": []}
        self.assertEqual([n for n, _ in pull.source_sections(self.conn, evs, size=1, structures=st2)][:2], ["直接来源", "引用：q"])
        fs = pull.source_sections(self.conn, evs, size=1, structures=st_, files=True)
        self.assertTrue(all(n.startswith("文件：a.md") for n, _ in fs))
        self.assertEqual([e for _, ids in fs for e in ids], evs[1:])                        # 只给文件那几条，和来源分开
        one = body(pull.relive(self.conn, [h], source=True, **self.kw))
        self.assertNotIn("再执行一次", one)
        n = len(pull.source_sections(self.conn, evs, size=1))
        self.assertEqual(n, len(evs))
        shrink = mock.patch.object(pull, "SHOW", 1)                           # 窗口压成 1 条，一节一条
        shrink.start()
        self.addCleanup(shrink.stop)
        seen = []
        for i in range(n):
            t = body(pull.relive(self.conn, [h], source=True, **self.kw))
            self.assertIn(f"第 {i + 1} 节，共 {n} 节", t)
            self.assertEqual("再执行一次" in t, i + 1 < n)
            seen += __import__("re").findall(r"^- E(\d+)", t, __import__("re").M)
        self.assertEqual([int(x) for x in seen], evs)                        # 按写下的顺序，不重不漏
        self.assertIn(f"来源一共 {n} 节，这一轮都给过了", pull.relive(self.conn, [h], source=True, **self.kw))
        self.assertIn("第 1 节", pull.relive(self.conn, [h], source=True, **dict(self.kw, session="t3")))
        track.use(self.conn, pull.VERSION, "associate", {"associate": {"prompt": "下一句"}}, "", {}, session="t",
                  purpose="prefetch", channel="push")
        self.assertIn("第 1 节", pull.relive(self.conn, [h], source=True, **self.kw))

    def test_evoke_fit(self):
        """v0.17：一页至少 6 条，字数够的话加到 8 条，超过 EVOKE_CHARS 就停；结果不够 6 条的全给。"""
        items = list(range(20))
        self.assertEqual(pull._fit(items, lambda x: "x" * 10), 8)
        self.assertEqual(pull._fit(items, lambda x: "x" * 300), 6)
        self.assertEqual(pull._fit(items, lambda x: "x" * 110), 7)
        self.assertEqual(pull._fit([1, 2, 3], lambda x: "x"), 3)

    def test_evoke_tail(self):
        """v0.31：只沾到一个词的不报个数——那是底数，不是还能读的结果（dsh 看到一千多条就丢下不用）；v0.32：几个词时按同时含几个词报。"""
        self.assertEqual(pull._tail(0, [], [1, 1, 1], 1, "话题"), "（还有 3 个话题：再 evoke 就接着往下）")
        self.assertEqual(pull._tail(0, [], [1] * pull.MORE_CAP, 1, "话题"), "（还有 16 个话题：再 evoke 就接着往下）")
        self.assertEqual(pull._tail(0, [], [1] * 1855, 1, "话题"), "（再 evoke 就是接着往下）")
        self.assertEqual(pull._tail(0, [], [3, 2, 2] + [1] * 900, 4, "话题"),
                         "（往下：含 3 个词的 1 个、含 2 个词的 2 个，其余只沾到一个词或意思相近；再 evoke 就接着往下）")
        self.assertNotIn("900", pull._tail(0, [], [1] * 900, 4, "话题"))
        self.assertEqual(pull._tail(7, ["L1", "L2"], [], 2, "话题"), "（这一轮前面给过的 7 条不再列，其中这次也排在前面的：L1、L2）")
        self.assertEqual(pull._tail(0, [], [], 2, "话题"), "")

    def test_evoke_terms(self):
        """v0.32：查词按空白切成词，每个词单独认；英文按整词（at 不算 format 里的），汉字按去掉空白的子串。"""
        from . import retrieve
        self.assertEqual(retrieve.terms(["vps 代理", "VPS", "mihomo", " 代理 "]), ["vps", "代理", "mihomo"])
        f = retrieve.finder("at")
        self.assertFalse(f(retrieve.plain("format 和 Status")))
        self.assertTrue(f(retrieve.plain("被 @ 时（at）回")))
        self.assertTrue(retrieve.finder("外接键盘")(retrieve.plain("接上 外接 键盘")))
        self.assertTrue(retrieve.finder("ds·")(retrieve.plain("例如「ds·Deepseek」")))

    def test_evoke_naming_shadow(self):
        """命名演进的影子（D5，2026-11-08）：词典所属的工作区里，查词用了非全名就记进足迹，不改给看的文本；别的工作区不记。"""
        from pathlib import Path
        from ...engram.form import glossary
        repo = str(Path(pull.__file__).resolve().parents[3])
        meta = lambda: json.loads(self.conn.execute("SELECT meta FROM use_detail ORDER BY occurrence DESC LIMIT 1").fetchone()[0])
        if glossary.home() != st.workspace(repo):
            self.skipTest("实例另配的词典")
        if not glossary.fuller(["pace", "甲服务"]):
            self.skipTest("词典里没有“pace → yorimichi”这一条（公开包里的词典要么没有、要么是公开版）")
        a = pull.evoke(self.conn, ["pace", "甲服务"], session="t", purpose="dev", cwd=repo)
        self.assertEqual(meta().get("naming"), [["pace", "yorimichi"]])
        b = pull.evoke(self.conn, ["pace", "甲服务"], session="t2", purpose="dev", cwd="/w")
        self.assertNotIn("naming", meta())
        self.assertEqual(a, b)

    def test_stmt_day(self):
        """v0.40：陈述的日期取它的 E 出自的轮次里最晚的那天；读视图里缓存。"""
        L = pull.layers(self.conn)
        for sid in list(L.stmts)[:3]:
            d = pull._stmt_day(L, sid)
            self.assertTrue(d == "日期未知" or (len(d) == 10 and d[4] == "-"), d)
            self.assertIs(pull._stmt_day(L, sid), d)

    def test_evoke_head(self):
        """v0.32：页头说同时含全部词的有几个；一个没有就说去掉哪个词有几个（查得太严时给个松开的方向）；没命中、太常见的也说。"""
        ts = ["QQ", "主动", "接话"]
        cover = {"T1": frozenset({0, 1}), "T2": frozenset({0, 1}), "T3": frozenset({0, 2}), "T4": frozenset({0})}
        self.assertEqual(pull._evoke_head(ts, cover, [], [], "话题"),
                         "（没有同时含全部 3 个词的话题：去掉「接话」有 2 个，去掉「主动」有 1 个）")
        cover["T5"] = frozenset({0, 1, 2})
        self.assertEqual(pull._evoke_head(ts, cover, [], [], "话题"), "（同时含全部 3 个词的话题 1 个）")
        self.assertEqual(pull._evoke_head(["QQ", "甲乙"], {"T1": frozenset({0})}, [], ["甲乙"], "话题"),
                         "（没有同时含全部 2 个词的话题：去掉「甲乙」有 1 个；字面没有命中：「甲乙」）")
        self.assertEqual(pull._evoke_head(["QQ"], {"T1": frozenset({0})}, [], [], "话题"), "")

    def test_evoke_vector(self):
        """v0.33：向量一路和字面按 RRF 合并，只有向量认得出的话题写“意思相近”；向量用不了（None）和以前一样；临时库不开向量。"""
        from . import vector
        L = pull.layers(self.conn)
        self.assertFalse(vector._main_db(self.conn))                    # 测试的临时库：不同步、不查
        self.assertIsNone(vector.topics(L, "X 升级"))
        self.assertEqual(pull._rrf(["a", "b", "c"], ["c", "d"]), ["c", "a", "b", "d"])
        plain = body(pull.evoke(self.conn, ["甲服务"], **dict(self.kw, session="v1")))
        real = vector.topics
        try:
            vector.topics = lambda L, text, kinds=("entry",): ["T1"]
            self.assertEqual(body(pull.evoke(self.conn, ["甲服务"], **dict(self.kw, session="v2"))), plain)   # 字面也认得出：和以前一样
            vector.topics = lambda L, text, kinds=("entry",): ["T1"]
            got = body(pull.evoke(self.conn, ["完全不相干的说法"], **dict(self.kw, session="v3")))
            self.assertIn("L1:T1 X 升级", got)
            self.assertIn("意思相近", got)
        finally:
            vector.topics = real

    def test_evoke_cited(self):
        """v0.32：每行标被〖〗引用过几次（只看引用回填，evoke 给看到不作数）；被新版本取代的回填不算。"""
        L = pull.layers(self.conn)
        sid = L.by_h["L1:T1:S1"]
        kw = dict(session="t", purpose="dev", channel="track")
        old = track.use(self.conn, pull.VERSION, "cite", {}, "", {"turn": [1], "form_record": [sid]}, **kw)
        track.use(self.conn, pull.VERSION, "cite", {}, "", {"turn": [1], "form_record": [sid], "use": [old]}, **kw)
        track.use(self.conn, pull.VERSION, "cite", {}, "", {"turn": [2], "form_record": [sid]}, **kw)
        self.assertEqual(pull._cited(self.conn, L)[sid], 2)
        self.assertEqual(pull._cited(self.conn, L)["T1"], 2)
        self.assertIn("引用过 2 次", pull.evoke(self.conn, ["甲服务"], **dict(self.kw, session="t8")))

    def test_evoke_round(self):
        """v0.17：同一轮前面 evoke 给过的不再列，再查同样的词就是接着往下；联想推送（新的一轮）以后重新给；别的会话不受影响。"""
        first = body(pull.evoke(self.conn, ["X 升级"], **self.kw))
        self.assertIn("X 升级", first)
        again = body(pull.evoke(self.conn, ["X 升级"], **self.kw))
        self.assertTrue(again.startswith("这一轮前面已经都给过了"), again)
        self.assertIn("排在前面的：L1", again)                               # 这次也相关的附句柄
        self.assertEqual(json.loads(self._uses()[-1]["query"])["skipped"], len(re.findall(r"^- ", first, re.M)))
        self.assertIn("X 升级", pull.evoke(self.conn, ["X 升级"], **dict(self.kw, session="t2")))
        track.use(self.conn, pull.VERSION, "associate", {"associate": {"prompt": "下一句"}}, "", {}, session="t",
                  purpose="prefetch", channel="push")
        self.assertEqual(body(pull.evoke(self.conn, ["X 升级"], **self.kw)), first)
        self.assertEqual(pull.seen_this_round(self.conn, ""), set())

    def test_map_skips_skeleton_root(self):
        """有骨架的工作区，roam 工作区直接给骨架的根下面那层，根不占一层（2026-10-22）。"""
        L = pull.layers(self.conn)
        L.nodes["k-root"] = {"kind": "node", "name": "W", "parent": "", "ws": "w"}
        L.nodes["L1"]["parent"] = "k-root"
        L.kids["k-root"].append("L1")
        text = pull.roam(self.conn, "w", **self.kw)
        self.assertIn("- L1 X 升级", text)
        self.assertNotIn("k-root", text)

    def test_cache_follows_library(self):
        pull.relive(self.conn, ["s-1"], **self.kw)
        jid, _ = self._legacy_j()                                    # 库里多了 act：读视图重建
        self.assertIn(f"[J{jid}]", pull.relive(self.conn, [f"J{jid}"], **self.kw))

    def test_push(self):
        out = push.hook(self.conn, {"session_id": "t", "cwd": "/w", "host": "dsh"})
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("# 手记\n\n## 扉页\n", text)                     # 2026-10-25：core 是扉页，标题降一级，工具到第四级
        self.assertIn("\n#### 工具\n", text)
        self.assertIn("roam w", text)                                # 常用入口 2026-10-24 并进“最近停下的几件事”，这里只留 roam 的提示
        self.assertNotIn("- L1 X 升级", text)
        self.assertTrue(text.startswith('<memory-harness>\n') and text.endswith("</memory-harness>"))      # 送达编号不进标签（2026-11-02）
        self.assertIn("## 环境\n", text)
        u = self._uses()[-1]
        self.assertEqual((u["channel"], u["mode"]), ("push", "assist"))
        real = push.render                                           # 近况里写出的话题（10-27）：线和话题都记成送达，工具节的示例不算
        push.render = lambda cwd, *a, **k: push.entry(None, cwd) + "\n- 甲服务（L1:T1）；还没整理（L?:T7）"
        try:
            push.hook(self.conn, {"session_id": "t", "cwd": "/w", "host": "dsh"})
        finally:
            push.render = real
        self.assertEqual(json.loads(self._uses()[-1]["meta"])["handles"], {"topic_line": [1], "session_entry": [1, 7]})

    def test_session_after_clear(self):
        """/clear 以后宿主换了会话、MCP 不重启（2026-10-27）：钩子按 claude 进程记下当前会话，MCP 按父进程读。"""
        import os
        from unittest import mock
        from ..hosts import claude
        from . import mcp
        me = os.getpid()
        with mock.patch.object(claude, "host_pid", lambda start=None: me):
            push.hook(self.conn, {"session_id": "new", "cwd": "/w"})              # SessionStart（clear）带着新会话
            push.hook(self.conn, {"session_id": "dsh-1", "cwd": "/w", "host": "dsh"})   # 别的宿主不记
        self.assertEqual(claude.current(me), "new")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "old"}), mock.patch.object(os, "getppid", lambda: me):
            self.assertEqual(mcp._session({}), "new")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "old"}), mock.patch.object(os, "getppid", lambda: 999999999):
            self.assertEqual(mcp._session({}), "old")                           # 没记过：照旧用环境变量
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            self.assertEqual(mcp._session({"session": "s"}), "s")              # 不是 Claude Code：经参数给
        claude.bind("x", pid=999999999)                                         # 进程已经不在的记录，下次写时删掉
        claude.bind("new", pid=me)
        self.assertFalse((claude.RUN / "999999999").exists())

    def test_check_clean(self):
        from ...engram import check
        pull.evoke(self.conn, ["X 升级"], **self.kw)
        pull.relive(self.conn, ["T1", "L1", "L1:S1-S2", "T1:S1", "L1:T1:S2"], recall="all", **self.kw)
        pull.roam(self.conn, "w", **self.kw)
        push.hook(self.conn, {"session_id": "t", "cwd": "/w"})
        report = check.check(self.conn, self.dir / "engram.db")
        self.assertNotIn("发生的输入指向", str(report))

    def test_associate(self):
        from unittest.mock import patch
        from ..push import pos, yorimichi
        from ..push import associate
        real = pos.tag
        pos.tag = lambda texts, timeout=5: [[("甲服务", "nz", 2, "SBV"), ("当时", "nt", 3, "ADV"), ("怎么", "r", 3, "ADV"),
                                             ("升级", "v", 0, "HED"), ("来着", "u", 4, "RAD")]]
        try:
            with patch.object(yorimichi, "state", return_value={"width": 0}):     # 只记录的样子；送达按宽度另有测试
                got = associate.associate(self.conn, "甲服务当时怎么升级来着", session="t", cwd="/w")
        finally:
            pos.tag = real
        self.assertTrue(got[1].startswith("[Souhatsu] 只记录，未推送 · Yorimichi 0\n1> "))      # 只记录：给用户看的是带序号的话题，没有附加上下文
        self.assertIn("（L1:T1:S1）", got[1])                                  # 片段带陈述句柄（10-30）
        self.assertIsNone(got[0])
        u = self._uses()[-1]
        self.assertEqual((u["channel"], u["mode"], u["purpose"]), ("push", "associate", "prefetch"))
        q = json.loads(u["query"])["associate"]
        self.assertTrue(q["shadow"])
        self.assertIn("L1:T1:S1", q["candidates"])
        self.assertIn("X 升级", u["shown"])                              # 分组的主题名是话题线的名字

    def test_associate_skips_host_memory_agent(self):
        """codex 的自动记忆任务是宿主起的，不当用户的话：不联想、不算查不到的词，只记宿主事件（10-26 埋点盘点）。"""
        from ..push import associate
        got = associate.associate(self.conn, "## Memory Writing Agent: Phase 2 (Consolidation)\n\nYou are a Memory Writing Agent.",
                                  session="t", cwd="/home/u/.codex/memories", host="codex")
        self.assertIsNone(got)
        q = json.loads(self._uses()[-1]["query"])["associate"]
        self.assertEqual((q["skip"], q["host"]["kind"]), ("宿主注入", "宿主自动记忆任务"))
        self.assertNotIn("missing", q)

    def test_associate_skips_orca_restart(self):
        """Orca 重启后的固定续接提示（2026-11-06）宿主记成用户消息，不是用户在说话：不联想，只记宿主事件。"""
        from ..push import associate
        got = associate.associate(self.conn, "Orca restarted, which stopped what you were doing: a reply in progress. Then carry on.",
                                  session="t", cwd="/w", host="codex")
        self.assertIsNone(got)
        q = json.loads(self._uses()[-1]["query"])["associate"]
        self.assertEqual((q["skip"], q["host"]["kind"]), ("宿主注入", "Orca 重启续接"))

    def test_associate_follows_yorimichi(self):
        """yorimichi：0 不送达；1–3 送达，按条数截。"""
        from unittest.mock import patch
        from ..push import associate, pos, yorimichi
        tag = lambda texts, timeout=5: [[("甲服务", "nz", 2, "SBV"), ("当时", "nt", 3, "ADV"), ("怎么", "r", 3, "ADV"),
                                         ("升级", "v", 0, "HED"), ("来着", "u", 4, "RAD")]]
        got = {}
        with patch.object(pos, "tag", tag), patch.object(associate, "DELIVER", {"专名", "强回想", "组合"}):
            for width, session in ((0, "p1"), (1, "p2")):
                with patch.object(yorimichi, "state", return_value={"width": width}):
                    got[width] = associate.associate(self.conn, "甲服务当时怎么升级来着", session=session, cwd="/w")
        self.assertIsNone(got[0][0])
        self.assertTrue(got[0][1].startswith("[Souhatsu] 只记录，未推送 · Yorimichi 0"))
        self.assertTrue(got[1][0].startswith("<memory-harness>\n[Souhatsu]\n- "))
        self.assertEqual(got[1][0].count("\n- "), 1)
        self.assertTrue(got[1][1].startswith("[Souhatsu] · Yorimichi 1\n1> "))
        self.assertNotIn("\n2> ", got[1][1])
        q = self.conn.execute("SELECT query FROM use_detail WHERE session='p2' ORDER BY occurrence DESC").fetchone()[0]
        self.assertEqual(json.loads(q)["associate"]["yorimichi"], 1)

    def test_head_marks_review(self):
        """话题的挂线还标着待审视（中间插入留下的，J6）：话题头下面提示；没有标记的头不变。"""
        class L:
            nodes = {"T1": {"name": "N", "date": "", "topic": "t", "items": [], "review": ""},
                     "T2": {"name": "N", "date": "", "topic": "t", "items": [], "review": "待审视：晚到的 E 插进了已整理的范围（E9）"}}
            where = staticmethod(lambda nid: nid)
        self.assertNotIn("待审视", pull._head(L, "T1"))
        head = pull._head(L, "T2")
        self.assertEqual(head.count("\n"), 1)
        self.assertIn("挂线待审视：晚到的 E", head)
        self.assertNotIn("feedback", head)                      # v0.36：feedback 工具撤了，不再指过去

    def test_status_kernel(self):
        """内核在这个仓库里时，近况列近几天的内核日志 warn；旧的、不到 warn 的不列。"""
        import json
        from datetime import datetime, timezone
        from unittest import mock
        from ..push import status
        repo, log = self.dir / "repo-k", self.dir / "kernel-log.jsonl"
        (repo / "design").mkdir(parents=True)
        log.write_text("\n".join(json.dumps(r) for r in [
            {"time": "2026-10-20T00:00:00+00:00", "kind": "unlogged", "diff": "+a\n", "found_by": {"host": "claude", "session": "old"}},
            {"time": "2026-10-29T00:00:00+00:00", "kind": "grant", "host": "claude", "session": "s", "prompt": "改称呼"},
            {"time": "2026-10-29T01:00:00+00:00", "kind": "unlogged", "diff": "+b\n", "found_by": {"host": "codex", "session": "s9", "tool": "shell"}}]) + "\n")
        now = datetime(2026, 10, 30, tzinfo=timezone.utc)
        with mock.patch("mh.synapse.push.CORE", repo / "design" / "core.md"):
            got = status._kernel(repo, log, now)
            self.assertEqual(len(got), 1)
            self.assertRegex(got[0], r"^- #3 .* warn 程序 unlogged codex:s9 \+1-0 shell 之后发现$")
            self.assertEqual(status._kernel(self.dir / "别的仓库", log, now), [])

    def test_status(self):
        """项目现状：手写的部分（仓库、检查命令、目录说明）加上从 git 读的（活跃、不进 git、最近的提交、没提交的改动）和检查的结果；
        不推操作文档；手记推送只读编好的，按宿主去掉它自己给了的节，没有就不给这一节。"""
        import subprocess
        from unittest import mock
        from ..push import status
        repo, src = self.dir / "repo", self.dir / "ws-status"
        (repo / "design").mkdir(parents=True)
        src.mkdir()
        (repo / "design" / "runbook.md").write_text("# r\n\n## 现状（今天）\n\n| 步骤 | 状态 |\n|---|---|\n| 1 数据层 | 做完 |\n\n"
                                                     "下一步：做第 2 步。\n\n## 别的\n")
        (repo / ".gitignore").write_text("tmp/\n")
        (repo / "tmp" / "exp").mkdir(parents=True)
        for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "第一次提交"]):
            subprocess.run(["git", *cmd], cwd=repo, check=True)
        (repo / "design" / "new.md").write_text("x")
        (src / "w.md").write_text(f"# 项目现状：w\n\n- 仓库：{repo}\n- 检查：printf '检查了 3 份\\n\\n- 没有发现事件'\n\n## 目录\n\n"
                                  "- design/：设计文档\n- tmp/：实验\n")
        with mock.patch.object(status, "SOURCES", src), mock.patch.object(status, "_dirty_sources", return_value=[]):   # 不读正式库
            text = status.compile("w")
            self.assertIn("- 近 3 天有提交：design/（1）；不进 git：tmp/", text)    # 2026-10-24 目录压成一行，说明不推
            self.assertNotIn("设计文档", text)
            self.assertIn("第一次提交", text)
            self.assertIn("1 个文件（可能是别的会话做到一半）：design/new.md", text)
            self.assertNotIn("数据层", text)                                   # 操作文档不推
            self.assertNotIn("### 检查", text)                                 # 检查还没跑过
            self.assertNotIn("### 内核日志", text)                             # 内核不在这个仓库里：不看内核日志
            self.assertIsNone(status.compile("没写的工作区"))
            self.assertTrue(status.run_check("w", home=self.dir / "status"))
            self.assertTrue(status.write("w", home=self.dir / "status"))
        full = status.read("w", home=self.dir / "status", host="dsh")
        self.assertIn("## 近况（w，", full)
        self.assertIn("### 最近的提交", full)
        self.assertIn("跑）\n检查了 3 份\n- 没有发现事件", full)                 # 检查的结果去掉空行
        cc = status.read("w", home=self.dir / "status", host="claude")       # Claude Code 自带 git 状态和最近的提交
        self.assertNotIn("### 最近的提交", cc)
        self.assertNotIn("### 没提交的改动", cc)
        from ..push.recent import TOUCHED
        touched = [f"  - design/new.md：Claude Code「别的会话」（L1:T1）10-27 12:00 最后{TOUCHED}"]       # 宿主给不了的“是谁碰的”留下
        with mock.patch.object(status, "SOURCES", src), mock.patch.object(status, "_dirty_sources", return_value=touched):
            status.write("w", home=self.dir / "status2")
        kept = status.read("w", home=self.dir / "status2", host="claude")
        self.assertIn("### 没提交的改动", kept)
        self.assertIn(f"最后{TOUCHED}", kept)
        self.assertNotIn("### 最近的提交", kept)
        self.assertIn("### 目录", cc)
        self.assertIn("### 检查", cc)
        self.assertEqual(status.read("没编过的", home=self.dir / "status"), "")
        self.assertEqual(status._errors([]), "；近 6 小时没有出错")
        self.assertEqual(status._errors([("2026-10-20T09:00:00+09:00", "[预览] x 第 1–2 轮：env_error：坏了"),
                                         ("2026-10-20T09:05:00+09:00", "本轮出错：OperationalError('locked')")]),
                         "；近 6 小时出错 2 行，最后一次 09:05：本轮出错：OperationalError('locked')")

    def test_missing_words(self):
        """查不到的词：名词和英文标识符，陈述正文、节点的名字和索引词里都逐字找不到的；贴进来的代码行不看。"""
        from ..push import associate as A
        data = {"stmts": A._norm(f"{U}想把甲服务升到新版本；ds 的事"), "nodes": A._norm("X 升级 docs/x.md"), "common": {"时候"}}
        toks = [("甲服务", "n", 0, "SBV"), ("乙仓库", "nz", 0, "SBV"), ("升级", "v", 0, "HED"), (f"{U}", "nh", 0, "SBV"),
                ("openclaw", "ws", 0, "ATT"), ("ds问", "nz", 0, "SBV"), ("时候", "n", 0, "ADV"), ("bar", "ws", 0, "ATT")]
        # 只看名词；“ds问”拆开看（ds 查得到，“问”一个字不算）；常用词“时候”不列；贴进来的代码行（bar 那行）不看
        none = lambda words: {}                                            # 测试不碰这台机器的词库
        self.assertEqual(A.missing(data, toks, "甲服务 乙仓库 openclaw ds问 时候\nfoo = bar(1)", none), ["乙仓库", "openclaw"])
        ctx, shown = A.recall_note(["乙仓库", "openclaw"])
        self.assertEqual(ctx, "<memory-harness>「乙仓库」「openclaw」没印象，记忆里还没有，当新的看。</memory-harness>")
        self.assertEqual(shown, "memory-harness · 新词：乙仓库、openclaw")       # 用户那边只显示简短的一行
        # 日语不交给 LTP：假名多于汉字的行整行不看（“進化”也不列），中文行里带假名的词不列
        toks = [("進化", "nz", 0, "SBV"), ("するよ", "n", 0, "SBV"), ("さくら", "ws", 0, "SBV"), ("丙仓库", "nz", 0, "SBV")]
        self.assertEqual(A.missing(data, toks, "今日は進化するよ もっともっと\n丙仓库写作さくら也行", none), ["丙仓库"])
        # 通用词汇不列：普通名词在词库里的（近况），专名照列（周防）；切坏的（高|通滤波器、特事|特办）；手动名单
        lex = {"近况": 19195, "周防": 0, "高通": 16685, "特事特办": 5000}
        toks = [("近况", "n", 0, "SBV"), ("周防", "nh", 0, "SBV"), ("通滤波器", "n", 0, "SBV"), ("特事", "n", 0, "SBV"),
                ("主循环", "n", 0, "SBV"), ("静音词", "n", 0, "SBV")]
        lookup = lambda words: {w: lex[w] for w in words if w in lex}
        from unittest import mock
        with mock.patch.object(A, "QUIET", frozenset({"静音词"})):
            self.assertEqual(A.missing(data, toks, "近况 周防 高通滤波器 特事特办 主循环 静音词", lookup), ["周防", "主循环"])

    def test_mcp(self):
        names = [t["name"] for t in mcp.handle(self.conn, {"id": 1, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["evoke", "relive", "roam", "yorimichi"])      # v0.36 撤了 feedback，v0.41 撤了 observe-now
        r = mcp.handle(self.conn, {"id": 2, "method": "tools/call",
                                   "params": {"name": "relive", "arguments": {"handles": ["s-1"], "session": "t"}}})["result"]
        self.assertFalse(r["isError"])
        gone = mcp.handle(self.conn, {"id": 3, "method": "tools/call", "params": {"name": "feedback", "arguments":
               {"handle": "T1", "reason": "x", "session": "t"}}})["result"]
        self.assertTrue(gone["isError"])
        self.assertIn("[L1:T1]", r["content"][0]["text"])       # 旧写法 s-1 也认，写 L1:T1
        bad = mcp.handle(self.conn, {"id": 4, "method": "tools/call",
                                     "params": {"name": "relive", "arguments": {"handle": "L1"}}})["result"]
        self.assertTrue(bad["isError"])                            # 参数名写错：说清楚，不默默回空
        self.assertIn("没有参数 handle", bad["content"][0]["text"])
        self.assertIn("缺 handles", bad["content"][0]["text"])
        self.assertNotIn("Traceback", bad["content"][0]["text"])
        props = next(t for t in mcp.handle(self.conn, {"id": 5, "method": "tools/list"})["result"]["tools"]
                     if t["name"] == "evoke")["inputSchema"]["properties"]
        self.assertNotIn("grain", props)                            # v0.35：主会话看不到这个参数
        hidden = mcp.handle(self.conn, {"id": 6, "method": "tools/call",
                                        "params": {"name": "evoke", "arguments": {"words": ["X 升级"], "grain": "L", "session": "t"}}})["result"]
        self.assertTrue(hidden["isError"])                          # 真带了照常报参数不对
        self.assertIn("没有参数 grain", hidden["content"][0]["text"])


for _name in [n for n in dir(ft.FormTest) if n.startswith("test_")]:
    setattr(PullTest, _name, None)        # 不是可调用的，unittest 不收
