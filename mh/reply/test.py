"""回复的测试：消息转文字、切段、叫没叫到、完整记录（连发、复读合并，看清过的标注）、跟复读、清理、限长、拆条、
看清（带图）、私聊、合批、额度。模型换成假的，不连协议端，不写数据目录。
python3 -m unittest mh.reply.test
"""
import io
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from . import (Reply, called, clean, clip, echo_due, echo_segments, fetch, image_urls, memos_of, picks_of, prompt, segment_start,
               short, split, text_of, view, visible, SKIP)


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").astimezone().timestamp()


def txt(uid, text, **k):
    return {"user_id": uid, "message": [{"type": "text", "data": {"text": text}}], **k}


class PureTest(unittest.TestCase):
    def test_text_of(self):
        msg = [{"type": "at", "data": {"qq": "1"}}, {"type": "text", "data": {"text": " 你好 https://example.com/a?b=1 看"}},
               {"type": "image", "data": {}}, {"type": "at", "data": {"qq": "2"}}]
        self.assertEqual(text_of(msg, {"1": "阿明"}), "@阿明 你好 [链接 example.com] 看[图片]@2")
        self.assertEqual(text_of([{"type": "image", "data": {"summary": "[躲车底下]"}},
                                  {"type": "image", "data": {"summary": "[图片]"}}]), "[表情：躲车底下][图片]")

    def test_segment_by_day_and_gap(self):
        times = [ts("2026-10-31 06:50"), ts("2026-10-31 07:10"), ts("2026-10-31 07:20"),
                 ts("2026-10-31 08:00"), ts("2026-10-31 08:10")]
        self.assertEqual(segment_start(times, ts("2026-10-31 08:15")), 3)        # 07:20 → 08:00 隔了 40 分钟
        self.assertEqual(segment_start(times[:3], ts("2026-10-31 07:25")), 1)    # 06:50 在 7 点前
        self.assertEqual(segment_start(times, ts("2026-10-31 09:00")), 5)        # 离最后一条也超过 30 分钟
        self.assertEqual(segment_start([ts("2026-10-31 23:50")], ts("2026-11-01 00:10")), 0)   # 跨零点不切

    def test_called(self):
        at = {"message": [{"type": "at", "data": {"qq": "9"}}]}
        self.assertTrue(called(at, 9, [], set()))
        self.assertTrue(called({"message": [{"type": "reply", "data": {"id": "77"}}]}, 9, [], {"77"}))
        self.assertTrue(called({"message": [{"type": "text", "data": {"text": "Assistant 在吗"}}]}, 9, ["assistant"], set()))
        self.assertFalse(called({"message": [{"type": "text", "data": {"text": "吃了吗"}}]}, 9, ["assistant"], set()))

    def test_clip_split_clean(self):
        self.assertEqual(clip("一二三。四五六七八", 5), "一二三。")
        self.assertEqual(clip("一二三四五六", 4), "一二三四")
        self.assertEqual(visible("a b\nc"), 3)
        self.assertEqual(split("嗯\n\n好的\n那就这样吧\n明天见"), ["嗯 好的", "那就这样吧", "明天见"])
        self.assertEqual(clean("咋啦<\\/|final|>"), "咋啦")
        self.assertEqual(clean("好<|end|>"), "好")

    def test_short_and_picks(self):
        self.assertEqual(short("测试昵称一二三❀"), "测试昵称")
        self.assertEqual(short("测试乙"), "测试乙")
        self.assertEqual(picks_of("（看 3 5）"), [3, 5])
        self.assertEqual(picks_of("（看3，12）"), [3, 12])
        self.assertIsNone(picks_of("看 3 吧"))

    def test_view_full_and_runs(self):
        long = "这是一条很长很长的消息，超过了二十个字也照样完整保留下来"
        seg = [txt("2", long), txt("9", "我算了一下，答案是三", note="〔回这句前看了 1 的详情〕"),
               txt("3", "hhh"), txt("3", "hhh"), txt("3", "hhh"),
               txt("2", "+1"), txt("3", "+1"),
               {"user_id": "3", "message": [{"type": "at", "data": {"qq": "9"}}, {"type": "text", "data": {"text": " 在吗"}}]}]
        v = view(seg, 9, {"2": "阿明", "3": "小红"}.get)
        self.assertEqual(v.splitlines(), [f"1 阿明：{long}", "2 我：我算了一下，答案是三〔回这句前看了 1 的详情〕",
                                          "3-5 小红：hhh〔连发 ×3〕", "6-7 阿明、小红：+1〔复读 ×2〕", "8 小红：@我 在吗"])
        # 只往后追加：多一条，前面的行不变
        self.assertTrue(view(seg + [txt("2", "嗯")], 9, {"2": "阿明", "3": "小红"}.get).startswith(v))

    def test_prompt_tail(self):
        p = prompt("1 阿明：在吗", "群「测试」", "新消息里有人叫到了你。", 5)
        self.assertTrue(p.startswith("群「测试」这一段的聊天：\n\n1 阿明：在吗\n\n———— 现在 "))
        self.assertIn("你上次说话是第 5 条。新消息里有人叫到了你。", p)

    def test_echo_due(self):
        self.assertIsNone(echo_due([txt("2", "草")], 9))
        self.assertEqual(echo_due([txt("4", "嗯"), txt("2", "草"), txt("3", "草")], 9)["user_id"], "3")
        self.assertIsNone(echo_due([txt("2", "草"), txt("2", "草")], 9))                   # 一个人连发不算
        self.assertIsNone(echo_due([txt("2", "草"), txt("3", "草"), txt("9", "草")], 9))   # 我跟过了
        at = {"user_id": "3", "message": [{"type": "at", "data": {"qq": "1"}}]}
        self.assertIsNone(echo_due([{**at, "user_id": "2"}, at], 9))                       # @ 不跟
        self.assertIsNone(echo_due([txt("2", "草"), txt("3", "草")], 9, after=0))
        img = [{"type": "image", "data": {"file": "a.jpg", "url": "http://x/a.jpg", "summary": ""}}]
        self.assertEqual(echo_segments(img), [{"type": "image", "data": {"file": "http://x/a.jpg", "summary": ""}}])

    def test_images(self):
        img = lambda u: {"type": "image", "data": {"url": u}}
        seg = [{"message": [img("a"), img("b")]}, {"message": [img("c")]}, {"message": [img("d"), img("e")]}]
        self.assertEqual(image_urls(seg, [3, 1, 9]), ["d", "e", "a"])

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def opener(u, timeout):
            if u == "bad":
                raise OSError("expired")
            return R(b"\x89PNG....")
        with tempfile.TemporaryDirectory() as d:
            got = fetch(["ok", "bad"], d, opener=opener)
            self.assertEqual([p.rsplit("/", 1)[1] for p in got], ["1.png"])


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, out, **cfg):
        calls = []
        self.images = []

        def call(p, images=()):
            calls.append(p)
            self.images.append(list(images))
            return out.pop(0), {"usage": {"input_tokens": 10, "cached_input_tokens": 8}, "seconds": 1.0}
        persona = self.dir / "AGENTS.md"
        persona.write_text("我是测试人设。")
        r = Reply({"group": 5, "names": ["assistant"], "ignore": ["8"], "hourly_max": 2, "bot_hourly": 1, "proactive_hourly": 1,
                   "owner": [2], "bots": [7], "proactive_after": 3, "max_chars": 10, "hint_chars": 5,
                   "persona": str(persona), "people": {"2": "阿明"}, "private": [2], **cfg}, call=call,
                  log_path=self.dir / "reply-log.jsonl", system_path=self.dir / "system.md")
        r.self_id = 9
        return r, calls

    def msg(self, mid, uid, text, t=None):
        return {"post_type": "message", "group_id": 5, "message_id": mid, "user_id": uid, "time": t or time.time(),
                "sender": {"nickname": f"u{uid}"}, "message": [{"type": "text", "data": {"text": text}}]}

    def test_note_and_compose(self):
        r, calls = self.make(["这句话实在是太长了超过十个字。", "短一点\n好<|end|>"])
        r.note(self.msg(1, 2, "assistant 在吗"))
        r.note(self.msg(2, 8, "assistant assistant"))                     # ignore 里的号叫也不算
        r.note(self.msg(1, 2, "assistant 在吗"))                      # 重复的不再记
        self.assertEqual(len(r.history[("group", "5")]), 2)
        self.assertIn(("group", "5"), r.pending)
        text, trail = r.compose()
        self.assertEqual(split(text), ["短一点", "好"])
        self.assertIn("1 阿明：assistant 在吗", calls[0])
        self.assertNotIn("（2）", calls[0])
        self.assertIn("太长了", calls[1])
        self.assertTrue(trail["rewrite"])
        self.assertEqual(trail["calls"][0]["usage"]["cached_input_tokens"], 8)
        sysp = (self.dir / "system.md").read_text()                # 人设和规则进系统提示，不进用户消息
        self.assertTrue(sysp.startswith("我是测试人设。"))
        self.assertIn("多数时候在 5 字以内", sysp)
        self.assertNotIn("我是测试人设", calls[0])

    def notice(self, mid, uid=2, kind="group_recall", **k):
        return {"post_type": "notice", "notice_type": kind, "group_id": 5, "user_id": uid,
                "message_id": mid, "operator_id": uid, **k}

    def test_recall(self):
        r, calls = self.make([])
        r.note(self.msg(1, 2, "assistant 在吗"))
        self.assertIn(("group", "5"), r.pending)
        r.note(self.notice(1))                                  # 叫我的那句撤回：不回了，记录里也没有
        self.assertEqual(list(r.history[("group", "5")]), [])
        self.assertEqual(r.pending, {})

    def test_recall_keeps_others(self):
        r, _ = self.make([])
        r.note(self.msg(1, 2, "assistant 在吗"))
        r.note(self.msg(2, 3, "闲聊"))
        r.note(self.notice(2))
        self.assertEqual([m["id"] for m in r.segment()], ["1"])
        self.assertIn(("group", "5"), r.pending)                # 还有叫我的那句，照回
        self.assertEqual(view(r.segment(), 9, r.who), "1 阿明：assistant 在吗")

    def test_recall_renumbers_and_skips_proactive(self):
        r, _ = self.make(["（不回）"], hourly_max=10)
        r.note(self.msg(1, 3, "a"))
        r.note(self.msg(2, 4, "b"))
        r.note(self.notice(1))
        self.assertEqual(view(r.segment(), 9, r.who), "1 u4：b")   # 编号顺延
        r.note(self.msg(3, 3, "c"))
        self.assertNotIn(("group", "5"), r.pending)              # 撤回后只有 2 条人话，不够 3 条
        r.note(self.msg(4, 3, "d"))
        self.assertIn(("group", "5"), r.pending)

    def test_recall_out_of_order_and_private(self):
        r, _ = self.make([])
        r.note(self.notice(9))                                   # 先到撤回
        r.note(self.msg(9, 2, "assistant 在吗"))
        self.assertEqual(list(r.history.get(("group", "5"), [])), [])
        self.assertEqual(r.pending, {})
        dm = {"post_type": "message", "message_type": "private", "message_id": 10, "user_id": 2,
              "time": time.time(), "sender": {"nickname": "x"},
              "message": [{"type": "text", "data": {"text": "在吗"}}]}
        r.note(dm)
        r.note({"post_type": "notice", "notice_type": "friend_recall", "user_id": 2, "message_id": 10})
        self.assertEqual(list(r.history[("private", "2")]), [])
        self.assertEqual(r.pending, {})

    def test_active_window(self):
        r, calls = self.make(["在", "嗯"], proactive_hourly=0)
        r.note(self.msg(1, 2, "assistant 在吗"))
        self.assertIn(("group", "5"), r.pending)
        r.pending.clear()                                   # 模拟这一句已经回完
        r.said[("group", "5")] = time.time()
        r.note(self.msg(2, 3, "接着聊啊"))                    # 没 @、没名字
        self.assertIn(("group", "5"), r.pending)
        self.assertTrue(r.pending[("group", "5")]["active"])
        r.pending.clear()
        r.note(self.msg(3, 7, "bot 也说话"))                  # bot 的话不开窗口
        self.assertNotIn(("group", "5"), r.pending)
        r.said[("group", "5")] = time.time() - r.active - 1  # 窗口过了
        r.pending.clear()
        r.note(self.msg(4, 3, "还在吗"))
        self.assertNotIn(("group", "5"), r.pending)

    def test_look_marks_reply(self):
        r, calls = self.make(["（看 2）", "好的"])
        r.note(self.msg(1, 2, "早"))
        r.note({**self.msg(2, 3, "assistant 你看这个"), "sender": {"nickname": "测试乙", "card": "乙"}})
        text, trail = r.compose()
        self.assertEqual(text, "好的")
        self.assertEqual(trail["look"], [2])
        self.assertIn("群名片 乙，昵称 测试乙，QQ 3", calls[1])
        self.assertNotIn("QQ 3", calls[0])
        r.notes["好的"] = "〔回这句前看了 2 的详情〕"           # work() 发送前登记，协议端回传那条时标上
        r.note({**self.msg(3, 9, "好的"), "post_type": "message_sent"})
        self.assertIn("3 我：好的〔回这句前看了 2 的详情〕", view(r.segment(), 9, r.who))

    def test_look_with_image(self):
        r, calls = self.make(["（看 1）", "是只猫"])
        r.note({**self.msg(1, 2, ""), "message": [{"type": "text", "data": {"text": "assistant 这是啥"}},
                                                  {"type": "image", "data": {"url": "http://x/1.jpg"}}]})
        with patch(f"{Reply.__module__}.fetch", lambda urls, folder: [f"{folder}/1.jpg"] if urls else []):
            text, trail = r.compose()
        self.assertEqual((text, trail["images"]), ("是只猫", 1))
        self.assertEqual(self.images[0], [])
        self.assertTrue(self.images[1][0].endswith("/1.jpg"))
        self.assertIn("里面的图按出现先后附上了", calls[1])

    def test_memo(self):
        self.assertEqual(memos_of("好的\n记下：166 是一张猫图"), ("好的", ["166 是一张猫图"]))
        r, calls = self.make(["（看 1）", "是只猫\n记下：1 的图是一只橘猫", "记下：又看了一眼", "嗯"])
        r.note({**self.msg(1, 2, ""), "message": [{"type": "text", "data": {"text": "assistant 这是啥"}},
                                                  {"type": "image", "data": {"url": "http://x/1.jpg"}}]})
        with patch(f"{Reply.__module__}.fetch", lambda urls, folder: [f"{folder}/1.jpg"] if urls else []):
            text, trail = r.compose()
        self.assertEqual((text, trail["memo"]), ("是只猫", ["1 的图是一只橘猫"]))
        text, trail = r.compose()                                  # 只记不回
        self.assertEqual(text, SKIP)
        self.assertIn("我记下的：\n- （第 1 条时）1 的图是一只橘猫\n", calls[2])
        r.record(trail)
        r2, calls2 = self.make(["嗯"])                             # 重启：从 reply-log 补回来
        r2.load(path=self.dir / "none.jsonl")
        self.assertEqual([m["text"] for m in r2.memos[("group", "5")]], ["又看了一眼"])

    def test_mine_and_quota(self):
        r, _ = self.make([])
        r.note({**self.msg(3, 9, "我说的"), "post_type": "message_sent"})
        self.assertIn("3", r.mine)
        self.assertEqual(r.pending, {})
        self.assertIsNone(r.kind_of({"2", "3"}))                   # 有阿明叫：不计
        self.assertEqual(r.kind_of({"7"}), "bot")
        self.assertEqual(r.kind_of({"7", "3"}), "total")
        self.assertEqual(r.kind_of({"proactive"}), "proactive")
        r.spend("bot")
        self.assertFalse(r.allow("bot"))                           # bot 那份 1 次用完
        self.assertTrue(r.allow("total"))                          # 总量 2 次还剩 1
        r.spend("total")
        self.assertFalse(r.allow("total"))
        self.assertTrue(r.allow(None))                             # 阿明的照回
        r.spend(None)
        self.assertEqual(r._used("total"), 2)
        self.assertEqual(SKIP, "（不回）")

    def test_proactive(self):
        r, calls = self.make(["（不回）"], hourly_max=10)
        for i, uid in enumerate([3, 7, 4]):                       # bot（7）的不算：别人的话只有 2 条
            r.note(self.msg(i + 1, uid, f"闲聊{i}"))
        self.assertEqual(r.pending, {})
        r.note(self.msg(5, 4, "闲聊4"))                            # 第 3 条别人的话
        self.assertEqual(r.pending[("group", "5")]["who"], {"proactive"})
        r.note(self.msg(6, 3, "assistant 在吗"))                        # 等着的时候被叫到：改成普通回复
        self.assertEqual(r.pending[("group", "5")]["who"], {"3"})
        text, _ = r.compose(proactive=True)
        self.assertIn("没人叫你", calls[0])

    def test_echo(self):
        r, _ = self.make([], hourly_max=5)
        sent = []

        class WS:
            def send(self, obj):
                sent.append(obj)
        r.ws = WS()
        r.note(self.msg(1, 2, "草"))
        r.note(self.msg(2, 3, "草"))
        self.assertEqual(sent[0]["params"]["message"], [{"type": "text", "data": {"text": "草"}}])
        r.note(self.msg(3, 4, "草"))                              # 同一串只跟一次
        self.assertEqual(len(sent), 1)
        self.assertIn('"echo": true', (self.dir / "reply-log.jsonl").read_text())

    def test_private(self):
        r, calls = self.make(["在的"])
        dm = lambda mid, uid, text, **k: {"post_type": "message", "message_type": "private", "message_id": mid,
                                          "user_id": uid, "time": time.time(), "sender": {"nickname": "x"},
                                          "message": [{"type": "text", "data": {"text": text}}], **k}
        r.note(dm(10, 2, "在吗"))                                   # 白名单里的人，不用叫到
        r.note(dm(11, 4, "你好"))                                   # 不在白名单，不记
        r.note({**dm(12, 9, "嗯"), "post_type": "message_sent", "target_id": 2})
        self.assertEqual([m["id"] for m in r.history[("private", "2")]], ["10", "12"])
        self.assertNotIn(("private", "4"), r.history)
        self.assertIn(("private", "2"), r.pending)
        text, trail = r.compose(("private", "2"))
        self.assertEqual(text, "在的")
        self.assertIn("和阿明私聊这一段的聊天", calls[0])
        self.assertIn("阿明在私聊你", calls[0])
        self.assertEqual(trail["chat"], ["private", "2"])

    def test_due(self):
        r, _ = self.make([])
        now = time.time()
        r.pending = {("group", "5"): {"at": now - 1, "who": {"3"}}, ("private", "2"): {"at": now - 9, "who": {"2"}}}
        r.last = {("group", "5"): now - 1, ("private", "2"): now - 1}
        self.assertEqual(r.due(now)[0], ("private", "2"))           # 等满 MERGE_MAX 了
        r.pending = {("group", "5"): {"at": now, "who": {"3"}}}
        r.last = {("group", "5"): now}
        chat, wait = r.due(now)
        self.assertIsNone(chat)
        self.assertGreater(wait, 0)


if __name__ == "__main__":
    unittest.main()
