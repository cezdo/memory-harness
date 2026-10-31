"""推送自己的送达去重；不调用模型、词性服务或正式库。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ...engram import store as st
from .. import track
from . import associate as a


class SentWordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'test.db'
        self.conn = st.connect(self.db)
        self.addCleanup(lambda: self.conn.close())
        for target, value in (
            ('mh.synapse.push.pos.tag', lambda texts: [[(w, 'nz', 0, 0) for w in texts[0].split()]]),
            ('mh.synapse.push.associate.cached', lambda conn: {'idx': SimpleNamespace(k={}), 'stmts': '', 'nodes': ''}),
            ('mh.synapse.push.associate.spans', lambda *args: ([], {}, {'level': None}, {})),
            ('mh.synapse.push.associate.combo', lambda *args: None),
            ('mh.synapse.push.associate.listed', lambda *args: {}),
            ('mh.synapse.push.associate.nudge', lambda *args: False),       # 提醒判 yorimichi 另测（YorimichiTest）
        ):
            self.enterContext(patch(target, value))

    def call(self, text, session='s'):
        return a.associate(self.conn, text, session=session)

    def record(self, rec, session='s'):
        track.use(self.conn, 'v0.12', 'associate', {'associate': rec}, '', {},
                  session=session, purpose='prefetch', channel='push')

    def test_missing_once_persists_and_only_new_words(self):
        self.assertIn('OpenClaw', self.call('OpenClaw')[0])
        self.conn.close()
        self.conn = st.connect(self.db)
        self.assertIsNone(self.call('openclaw'))
        got = self.call('OpenClaw thinkterm')
        self.assertIn('thinkterm', got[0])
        self.assertNotIn('OpenClaw', got[0])
        q = json.loads(self.conn.execute('SELECT query FROM use_detail ORDER BY occurrence DESC LIMIT 1').fetchone()[0])
        self.assertEqual(q['associate']['missing'], ['OpenClaw', 'thinkterm'])
        self.assertEqual(q['associate']['sent_words'], ['thinkterm'])
        self.assertIsNone(self.call('OpenClaw', session='other'))          # 说过“没印象”的词，别的会话也不再说（10-30）
        self.assertIn('thinkterm2', self.call('thinkterm2', session='other')[0])

    def test_legacy_missing_and_delivered_words(self):
        self.record({'shadow': True, 'missing': ['OpenClaw'], 'used': ['shadowword']})
        self.record({'shadow': False, 'used': ['thinkterm']})
        self.assertEqual(a._sent_words(self.conn, 's'), {'openclaw', 'thinkterm'})
        self.assertIsNone(self.call('OpenClaw thinkterm'))
        self.assertIn('shadowword', self.call('shadowword')[0])

    def test_new_shadow_does_not_consume_word(self):
        self.record({'shadow': True, 'missing': ['OpenClaw'], 'sent_words': []})
        self.assertIn('OpenClaw', self.call('OpenClaw')[0])

    def test_no_session_does_not_share_dedup(self):
        self.assertIsNotNone(self.call('OpenClaw', session=''))
        self.assertIsNotNone(self.call('OpenClaw', session=''))

    def test_recalled_word_also_obeys_sent_words(self):
        self.record({'shadow': False, 'sent_words': ['OpenClaw']})
        data = {'idx': SimpleNamespace(k={}, match=lambda text: {}), 'stmts': 'openclaw', 'nodes': ''}
        terms = [{'t': 'OpenClaw', 'ok': True, 'tier': '专名'}]
        with patch.object(a, 'cached', return_value=data), \
                patch.object(a, 'spans', return_value=(terms, {'OpenClaw': set()}, {'level': None}, {})), \
                patch.object(a, 'DELIVER', {'专名'}), \
                patch('mh.synapse.push.yorimichi.state', return_value={'width': 1}):     # 允许拐才送达
            self.assertIsNone(self.call('OpenClaw'))
        q = json.loads(self.conn.execute('SELECT query FROM use_detail ORDER BY occurrence DESC LIMIT 1').fetchone()[0])
        self.assertEqual(q['associate']['skip'], '同词已送达')
        self.assertEqual(q['associate']['sent_words'], [])


    def test_no_old_j(self):
        """旧 J 退役（2026-10-23）：联想只记一条足迹，不再另开旧 J 的影子。"""
        k = {('s', 1): {'h': 'L1:T1:S1', 'text': '陈述 Foo', 'anchor': '2026-10-20', 'workspace': 'w', 'holder': '', 'name': '甲话题'}}
        data = {'idx': SimpleNamespace(k=k, match=lambda text: {}), 'stmts': 'foo', 'nodes': ''}
        terms = [{'t': 'Foo', 'ok': True, 'tier': '专名'}]
        with patch.object(a, 'cached', return_value=data), \
                patch.object(a, 'spans', return_value=(terms, {'Foo': set(k)}, {'level': None}, {})), \
                patch.object(a, 'clues', return_value='线索'):
            self.call('Foo')
        rows = [json.loads(q)['associate'] for (q,) in self.conn.execute('SELECT query FROM use_detail')]
        self.assertEqual(len(rows), 1)
        self.assertFalse(any(q.get('old_j') for q in rows))



class HookOutputTest(unittest.TestCase):
    def test_message_on_its_own_line(self):
        """Claude Code 显示 systemMessage 时前面加“UserPromptSubmit says:”：另起一行；codex 不加（用户 10-30）。"""
        import io
        for host, head in (("claude", "\n[Souhatsu]"), ("codex", "[Souhatsu]")):
            out = io.StringIO()
            with patch("mh.synapse.library", lambda: None), patch.object(a, "associate", lambda *x: ("上下文", "[Souhatsu] 1> 话题")):
                a.hook(io.StringIO(json.dumps({"prompt": "随便聊聊", "session_id": "s", "host": host})), out)
            got = json.loads(out.getvalue())
            self.assertTrue(got["systemMessage"].startswith(head), host)
            self.assertEqual(got["hookSpecificOutput"]["additionalContext"], "上下文")


class RecentTest(unittest.TestCase):
    """最近停下的几件事：fork 去重、一行的写法、送手记时去掉当前会话。"""

    def item(self, session, first, last, title='?unknown', said='接着做'):
        from datetime import datetime
        human = [{'text': first, 'user': '第一句', 'started': '2026-10-24T01:00:00Z'},
                 {'text': first + 'x', 'user': said, 'started': '2026-10-24T02:00:00Z'}]
        return {'source': 'claude', 'session': session, 'title': title, 'human': human,
                'last': datetime.fromisoformat(last)}

    def test_forks_collapse_to_latest(self):
        from . import recent
        items = [self.item('b', 't1', '2026-10-24T03:00:00+00:00'), self.item('a', 't1', '2026-10-24T02:00:00+00:00'),
                 self.item('c', 't2', '2026-10-24T01:00:00+00:00')]
        self.assertEqual([x['session'] for x in recent.dedupe(items)], ['b', 'c'])

    def test_line_falls_back_to_first_words_and_cuts(self):
        from . import recent
        x = self.item('a', 't', '2026-10-24T03:00:00+00:00', said='长' * 80)
        got = recent.line(x, 'L44:T1', '提交说明')
        self.assertIn('第一句（2 轮，L44:T1）', got)
        self.assertIn('最后提交：提交说明；你最后说：' + '长' * recent.SAID + '……', got)

    def test_foreign_sessions_that_crossed(self):
        """别处开的会话：只收比本区第 n+1 条还近、碰过本区的；那一行标出在哪开的（2026-10-26）。"""
        from unittest import mock
        from . import recent
        own = [self.item(f'o{i}', f'o{i}', f'2026-10-24T0{5 - i}:00:00+00:00') for i in range(5)]
        far = [self.item('new', 'n', '2026-10-24T04:30:00+00:00'), self.item('idle', 'i', '2026-10-24T04:40:00+00:00'),
               self.item('old', 'x', '2026-10-24T01:30:00+00:00')]
        seen = []
        with mock.patch.object(recent, 'sessions', lambda c, ws, foreign=False: far if foreign else own), \
             mock.patch.object(recent, 'crossed', lambda x, ws: seen.append(x['session']) or x['session'] != 'idle'):
            got = [x['session'] for x in recent.candidates(None, 'mh', n=3)]
        self.assertEqual(got, ['o0', 'new', 'o1', 'o2', 'o3', 'o4'])
        self.assertNotIn('old', seen)                                            # 比第 n+1 条还旧的不读原始记录
        x = dict(self.item('new', 'n', '2026-10-24T04:30:00+00:00'), cwd='/nowhere/other', ws='mh')
        with mock.patch.object(recent, 'workspace', lambda p: 'other'):
            self.assertIn('Claude Code（在 other 开的） · ', recent.line(x, 'L7:T1'))

    def test_dirty_sources(self):
        """没提交的改动归给改动时刻之前最后碰过它的会话；碰过文件本身的优先于只碰过目录的；都没碰过的另列（2026-10-26）。"""
        import os, tempfile
        from pathlib import Path
        from unittest import mock
        from . import recent
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / "a.md").write_text("x"); (repo / "b.md").write_text("x"); (repo / "c.md").write_text("x")
            t = 1793000000
            for f in ("a.md", "b.md", "c.md"):
                os.utime(repo / f, (t, t))
            iso = lambda dt: __import__("mh.engram.observe.written_files", fromlist=["_t"])._t(dt * 1000)
            ses = [{"source": "claude", "session": "s-here", "file": "f1", "cwd": d, "title": "这里"},
                   {"source": "claude", "session": "s-other", "file": "f2", "cwd": "/x/other", "title": "那边"}]
            touch = {"f1": {d: iso(t - 10), str(repo / "b.md"): iso(t + 3600)},             # 只碰过目录；碰 b 在改动之后
                     "f2": {str(repo / "a.md"): iso(t - 100), str(repo / "b.md"): iso(t - 50)}}
            with mock.patch.object(recent, "active", lambda c, since: ses), mock.patch.object(recent, "handle", lambda c, x: "L1:T1"), \
                 mock.patch.object(recent, "workspace", lambda p: "mh" if p == d else "other"), \
                 mock.patch("mh.engram.observe.written_files.touches", lambda s, f, c=None: touch[f]):
                got = recent.dirty_sources(None, repo, ["a.md", "b.md", "c.md"], "mh")
        self.assertEqual(len(got), 2)
        self.assertTrue(got[0].startswith("- a.md、b.md：Claude Code（在 other 开的）「那边」（L1:T1）"))
        self.assertEqual(got[1], f"- c.md：近 {recent.DAYS} 天的会话都没{recent.TOUCHED}")   # 目录是仓库根本身：不算

    def test_host_given_section_keeps_sources(self):
        from . import status
        part = "### 没提交的改动\n- 2 个文件：a、b\n  - a：Claude Code「x」（L1:T1）10-26 15:18 最后碰过"
        self.assertEqual(status._given(part, ("没提交的改动",)), "### 没提交的改动\n- a：Claude Code「x」（L1:T1）10-26 15:18 最后碰过")
        self.assertIsNone(status._given("### 没提交的改动\n- 没有", ("没提交的改动",)))
        self.assertEqual(status._given("### 服务\n- 在跑", ("没提交的改动",)), "### 服务\n- 在跑")

    def test_drop_self_and_cut(self):
        from . import recent
        body = '\n'.join(f'- 10-24 0{i}:00 · Claude Code · 事{i}（1 轮，claude:s{i}，还没整理）\n  - 你最后说：好 <!-- claude:s{i} -->'
                         for i in range(4))
        text = '## 项目现状\n\n### 服务\n- 在跑\n\n' + recent.TITLE + '\n' + body
        got = recent.drop_self(text, 'claude', 's0')
        self.assertNotIn('事0', got)
        self.assertEqual(got.count('\n- 10-24'), recent.N)
        self.assertNotIn('<!--', got)
        self.assertIn('### 服务\n- 在跑', got)
        self.assertEqual(recent.drop_self('没有这一节', 'claude', 's0'), '没有这一节')


class BootTest(unittest.TestCase):
    """手记的几处程序生成：检查只推变了的、环境段。"""

    def test_check_only_changes(self):
        from datetime import datetime
        from .status import changed
        now = "检查了 10 份原文\n- 指向按要求删掉的（条数）：10\n- observe：待观测（还没预览）：28（例：x）\n- 记下的引用未命中（event，条数）：80"
        before = "检查了 9 份原文\n- 指向按要求删掉的（条数）：10\n- observe：待观测（还没预览）：3（例：y）\n- 记下的引用未命中（event，条数）：79"
        at = datetime(2026, 10, 24, 17, 4)
        self.assertEqual(changed(now, (at, before)), ["- 记下的引用未命中（event，条数）：80（上次：79）"])
        self.assertEqual(changed(before, (at, before)), ["- 和上一次（10-24 17:04）比没有变化"])
        self.assertEqual(len(changed(now, None)), 4)                    # 没有上一次：全推
        self.assertIn("（新出现）", changed(now + "\n- 新的一类（条数）：1", (at, now))[0])

    def test_environment(self):
        from datetime import datetime
        from . import environment
        got = environment("claude", "compact", "claude-opus-5-5", now=datetime(2026, 10, 24, 17, 40))
        self.assertEqual(got, "## 环境\n\n2026-10-24 17:40 Sat | Claude Code | claude-opus-5-5 | compact")
        self.assertEqual(environment("dsh", now=datetime(2026, 10, 24, 17, 40)), "## 环境\n\n2026-10-24 17:40 Sat | dsh")


class HopTest(unittest.TestCase):
    """往外一跳（影子）：不连库，用手搭的边。"""

    def setUp(self):
        k = lambda i, entry, line: {"h": f"L1:T{entry}:S{i}", "entry": entry, "line": line}
        keys = {("s", 1): k(1, 1, "L1"), ("s", 2): k(2, 1, "L1"), ("s", 3): k(3, 2, "L1"), ("s", 4): k(4, 3, "L1"),
                ("s", 5): k(5, 9, "L9")}
        self.data = {"idx": SimpleNamespace(k=keys),
                     "hop": {"src": {100: [("s", 1), ("s", 2), ("s", 5)]},
                             "line": {"L1": {1: [("s", 1), ("s", 2)], 2: [("s", 3)], 3: [("s", 4)]}}}}

    def test_source_first_then_line_and_never_literal_hits(self):
        got = a.hop(self.data, [("s", 1)], {("s", 4): 2, ("s", 3): 1}, set())
        self.assertEqual([(x["h"], x["edge"]) for x in got],
                         [("L1:T1:S2", "同源"), ("L1:T9:S5", "同源"), ("L1:T2:S3", "同线异题"), ("L1:T3:S4", "同线异题")])
        self.assertTrue(all(x["from"] == "L1:T1:S1" for x in got))
        self.assertNotIn("L1:T1:S1", [x["h"] for x in got])         # 字面命中的起点自己不列

    def test_skip_seen_and_no_seed_edges(self):
        got = a.hop(self.data, [("s", 1), ("s", 2)], {}, {("s", 5), ("s", 3)})
        self.assertEqual([x["h"] for x in got], ["L1:T3:S4"])      # 已出现过的不列；字面命中的两条也不列
        self.assertEqual(a.hop(self.data, [("s", 5)], {}, {("s", 1), ("s", 2)}), [])


class CluesTest(unittest.TestCase):
    """线索按话题归并：用户看的和模型看的是同一份。"""

    def setUp(self):
        mk = lambda t, i, name, text: {"h": f"L1:T{t}:S{i}", "name": name, "text": text, "anchor": "2026-10-2%d" % t}
        self.idx = SimpleNamespace(k={("s", 1): mk(1, 1, "甲话题", "配色方案定了"), ("s", 2): mk(1, 2, "甲话题", "配色再调"),
                                      ("s", 3): mk(2, 1, "乙话题", "另一个配色"), ("s", 4): mk(3, 1, "乙话题", "同名但是另一个话题")})

    def test_by_topic_orders_by_rank_and_keeps_same_name_apart(self):
        tops = a.by_topic(self.idx, [("s", 3), ("s", 1), ("s", 4), ("s", 2)])
        self.assertEqual({h: len(ks) for h, ks in tops.items()}, {"L1:T2": 1, "L1:T1": 2, "L1:T3": 1})
        self.assertEqual(list(tops), ["L1:T2", "L1:T1", "L1:T3"])
        with patch.object(a, "SHOW_MAX", 2):
            self.assertEqual(list(a.by_topic(self.idx, [("s", 3), ("s", 1), ("s", 4)])), ["L1:T2", "L1:T1"])

    def test_clues_and_shown_share_rows(self):
        rows = a.topic_rows(self.idx, [("s", 1), ("s", 2), ("s", 3)], ["配色"])
        self.assertEqual([(r["h"], r["date"], r["name"], r["n"]) for r in rows],
                         [("L1:T1", "10-21", "甲话题", 2), ("L1:T2", "10-22", "乙话题", 1)])
        model, user = a.clues(rows), a.shown_text(rows, False)
        self.assertTrue(model.startswith(f"<memory-harness>\n[Souhatsu]\n- L1:T1 · 10-21 · 甲话题 · 2：（{rows[0]['s']}）配色方案定了"))  # 和其他注入同一个标签，第一行说明是联想，命中只写数字
        self.assertTrue(model.endswith("\n</memory-harness>"))                               # 末尾不写用法提示
        self.assertNotIn("relive", model)
        self.assertEqual(model.count("\n- "), 2)
        self.assertRegex(rows[0]["s"], r"^L1:T1:S\d+$")                                    # 片段带陈述句柄，一次 relive 直达（10-30）
        self.assertTrue(user.startswith(f"[Souhatsu] 只记录，未推送\n1> 甲话题 · L1:T1 · 10-21 · 2\n   （{rows[0]['s']}）配色方案定了"))
        for r in rows:                                                                       # 同一份内容，两种排版
            self.assertTrue(all(x in model and x in user for x in (r["h"], r["s"], r["date"], r["name"], r["head"])))
        self.assertTrue(a.shown_text(rows, True).startswith("[Souhatsu]\n1> "))

    def test_shown_keys_follow_topics(self):
        self.assertEqual(a.shown_keys(self.idx, [("s", 1), ("s", 3), ("s", 2)]), [("s", 1), ("s", 3)])   # 每个话题开头那条，和显示一致
        with patch.object(a, "SHOW_MAX", 1):
            self.assertEqual(a.shown_keys(self.idx, [("s", 3), ("s", 1)]), [("s", 3)])

    def test_fit_by_count(self):
        """送达的片段（10-30）：按实际送出的条数分总字数；放得下给整条，放不下在 ±10% 里找句末截断，找不到截在上限。"""
        short = "整条放得下。"
        self.assertEqual(a._fit(short, [], 100), short)
        self.assertEqual(a._fit("甲" * 108, [], 100), "甲" * 108)                         # 超出不到 10%：整条
        text = "甲" * 95 + "。" + "乙" * 200
        self.assertEqual(a._fit(text, [], 100), "甲" * 95 + "。")                          # 句末在范围里：截在句末
        self.assertEqual(a._fit("甲" * 300, [], 100), "甲" * 100 + "……")                   # 没有句末：截在上限
        got = a._fit("乙" * 250 + "配色" + "丙" * 200, ["配色"], 100)                      # 命中词在后面放不下：从它前面一点起
        self.assertTrue(got.startswith("……") and "配色" in got)
        rows = a.topic_rows(self.idx, [("s", 1), ("s", 2), ("s", 3)], ["配色"], keep=1, budget={1: 300, 2: 400, 3: 500})
        self.assertEqual(len(rows), 1)

    def test_around_no_punctuation_before_ellipsis(self):
        text = "前面一段话，" + "甲" * 5 + "配色" + "，这里是后面比较长的一段内容，到这里还没有完，" + "乙" * 40
        got = a._around(text, ["配色"], before=3, width=14)
        self.assertTrue(got.startswith("……") and got.endswith("……"))
        self.assertNotRegex(got, r"[，。；、：,.;:]……$")


class SwitchTest(unittest.TestCase):
    """联想开关：整句命令、按会话最后一次说了算、拦法；关了以后联想不做、“查不到的词”照旧。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'switch.jsonl'

    def test_command_must_be_the_whole_message(self):
        from .switch import command
        self.assertEqual((command('/no-souhatsu'), command(' $do-souhatsu \n'), command('/do-souhatsu')), ('off', 'on', 'on'))
        self.assertIsNone(command('先 /no-souhatsu 一下'))
        self.assertIsNone(command('/no-souhatsu 现在'))
        self.assertIsNone(command(None))

    def test_last_one_wins_per_session(self):
        from .switch import record, state
        self.assertEqual(state('a', self.log), 'on')                   # 没有记录：开
        record('off', 'claude', 'a', '/no-souhatsu', self.log)
        record('off', 'claude', 'b', '/no-souhatsu', self.log)
        record('on', 'claude', 'b', '/do-souhatsu', self.log)
        self.assertEqual((state('a', self.log), state('b', self.log), state('c', self.log)), ('off', 'on', 'on'))
        self.assertEqual(state('', self.log), 'on')                    # 没有会话号：开

    def test_handle_blocks_by_host(self):
        import io
        from .switch import handle, state
        out, err = io.StringIO(), io.StringIO()
        d = {'prompt': '/no-souhatsu', 'session_id': 's1'}
        self.assertEqual(handle(d, 'claude', out, err, self.log), 0)
        got = json.loads(out.getvalue())
        self.assertEqual(got['decision'], 'block')
        self.assertIn('/do-souhatsu', got['reason'])
        self.assertEqual(state('s1', self.log), 'off')
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(handle({'prompt': '$do-souhatsu', 'session_id': 's1'}, 'codex', out, err, self.log), 2)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('联想', err.getvalue())
        self.assertEqual(state('s1', self.log), 'on')
        self.assertIsNone(handle({'prompt': '随便说句话', 'session_id': 's1'}, 'claude', out, err, self.log))

    def test_off_skips_souhatsu_but_keeps_missing_words(self):
        from . import switch
        conn = st.connect(Path(self.tmp.name) / 'test.db')
        self.addCleanup(conn.close)
        switch.record('off', 'claude', 's', '/no-souhatsu', self.log)
        for target, value in (
            ('mh.synapse.push.pos.tag', lambda texts: [[(w, 'nz', 0, 0) for w in texts[0].split()]]),
            ('mh.synapse.push.associate.cached', lambda c: {'idx': SimpleNamespace(k={}), 'stmts': '', 'nodes': ''}),
            ('mh.synapse.push.switch.LOG', self.log),
            ('mh.synapse.push.associate.spans', lambda *args: self.fail('关了不该再找词段')),
        ):
            self.enterContext(patch(target, value))
        got = a.associate(conn, 'OpenClaw', session='s')
        self.assertIn('OpenClaw', got[0])                              # 查不到的词照旧
        q = json.loads(conn.execute('SELECT query FROM use_detail ORDER BY occurrence DESC LIMIT 1').fetchone()[0])
        self.assertEqual(q['associate']['skip'], '已关闭')



class MissingNamesTest(unittest.TestCase):
    """查不到的词：夹着标点、空格的词拆开看；词典词头里的叫法不列（项目自己起的名字，词典里查得到）。"""

    def test_split_punctuation_and_skip_glossary(self):
        from ...engram.form import glossary
        data = {"stmts": "", "nodes": "", "common": set()}
        toks = [("Souhatsu] · Yorimichi", "nz", 0, "SBV"), ("新服务", "nz", 0, "SBV")]
        text = "[Souhatsu] · Yorimichi 2 新服务"
        entries = [{"full": "souhatsu", "names": ["souhatsu"], "head": ""}, {"full": "yorimichi", "names": ["yorimichi"], "head": ""}]
        with patch.object(glossary, "entries", return_value=[]):
            self.assertEqual(a.missing(data, toks, text, lambda w: {}), ["Souhatsu", "Yorimichi", "新服务"])   # 拆开，不带标点
        with patch.object(glossary, "entries", return_value=entries):
            self.assertEqual(a.missing(data, toks, text, lambda w: {}), ["新服务"])                          # 词典里有的不列（不分大小写）


class YorimichiTest(unittest.TestCase):
    """yorimichi：按会话最后一次说了算，默认 0；0 不送达，1–3 按条数截；给用户看的标题带上它。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = Path(self.tmp.name) / 'yorimichi.jsonl'

    def test_last_one_wins_default_zero(self):
        from . import yorimichi as y
        self.assertEqual(y.state('s', self.log)['width'], 0)                             # 没说过：深挖，宁可少给
        y.record('s', 2, '在闲聊', log=self.log)
        y.record('t', 0, log=self.log)
        self.assertEqual(y.state('s', self.log)['width'], 2)
        y.record('s', 0, log=self.log)
        self.assertEqual(y.state('s', self.log)['width'], 0)
        self.assertEqual(y.state('', self.log)['width'], 0)

    def test_bad_args(self):
        from . import yorimichi as y
        for args in (('s', 4), ('s', -1), ('s', 'open'), ('s', None), ('', 1)):
            with self.assertRaises(ValueError):
                y.record(*args, log=self.log)
        with patch.object(a, 'DELIVER', {'专名'}):
            self.assertEqual(y.record('s', '1', log=self.log), '记下了：Yorimichi 1。')

    def test_record_tells_the_truth(self):
        """记下以后回给“我”实情（2026-10-30）：送达关着就说只记下判断；用户关了联想先说这个。"""
        from . import switch, yorimichi as y
        slog = Path(self.tmp.name) / 'switch.jsonl'
        with patch.object(switch, 'LOG', slog), patch.object(a, 'DELIVER', set()):
            self.assertIn('送达现在关着', y.record('s', 2, log=self.log))
            switch.record('off', 'claude', 's', '/no-souhatsu', slog)
            got = y.record('s', 2, log=self.log)
            self.assertIn('/no-souhatsu', got)
            self.assertNotIn('送达', got)

    def test_nudge_once(self):
        """一直没判过 yorimichi 的对话，第 NUDGE_AT 句用户的话提醒一次；命令、宿主注入不算句数；判过（哪怕 0）就不提醒。"""
        from . import switch, yorimichi as y
        conn = st.connect(Path(self.tmp.name) / 'test.db')
        self.addCleanup(conn.close)
        for target, value in (
            ('mh.synapse.push.pos.tag', lambda texts: [[(w, 'nz', 0, 0) for w in texts[0].split()]]),
            ('mh.synapse.push.associate.cached', lambda c: {'idx': SimpleNamespace(k={}), 'stmts': '', 'nodes': ''}),
            ('mh.synapse.push.associate.missing', lambda *args: []),
            ('mh.synapse.push.switch.LOG', Path(self.tmp.name) / 'switch.jsonl'),
            ('mh.synapse.push.yorimichi.LOG', self.log),
            ('mh.synapse.push.associate.spans', lambda *args: ([], {}, {'level': None}, {})),
            ('mh.synapse.push.associate.listed', lambda *args: {}),
        ):
            self.enterContext(patch(target, value))
        say = lambda session, text='随便 说说': a.associate(conn, text, session=session)
        self.assertEqual((say('s'), say('s', '/model')), (None, None))
        got = say('s')                                                    # 第 3 句（/model 不算）还没到
        self.assertIsNone(got)
        got = say('s')
        self.assertIn('yorimichi', got[0])
        self.assertIn('提醒', got[1])
        self.assertIsNone(say('s'))                                       # 只提醒一次
        y.record('t', 0, log=self.log)                                    # 判过 0 的不提醒
        self.assertEqual([say('t') for _ in range(4)], [None] * 4)
        with patch('mh.synapse.push.pos.tag', lambda texts: None):        # 词性未就绪：提醒用不到分词，照样给
            got = [say('u') for _ in range(3)]
        self.assertEqual(got[:2], [None, None])
        self.assertIn('yorimichi', got[2][0])

    def test_header_shows_yorimichi(self):
        rows = [{"h": "L1:T1", "s": "L1:T1:S1", "date": "10-21", "name": "甲", "n": 1, "head": "x"}]
        self.assertTrue(a.shown_text(rows, False, 'Yorimichi 0').startswith('[Souhatsu] 只记录，未推送 · Yorimichi 0\n1> '))
        self.assertTrue(a.shown_text(rows, True, 'Yorimichi 2').startswith('[Souhatsu] · Yorimichi 2\n'))

    def test_mcp_tool_records(self):
        import os
        from ..pull import mcp
        from . import yorimichi as y
        with patch.object(y, 'LOG', self.log), patch.dict('os.environ', {}, clear=False):
            os.environ.pop('CLAUDE_CODE_SESSION_ID', None)
            self.assertIn('Yorimichi 3', mcp.call(None, 'yorimichi', {'width': 3, 'why': '轻松讨论', 'session': 's'}))
            self.assertEqual(y.state('s')['why'], '轻松讨论')
            self.assertIn('Yorimichi 0', mcp.call(None, 'yorimichi', {'width': 0, 'session': 's'}))      # 0 不算没给
            with self.assertRaises(ValueError):
                mcp.call(None, 'yorimichi', {'width': 9, 'session': 's'})

if __name__ == '__main__':
    unittest.main()


class RunbookTest(unittest.TestCase):
    """runbook 跟上（2026-10-29）：程序认出落后之处；更新稿写进数据目录，runbook 一改就不再提示。"""

    def setUp(self):
        import subprocess
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.home = Path(self.tmp.name) / "status"
        self.repo.mkdir()
        self.git = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo,
                                             capture_output=True, text=True, check=True)
        self.git("init", "-q")
        (self.repo / "runbook.md").write_text("# runbook\n\n- [x] A1 做完了\n- [ ] B2 还在做\n- [ ] C3 " + "长" * 1600 + "\n")
        self.git("add", "."), self.git("commit", "-qm", "runbook")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, msg):
        (self.repo / "x.txt").write_text(msg)
        self.git("add", "."), self.git("commit", "-qm", msg)

    def test_behind(self):
        from . import runbook
        self.commit("form：B2 的第一步、A1 的尾巴")
        got = "\n".join(runbook.behind(self.repo, "runbook.md"))
        self.assertIn("打了勾还留着：A1", got)
        self.assertIn("之后 1 次提交提到了 B2", got)          # 勾过的 A1 不算
        self.assertIn("经过写长了：C3", got)
        self.assertNotIn("B2（", got)

    def test_long_counts_sub_items(self):
        from . import runbook
        (self.repo / "runbook.md").write_text("- [ ] E5 头\n" + "  - 经过\n" * 300 + "- [ ] F6 短\n")
        self.git("commit", "-qam", "长")
        got = "\n".join(runbook.behind(self.repo, "runbook.md"))
        self.assertIn("经过写长了：E5", got)
        self.assertNotIn("F6", got)

    def test_waiting_for_user_ages(self):
        from datetime import datetime, timedelta, timezone
        from . import runbook
        name = runbook.USER_NAME
        (self.repo / "runbook.md").write_text(f"- [ ] D4 样例等{name}看\n")
        self.git("commit", "-qam", "等")
        later = datetime.now(timezone.utc) + timedelta(days=runbook.WAIT_DAYS)
        self.assertIn("D4（3 天）", "\n".join(runbook.behind(self.repo, "runbook.md", now=later)))
        self.assertEqual(runbook.behind(self.repo, "runbook.md"), [])

    def test_draft_pending_until_runbook_changes(self):
        from . import runbook
        calls = []
        model = lambda p: calls.append(p) or ("- B2：剩改成……（依据：abc）", {"model": "fake"})
        self.assertTrue(runbook.draft("ws", self.repo, "runbook.md", model, home=self.home))
        self.assertIn("A1 做完了", calls[0])                                    # 材料里有 runbook 全文
        self.assertFalse(runbook.draft("ws", self.repo, "runbook.md", model, home=self.home))   # 没变就不再调
        self.assertEqual(len(calls), 1)
        self.assertIn("没处理的更新稿（1 处）", "\n".join(runbook.section("ws", self.repo, "runbook.md", self.home)))
        (self.repo / "runbook.md").write_text("# runbook\n\n- [ ] B2 还在做\n")
        self.assertEqual(runbook.section("ws", self.repo, "runbook.md", self.home), [])   # 改过 = 处理过

    def test_commits_without_ids_not_shown(self):
        from . import runbook
        self.commit("synapse：修一个小问题")
        self.assertNotIn("提交", "\n".join(runbook.behind(self.repo, "runbook.md")))

    def test_empty_draft_not_shown(self):
        from . import runbook
        runbook.draft("ws", self.repo, "runbook.md", lambda p: ("无", {}), home=self.home)
        self.assertNotIn("更新稿", "\n".join(runbook.section("ws", self.repo, "runbook.md", self.home)))

    def test_drift_between_design_and_runbook(self):
        from . import runbook
        d = self.repo / "design"
        (d / "global" / "cases").mkdir(parents=True), (d / "synapse").mkdir()
        (d / "global" / "runbook.md").write_text("- [ ] B2 还在做，见 [近况](../synapse/设计.md#近况)、[检索](../synapse/设计.md#检索)\n")
        (d / "synapse" / "设计.md").write_text("# Synapse\n\n### 近况（用户 2026-10-24）\n\n<a id=\"x-y\"></a>\n"
                                              "见 [x](#x-y)、[旧](#项目现状)、[没了](../gone.md)；runbook B2、runbook I3\n")
        (d / "global" / "cases" / "a.md").write_text("[旧](../../synapse/设计.md#项目现状) runbook A1\n")   # 历史不核
        got = "\n".join(runbook.behind(self.repo, "design/global/runbook.md"))
        self.assertIn("runbook.md → ../synapse/设计.md#检索", got)
        self.assertNotIn("#近况", got)                                   # 标题的前一段算对得上
        self.assertIn("synapse/设计.md → #项目现状", got)
        self.assertIn("synapse/设计.md → ../gone.md", got)
        self.assertNotIn("#x-y", got)                                    # <a id> 也算锚点
        self.assertIn("synapse/设计.md 的 I3", got)
        self.assertNotIn("B2；", got), self.assertNotIn("A1", got), self.assertNotIn("cases", got)

    def test_nothing_to_do_skips_model(self):
        from . import runbook
        (self.repo / "runbook.md").write_text("- [ ] B2 还在做\n")
        self.git("commit", "-qam", "清理")
        self.assertFalse(runbook.draft("ws", self.repo, "runbook.md", lambda p: 1 / 0, home=self.home))
