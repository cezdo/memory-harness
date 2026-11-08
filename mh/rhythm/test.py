"""节律的测试：文件锁只让一个实例跑；观察循环的一轮是“采集 → observe 跑一批”，形成循环的一轮是“form 跑一批”，
触发原因都记为定时。

python3 -m unittest mh.rhythm.test
"""
import io
import os
import tempfile
import time
import unittest
from pathlib import Path

from . import _form_inputs, _requests_to, _rest, _touched, form_loop, lock, observe_loop, pick, tidy_once
from .. import rhythm
from ..engram import observe, store as st
from ..engram.observe.sources.turn import Session, Turn
from ..engram.store.instance import USER_NAME as U


class Source:
    def __init__(self, path, session, turns):
        self.path, self.session, self.turns = path, session, turns

    def files(self):
        return [self.path]

    def parse(self, path):
        return self.session, self.turns


class Model:
    model, platform, isolation = "fake-model", "fake", "fake-iso"

    def __init__(self, outputs):
        self.outputs = list(outputs)

    def __call__(self, prompt):
        return self.outputs.pop(0), {}


class RhythmTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_tidy_once(self):
        """整理循环：到了周期的任务才跑，上次跑的时刻记在状态文件里；一个任务出错不挡别的。"""
        ran = []

        def ok(conn, log):
            ran.append("ok")
            return "做了"

        def bad(conn, log):
            raise RuntimeError("坏了")
        tasks = [("快", "程序", 60, ok), ("坏", "程序", 60, bad), ("慢", "模型", 86400, ok)]
        state, log = self.dir / "tidy-state.json", io.StringIO()
        self.assertEqual(tidy_once(None, log=log, tasks=tasks, state=state, now=1000), ["快", "坏", "慢"])
        self.assertIn("[整理·坏] 出错", log.getvalue())
        self.assertEqual(tidy_once(None, log=log, tasks=tasks, state=state, now=1030), [])          # 都没到周期
        self.assertEqual(tidy_once(None, log=log, tasks=tasks, state=state, now=1070), ["快", "坏"])  # 一天一次的还没到
        self.assertEqual(ran, ["ok", "ok", "ok"])

    def test_pick(self):
        """预览按额度选模型（2026-10-20）：周额度用得少的先；5 小时快用完、用不了的往后；读不到的最后。"""
        def q(codex, claude):
            return lambda names: [x for x in (codex, claude) if x["name"] in names]
        def w(name, h5, week, ok=True):
            return {"name": name, "ok": ok, "windows": [{"name": "5h", "used": h5}, {"name": "week", "used": week}]}
        names = ["sol", "sonnet"]
        self.assertEqual(pick(names, q(w("codex", 0, 10), w("claude", 0, 58))), ["sol", "sonnet"])
        self.assertEqual(pick(names, q(w("codex", 0, 70), w("claude", 0, 58))), ["sonnet", "sol"])
        self.assertEqual(pick(names, q(w("codex", 0, 70), w("claude", 85, 20))), ["sol", "sonnet"])
        self.assertEqual(pick(names, q(w("codex", 0, 70), w("claude", 0, 20, ok=False))), ["sol", "sonnet"])
        self.assertEqual(pick(names, q({"name": "codex", "error": "x"}, w("claude", 0, 90))), ["sonnet", "sol"])

    def test_lock(self):
        a = lock("x", self.dir)
        self.assertIsNotNone(a)
        self.assertIsNone(lock("x", self.dir))          # 第二个实例拿不到
        a.close()
        self.assertIsNotNone(lock("x", self.dir))

    def test_rest(self):
        """叫醒（observe 设计“叫醒”）：休眠中叫醒文件被碰就提前醒；一轮跑着时被碰过的，跑完立即醒；没人碰就睡满。"""
        import threading
        saved = rhythm.WAKE_POLL, rhythm.WAKE_SETTLE
        rhythm.WAKE_POLL, rhythm.WAKE_SETTLE = 0.01, 0
        try:
            wake = self.dir / "observe.wake"
            since = _touched(wake)                                     # 还没有文件
            t = time.monotonic()
            self.assertFalse(_rest(wake, since, 0.05))
            self.assertGreaterEqual(time.monotonic() - t, 0.05)
            threading.Timer(0.05, wake.touch).start()
            t = time.monotonic()
            self.assertTrue(_rest(wake, since, 30))
            self.assertLess(time.monotonic() - t, 5)
            since = _touched(wake)
            os.utime(wake, ns=(since + 10**9, since + 10**9))            # 跑这一轮时被碰过
            self.assertTrue(_rest(wake, since, 30))
            since = _touched(wake)                                       # 库里的输入变了也醒（G2：form 看新的 E、申请）
            inputs = {"n": 1}
            threading.Timer(0.05, lambda: inputs.update(n=2)).start()
            t = time.monotonic()
            self.assertTrue(_rest(wake, since, 30, watch=lambda: inputs["n"], seen=1))
            self.assertLess(time.monotonic() - t, 5)
            self.assertFalse(_rest(wake, since, 0.05, watch=lambda: 2, seen=2))
        finally:
            rhythm.WAKE_POLL, rhythm.WAKE_SETTLE = saved

    def test_watch(self):
        """休眠时各循环看库里自己的输入（G2）：form 看最新的 E 和写给 form 的申请，observe 看写给 observe 的申请。"""
        conn = st.connect(self.dir / "engram.db")
        before = _form_inputs(conn), _requests_to("observe")(conn)
        me = st.main_operator(conn, "m")
        st.request(conn, st.MAIN, "form", {"session": [1]}, "这段会话切错了", me)
        conn.commit()
        self.assertNotEqual(_form_inputs(conn), before[0])
        self.assertEqual(_requests_to("observe")(conn), before[1])
        st.request(conn, st.MAIN, "observe", {"turn": [1]}, "读掉尾巴", me)
        conn.commit()
        self.assertNotEqual(_requests_to("observe")(conn), before[1])

    def test_observe_once(self):
        f = self.dir / "s.jsonl"
        f.write_text("x")
        sess = Session("dsh", "s1", str(f), started="2026-01-01T00:00:00Z", cwd="/w")
        src = Source(f, sess, [Turn("一", "答", closed=True), Turn("二", "答", closed=True)])
        models = {"preview": Model(["### 第 1–2 轮\n读法：粗读\n"]), "deep": Model([]),
                  "rough": Model([f"### 事件1\n{U}说一。\n来源：1:U1"])}
        db = self.dir / "engram.db"
        new, done, env = observe_loop(db, once=True, log=io.StringIO(), models=models, modules={"dsh": src})
        self.assertEqual((new, done, env), (2, 2, 0))                         # 采了两轮，预览一次、粗读一次
        conn = st.connect(db)
        causes = {r[0] for r in conn.execute("SELECT o.cause FROM occurrence o JOIN act_detail a ON a.occurrence=o.id")}
        self.assertEqual(causes, {"schedule"})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM event").fetchone()[0], 1)
        held = lock("observe", self.dir)                                    # 跑完一轮放开了锁
        self.assertIsNotNone(held)
        held.close()

    def test_form_once(self):
        f = self.dir / "s.jsonl"
        f.write_text("x")
        sess = Session("dsh", "s1", str(f), started="2026-01-01T00:00:00Z", cwd="/w")
        src = Source(f, sess, [Turn("一", "答", closed=True), Turn("二", "答", closed=True)])
        db = self.dir / "engram.db"
        observe_loop(db, once=True, log=io.StringIO(), modules={"dsh": src},
                     models={"preview": Model(["### 第 1–2 轮\n读法：粗读\n"]), "deep": Model([]),
                             "rough": Model([f"### 事件1\n{U}说一。\n来源：1:U1"])})
        conn = st.connect(db)
        eid = conn.execute("SELECT id FROM event").fetchone()[0]
        models = {"pack": Model([f"名字：一\n主题：说一\n### 陈述1\n{U}说一。\n来源：E{eid}\n"]),
                  "induce": Model(["无"]),
                  "label": Model(["名字：一\n停在：说了一（陈述1）\n索引词：\n- 一号\n"]),
                  "attach": Model([]), "attach_lines": Model(["挂到：新线 一号｜为什么：新的目标"])}
        self.assertEqual(form_loop(db, once=True, log=io.StringIO(), models=models), (4, 0))   # 整理、归纳、起名、挂线
        causes = {r[0] for r in conn.execute("SELECT o.cause FROM occurrence o JOIN act_detail a ON a.occurrence=o.id "
                                             "WHERE a.process='form'")}
        self.assertEqual(causes, {"schedule"})
        self.assertEqual(conn.execute("SELECT name FROM topic_line").fetchone()[0], "一号")
        held = lock("form", self.dir)                                       # 跑完一轮放开了锁
        self.assertIsNotNone(held)
        held.close()


    def test_reobserve(self):
        """再观察和新会话走同一个队列（2026-10-19）：只被旧版本逐轮观察过的轮次，用当前做法预览、读。"""
        f = self.dir / "s.jsonl"
        f.write_text("x")
        sess = Session("dsh", "s1", str(f), started="2026-01-01T00:00:00Z", cwd="/w")
        src = Source(f, sess, [Turn("一", "答", closed=True), Turn("二", "答", closed=True)])
        db = self.dir / "engram.db"
        conn = st.connect(db)
        from ..engram.observe import collect
        collect.run(conn, sources=("dsh",), log=io.StringIO(), modules={"dsh": src})
        old = st.operator(conn, "old", "observe", "observer")
        for (t,) in conn.execute("SELECT occurrence FROM turn_detail").fetchall():
            a = st.occurrence(conn, "act", old, outcome="ok", scale="turn")
            conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (a, "observe", "split", st.dumps({"turn": t}), "", "", "{}"))
        conn.commit()
        models = {"preview": Model(["### 第 1–2 轮\n读法：粗读\n"]), "deep": Model([]),
                  "rough": Model([f"### 事件1\n{U}说一。\n来源：1:U1"])}
        observe.request_reobserve(conn, [("dsh", "s1")], "再观察", st.main_operator(conn, "m"))
        conn.commit()
        self.assertEqual(observe_loop(db, once=True, log=io.StringIO(), models=models, modules={}), (0, 2, 0))   # 预览、粗读
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM event").fetchone()[0], 1)
        # 点名申请在下一批观察里纠正（C1d，2026-10-21）：排在新原话后面
        e, = [r[0] for r in conn.execute("SELECT id FROM event WHERE relations='[]' AND born IN "
                                         "(SELECT occurrence FROM act_detail WHERE step='rough')")]
        st.request(conn, st.MAIN, "observe", {"event": [e]}, "说话人不对", st.main_operator(conn, "m"), cause="manual")
        conn.commit()
        models["rough"] = Model([f"### 核对\nE{e}：改正。\n\n### 事件1\n我说一。\n来源：1:A1\n取代：E{e}"])
        self.assertEqual(observe_loop(db, once=True, log=io.StringIO(), models=models, modules={}), (0, 1, 0))
        self.assertEqual(observe.superseded(conn) & {e}, {e})

    def test_idle_releases_write(self):
        """没活干的一轮也不留写事务：登记 Operator 之后不提交，另一个循环就写不进库（A14 的真实案例，2026-10-19）。"""
        from . import form_once, observe_once
        conn = st.connect(self.dir / "engram.db")
        models = {"preview": Model([]), "deep": Model([]), "rough": Model([])}
        self.assertEqual(observe_once(conn, log=io.StringIO(), models=models, modules={}), (0, 0, 0))
        self.assertFalse(conn.in_transaction)
        models = {"pack": Model([]), "label": Model([]), "attach": Model([]), "attach_lines": Model([])}
        self.assertEqual(form_once(conn, log=io.StringIO(), models=models), (0, 0))
        self.assertFalse(conn.in_transaction)

class RegisterTest(unittest.TestCase):
    """常驻服务的注册（X1，2026-10-29）：unit 和 plist 由同一份定义生成；两个平台的日志都读成 (时刻, 正文)，
    launchd 下日志每行由 stamp 补时刻，按时刻取近几个小时的。"""

    def test_render(self):
        from . import register as r
        u = r.unit("memory-harness-form", python="/py", path="/a:/b")
        self.assertIn("ExecStart=/py -m mh rhythm --form", u)
        self.assertIn("Environment=PATH=/a:/b", u)
        self.assertIn("Restart=on-failure", u)
        import plistlib
        d = plistlib.loads(r.plist("memory-harness-tidy", python="/py", path="/a").encode())
        self.assertEqual(d["Label"], "memory-harness-tidy")
        self.assertEqual(d["ProgramArguments"], ["/py", "-m", "mh", "rhythm", "--tidy"])
        self.assertEqual(d["EnvironmentVariables"][r.STAMP_ENV], "1")
        self.assertEqual(d["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(d["StandardOutPath"], d["StandardErrorPath"])

    def test_journal_lines(self):
        from unittest import mock
        from . import register as r
        out = ("-- No entries --\n2026-10-20T09:00:00+09:00 host python3[1]: [预览] x：env_error：坏了\n"
               "2026-10-20T09:05:00+09:00 host python3[1]: 本轮出错：E('a: b')")
        with mock.patch.object(r, "PLATFORM", "systemd"), mock.patch.object(r, "_run", return_value=out):
            got = r.lines("memory-harness-observe", 6)
        self.assertEqual(got, [("2026-10-20T09:00:00+09:00", "[预览] x：env_error：坏了"),
                               ("2026-10-20T09:05:00+09:00", "本轮出错：E('a: b')")])

    def test_stamped_file(self):
        from datetime import datetime
        from . import register as r
        buf = io.StringIO()
        w = r._Stamped(buf)
        w.write("本轮出错：x\nTrace")
        w.write("back\n")
        text = buf.getvalue().splitlines()
        self.assertEqual(len(text), 2)
        self.assertTrue(text[1].endswith(" Traceback"))             # 半行接着写，不重复补时刻
        log = Path(tempfile.mkdtemp()) / "x.log"
        log.write_text("2026-10-20T01:00:00+09:00 旧的\n没有时刻的行\n2026-10-20T08:00:00+09:00 本轮出错：y\n")
        now = datetime.fromisoformat("2026-10-20T09:00:00+09:00")
        self.assertEqual(r._file_lines(log, 6, now=now), [("2026-10-20T08:00:00+09:00", "本轮出错：y")])
        self.assertEqual(r._file_lines(log.with_name("没有.log"), 6, now=now), [])


if __name__ == "__main__":
    unittest.main()
