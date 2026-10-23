"""表达规范保留嵌套文档的视角与正文。"""
import unittest
from . import context, present, prompt


class ExpressTest(unittest.TestCase):
    def test_preserve_nested_viewpoints(self):
        text = '作者：青禾\n我的名字是青禾。\n『你是观察者。』\n代拟邮件：我已收到，明天给你答复。\n来源：E7 / 3:U1'
        ctx = context('青禾', ['松风'], purpose='memory')
        shown = present(text, ctx, raw=True)
        self.assertTrue(shown.endswith(text))
        self.assertIn('来源原文', shown)
        self.assertEqual(present(text, dict(ctx, purpose='form')), text)
        self.assertEqual(present(text, dict(ctx, reader='human')), text)
        self.assertEqual(ctx['author'], 'source')
        self.assertIn('按署名人解释', prompt('称呼：{{用户}}', '青禾'))
        self.assertIn('称呼：青禾', prompt('称呼：{{用户}}', '青禾'))
