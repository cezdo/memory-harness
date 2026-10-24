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
        self.assertIn('OpenClaw', self.call('OpenClaw', session='other')[0])

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
                patch.object(a, 'DELIVER', {'专名'}):
            self.assertIsNone(self.call('OpenClaw'))
        q = json.loads(self.conn.execute('SELECT query FROM use_detail ORDER BY occurrence DESC LIMIT 1').fetchone()[0])
        self.assertEqual(q['associate']['skip'], '同词已送达')
        self.assertEqual(q['associate']['sent_words'], [])


    def test_no_old_j(self):
        """旧 J 退役（2026-10-23）：联想只记一条足迹，不再另开旧 J 的影子。"""
        k = {('s', 1): {'h': 'L1:T1:S1', 'text': '陈述 Foo', 'anchor': '2026-10-20', 'workspace': 'w', 'holder': ''}}
        data = {'idx': SimpleNamespace(k=k, match=lambda text: {}), 'stmts': 'foo', 'nodes': ''}
        terms = [{'t': 'Foo', 'ok': True, 'tier': '专名'}]
        with patch.object(a, 'cached', return_value=data), \
                patch.object(a, 'spans', return_value=(terms, {'Foo': set(k)}, {'level': None}, {})), \
                patch.object(a, 'clues', return_value='线索'):
            self.call('Foo')
        rows = [json.loads(q)['associate'] for (q,) in self.conn.execute('SELECT query FROM use_detail')]
        self.assertEqual(len(rows), 1)
        self.assertFalse(any(q.get('old_j') for q in rows))


if __name__ == '__main__':
    unittest.main()


class RecentTest(unittest.TestCase):
    """最近停下的几件事：fork 去重、一行的写法、开场去掉当前会话。"""

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
    """开场映像的几处程序生成：检查只推变了的、环境段。"""

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
