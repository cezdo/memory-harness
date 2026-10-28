"""节律的测试：文件锁只让一个实例跑；观察循环的一轮是“采集 → observe 跑一批”，形成循环的一轮是“form 跑一批”，
触发原因都记为定时。

python3 -m unittest mh.rhythm.test
"""
import io
import tempfile
import time
import unittest
from pathlib import Path

from . import form_loop, lock, observe_loop, pick, tidy_once
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
                  "label": Model(["名字：一\n停在：说了一（陈述1）\n索引词：\n- 一号\n"]),
                  "attach": Model([]), "attach_lines": Model(["挂到：新线 一号｜为什么：新的目标"])}
        self.assertEqual(form_loop(db, once=True, log=io.StringIO(), models=models), (3, 0))   # 整理、起名、挂线
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

class QuotaCacheTest(unittest.TestCase):
    """Claude 的额度接口限流：读到的缓存 CLAUDE_TTL 秒，期间不再请求；读不到退回缓存并注明。"""

    def setUp(self):
        from . import quota
        self.quota, self.old = quota, quota.CLAUDE_CACHE
        self.tmp = tempfile.TemporaryDirectory()
        quota.CLAUDE_CACHE = Path(self.tmp.name) / "quota-claude.json"

    def tearDown(self):
        self.quota.CLAUDE_CACHE = self.old
        self.tmp.cleanup()

    def test_cache_then_stale(self):
        import ssl, urllib.error
        q, calls = {"name": "claude", "plan": "pro", "windows": [], "ok": True}, []

        def ok():
            calls.append(1)
            return q

        def cut():
            raise urllib.error.URLError(ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred"))
        self.assertEqual(self.quota.claude(fetch=ok, now=1000), q)
        self.assertEqual(self.quota.claude(fetch=ok, now=1000 + 60), q)              # TTL 内不再请求
        self.assertEqual(len(calls), 1)
        got = self.quota.claude(fetch=cut, now=1000 + self.quota.CLAUDE_TTL + 60)    # 过了 TTL、被掐断：退回缓存
        self.assertIn("限流", got["stale"])
        self.assertIn("（", self.quota.text([got]))

    def test_no_cache_limited(self):
        def cut():
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        import urllib.error
        with self.assertRaisesRegex(RuntimeError, "限流"):
            self.quota.claude(fetch=cut, now=0)


class QuotaArgsTest(unittest.TestCase):
    """额度检查只读：启动 app-server 要关掉插件、apps、更新检查和在线模型目录（2026-10-28，默认一次启动拉 20MB）。"""

    def test_app_server_args(self):
        from . import quota
        plain = quota._app_server_args()
        self.assertEqual(plain[1], "app-server")
        self.assertIn("features.plugins=false", plain)
        self.assertIn("features.apps=false", plain)
        self.assertIn("features.remote_plugin=false", plain)
        self.assertIn("check_for_update_on_startup=false", plain)
        self.assertNotIn("model_catalog_json=/x.json", plain)
        self.assertIn("model_catalog_json=/x.json", quota._app_server_args(Path("/x.json")))


class QuotaTimeoutTest(unittest.TestCase):
    """app-server 不出声也不退出：到 CODEX_TIMEOUT 杀掉，记成读不到，不一直等（审查 09，2026-10-28）。"""

    def test_codex_timeout(self):
        from . import quota
        d = Path(tempfile.mkdtemp())
        fake = d / "codex"
        fake.write_text("#!/bin/sh\nexec sleep 30\n")
        fake.chmod(0o755)
        (d / "home").mkdir()
        (d / "home" / "auth.json").write_text("{}")
        saved = quota.CODEX, quota.CODEX_HOME, quota.CODEX_TIMEOUT
        quota.CODEX, quota.CODEX_HOME, quota.CODEX_TIMEOUT = str(fake), d / "home", 1
        try:
            t = time.time()
            (q,) = quota.read(["codex"])
        finally:
            quota.CODEX, quota.CODEX_HOME, quota.CODEX_TIMEOUT = saved
        self.assertLess(time.time() - t, 10)
        self.assertIn("1 秒没返回额度", q["error"])
        self.assertIsNone(quota.wait_until(q))                 # 读不到，不是用完了


if __name__ == "__main__":
    unittest.main()
