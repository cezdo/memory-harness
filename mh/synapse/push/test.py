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
