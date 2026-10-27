"""Synapse 换库（E1a）的测试：新库上的 search / get / map、旧 J 和会话入口的重叠、足迹（use）与旧足迹的补录、手记推送、MCP。
数据照 form 的测试造（假模型），旧系统导入的 J 手写一条。python3 -m unittest mh.synapse.pull.test
"""
import json

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

    def test_search_big_words(self):
        """v0.12：几乎处处都有的词（这个工作区里 UBIQUITOUS 以上的会话都出现过）不拿去 BM25 查；全是这种词又没碰上，就说没有命中。"""
        text = pull.evoke(self.conn, [f"{U}"], **self.kw)               # 测试库只有一段会话，“用户”在它的 E 里：大词
        self.assertIn(f"没有命中（查词都是几乎每段会话都有的词：{U}", text)
        self.assertEqual(json.loads(self._uses()[-1]["query"])["big"], [f"{U}"])
        self.assertIn("X 升级", pull.evoke(self.conn, [f"{U}", "甲服务"], **self.kw))   # 别的词照常查

    def test_search_get_layers(self):
        text = pull.evoke(self.conn, ["X 升级"], **self.kw)
        self.assertIn("T1 X 升级", text)
        self.assertIn("- L1 X 升级（1 个话题", text)
        self.assertNotIn("往下", text)                                  # 只给主要内容，不带说明
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
        self.assertIn("▶ L1:T1:S2", pull.relive(self.conn, ["L?:T1:S2"], **self.kw))
        self.assertIn("▶ L1:T1:S1", pull.relive(self.conn, ["L1:S1"], **self.kw))   # v0.10 按线编的旧句柄照认
        # 一条陈述给它在话题里的前后几条（v0.13）；source 给依据的 E，不再重复正文；said 一路到原话
        r = pull.relive(self.conn, [rid], **self.kw)
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
        self.assertIn("▶ L1:T2:S3", pull.relive(self.conn, ["L1:S3"], **self.kw))           # 按线编的旧句柄
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
        """v0.14：一页话题和线、陈述各占一半，不够的让给另一路；按页模拟不重不漏；limit 有上限；截断写出下一页（v0.16 旧 J 退役，不再分名额）。"""
        rem = {"node": list(range(10)), "stmt": list(range(100, 110))}
        self.assertEqual(pull._alloc(rem, 8), {"node": 4, "stmt": 4})
        self.assertEqual(pull._alloc({"node": [1], "stmt": list(range(20))}, 8), {"node": 1, "stmt": 7})
        seen = []
        for _ in range(5):
            take = pull._alloc(rem, 8)
            seen += rem["node"][:take["node"]] + rem["stmt"][:take["stmt"]]
            rem = {k: rem[k][take[k]:] for k in rem}
        self.assertEqual(sorted(seen), list(range(10)) + list(range(100, 110)))
        self.assertEqual(pull.paging(100, 0), (pull.LIMIT_MAX, 1))
        self.assertEqual(pull.paging("x", None), (pull.SHOW, 1))
        tok = pull._WIN.set((2, 2))
        try:
            self.assertEqual(pull._cap(list("abcde"), 5), ["c", "d", "（还有 1 条：下一页 page=3）"])
        finally:
            pull._WIN.reset(tok)

    def test_grain_and_window_in_use(self):
        """grain=T 不给陈述，记进足迹；evoke 不认 limit（v0.17）；relive 照旧翻页。"""
        text = pull.evoke(self.conn, ["X 升级"], grain="T", limit=3, **self.kw)
        self.assertNotIn(":S", text)
        q = json.loads(self._uses()[-1]["query"])
        self.assertEqual(q["grain"], "T")
        self.assertNotIn("limit", q)
        self.assertIn("第 9 页没有了", pull.relive(self.conn, ["T1"], page=9, **self.kw))

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
            self.assertIn("先给目录", text)
            self.assertIn("第 1–2 轮 升级方案确认：", text)
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
        seen = []
        for i in range(n):
            t = body(pull.relive(self.conn, [h], source=True, limit=1, **self.kw))
            self.assertIn(f"第 {i + 1} 节，共 {n} 节", t)
            self.assertEqual("再执行一次" in t, i + 1 < n)
            seen += __import__("re").findall(r"^- E(\d+)", t, __import__("re").M)
        self.assertEqual([int(x) for x in seen], evs)                        # 按写下的顺序，不重不漏
        self.assertIn(f"来源一共 {n} 节，这一轮都给过了", pull.relive(self.conn, [h], source=True, limit=1, **self.kw))
        self.assertIn("第 1 节", pull.relive(self.conn, [h], source=True, limit=1, **dict(self.kw, session="t3")))
        track.use(self.conn, pull.VERSION, "associate", {"associate": {"prompt": "下一句"}}, "", {}, session="t",
                  purpose="prefetch", channel="push")
        self.assertIn("第 1 节", pull.relive(self.conn, [h], source=True, limit=1, **self.kw))

    def test_evoke_fit(self):
        """v0.17：一页至少 6 条，字数够的话加到 8 条，超过 EVOKE_CHARS 就停；结果不够 6 条的全给。"""
        rem = {"node": list(range(10)), "stmt": list(range(100, 110))}
        self.assertEqual(sum(pull._fit(rem, lambda k, x: "x" * 10).values()), 8)
        self.assertEqual(sum(pull._fit(rem, lambda k, x: "x" * 300).values()), 6)
        self.assertEqual(sum(pull._fit(rem, lambda k, x: "x" * 110).values()), 7)
        self.assertEqual(pull._fit({"node": [1], "stmt": [2, 3]}, lambda k, x: "x"), {"node": 1, "stmt": 2})

    def test_evoke_round(self):
        """v0.17：同一轮前面 evoke 给过的不再列，再查同样的词就是接着往下；联想推送（新的一轮）以后重新给；别的会话不受影响。"""
        first = body(pull.evoke(self.conn, ["X 升级"], **self.kw))
        self.assertIn("X 升级", first)
        again = body(pull.evoke(self.conn, ["X 升级"], **self.kw))
        self.assertTrue(again.startswith("这一轮前面已经都给过了"), again)
        self.assertIn("排在前面的：L1", again)                               # 这次也相关的附句柄
        self.assertEqual(json.loads(self._uses()[-1]["query"])["skipped"], first.count("\n- ") + 1)
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
        self.assertTrue(text.startswith('<memory-harness use="') and text.endswith("</memory-harness>"))
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
        from ..push import pos
        from ..push import associate
        real = pos.tag
        pos.tag = lambda texts, timeout=5: [[("甲服务", "nz", 2, "SBV"), ("当时", "nt", 3, "ADV"), ("怎么", "r", 3, "ADV"),
                                             ("升级", "v", 0, "HED"), ("来着", "u", 4, "RAD")]]
        try:
            got = associate.associate(self.conn, "甲服务当时怎么升级来着", session="t", cwd="/w")
        finally:
            pos.tag = real
        self.assertIn("L1:T1:S1", got[1])                                 # 只记录：给用户看的一行里有，没有附加上下文
        self.assertIsNone(got[0])
        u = self._uses()[-1]
        self.assertEqual((u["channel"], u["mode"], u["purpose"]), ("push", "associate", "prefetch"))
        q = json.loads(u["query"])["associate"]
        self.assertTrue(q["shadow"])
        self.assertIn("L1:T1:S1", q["candidates"])
        self.assertIn("X 升级", u["shown"])                              # 分组的主题名是话题线的名字

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
        self.assertIn("feedback", head)

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
        self.assertEqual(status._errors(["2026-10-20T09:00:00+09:00 host python3[1]: [预览] x 第 1–2 轮：env_error：坏了",
                                         "2026-10-20T09:05:00+09:00 host python3[1]: 本轮出错：OperationalError('locked')"]),
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
        self.assertEqual(names, ["evoke", "relive", "roam", "feedback"])
        r = mcp.handle(self.conn, {"id": 2, "method": "tools/call",
                                   "params": {"name": "relive", "arguments": {"handles": ["s-1"], "session": "t"}}})["result"]
        self.assertFalse(r["isError"])
        uid = self.conn.execute('SELECT max(occurrence) FROM use_detail').fetchone()[0]
        feedback = mcp.handle(self.conn, {"id": 3, "method": "tools/call", "params": {"name": "feedback", "arguments":
                   {"scope": "presentation", "use": uid, "reason": "保留原说话人", "session": "t"}}})["result"]
        self.assertFalse(feedback["isError"])
        self.assertIn(f"use {uid}", feedback["content"][0]["text"])
        self.assertIn("[L1:T1]", r["content"][0]["text"])       # 旧写法 s-1 也认，写 L1:T1
        bad = mcp.handle(self.conn, {"id": 4, "method": "tools/call",
                                     "params": {"name": "relive", "arguments": {"handle": "L1"}}})["result"]
        self.assertTrue(bad["isError"])                            # 参数名写错：说清楚，不默默回空
        self.assertIn("没有参数 handle", bad["content"][0]["text"])
        self.assertIn("缺 handles", bad["content"][0]["text"])
        self.assertNotIn("Traceback", bad["content"][0]["text"])


for _name in [n for n in dir(ft.FormTest) if n.startswith("test_")]:
    setattr(PullTest, _name, None)        # 不是可调用的，unittest 不收
