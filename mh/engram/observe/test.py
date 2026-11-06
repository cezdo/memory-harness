"""observe 的测试：采集写入（来源、更正只从无到有）、解析事件、预览与观察（读法、打码、事件挂到哪一轮）、
什么时候预览、关掉的会话、失败计数与待重做、被内容过滤的不要了。
观察者换成假的，不调模型。python3 -m unittest mh.engram.observe.test
"""
import io
import json
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path

from .sources.turn import Session, Turn
from .. import store as st
from ..store import NA, NOT_DONE, UNKNOWN
from . import collect, modes, parse, preview
from .. import observe
from ..store.instance import USER_NAME as U

OUTPUT = f"""### 事件1
{U}提出先做一版看看。
说话人：{U}
行为：提出
限定：先做一版看看
来源：U1

### 事件2
我执行了检查，并说明结果。
说话人：我
行为：未知
限定：无
来源：A1-A2、A9
"""


PLAN = f"""### 第 1–2 轮
类型：讨论方向
判断：{U}要先做一版。
关注：结果。
问题：
- 结果在哪？
读法：精读

### 第 3 轮
类型：执行
读法：粗读

### 第 4 轮
类型：确认
读法：不读

### 精读顺序
第 1–2 轮
"""

READ = f"""### 事件1
{U}提出可能先做一版看看。
来源：1:U1

### 事件2
我说明结果在 path_A/file_A.md 里。来源：2:A1
"""


def peer_text(body="把包挪到 mh/engram。", name="迁移"):
    """另一个会话发来的消息，形状照 Claude Code 2.1.29x 的会话文件。"""
    return ("Another Claude session sent a message:\n"
            f'<cross-session-message from="uds:/x" from-name="{name}" from-mode="prompting">{body}</cross-session-message>\n\n'
            "This came from another Claude session — not typed by your user.")


def states(conn):
    """progress → {轮次: state}。"""
    return {p["turn"]: p["state"] for p in observe.progress(conn)}


def waiting(conn):
    """progress 里待观测的。"""
    return [p for p in observe.progress(conn) if p["state"] in observe.TO_OBSERVE]


def assert_live(test, conn):
    """活性（J3，2026-10-27）：每个待观测的轮（会话都当作已告一段落）都有办法前进——在 ready 排出的预览窗口里，
    或在 to_read 排出的某一组里。没有的就是永远待观测，form 的门槛会一直被它钉住（10-27 的漏分组）。"""
    far = datetime(2099, 1, 1, tzinfo=timezone.utc)
    pending = {p["turn"] for p in observe.progress(conn, now=far) if p["state"] in observe.TO_OBSERVE}
    cover = set()
    for rows, _ in observe.ready(conn, list(observe.pool_ops(conn).values()), now=far):
        cover |= {r["occurrence"] for r in rows}
    for _, group, _ in observe.to_read(conn):
        cover |= set(group["turns"])
    test.assertEqual(pending - cover, set(), "待观测却没有办法前进的轮")


class FakeSource:
    def __init__(self, path, session, turns):
        self.path, self.session, self.turns = path, session, turns

    def files(self):
        return [self.path]

    def parse(self, path):
        return self.session, self.turns


class FakeModel:
    model, platform, isolation = "fake-model", "fake", "fake-iso"

    def __init__(self, outputs, usage=None):
        self.outputs, self.usage, self.prompts = list(outputs), usage, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out, {"usage": self.usage or [{"inputTokens": 3, "outputTokens": 4}]}


class ObserveTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.conn = st.connect(self.dir / "engram.db")
        self.file = self.dir / "s.jsonl"
        self.file.write_text("x")

    def _collect(self, turns, closed_last=True, sid="s1", started="2026-01-01T00:00:00Z"):
        sess = Session("dsh", sid, str(self.file), started=started, cwd="/w")
        src = FakeSource(self.file, sess, turns)
        return collect.run(self.conn, sources=("dsh",), force=True, log=io.StringIO(), modules={"dsh": src})

    def test_collect_grows_while_parsing(self):
        """解析期间文件又写进一轮（审查 02，2026-10-28）：记下的是解析前的文件状态，下次不当成没变，新的一轮照样采到。"""
        sess = Session("dsh", "s1", str(self.file), started="2026-01-01T00:00:00Z", cwd="/w")
        a, b = Turn("甲", "答甲", closed=True), Turn("乙", "答乙", closed=True)

        class Growing(FakeSource):
            def parse(self, path):
                path.write_text(path.read_text() + "y")
                return self.session, [a]
        collect.run(self.conn, sources=("dsh",), log=io.StringIO(), modules={"dsh": Growing(self.file, sess, None)})
        got = collect.run(self.conn, sources=("dsh",), log=io.StringIO(), modules={"dsh": FakeSource(self.file, sess, [a, b])})
        self.assertEqual((got["dsh"]["skipped"], got["dsh"]["turns"]), (0, 1))
        got = collect.run(self.conn, sources=("dsh",), log=io.StringIO(), modules={"dsh": FakeSource(self.file, sess, [a, b])})
        self.assertEqual(got["dsh"]["skipped"], 1)                        # 没再变就跳过

    def test_parse(self):
        table = {"U1": "x", "A1": "y", "A2": "z"}
        events, nothing = parse.parse(OUTPUT, table)
        self.assertFalse(nothing)
        a, b = events
        self.assertEqual((a["speaker"], a["mode"], a["qualifier"], a["segments"]), (f"{U}", "提出", "先做一版看看", ["U1"]))
        self.assertEqual((b["mode"], b["qualifier"], b["segments"], b["unresolved"]), (UNKNOWN, NA, ["A1", "A2"], ["A9"]))
        self.assertEqual(b["text"], "我执行了检查，并说明结果。")
        self.assertEqual(parse.parse("无", table), ([], True))
        events, _ = parse.parse("### 事件1\n只有正文。", table)
        self.assertEqual((events[0]["speaker"], events[0]["unresolved"]), (UNKNOWN, ["（没写来源）"]))

    def test_precise_cap(self):
        """精读兜底（2026-10-19）：前 2 轮不受限；之后轮数不超过三成、回复字数不超过四成，超出的按精读顺序从后往前改粗读。"""
        from . import preview
        turns = [{"ordinal": i, "assistant": "回" * (500 if i == 0 else 100), "commentary": "[]"} for i in range(10)]
        plan = "".join(f"### 第 {k} 轮\n类型：讨论\n读法：精读\n\n" for k in (1, 3, 5, 7)) + "### 精读顺序\n第 1 轮\n第 3 轮\n第 5 轮\n第 7 轮\n"
        groups, info = preview.parse(plan, turns)
        # 第 1 轮 500 字占了 1400 字的三成多，仍在前 2 轮里；第 3 轮也是；第 5 轮：3 轮 ≤ 三成，字数 700 > 四成（560）→ 粗读
        self.assertEqual([(g["from"], g["how"]) for g in groups], [(1, "精读"), (3, "精读"), (5, "粗读"), (7, "粗读")])
        self.assertEqual(info["precise_turns"], 2)
        turns = [{"ordinal": i, "assistant": "回" * 100, "commentary": "[]"} for i in range(10)]
        groups, _ = preview.parse(plan, turns)
        # 字数平均：第 5 轮放得下（3 轮、三成字数），第 7 轮超过三成的轮 → 粗读
        self.assertEqual([g["how"] for g in groups], ["精读", "精读", "精读", "粗读"])

    def test_fold_host_turns(self):
        """后台通知、压缩总结不界定轮（2026-10-20）：预览里接在前一轮下面；解析时跟着那一轮的组；压缩总结只留标记。
        宿主消息后回复不超过 HOST_SKIP 字的默认不显示：预览不列、程序判不读、读的时候不带上；超过 HOST_FLOOR 字的至少粗读。"""
        from . import label
        def row(i, user, origin="human", kind="?n/a", reply="回复"):
            return {"ordinal": i, "user": user, "assistant": reply, "commentary": "[]", "tools": "{}", "closed": 1,
                    "origin": origin, "host_kind": kind}
        note = "<task-notification>\n<task-id>b</task-id>\n<summary>Background command \"跑\" completed</summary>\n</task-notification>"
        long = "汇" * 150
        turns = [row(0, "跑一下实验"), row(1, note, "host", "notification", "结果" * 500), row(2, note, "host", "notification", long),
                 row(3, label.COMPACTION + " 总结……", "host", "compaction", long), row(4, "好的"),
                 row(5, note, "host", "notification", "还在跑")]
        self.assertEqual(preview.folds(turns), {2: 1, 3: 1, 4: 1, 6: 5})
        restart = [row(0, "改一下", reply=""), row(1, label.RESTART + ": …", "host", "restart", long)]   # Orca 重启续接（2026-11-06）
        self.assertEqual(preview.folds(restart), {2: 1})
        self.assertIn(f"（第 2 轮：宿主转来{label.RESTART_MARK}；我接着回复 150 字", preview.build("提示词", "## 环境", restart, 0))
        self.assertEqual(preview.hidden(turns), {6})
        text = preview.build("提示词", "## 环境", turns, 0)
        self.assertIn("## 第 1 轮（连同第 2–4 轮）", text)
        self.assertIn("## 第 5 轮\n", text)
        self.assertNotIn("第 6 轮", text)                                   # 不显示
        self.assertNotIn("## 第 2 轮", text)
        self.assertIn('（第 2 轮：宿主转来后台任务通知：Background command "跑" completed；我接着回复 1000 字', text)
        self.assertIn("（第 4 轮：上下文压缩，宿主插入了之前的“我”写的总结（内容略）；我接着回复 150 字", text)
        self.assertNotIn("总结……", text)
        groups, info = preview.parse("### 第 1 轮\n类型：实验\n读法：粗读\n\n### 第 5 轮\n类型：确认\n读法：粗读\n", turns)
        self.assertEqual([(g["from"], g["to"], g["how"], g["hidden"]) for g in groups],
                         [(1, 4, "粗读", []), (5, 6, "粗读", [6]), (6, 6, "不读", [])])
        self.assertTrue(groups[2]["program"])
        self.assertEqual(info["missing"], [])
        self.assertEqual(groups[0]["chars"], 2 + 1000 + 150 + 150)
        # 预览把接续的通知单独写成一组（Sol 2026-10-20）：并回前一轮的组
        groups, _ = preview.parse("### 第 1 轮\n读法：粗读\n\n### 第 2–5 轮\n读法：粗读\n", turns)
        self.assertEqual([(g["from"], g["to"]) for g in groups], [(1, 4), (5, 6), (6, 6)])
        # 宿主消息后回复超过 HOST_FLOOR 字：至少粗读
        turns[1] = row(1, note, "host", "notification", "结果" * 501)
        groups, _ = preview.parse("### 第 1 轮\n读法：不读\n\n### 第 5 轮\n读法：不读\n", turns)
        self.assertEqual([g["how"] for g in groups], ["粗读", "不读", "不读"])
        self.assertEqual(label.user_text(label.COMPACTION + " 总结\n\n第二段")[0], label.COMPACTION_MARK)
        self.assertIn("第二段", label.user_text(label.COMPACTION + " 总结\n\n第二段", compaction=False)[0])

    def test_rough_split_and_checks(self):
        """粗读（2026-10-19）：超过写窗口按轮切开；附上读写的文件；说话人可疑、指路没有出处由程序标出。"""
        from . import deep_rough_read as drr, written_files
        rows = [{"ordinal": i, "occurrence": 100 + i, "assistant": "回" * n, "commentary": "[]"} for i, n in enumerate((1500, 1500, 3000, 2000, 1000, 1000))]
        g = {"from": 1, "to": 6, "turns": [100 + i for i in range(6)], "how": "粗读", "plan": "计划"}
        parts = drr.split(g, rows)             # 共 10000 字：切两段，最接近 5000 的边界在第 3 轮后（6000 / 4000）
        self.assertEqual([(x["from"], x["to"], x["part"]) for x in parts], [(1, 3, "1/2"), (4, 6, "2/2")])
        self.assertEqual(drr.split(g, rows[:2]), [g])                  # 不超过 5000 不切
        big = [{"ordinal": i, "occurrence": 100 + i, "assistant": "回" * 1000, "commentary": "[]"} for i in range(13)]
        sizes = [sum(1000 for _ in p["turns"]) for p in drr.split({**g, "turns": [r["occurrence"] for r in big]}, big)]
        self.assertEqual(sizes, [4000, 5000, 4000])                    # 13000 字：三段，平均，不是 4000+4000+4000+1000
        self.assertEqual(drr.amount({1: {"U1": "短", "A1": "长" * 40, "A2": "长" * 50}}), (2, 4))        # 用户说了话至少算 1 条
        self.assertEqual(drr.amount({1: {f"A{i}": "长" * 40 for i in range(10)}}), (7, 13))
        about = {1: {**{f"A{i}": "长" * 40 + "见 a.py" for i in range(4)}, **{f"A{i}": "长" * 40 for i in range(4, 10)}}}
        self.assertEqual(drr.amount(about, {1: {"read": ["mh/a.py"]}}), (5, 9))         # 提到附上文件的 4 段合算 1 条指路，其余 6 段照常（2026-10-25）
        self.assertEqual(drr.amount({1: {f"A{i}": "长" * 40 for i in range(10)}}, {1: {"read": ["a.py"]}}), (7, 13))   # 读过文件但段落没提到：照常算
        self.assertEqual(drr.amount({1: {"A1": "短"}}, {1: {"read": ["a.py"]}}), (1, 2))
        self.assertEqual(drr.amount({1: {f"A{i}": "长" * 40 for i in range(200)}}), (37, 69))   # 单轮自成一窗：中心不超过 LONG_CAP
        self.assertEqual(drr.amount({1: {f"A{i}": "长" * 40 for i in range(40)}, 2: {f"A{i}": "长" * 40 for i in range(40)}}), (56, 104))   # 多轮不受限  # 只读了文件、没有有内容的段落：1 条指路
        self.assertEqual(drr.file_names({"read": ["tmp/x/", "mh/engram/observe/"], "wrote": ["写了 a/b.md", "提交 1a2b「runbook：改 c.py」：d/e.py、f/g.md"]}),
                         {"observe", "b.md", "e.py", "g.md"})                      # 太短的目录名（x）不认；提交说明里的不算
        self.assertEqual(written_files.bash_reads("cd tmp/x; sed -n 1,40p a.py && cat $f-v0.1.md | head"), ["tmp/x", "a.py"])
        with tempfile.TemporaryDirectory() as d:                                   # 碰过：heredoc、变量赋值、文件参数里的绝对路径都算（2026-10-26）
            log = Path(d) / "s.jsonl"
            uses = [{"type": "tool_use", "id": "1", "name": "Bash", "input": {"command": "D=~/mh/tmp/x; cat > ~/mh/a.md <<'EOF'\nhi\nEOF\ncd /srv/w && ls"}},
                    {"type": "tool_use", "id": "2", "name": "Read", "input": {"file_path": "/etc/hosts"}}]
            log.write_text(json.dumps({"timestamp": "2026-10-26T01:00:00Z", "message": {"content": uses}}) + "\n")
            self.assertEqual(written_files.touched("claude", str(log)),
                             {written_files.HOME_DIR + "/mh/tmp/x", written_files.HOME_DIR + "/mh/a.md", "/srv/w", "/etc/hosts"})
            log.write_text(json.dumps({"timestamp": "2026-10-26T01:00:00Z", "message": {"content": [   # 相对路径：按命令里前面的 cd，没有就按 cwd
                {"type": "tool_use", "id": "3", "name": "Bash", "input": {"command": "sed -n 1p design/a.md; cd /srv/w && python3 x/b.py"}}]}}) + "\n")
            self.assertEqual(written_files.touched("claude", str(log), "/home/u/p"), {"/home/u/p/design/a.md", "/srv/w", "/srv/w/x/b.py"})
            js = ('text(await tools.exec_command({cmd:"sed -n 1p design/a.md",workdir:"/srv/w","max_output_tokens":4000}));\n'
                  'text(await tools.apply_patch("*** Begin Patch\\n*** Update File: mh/b.py\\n@@\\n-x\\n+y\\n*** Add File: /srv/c.md\\n+z\\n*** End Patch"));\n'
                  'text(await tools.exec_command({cmd: c}));')                     # codex：exec 里的 JS（2026-10-26）；变量拼的命令认不出
            rows = [{"timestamp": "2026-10-26T01:00:00Z", "type": "turn_context", "payload": {"cwd": "/home/u/p"}},
                    {"timestamp": "2026-10-26T01:00:01Z", "type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c1", "input": js}}]
            log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            uses, _ = written_files._codex(str(log))
            self.assertEqual(sorted((v[1], v[2].get("file_path") or v[2].get("command")) for v in uses.values()),
                             [("Bash", "cd /srv/w && sed -n 1p design/a.md"), ("Write", "/home/u/p/mh/b.py"), ("Write", "/srv/c.md")])
            self.assertEqual(written_files.touched("codex", str(log), "/home/u/p"), {"/srv/w", "/srv/w/design/a.md", "/home/u/p/mh/b.py", "/srv/c.md"})
        self.assertEqual(drr.file_note({"read": ["a.py", "tmp/x/"], "wrote": []}), ["这一轮“我”读过：a.py、tmp/x/"])
        events = [{"text": f"{U}说明脚本的四步。", "segments": ["8:A1"]}, {"text": f"{U}决定先做 A。", "segments": ["8:U1"]},
                  {"text": "我说明了做法，细节见当轮设计材料。", "segments": ["8:A2"]}, {"text": "我在讨论中提出先做 B。", "segments": ["8:A2"]}]
        tables = {8: {"U1": "x", "A1": "y", "A2": "z"}}
        events.append({"text": f"{U}指出顺序错了；我承认这是根本问题。", "segments": ["8:U1", "8:A2"]})
        self.assertEqual(drr.checks(events, tables, {}), {"speaker_suspect": [0], "pointer_without_file": [2], "mixed_speaker": [4]})
        self.assertEqual(drr.checks(events, tables, {8: {"read": ["a.py"], "wrote": []}})["pointer_without_file"], [])

    def test_condense_notification(self):
        """后台通知只留主要信息（2026-10-20）：摘要、状态、Monitor 输出（截短）；id、输出文件、固定说明不给；段落数不变。"""
        from . import label
        from ..store import blocks
        raw = ("<task-notification>\n<task-id>b1</task-id>\n<tool-use-id>toolu_x</tool-use-id>\n<output-file>/tmp/x.output</output-file>\n"
               "<status>completed</status>\n<summary>Background command \"跑对照\" completed (exit code 0)</summary>\n</task-notification>")
        self.assertEqual(label.user_text(raw)[0], '后台任务通知（completed）：Background command "跑对照" completed (exit code 0)')
        ev = ("<task-notification>\n<task-id>b2</task-id>\n<summary>Monitor event: \"批次\"</summary>\n<event>" + "错" * 400
              + "</event>\n</task-notification>")
        got = label.user_text(ev)[0]
        self.assertTrue(got.startswith('后台任务通知：Monitor event: "批次"；输出：错'))
        self.assertTrue(got.endswith("……（共 400 字）"))
        self.assertEqual(len(blocks.split(got)), len(blocks.split(label.strip_ide(ev)[0])))
        self.assertEqual(label.user_text(f"{U}的话")[0], f"{U}的话")

    def test_rough_split_progress(self):
        """粗读的组超过写窗口切开读，读完以后这几轮都算读过，不挡上一层（2026-10-19：决定原来按整组查，切开读的永远是 to_read）。"""
        self._collect([Turn("一", "回" * 3000, closed=True), Turn("二", "回" * 3000, closed=True), Turn("三", "回" * 3000, closed=True)])
        fakes = self._fakes(["### 第 1–3 轮\n类型：执行\n读法：粗读\n"], [],
                            [f"### 事件1\n{U}说了一。\n来源：1:U1", f"### 事件1\n{U}说了三。\n来源：3:U1"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        self.assertEqual(len([a for a in self._acts() if a["step"] == "rough"]), 2)       # 9000 字：切成两段
        self.assertEqual(set(states(self.conn).values()), {"read"})
        self.assertEqual(waiting(self.conn), [])

    def test_label(self):
        from . import label
        from .label import kind, relayed, strip_ide
        self.assertEqual(kind("<task-notification>x"), "通知")
        self.assertEqual(kind("[Request interrupted by user]"), "打断")
        self.assertIsNone(kind("帮我看看"))
        restart = ("Orca restarted, which stopped what you were doing: a reply in progress, any subagents or background commands "
                   "you had running, and any request waiting on the user. Before continuing, check what finished before the restart "
                   "and don't repeat it. Restart anything still needed, ask again for anything still waiting on the user, then carry on.")
        self.assertEqual(kind(restart), "重启")
        self.assertEqual(label.user_text(restart)[0], label.RESTART_MARK)      # 正文不给，原文一段、标记一段
        self.assertIn("Orca 重启后自动发来的续接提示", label.notes(restart, "好", True)[0])
        self.assertTrue(label.notes("改一下", "", False, nxt=restart)[0].endswith("Orca 重启了，下一轮是重启后的续接。"))
        self.assertEqual(strip_ide("<ide_opened_file>The user opened the file /a/b.py in the IDE.</ide_opened_file>改一下")[0], "改一下")
        pasted = "看看这个：\n# 方案\n## 一\n" + "- 要点\n" * 10 + "正文" * 600
        self.assertEqual(relayed(pasted), "看看这个：")
        self.assertIsNone(relayed("短的一句"))
        self.assertIsNone(relayed("没有结构的长文本" * 300))

    def test_language(self):
        from .label import USER_NAME, language, language_note
        en = "Observation is effectively finished. The only thing left is one session that kept timing out on the first try, so I will check it again later."
        zh = "我刚才看了一下，那边的额度快用完了还没跑完，不过基本马上观察完了，你检查一下结果吧。"
        self.assertEqual(language(en), "英文")
        self.assertEqual(language(zh), "中文")
        self.assertEqual(language("せーのっ！でもそんなんじゃだめ、もうそんなんじゃほら、心は進化するよもっともっと"), "日文")
        self.assertIsNone(language("ok"))                                   # 太短
        # 按骨架认，不按字数：英文名词再多，谓语和虚词是中文的就是中文；反过来也一样
        self.assertEqual(language("我把 pattern-based formatted Formation replay pipeline 停了，因为 observation service 还在跑，所以先等一下再看"), "中文")
        self.assertEqual(language("I stopped the 格式化形成回放流程 because the 观察服务和整理服务 is still running, so we can wait for it"), "英文")
        self.assertEqual(language("A1、A3 已经完成并提交了，接着做 A6，把旧的段撤掉"), "中文")      # A1 不是冠词
        self.assertEqual(language("[执行清单](a.md) 已经改好了，之前的判断错了，我把原因也写上了"), "中文")   # 链接开头不是 JSON
        self.assertIsNone(language('{"risk_level": "low", "outcome": "allow", "rationale": "' + en + '"}'))   # JSON
        self.assertEqual(language(zh + "\n```\n" + en * 5 + "\n```"), "中文")      # 代码块不算
        log = "\n".join(f"drwxr-xr-x 2 me me 4096 Oct 26 path=/home/me/x{i}" for i in range(30))
        self.assertIsNone(language(log, pasted=True))                       # 贴进来的日志行
        self.assertEqual(language_note(zh, en), f"这一轮{USER_NAME}用中文说，我换成英文回复（上一轮用中文）。")
        self.assertEqual(language_note(zh, en, before=en), f"这一轮{USER_NAME}用中文说，我用英文回复。")
        self.assertEqual(language_note(zh, zh, before=en), "这一轮我换成中文回复（上一轮用英文）。")
        self.assertEqual(language_note("<task-notification>done</task-notification>", en, before=en), "这一轮我用英文回复。")
        self.assertEqual(language_note(log + "\n" + en, zh), None)          # 贴了日志的不判用户这一侧
        self.assertIsNone(language_note(zh, zh))
        self.assertIsNone(language_note(zh, zh, before="ok"))               # 上一轮判不出：不说换
        self.assertIsNone(language_note(zh, en, before=en, steady=False))   # 接着用英文：form 那边不写
        self.assertEqual(language_note(zh, en, steady=False), f"这一轮{USER_NAME}用中文说，我换成英文回复（上一轮用中文）。")

    def test_collect(self):
        turns = [Turn("你好", "回答", closed=True), Turn("<task-notification>完成</task-notification>", "收到", closed=True),
                 Turn("（定时任务）巡检", "好", closed=False), Turn("还在写", "", closed=False)]
        n = self._collect(turns)["dsh"]
        self.assertEqual(n["turns"], 3)                    # 末尾没结束的一轮留到下次
        rows = self.conn.execute("SELECT origin, host_kind, closed, ending, prev FROM turn_detail ORDER BY ordinal").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("human", NA, 1, "normal", UNKNOWN), ("host", "notification", 1, "normal", UNKNOWN),
                                                    ("script", NA, 0, "normal", UNKNOWN)])
        # 再采：第 3 轮来源标了结束 → 更正；同一位置内容变了 → 不写
        turns[2] = Turn("（定时任务）巡检", "好", closed=True, ending="aborted:user")
        turns[0] = Turn("你好（改过）", "回答", closed=True)
        n = self._collect(turns)["dsh"]
        self.assertEqual((n["turns"], n["fixed"]), (0, 2))
        self.assertEqual(self.conn.execute("SELECT closed, ending FROM turn_detail WHERE ordinal=2").fetchone()[:], (1, "aborted:user"))
        self.assertEqual(self.conn.execute("SELECT text FROM turn_detail WHERE ordinal=0").fetchone()[0], Turn("你好", "回答").id)

    def test_clean_empty_sessions(self):
        """清理空会话（2026-11-06）：没有用户的话、也没有回答的主会话删掉原文件；用户说了话没等到回答的、
        末尾那轮还没入库的、原文里有回答的（解析器漏认了用户的话）、最近动过的都不删；codex 压成 .zst 的照样认；宿主开着的照删（用户 11-06）；删掉的记一笔。"""
        from unittest.mock import patch
        from . import clean
        files, parsed = {}, {}
        def add(sid, turns, kind="user"):
            f = self.dir / f"{sid}.jsonl"
            f.write_text("x")
            files[sid] = f
            sess = Session("codex", sid, str(f), kind=kind, started="2026-01-01T00:00:00Z", cwd="/w")
            parsed[str(f)] = (sess, turns)
            collect.run(self.conn, sources=("codex",), force=True, log=io.StringIO(), modules={"codex": FakeSource(f, sess, turns)})
        add("empty", [])
        add("command", [Turn("<command-name>/model</command-name>", "", closed=True)])
        add("asked", [Turn("帮我看看", "", closed=True)])                        # 说了话、没等到回答
        add("pending", [Turn("<command-name>/model</command-name>", "", closed=True), Turn("刚说", "")])   # 末尾一轮没入库
        add("sub", [], kind="subagent")
        add("open", [])
        add("goal", [])                                                           # 解析器没认出用户的话，原文里却有回答
        files["goal"].write_text('{"payload": {"type": "message", "role": "assistant"}}')
        add("zipped", [])                                                         # codex 后来压成了 .zst，库里记的是压缩前的路径
        subprocess.run(["zstd", "-q", "--rm", str(files["zipped"])], check=True)
        fake = type("M", (), {"parse": staticmethod(lambda path: parsed.get(str(path), (None, [])))})
        now = max(f.stat().st_mtime for f in self.dir.glob("*.jsonl*")) + clean.AGE + 1
        record = self.dir / "cleaned.jsonl"
        with patch.dict(clean.SOURCES, {"codex": fake}):
            self.assertEqual(clean.clean(self.conn, now=files["empty"].stat().st_mtime + 60, record=record), [])   # 最近动过
            got = clean.clean(self.conn, apply=True, now=now, record=record)
        self.assertEqual(sorted(s for _, s, _ in got), ["command", "empty", "open", "zipped"])
        self.assertEqual(sorted(f.name for f in self.dir.glob("*.jsonl")), ["asked.jsonl", "cleaned.jsonl", "goal.jsonl", "pending.jsonl", "s.jsonl", "sub.jsonl"])
        self.assertEqual(len(record.read_text().splitlines()), 4)
        self.assertFalse(list(self.dir.glob("*.zst")))

    def test_collect_restart(self):
        """Orca 重启后的固定续接提示（2026-11-06）是宿主注入；程序新认得以前当成 human 的轮，再采时改过来（只从 human 改出去）。"""
        from . import label
        turns = [Turn("改一下", "好", closed=True), Turn(label.RESTART + ": a reply in progress… then carry on.", "接着改完了", closed=True)]
        self._collect(turns)
        get = lambda: [tuple(r) for r in self.conn.execute("SELECT origin, host_kind FROM turn_detail ORDER BY ordinal")]
        self.assertEqual(get(), [("human", NA), ("host", "restart")])
        self.conn.execute("UPDATE turn_detail SET origin='human', host_kind=? WHERE ordinal=1", (NA,))
        self.assertEqual(self._collect(turns)["dsh"]["fixed"], 1)
        self.assertEqual(get(), [("human", NA), ("host", "restart")])
        self.assertEqual(self._collect(turns)["dsh"]["fixed"], 0)

    def test_collect_main_only(self):
        """只采主会话（用户 2026-11-06）：子代理、自动审查不入库，文件照样记下处理过，下一轮不再解析。"""
        f = self.dir / "sub.jsonl"
        f.write_text("x")
        sess = Session("codex", "sub", str(f), kind="subagent", started="2026-01-01T00:00:00Z", cwd="/w")
        got = collect.run(self.conn, sources=("codex",), force=True, log=io.StringIO(),
                          modules={"codex": FakeSource(f, sess, [Turn("做", "做完了", closed=True)])})
        self.assertEqual((got["codex"]["files"], got["codex"]["turns"]), (1, 0))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM session_detail").fetchone()[0], 0)

    def test_codex_goal(self):
        """codex 的 goal 模式（2026-11-06）：用户的话是 thread_goal_updated 里的目标，包在 <codex_internal_context source="goal"> 里的
        开工提示不是用户写的；目标设下后第一次开工起一轮，之后的“继续朝目标做”接在原来那一轮里，不起新的一轮；改了目标再起一轮。"""
        from .sources import codex
        block = {"type": "input_text", "text": '<codex_internal_context source="goal">\nContinue working toward the active thread goal.\n'
                                                '<objective>\nx\n</objective>\n</codex_internal_context>'}
        ev = lambda typ, **kw: {"type": "event_msg", "payload": {"type": typ, **kw}}
        user = {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [block]}}
        say = lambda text: {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                                                                 "content": [{"type": "output_text", "text": text}]}}
        lines = [{"type": "session_meta", "payload": {"id": "g1", "timestamp": "2026-09-15T19:48:44Z", "cwd": "/w"}},
                 ev("thread_goal_updated", goal={"objective": "修正设计", "status": "active"}),
                 user, say("先读报告"), ev("task_complete"),
                 user, say("改好了"), ev("task_complete"),
                 ev("thread_goal_updated", goal={"objective": "修正设计", "status": "complete"}),     # 只改状态：不算新目标
                 ev("thread_goal_updated", goal={"objective": "再写测试", "status": "active"}),
                 user, say("写好了"), ev("task_complete")]
        path = self.dir / "goal.jsonl"
        path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
        _, turns = codex.parse(path)
        self.assertEqual([(t.user, t.assistant) for t in turns], [("修正设计", "改好了"), ("再写测试", "写好了")])
        self.assertEqual(turns[0].commentary, ["先读报告"])

    def test_zeron_title(self):
        """Zeron 起标题另起的调用（2026-11-07）：唯一一轮是起标题的提示词，记成 kind = title，采集不收。"""
        from .sources import claude
        lines = [
            {"type": "user", "uuid": "u1", "cwd": "/tmp/.tmpAbc", "entrypoint": "zeron",
             "message": {"role": "user", "content": "You generate session titles. Treat the supplied session request as quoted data."}},
            {"type": "assistant", "uuid": "a1", "message": {"id": "m1", "role": "assistant", "stop_reason": "end_turn",
                                                             "content": [{"type": "text", "text": "Some Title"}]}},
        ]
        path = self.dir / "title.jsonl"
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        sess, turns = claude.parse(path)
        self.assertEqual((sess.kind, len(turns)), ("title", 1))
        self.assertEqual(collect.save(self.conn, collect.collector(self.conn), sess, turns), (0, 0, 0))
        lines[0]["message"]["content"] = "帮我 generate session titles"           # 只认开头
        path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
        self.assertEqual(claude.parse(path)[0].kind, "user")

    def test_peer(self):
        """别的会话发来的消息（2026-11-01）：不是用户的话，也不成轮。解析时认出来记在 asides：夹在一轮中间的记这一轮，
        落在两轮之间的记前一轮、连同“我”接着的回答；采集存进库、只往后添，读的时候只读库（原文件不保证一直在）；
        观察时排在 U 后面、写明是谁，轮后的回答排在 A 最后。"""
        from .sources import claude
        from . import deep_rough_read as drr, label, participants
        self.assertTrue(participants.is_peer(peer_text()))
        self.assertEqual((participants.name(peer_text()), participants.body(peer_text())), ("迁移", "把包挪到 mh/engram。"))
        meta = lambda uuid, text, **kw: {"type": "user", "uuid": uuid, "isMeta": True, "message": {"role": "user", "content": text}, **kw}
        say = lambda uuid, mid, text, **kw: {"type": "assistant", "uuid": uuid, "message": {
            "id": mid, "role": "assistant", "content": [{"type": "text", "text": text}], **kw}}
        lines = [
            {"type": "user", "uuid": "u1", "message": {"role": "user", "content": "先停一下"}},
            meta("p1", peer_text("中间插一句"), origin={"kind": "peer", "name": "迁移"}),
            say("a1", "m1", "好的", stop_reason="end_turn"),
            {"type": "system", "subtype": "turn_duration", "uuid": "d1"},
            meta("p2", peer_text()),
            say("a2", "m2", "挪好了"),
            meta("x1", "（别的宿主注入，不算输入）"),
            {"type": "user", "uuid": "p3", "message": {"role": "user", "content": peer_text("没写 isMeta")}},   # 宿主版本不同也认
            {"type": "user", "uuid": "u2", "parentUuid": "d1", "message": {"role": "user", "content": "拉回来"}},
            say("a3", "m3", "回来了", stop_reason="end_turn"),
            {"type": "system", "subtype": "turn_duration", "uuid": "d2"},
            say("a4", "m4", "没有归属的一句"),                    # 结束之后、没有会话消息：不出错，也不记
        ]
        path = self.dir / "peer.jsonl"
        path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
        _, turns = claude.parse(path)
        self.assertEqual([(t.user, t.assistant) for t in turns], [("先停一下", "好的"), ("拉回来", "回来了")])
        who = "另一个会话「迁移」"
        self.assertEqual(turns[0].asides, [{"who": who, "text": "中间插一句"},
                                           {"who": who, "text": "把包挪到 mh/engram。", "after": True, "reply": "挪好了"},
                                           {"who": who, "text": "没写 isMeta", "after": True, "reply": ""}])
        self.assertEqual(turns[1].asides, [])
        self.assertEqual(turns[0].id, Turn("先停一下", "好的").id)          # 不进哈希
        # 采集：2026-11-01 前入库的行没有 asides，再收一次补上；只往后添，已有的不改
        sess = Session("claude", "p", str(path), started="2026-01-01T00:00:00Z", cwd="/w")
        run = lambda ts: collect.run(self.conn, sources=("claude",), force=True, log=io.StringIO(),
                                     modules={"claude": FakeSource(path, sess, ts)})["claude"]["fixed"]
        run([Turn("先停一下", "好的", closed=True)])
        self.assertEqual(run([Turn("先停一下", "好的", closed=True, asides=turns[0].asides[:1])]), 1)
        self.assertEqual(run([turns[0]]), 1)
        self.assertEqual(run([Turn("先停一下", "好的", closed=True, asides=[{"who": "别的", "text": "改了"}] * 4)]), 0)
        path.unlink()
        row = self.conn.execute("SELECT d.*, x.user, x.assistant, x.commentary, x.tools FROM turn_detail d "
                                "JOIN turn_text x ON x.id=d.text WHERE d.session='p'").fetchone()
        self.assertEqual(label.asides(row), turns[0].asides)
        _, block = drr.turn_blocks(row)
        self.assertIn(f"这一轮中间夹着{who}发来的 1 条消息，排在 U2，不是{U}的话", block)
        self.assertIn(f"这一轮结束之后，{who}又发来 2 条消息，排在 U3、U4，不是{U}的话；“我”对它们的回答排在 A2", block)
        self.assertIn(f"[1:U2] （{who}发来消息，不是用户的话）中间插一句", block)
        self.assertIn(f"[1:A2] （这一轮结束之后“我”对{who}的回答）挪好了", block)
        line = preview.turn_line(row)
        self.assertIn(f"（这一轮中间夹着{who}发来的 1 条消息", line)
        self.assertIn(f"（这一轮结束之后又有{who}发来的 2 条消息，不是{U}的话，我回了 3 字", line)

    def _fakes(self, preview, deep=(), rough=()):
        return {"preview": FakeModel(preview, usage={"input_tokens": 5, "output_tokens": 6}),
                "deep": FakeModel(deep), "rough": FakeModel(rough)}

    def _acts(self):
        return self.conn.execute("SELECT o.*, a.step, a.window, a.input, a.meta FROM occurrence o JOIN act_detail a "
                                 "ON a.occurrence=o.id ORDER BY o.id").fetchall()

    def test_observe(self):
        self._collect([Turn("先做一版看看", "好的", closed=True), Turn("结果呢", "结果在 /home/someone/x/report.md 里", closed=True),
                       Turn("再跑一遍", "跑完了" * 20, closed=True), Turn("好的", "嗯", closed=True)])
        fakes = self._fakes([PLAN], [READ], [TimeoutError("超时"), "### 事件1\n我跑完了。\n来源：3:A1"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        acts = self._acts()
        self.assertEqual([(a["step"], a["outcome"]) for a in acts], [("preview", "ok"), ("deep", "ok"), ("rough", "timeout")])
        self.assertEqual(json.loads(acts[0]["cost"])["tokens"], {"input": 5, "output": 6, "cache_read": 0, "cache_write": 0})   # claude -p 的 usage
        for a in acts:
            import hashlib
            ex = json.loads(a["meta"])["expression"]
            self.assertEqual(ex["text_hash"], hashlib.sha256(a["input"].encode()).hexdigest())
            self.assertEqual(ex["user_name"], U)
            self.assertIn("## 表达规范", a["input"])
        segs = json.loads(acts[0]["meta"])["reads"]
        self.assertEqual([(x["from"], x["to"], x["how"]) for x in segs], [(1, 2, "精读"), (3, 3, "粗读"), (4, 4, "不读")])
        deep = acts[1]
        self.assertIn("mh-count", deep["input"])
        rough = acts[2]
        self.assertIn("这几轮写 1–3 条", rough["input"])                      # 条数范围由程序填：用户 1 条 + 我 1 段（2026-10-19）
        self.assertNotIn("{amount}", rough["input"])
        self.assertNotIn("/home/someone", deep["input"])                       # 路径打码后才给观察者
        turns = [r[0] for r in self.conn.execute("SELECT occurrence FROM turn_detail ORDER BY ordinal")]
        ev = self.conn.execute("SELECT * FROM event ORDER BY idx").fetchall()
        self.assertEqual([(e["turn"], e["text"], json.loads(e["segments"])) for e in ev],
                         [(turns[0], f"{U}提出可能先做一版看看。", ["1:U1"]), (turns[1], "我说明结果在 /home/someone/x/report.md 里。", ["2:A1"])])
        self.assertEqual({e["mode"] for e in ev}, {NOT_DONE})                # 三项由程序从正文读出，不存
        # 再跑：四轮都被预览覆盖了，不再预览；超时的那一组粗读再挑一次
        self.assertEqual(observe.ready(self.conn, observe.operator(self.conn)[0]), [])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        last = self._acts()[-1]
        self.assertEqual((last["step"], last["outcome"], last["predecessor"]), ("rough", "ok", acts[2]["id"]))
        self.assertEqual(observe.to_read(self.conn), [])
        self.assertEqual(len(fakes["preview"].outputs), 0)

    def test_ready(self):
        now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
        recent = "2026-10-17T11:50:00Z"
        self._collect([Turn("一", "答", closed=True, started="2026-10-17T11:00:00Z"), Turn("二", "答", closed=True, started=recent),
                       Turn("还在写", "", closed=False, started=recent)], closed_last=False)
        op = observe.operator(self.conn)[0]
        self.assertEqual(observe.ready(self.conn, op, now=now), [])           # 最后一轮 10 分钟前才开始：还没告一段落
        self.assertEqual(observe.ready(self.conn, op, now=now + timedelta(hours=2)), [])   # dsh 要等 3 小时
        later = now + timedelta(hours=3)
        (rows, earlier), = observe.ready(self.conn, op, now=later)
        self.assertEqual(([r["ordinal"] for r in rows], earlier), ([0, 1], 0))    # 没写出文字、没结束的不看
        rows = [Turn(f"第{i}", "答", closed=True, started="2026-10-17T11:00:00Z") for i in range(80)]
        self._collect(rows, sid="long")                                    # 轮数不设上限：80 轮整段一次
        self.assertEqual([len(w) for w, _ in observe.ready(self.conn, op, session="long", now=later)], [80])

    def test_preview_missing(self):
        """预览漏分组的轮次（2026-10-27，668f6657 的第 12–14 轮）：不算处理过，下一次照常预览；不然它永远待观测，挡住 form 的门槛。"""
        self._collect([Turn(f"第{i}", "答" * 50, closed=True) for i in range(4)])
        assert_live(self, self.conn)
        fakes = self._fakes(["### 第 1–2 轮\n类型：闲聊\n读法：不读\n\n### 第 4 轮\n类型：闲聊\n读法：不读\n",
                             "### 第 3 轮\n类型：闲聊\n读法：不读\n"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        acts = self._acts()                                                  # 同一批里接着把漏掉的那一轮单独预览
        self.assertEqual([(a["step"], json.loads(a["meta"])["missing"]) for a in acts], [("preview", [3]), ("preview", [])])
        self.assertEqual(json.loads(acts[1]["window"])["turns"], [self.conn.execute("SELECT occurrence FROM turn_detail WHERE ordinal=2").fetchone()[0]])
        self.assertEqual({p["state"] for p in observe.progress(self.conn)}, {"not_read"})
        self.assertEqual(observe.ready(self.conn, observe.operator(self.conn)[0]), [])

    def test_fast(self):
        """快道（runbook I7，2026-10-25 上线，11-06 撤）：没告一段落的会话不再读；库里已有的快道读照样接住：之后照常预览，
        判粗读的不再读、快道 E 是终稿，判精读的读完后快道 E 退场，判不读的收回。"""
        from unittest.mock import patch
        t0 = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
        at = lambda m: (t0 + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._collect([Turn("一", "回" * 3000, closed=True, started=at(0)), Turn("二", "回" * 1500, closed=True, started=at(5)),
                       Turn("三", "回" * 4100, closed=True, started=at(10)), Turn("四", "回" * 300, closed=True, started=at(15))],
                      started=at(0))
        turns = [r[0] for r in self.conn.execute("SELECT occurrence FROM turn_detail ORDER BY ordinal")]
        observe.run(self.conn, workers=1, models=self._fakes([], [], []), log=io.StringIO(), now=t0 + timedelta(minutes=20))
        self.assertEqual(self._acts(), [])                                                                 # 没告一段落：不读
        op = observe.operator(self.conn, "rough")[0]
        for ts, out in ((turns[:2], f"### 事件1\n{U}说了一。\n来源：1:U1\n\n### 事件2\n{U}说了二。\n来源：2:U1"),
                        ([turns[2]], f"### 事件1\n{U}说了三。\n来源：3:U1")):                   # 撤之前快道读下的
            rows = self.conn.execute(observe.TURNS + f" WHERE d.occurrence IN ({','.join('?' * len(ts))}) ORDER BY d.ordinal", ts).fetchall()
            group = {"turns": ts, "from": rows[0]["ordinal"] + 1, "to": rows[-1]["ordinal"] + 1, "how": "粗读", "plan": "计划"}
            tables, mask, text = observe.build_read(self.conn, group, "rough", FakeModel([]))
            real = observe._act
            with patch.object(observe, "_act", lambda conn, op_id, step, window, *a, **k: real(conn, op_id, step, {**window, "fast": True}, *a, **k)):
                observe.store_read(self.conn, op, None, group, "rough", tables, mask, text, out, {}, None, {})
        self.assertEqual(len(observe.frame(self.conn)["events"]), 3)
        self.assertEqual(set(states(self.conn).values()), {"not_previewed"})                               # form 照样等预览
        # 告一段落：照常预览。第 1 轮精读、第 2 轮粗读（快道读过，不再读）、第 3 轮不读、第 4 轮粗读
        plan = "### 第 1 轮\n类型：讨论\n读法：精读\n\n### 第 2 轮\n类型：执行\n读法：粗读\n\n### 第 3 轮\n类型：执行\n读法：不读\n\n" \
               "### 第 4 轮\n类型：执行\n读法：粗读\n\n### 精读顺序\n第 1 轮\n"
        fakes = self._fakes([plan], [f"### 事件1\n{U}细说了一。\n来源：1:U1"], [f"### 事件1\n{U}说了四。\n来源：4:U1"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO(), now=t0 + timedelta(hours=4))
        rough = [json.loads(a["window"])["turns"] for a in self._acts() if a["step"] == "rough" and not json.loads(a["window"]).get("fast")]
        self.assertEqual(rough, [[turns[3]]])                                                                # 第 2 轮没有再读
        self.assertEqual(sorted(e["text"] for e in observe.frame(self.conn)["events"]),
                         sorted([f"{U}细说了一。", f"{U}说了二。", f"{U}说了四。"]))     # 一：精读取代；三：收回；二：快道 E 留着
        self.assertEqual(self.conn.execute("SELECT count(*) FROM event").fetchone()[0], 5)                   # 退场的不删
        self.assertEqual(states(self.conn), dict(zip(turns, ["read", "read", "not_read", "read"])))
        self.assertEqual(waiting(self.conn), [])
        assert_live(self, self.conn)

    def test_preview_pool(self):
        """预览按给的先后用池里的模型（2026-10-20）：前一个环境错误就换下一个，同一窗口接着预览；各是一个 Operator。"""
        self._collect([Turn("一", "答", closed=True), Turn("二", "答", closed=True)])
        sol = FakeModel([RuntimeError("You've hit your usage limit")])
        sonnet = FakeModel(["### 第 1–2 轮\n类型：闲聊\n读法：不读\n"])
        saved = dict(observe.MODELS)
        observe.MODELS.update(sol=lambda: sol, sonnet=lambda: sonnet)
        try:
            log = io.StringIO()
            done, env = observe.run(self.conn, workers=1, models={"deep": FakeModel([]), "rough": FakeModel([])}, log=log,
                                    order=["sol", "sonnet"])
            ops = observe.pool_ops(self.conn)
        finally:
            observe.MODELS.clear()
            observe.MODELS.update(saved)
        self.assertEqual(env, 0)
        self.assertIn("换 sonnet", log.getvalue())
        acts = [(x["outcome"], x["operator"]) for x in self._acts() if x["step"] == "preview"]
        self.assertEqual(acts, [("env_error", ops["sol"]), ("ok", ops["sonnet"])])

    def test_operator_window(self):
        """IDLE 决定预览看哪些轮，计入预览的 Operator：改了就是新的 Operator。"""
        a = observe.operator(self.conn)
        saved, observe.IDLE_DEFAULT = observe.IDLE_DEFAULT, observe.IDLE_DEFAULT + 1
        try:
            b = observe.operator(self.conn)
        finally:
            observe.IDLE_DEFAULT = saved
        self.assertNotEqual(a, b)
        self.assertEqual(observe.operator(self.conn), a)
        params = json.loads(self.conn.execute("SELECT params FROM operator WHERE id=?", (b[0],)).fetchone()[0])
        self.assertEqual(params["idle_default"], saved + 1)
        self.assertNotIn("max_window", params)

    def test_hidden_turns(self):
        """宿主消息后回复不超过 HOST_SKIP 字的轮默认不显示（2026-10-20）：全是这种轮的窗口不调模型；模型分的组跨过它们，
        读的时候不带上；进度里算不读。"""
        note = "<task-notification>\n<summary>Background command \"跑\" completed</summary>\n</task-notification>"
        self._collect([Turn(note, "跑着呢", closed=True)], sid="only")
        self._collect([Turn("跑实验", "好", closed=True), Turn(note, "还在跑", closed=True), Turn("看看", "结果" * 100, closed=True)])
        fakes = self._fakes(["### 第 1–3 轮\n类型：实验\n读法：粗读\n"], rough=["### 事件1\n我记下了结果。\n来源：1:A1\n"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        acts = [x for x in self._acts() if x["step"] in ("preview", "rough")]
        self.assertEqual([(x["step"], x["outcome"]) for x in acts], [("preview", "ok"), ("preview", "ok"), ("rough", "ok")])
        self.assertEqual(fakes["preview"].outputs, [])                       # 只调了一次（s1），only 没调
        occ = {r[0]: r[1] for r in self.conn.execute("SELECT ordinal, occurrence FROM turn_detail WHERE session='s1'")}
        self.assertEqual(json.loads(acts[2]["window"])["turns"], [occ[0], occ[2]])
        states = {(p["session"], p["ordinal"]): p["state"] for p in observe.progress(self.conn)}
        self.assertEqual((states[("only", 0)], states[("s1", 1)], states[("s1", 0)]), ("not_read", "not_read", "read"))

    def test_reread(self):
        """补读（2026-10-20）：当时判了不读、按现在的规则要读的轮次，程序重算成粗读的组，照常粗读；只补一次，读过的不动。"""
        note = "<task-notification>\n<summary>Background command \"跑\" completed</summary>\n</task-notification>"
        self._collect([Turn("跑实验", "好", closed=True), Turn(note, "结果" * 600, closed=True), Turn(note, "还在跑", closed=True)])
        fakes = self._fakes(["### 第 1 轮\n类型：实验\n读法：粗读\n\n### 第 2–3 轮\n类型：通知\n读法：不读\n"],
                            rough=["### 事件1\n我记下了结果。\n来源：1:A1\n"])
        saved = preview.FOLD, preview.HOST_FLOOR
        preview.FOLD, preview.HOST_FLOOR = (), 10 ** 9           # 当时的规则：不并轮，没有下限
        try:
            observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        finally:
            preview.FOLD, preview.HOST_FLOOR = saved
        pid = [x["id"] for x in self._acts() if x["step"] == "preview"][0]
        self.assertEqual(observe.previews_of(self.conn, "dsh", "s1"), [pid])
        (g,) = observe.reread_groups(self.conn, pid)
        self.assertEqual((g["from"], g["to"], g["how"], g["asked"]), (2, 2, "粗读", "不读"))   # 逐轮：第 3 轮只是进度，不补
        self.assertIn("接在第 1 轮下面", g["plan"])
        (got,) = observe.reread(self.conn, [pid], "补回漏掉的")
        self.assertEqual(observe.reread(self.conn, [pid], "再来一次"), [])          # 只补一次
        self.assertEqual(observe.previews_of(self.conn, "dsh", "s1"), [pid])        # 补读不算模型的预览
        todo = observe.to_read(self.conn)
        self.assertEqual([(p, s["from"], s["to"], step) for p, s, step in todo], [(got[1], 2, 2, "rough")])

    def test_read_no_event(self):
        """既没有事件也没写“无”的输出是格式错误（审查 03，2026-10-28）：记 content_error，这组留着重试；写“无”的照常算读过。"""
        self._collect([Turn("先做一版看看", "好的", closed=True)])
        fakes = self._fakes(["### 第 1 轮\n类型：讨论\n读法：粗读\n"], rough=["处理完成，请查看结果。", "无"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        self.assertEqual([(a["step"], a["outcome"]) for a in self._acts()], [("preview", "ok"), ("rough", "content_error")])
        self.assertEqual([step for _, _, step in observe.to_read(self.conn)], ["rough"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        self.assertEqual(self._acts()[-1]["outcome"], "ok")
        self.assertEqual(observe.to_read(self.conn), [])

    def test_reread_old(self):
        """重读旧粗读组（2026-10-25）：同样的几轮按现在的做法再读，读过的轮次更早的 E 整轮被取代，form 只看到新 E。"""
        self._collect([Turn("先做一版看看", "好的", closed=True), Turn("结果呢", "结果在报告里", closed=True)])
        fakes = self._fakes(["### 第 1–2 轮\n类型：讨论\n读法：粗读\n"],
                            rough=[f"### 事件1\n{U}提出先做一版。\n来源：1:U1\n\n### 事件2\n我说明结果在报告里。\n来源：2:A1\n"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        old = [a for a in self._acts() if a["step"] == "rough"][0]
        old_e = [r[0] for r in self.conn.execute("SELECT id FROM event WHERE born=? ORDER BY id", (old["id"],))]
        w = json.loads(old["window"])
        rows = self.conn.execute(observe.TURNS + f" WHERE d.occurrence IN ({','.join('?' * len(w['turns']))}) ORDER BY d.ordinal",
                                 w["turns"]).fetchall()
        group = {"turns": w["turns"], "from": rows[0]["ordinal"] + 1, "to": rows[-1]["ordinal"] + 1, "how": "粗读", "plan": "计划"}
        tables, mask, text = observe.build_read(self.conn, group, "rough", FakeModel([]))
        op = observe.operator(self.conn, "rough")[0]
        out = (f"### 事件1\n{U}提出先做一版看看，理由是还没有现成的。\n来源：1:U1\n\n"
               f"### 事件2\n我说明结果在报告里，并给出两个数字。\n来源：2:A1\n")
        observe.store_read(self.conn, op, None, group, "rough", tables, mask, text, out, {}, None, {}, cause="manual",
                           reread=old["id"])
        new = [a for a in self._acts() if a["step"] == "rough"][-1]
        self.assertGreater(new["id"], old["id"])
        self.assertEqual(json.loads(new["window"])["reread"], old["id"])
        self.assertEqual(observe.superseded(self.conn), set(old_e))                       # 这几轮更早的 E 整轮被取代
        self.assertEqual([e["text"] for e in observe.frame(self.conn, "dsh", "s1")["events"]],
                         [f"{U}提出先做一版看看，理由是还没有现成的。", "我说明结果在报告里，并给出两个数字。"])
        self.assertEqual(observe.reread_reads(self.conn)[self.conn.execute(
            "SELECT text FROM turn_detail WHERE ordinal=0").fetchone()[0]], new["id"])

    def test_prune(self):
        """程序收回（2026-10-26）：Jev 判过没有必要的 E 不删，只是不再交给 form；用户段落不收；放回后照常交出。"""
        self._collect([Turn("先做一版看看", "好的", closed=True), Turn("结果呢", "结果在报告里", closed=True)])
        fakes = self._fakes(["### 第 1–2 轮\n类型：讨论\n读法：粗读\n"],
                            rough=[f"### 事件1\n{U}提出先做一版。\n来源：1:U1\n\n### 事件2\n我说明结果在报告里。\n来源：2:A1\n"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        e1, e2 = [r[0] for r in self.conn.execute("SELECT id FROM event ORDER BY id")]
        self.assertIsNone(observe.prune(self.conn, [], "空的"))                    # 没有可收回的就不写 act
        self.assertIsNone(observe.prune(self.conn, [e1], "Jev 判：确认继续"))       # 出处是用户段落（1:U1），不收
        act = observe.prune(self.conn, [e1, e2], "Jev 判：只是执行记录，没有判断")
        self.assertEqual(observe.pruned(self.conn), {e2})
        self.assertEqual([e["id"] for e in observe.frame(self.conn, "dsh", "s1")["events"]], [e1])   # 只交出没收回的
        self.assertEqual(self.conn.execute("SELECT a.step FROM act_detail a WHERE a.occurrence=?", (act,)).fetchone()[0], "prune")
        self.assertIsNone(observe.unprune(self.conn, [e1], "没收着的"))             # 只放回现在收着的
        back = observe.unprune(self.conn, [e2], "看过以后放回")
        self.assertEqual(observe.pruned(self.conn), set())                          # 收回、放回按先后，后一次说了算
        self.assertEqual([e["id"] for e in observe.frame(self.conn, "dsh", "s1")["events"]], [e1, e2])
        self.assertEqual(self.conn.execute("SELECT a.step FROM act_detail a WHERE a.occurrence=?", (back,)).fetchone()[0], "unprune")
        observe.prune(self.conn, [e2], "再收")
        self.assertEqual(observe.pruned(self.conn), {e2})
        self.assertNotIn(("turns", ()), observe.history(self.conn).finished)       # 收回、放回的记录不当成一次读

    def test_previous(self):
        """会话续上：之前预览定下的分组给新的预览接着划分，前面的原话不再给，前面的轮次不重看。"""
        first = [Turn("甲一", "答", closed=True), Turn("甲二", "答", closed=True)]
        fakes = self._fakes([f"### 第 1–2 轮\n类型：讨论方向\n判断：{U}定了方向。\n读法：不读\n",
                             "### 第 3 轮\n类型：闲聊\n读法：不读\n"])
        self._collect(first)
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        self._collect(first + [Turn("乙一", "答", closed=True)])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        a, b = [x for x in self._acts() if x["step"] == "preview"]
        self.assertNotIn("## 之前的预览", a["input"])
        self.assertIn(f"## 之前的预览\n- 第 1–2 轮：讨论方向，不读。判断：{U}定了方向。", b["input"])
        self.assertNotIn("甲一", b["input"])
        self.assertIn("乙一", b["input"])

    def test_old_acts(self):
        """2026-10-17 上线时的 act：预览的分组在 meta["segments"]，观察的窗口多一个 segment 键。只追加不改，照样读。"""
        self._collect([Turn("一", "答", closed=True), Turn("二", "答", closed=True)])
        t1, t2 = [r[0] for r in self.conn.execute("SELECT occurrence FROM turn_detail ORDER BY ordinal")]
        op = st.operator(self.conn, "old", "observe", "observer")
        groups = [{"no": 1, "from": 1, "to": 1, "turns": [t1], "how": "粗读", "plan": ""},
                  {"no": 2, "from": 2, "to": 2, "turns": [t2], "how": "粗读", "plan": ""}]
        pid = st.occurrence(self.conn, "act", op, outcome="ok", scale="segment")
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (pid, "observe", "preview", st.dumps({"turns": [t1, t2]}),
                                                                           "", "", st.dumps({"segments": groups})))
        aid = st.occurrence(self.conn, "act", op, outcome="ok", scale="segment")
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)",
                          (aid, "observe", "rough", st.dumps({"turns": [t1], "preview": pid, "segment": 1}), "", "", "{}"))
        self.assertEqual([g["turns"] for _, g, _ in observe.to_read(self.conn)], [[t2]])
        self.assertEqual(observe.ready(self.conn, observe.operator(self.conn)[0]), [])

    def test_reobserve(self):
        """再观察（2026-10-19）：只被旧版本逐轮观察过的轮次，点名一个范围才用当前做法重新预览、读，从最新的会话往前；
        平时旧的照样算处理过。再观察出的 E 和旧版本的并存，决定按新的预览算。一段会话在一批里预览完：中间有一轮没回答、
        窗口断成两个，也接着预览第二个，form 不会看到半段。"""
        self._collect([Turn("甲一", "答", closed=True), Turn("甲二", "答", closed=True)], sid="s1", started="2026-01-01T00:00:00Z")
        self._collect([Turn("乙一", "答", closed=True), Turn("乙二", "答", closed=True), Turn("乙三", "", closed=True),
                       Turn("乙四", "答", closed=True)], sid="s2", started="2026-01-02T00:00:00Z")
        self._collect([Turn("丙一", "答", closed=True)], sid="s3", started="2026-03-01T00:00:00Z")
        old = st.operator(self.conn, "old", "observe", "observer")
        turns = {r[1]: r[0] for r in self.conn.execute("SELECT d.occurrence, d.session || ':' || d.ordinal FROM turn_detail d")}
        legacy = {}
        for k, t in turns.items():                                           # 旧版本逐轮观察过全部轮次，每轮写了一条 E
            a = st.occurrence(self.conn, "act", old, outcome="ok", scale="turn")
            self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (a, "observe", "split", st.dumps({"turn": t}), "", "", "{}"))
            legacy[k] = self.conn.execute("INSERT INTO event(born, idx, turn, text, mode, qualifier, speaker, segments, unresolved) "
                                          "VALUES(?,0,?,?,?,?,?,'[]','[]')", (a, t, f"旧的 {k}", "?unknown", "", "?unknown")).lastrowid
        op = observe.operator(self.conn)[0]
        self.assertEqual(observe.ready(self.conn, op), [])                   # 平时：旧的算处理过
        keys = observe.reobserve_candidates(self.conn, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
        self.assertEqual(keys, [("dsh", "s1"), ("dsh", "s2")])                # s3 不在范围
        req = observe.request_reobserve(self.conn, keys, "用现在的做法再观察", st.main_operator(self.conn, "m"))
        self.assertEqual(st.scope(st.requests(self.conn, "observe")[0]["inputs"]), "range")
        self.assertEqual([w[0][0]["session"] for w in observe.ready(self.conn, op, reobserve=set(keys))], ["s2", "s1"])   # 从新往旧
        fakes = self._fakes(["### 第 1–2 轮\n类型：讨论方向\n读法：粗读\n", "### 第 4 轮\n类型：确认\n读法：不读\n"], [],
                            [f"### 事件1\n{U}说了乙一。\n来源：1:U1"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO(), limit=1)
        previews = [a for a in self._acts() if a["step"] == "preview"]
        self.assertEqual(len(previews), 2)                                  # s2 的两个窗口一批里都预览了
        self.assertEqual({json.loads(a["inputs"])["request"][0] for a in previews}, {req})   # 预览带着再观察的申请（认领）
        self.assertEqual(st.requests(self.conn, "observe", "handled")[0]["id"], req)
        ev, = self.conn.execute("SELECT e.turn, o.scale FROM event e JOIN occurrence o ON o.id=e.born WHERE o.scale='turns'").fetchall()
        self.assertEqual(tuple(ev), (turns["s2:0"], "turns"))
        d = states(self.conn)
        self.assertEqual((d[turns["s2:0"]], d[turns["s2:3"]], d[turns["s1:0"]]), ("read", "not_read", "read"))   # s1 仍是旧版本读过
        self.assertEqual(waiting(self.conn), [])
        self.assertEqual([w[0][0]["session"] for w in observe.ready(self.conn, op, reobserve=set(keys))], ["s1"])
        self.assertEqual(observe.reobserve_candidates(self.conn, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"), [("dsh", "s1")])
        # 范围申请按轮次取代（2026-10-21）：读过的第 1–2 轮旧 E 整轮被取代、留在那一轮上；定了不读的第 4 轮、没轮到的 s1 照旧
        gone = observe.superseded_by_turn(self.conn)
        self.assertEqual(set(gone), {legacy["s2:0"], legacy["s2:1"]})
        texts = [e["text"] for e in observe.frame(self.conn, "dsh", "s2")["events"]]
        self.assertEqual(texts, [f"{U}说了乙一。", "旧的 s2:2", "旧的 s2:3"])
        self.assertIn("旧的 s1:0", [e["text"] for e in observe.frame(self.conn)["events"]])

    def test_backfill_reobserve(self):
        """以前的再观察标记各补一张范围申请（2026-10-21）：parent 指向标记、到达时刻沿用，标记下的预览算认领；
        补记的标记（预览早于它、没带它）认标记之前这几段会话的预览。只补一次。"""
        self._collect([Turn("甲", "答", closed=True)], sid="a", started="2026-01-01T00:00:00Z")
        self._collect([Turn("乙", "答", closed=True)], sid="b", started="2026-01-01T00:00:00Z")
        me = st.main_operator(self.conn, "m")
        sess = dict(self.conn.execute("SELECT session, occurrence FROM session_detail"))
        turn = dict(self.conn.execute("SELECT session, occurrence FROM turn_detail"))
        pop = observe.operator(self.conn)[0]
        def act(step, inputs, meta="{}", window="{}"):
            a = st.occurrence(self.conn, "act", pop if step == "preview" else me, started="2026-01-02T00:00:00Z",
                              inputs=inputs, outcome="ok", cause="manual")
            self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (a, "observe", step, window, "", "", meta))
            return a
        early = act("preview", {"turn": [turn["b"]]}, window=st.dumps({"turns": [turn["b"]]}))       # 补记那批：预览在前
        m1 = act("reobserve", {"session": [sess["a"]]}, st.dumps({"reason": "第二批", "since": "x"}))
        m2 = act("reobserve", {"session": [sess["b"]]}, st.dumps({"reason": "第一批", "note": "补记"}))
        p1 = act("preview", {"turn": [turn["a"]], "act": [m1]}, window=st.dumps({"turns": [turn["a"]]}))
        self.assertEqual(observe.backfill_reobserve(self.conn), 2)
        self.assertEqual(observe.backfill_reobserve(self.conn), 0)
        r1, r2 = st.requests(self.conn, "observe", "all")
        self.assertEqual((r1["parent"], r1["acts"], r2["parent"], r2["acts"]), (m1, [(p1, "ok")], m2, [(early, "ok")]))
        self.assertIn("第二批", r1["reason"])
        self.assertEqual([k for _, _, k in observe.reobserve_requests(self.conn)], [{("dsh", "a")}, {("dsh", "b")}])

    def test_correct(self):
        """纠正（C1b，2026-10-21）：点名申请按 E 出生的窗口重新观察，只回应申请：改正的取代旧 E，不成立的记下，漏了的补上；
        没被点名的 E 照旧交出；form 读过的旧 E 给 form 写修订提示；做完的不再做。"""
        self._collect([Turn("先做一版看看", "好的", closed=True), Turn("结果呢", "结果在报告里", closed=True)])
        fakes = self._fakes(["### 第 1–2 轮\n类型：讨论\n读法：粗读\n"], [],
                            ["### 事件1\n我提出先做一版。\n来源：1:U1\n\n### 事件2\n我说明结果在报告里。\n来源：2:A1\n\n"
                             f"### 事件3\n{U}要求跑迁移脚本。\n来源：2:U1"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        e1, e2, e3 = [r[0] for r in self.conn.execute("SELECT id FROM event ORDER BY id")]
        form_op = st.operator(self.conn, "form-test", "form", "former")
        fa = st.occurrence(self.conn, "act", form_op, outcome="ok", inputs={"event": [e1, e2, e3]})
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (fa, "form", "pack", "{}", "", "", "{}"))
        me = st.main_operator(self.conn, "m")
        rid = st.request(self.conn, st.MAIN, "observe", {"event": [e1, e3]}, f"第 1 轮是{U}说的；迁移脚本没出现过", me,
                         cause="manual")
        rough = FakeModel([f"### 核对\nE{e1}：改正。1:U1 是{U}说的\nE{e3}：不成立。原文里没有迁移脚本\nE{e2}：保留。顺手写的\n\n"
                           f"### 事件1\n{U}提出先做一版看看。\n来源：1:U1\n取代：E{e1}、E{e2 + 99}"])
        log = io.StringIO()
        self.assertEqual(observe.correct(self.conn, models={"rough": rough}, log=log), (1, 0))
        act = [a for a in self._acts() if a["step"] == "correct"][0]
        self.assertIn("## 申请", act["input"])
        self.assertIn(f"E{e1}：我提出先做一版。", act["input"])
        self.assertIn("correct-v0.1", self.conn.execute("SELECT name FROM operator WHERE id=?", (act["operator"],)).fetchone()[0])
        new, = self.conn.execute("SELECT id, text, relations, unresolved FROM event WHERE born=?", (act["id"],)).fetchall()
        self.assertEqual(json.loads(new["relations"]), [{"kind": "supersedes", "to": e1}])
        self.assertIn(f"取代 E{e2 + 99}", json.loads(new["unresolved"]))            # 不是被点名的：照实记下
        meta = json.loads(act["meta"])
        self.assertEqual(meta["invalid"], [e3])
        self.assertEqual(meta["check"][str(e1)], {"verdict": "改正", "why": f"1:U1 是{U}说的"})
        self.assertEqual(meta["check"]["unresolved"], [f"E{e2}"])                   # 不是被点名的：照实记下
        self.assertEqual(observe.superseded(self.conn), {e1, e3})
        self.assertEqual([e["text"] for e in observe.frame(self.conn, "dsh", "s1")["events"]],
                         [f"{U}提出先做一版看看。", "我说明结果在报告里。"])          # 没被点名的照旧，纠正不顶掉同一轮的 E
        rev, = st.requests(self.conn, "form")
        self.assertEqual((rev["from"], rev["inputs"], rev["parent"]), ("observe", {"event": [e1, e3]}, act["id"]))
        self.assertEqual(observe.correct(self.conn, models={"rough": rough}, log=io.StringIO()), (0, 0))   # 做完了
        self.assertEqual(st.requests(self.conn, "observe", "handled")[0]["id"], rid)
        # 纠错后再次犯错的改用精读（2026-10-21）：纠正写出的 E、纠正时判了保留的 E 又被点名
        st.request(self.conn, st.MAIN, "observe", {"event": [new["id"], e2]}, "还是不对", me, cause="manual")
        deep = FakeModel([f"### 核对\nE{new['id']}：保留。\nE{e2}：保留。"])
        self.assertEqual(observe.correct(self.conn, models={"deep": deep, "rough": FakeModel([])}, log=io.StringIO()), (1, 0))
        self.assertEqual(deep.outputs, [])

    def test_correct_legacy_and_off(self):
        """旧版本逐轮观察的 E：按那一轮精读；会话关着观察的跳过，申请留着；失败满次数不再试。"""
        self._collect([Turn("一", "答", closed=True)])
        t, = [r[0] for r in self.conn.execute("SELECT occurrence FROM turn_detail")]
        old = st.operator(self.conn, "old", "observe", "observer")
        a = st.occurrence(self.conn, "act", old, outcome="ok", scale="turn")
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (a, "observe", "split", st.dumps({"turn": t}), "", "", "{}"))
        e = self.conn.execute("INSERT INTO event(born, idx, turn, text, mode, qualifier, speaker, segments, unresolved) "
                              "VALUES(?,0,?,'他说了一','?unknown','','?unknown','[\"U1\"]','[]')", (a, t)).lastrowid
        st.request(self.conn, st.MAIN, "observe", {"event": [e]}, "用了“他”", st.main_operator(self.conn, "m"), cause="manual")
        deep = FakeModel([TimeoutError("超时")] * 3 + ["### 核对\nE1：保留。"])
        for _ in range(4):
            observe.correct(self.conn, models={"deep": deep}, log=io.StringIO())
        acts = [x for x in self._acts() if x["step"] == "correct"]
        self.assertEqual([x["outcome"] for x in acts], ["timeout"] * 3)           # 满 3 次不再试
        self.assertIn("没有预览的计划", acts[0]["input"])
        self.assertEqual(st.requests(self.conn, "observe")[0]["state"], "failed")

    def test_fork_once(self):
        """分叉、副本（2026-10-19）：同一份原文在同一批里也只观察一次；后到的会话从自己独有的轮次接着预览，看得到共享那段的
        预览；交给 form 时按原文认，每段会话都拿到自己所有轮次的 E。"""
        shared = [Turn("一", "答一", closed=True), Turn("二", "答二", closed=True)]
        self._collect(shared, sid="a")
        self._collect(shared + [Turn("三", "答三", closed=True)], sid="b")
        fakes = self._fakes(["### 第 1–2 轮\n类型：讨论\n读法：粗读\n", "### 第 3 轮\n类型：讨论\n读法：粗读\n"], [],
                            [f"### 事件1\n{U}说了一。\n来源：1:U1", f"### 事件1\n{U}说了三。\n来源：3:U1"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        previews = [a for a in self._acts() if a["step"] == "preview"]
        self.assertEqual(len(previews), 2)                                  # 共享的两轮只预览一次
        self.assertIn("## 之前的预览", previews[1]["input"])                  # b 接着划分，看得到共享那段的预览
        self.assertNotIn("答一", previews[1]["input"])
        self.assertEqual(observe.queue(self.conn, observe.operator(self.conn)[0]), [])
        tb = {r[0]: r[1] for r in self.conn.execute("SELECT ordinal, occurrence FROM turn_detail WHERE session='b'")}
        got = observe.frame(self.conn, "dsh", "b")["events"]
        self.assertEqual([(e["text"], e["ordinal"], e["turn"], e["session"]) for e in got],
                         [(f"{U}说了一。", 0, tb[0], "b"), (f"{U}说了三。", 2, tb[2], "b")])
        self.assertEqual([e["text"] for e in observe.frame(self.conn, "dsh", "a")["events"]], [f"{U}说了一。"])

    def test_queue(self):
        """新会话和再观察资源平等，先到先得（2026-10-19）：新会话在可以预览的那一刻到（dsh：最后一轮开始后 3 小时），
        再观察的在点名范围的那一刻到。"""
        self._collect([Turn("旧", "答", closed=True)], sid="old", started="2026-01-01T00:00:00Z")
        self._collect([Turn("新", "答", closed=True, started="2026-06-01T00:00:00Z")], sid="new", started="2026-06-01T00:00:00Z")
        t, = [r[0] for r in self.conn.execute("SELECT occurrence FROM turn_detail WHERE session='old'")]
        a = st.occurrence(self.conn, "act", st.operator(self.conn, "old", "observe", "observer"), outcome="ok", scale="turn")
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (a, "observe", "split", st.dumps({"turn": t}), "", "", "{}"))
        op, now = observe.operator(self.conn)[0], datetime(2026, 6, 2, tzinfo=timezone.utc)
        me = st.main_operator(self.conn, "m")
        order = lambda: [k[1] for _, k, _ in observe.queue(self.conn, op, now=now)]
        self.assertEqual(order(), ["new"])                                  # 没有申请：旧的算处理过
        observe.request_reobserve(self.conn, [("dsh", "old")], "再观察", me, at="2026-06-01T04:00:00Z")
        self.assertEqual(order(), ["new", "old"])                           # 新会话 03:00 就能预览，先到
        observe.request_reobserve(self.conn, [("dsh", "old")], "再观察", me, at="2026-06-01T02:00:00Z")
        self.assertEqual(order(), ["old", "new"])                           # 更早的申请：再观察先到
        self.assertEqual(observe.queue(self.conn, op, now=now, reobserve=False)[0][1], ("dsh", "new"))

    def test_switch_marks(self):
        """开关影响 observe 全流程：关掉以后已经产出的 E 不再交出去。只在 observe → form 的边界上留记录：
        被 form 读过的，关掉记 withdraw、恢复记 restore；没被 form 读过的两头都不记。"""
        two = [Turn("一", "答", closed=True), Turn("二", "答", closed=True)]
        self._collect(two)
        fakes = self._fakes(["### 第 1–2 轮\n类型：闲聊\n读法：粗读\n"],
                            rough=[f"### 事件1\n{U}说一。\n来源：1:U1\n\n### 事件2\n{U}说二。\n来源：2:U1"])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        e1, e2 = [r[0] for r in self.conn.execute("SELECT id FROM event ORDER BY id")]
        op = st.operator(self.conn, "form-test", "form", "former")
        a = st.occurrence(self.conn, "act", op, outcome="ok", inputs={"event": [e1]})
        self.conn.execute("INSERT INTO act_detail VALUES(?,?,?,?,?,?,?)", (a, "form", "organize", "{}", "", "", "{}"))
        steps = lambda: [(r["step"], json.loads(r["inputs"])["event"]) for r in self._acts() if r["step"] in ("withdraw", "restore")]
        self.assertEqual(observe.withheld(self.conn), set())
        switches = []
        for cmd in ("/no-observe", "/do-observe", "/no-observe"):
            switches.append(Turn(cmd, "", closed=True))
            self._collect(two + switches)
            observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
            observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())       # 再跑不重复记
            self.assertEqual(observe.withheld(self.conn), set() if cmd == "/do-observe" else {e1, e2})   # 没被 form 读过的也不交出去
            h = observe.frame(self.conn)
            if cmd == "/do-observe":                                             # 恢复：又交出去，带着边界上的记录
                self.assertEqual([(e["id"], [m["step"] for m in e["marks"]]) for e in h["events"]],
                                 [(e1, ["withdraw", "restore"]), (e2, [])])
            else:
                self.assertEqual((h["events"], [w["event"] for w in h["withdrawn"]]), ([], [e1]))
        self.assertEqual(steps(), [("withdraw", [e1]), ("restore", [e1]), ("withdraw", [e1])])
        w = [r for r in self._acts() if r["step"] == "withdraw"][0]
        self.assertIn("/no-observe", json.loads(w["meta"])["reason"])

    def test_switch_after_preview(self):
        """预览过、还没读的几轮，会话随后被关掉：不读。"""
        self._collect([Turn("一", "答", closed=True), Turn("二", "答", closed=True)])
        fakes = self._fakes(["### 第 1–2 轮\n类型：闲聊\n读法：粗读\n"], rough=[RuntimeError("quota exceeded")])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        self.assertEqual(len(observe.to_read(self.conn)), 1)
        self._collect([Turn("一", "答", closed=True), Turn("二", "答", closed=True), Turn("/no-observe", "", closed=True)])
        self.assertEqual(observe.to_read(self.conn), [])

    def test_interface(self):
        """对外接口：每一轮的决定、还会产出 E 的轮次、交给 form 的 E。"""
        self._collect([Turn("一", "答", closed=True), Turn("二", "答", closed=True), Turn("三", "答", closed=True),
                       Turn("没写完", "", closed=True), Turn("四", "答", closed=True), Turn("还在写", "答", closed=False)])
        t = [r[0] for r in self.conn.execute("SELECT occurrence FROM turn_detail ORDER BY ordinal")]
        self.assertEqual(states(self.conn), {t[0]: "not_previewed", t[1]: "not_previewed", t[2]: "not_previewed",
                                                        t[3]: "no_answer", t[4]: "not_previewed"})   # 还没结束的不列
        self.assertEqual([p["turn"] for p in waiting(self.conn)], [t[0], t[1], t[2], t[4]])
        fakes = self._fakes(["### 第 1 轮\n读法：精读\n\n### 第 2 轮\n读法：粗读\n\n### 第 3 轮\n读法：不读\n",
                             "### 第 5 轮\n读法：不读\n"],                   # 同一批里接着预览第二个窗口
                            deep=[f"### 事件1\n{U}说一。\n来源：1:U1"], rough=[TimeoutError("超时")] * 3)
        for _ in range(3):
            observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        d = states(self.conn)
        self.assertEqual([d[x] for x in (t[0], t[1], t[2], t[4])], ["read", "failed", "not_read", "not_read"])
        self.assertEqual(waiting(self.conn), [])                    # 失败满次数、定了不读的不挡
        h = observe.frame(self.conn)
        self.assertEqual([(e["text"], e["speaker"], e["mode"], e["turn"]) for e in h["events"]], [(f"{U}说一。", f"{U}", "告知", t[0])])
        self.assertEqual(h["withdrawn"], [])
        self.assertEqual(observe.frame(self.conn, "dsh", "其他会话")["events"], [])

    def test_stuck_preview(self):
        """预览失败满次数的窗口整窗跳过，同一会话后面的轮次照常预览。"""
        self._collect([Turn("一", "答", closed=True), Turn("二", "答", closed=True)])
        fakes = self._fakes([RuntimeError("解析不了")] * 3 + ["### 第 3–4 轮\n读法：不读\n"])
        for _ in range(3):
            observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        self._collect([Turn("一", "答", closed=True), Turn("二", "答", closed=True), Turn("三", "答", closed=True),
                       Turn("四", "答", closed=True)])
        (rows, _), = observe.ready(self.conn, observe.operator(self.conn)[0])
        self.assertEqual([r["ordinal"] for r in rows], [2, 3])
        observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        self.assertEqual(sorted(states(self.conn).values()), ["failed", "failed", "not_read", "not_read"])

    def test_switch_off(self):
        self._collect([Turn("一", "答", closed=True), Turn("/no-observe", "", closed=True), Turn("三", "答", closed=True)])
        self.assertEqual(observe.ready(self.conn, observe.operator(self.conn)[0]), [])

    def test_redo(self):
        self._collect([Turn("一", "答", closed=True), Turn("二", "答" * 10, closed=True)])
        fakes = self._fakes([RuntimeError("观察者调用了工具")] * 3 + [RuntimeError("quota exceeded")])
        for _ in range(3):
            observe.run(self.conn, workers=1, models=fakes, log=io.StringIO())
        self.assertEqual([(k, c) for k, _, c in observe.redo(self.conn)], [("preview", {"content_error": 3})])
        self.assertEqual(observe.ready(self.conn, observe.operator(self.conn)[0]), [])     # 不再自动重试，不挡后面
        # 被内容过滤拦下的几轮：不重试，也不进清单
        self._collect([Turn("甲", "答", closed=True), Turn("乙", "答" * 10, closed=True), Turn("丙", "答", closed=True)], sid="s2")
        fakes = self._fakes(["### 第 1–3 轮\n类型：闲聊\n读法：粗读\n"], rough=[RuntimeError("finish_reason: content_filter")])
        observe.run(self.conn, workers=1, models=fakes, session="s2", log=io.StringIO())
        self.assertEqual(self._acts()[-1]["outcome"], "filtered")
        self.assertEqual(observe.to_read(self.conn, "s2"), [])
        self.assertEqual(len(observe.redo(self.conn)), 1)

class ListenTest(unittest.TestCase):
    """监听：心跳不记、原样记；WS 客户端的握手、分片、ping、close（本机起一个假的协议端）。"""

    def test_record(self):
        from . import listen
        at = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(listen.record('{"post_type":"meta_event","meta_event_type":"heartbeat"}', now=at))
        msg = {"post_type": "message", "message_type": "private", "raw_message": "你好"}
        self.assertEqual(json.loads(listen.record(json.dumps(msg), now=at)),
                         {"received": "2026-10-31T12:00:00.000+00:00", "event": msg})
        self.assertEqual(json.loads(listen.record("not json", now=at))["raw"], "not json")

    def test_keep_groups(self):
        from . import listen
        g = {100000001}
        self.assertTrue(listen.keep({"post_type": "message", "group_id": 100000001}, g))
        self.assertTrue(listen.keep({"post_type": "message_sent", "group_id": 100000001}, g))
        self.assertTrue(listen.keep({"post_type": "meta_event", "meta_event_type": "lifecycle"}, g))
        self.assertFalse(listen.keep({"post_type": "message", "message_type": "private", "user_id": 1}, g))
        self.assertFalse(listen.keep({"post_type": "message", "group_id": 1}, g))
        self.assertFalse(listen.keep({"post_type": "request", "request_type": "friend"}, g))
        self.assertFalse(listen.keep({"status": "ok", "echo": "a"}, g))
        self.assertTrue(listen.keep({"post_type": "message", "message_type": "private"}, None))
        self.assertTrue(listen.keep({"post_type": "message", "message_type": "private", "user_id": 7}, g, {7}))
        self.assertTrue(listen.keep({"post_type": "message_sent", "message_type": "private", "user_id": 9,
                                     "target_id": 7}, g, {7}))
        self.assertFalse(listen.keep({"post_type": "message", "message_type": "private", "user_id": 8}, g, {7}))

    def test_ws(self):
        import socket
        import struct
        import threading
        from . import listen
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        got = {}

        def frame(op, data, fin=True):
            return struct.pack(">BB", (0x80 if fin else 0) | op, len(data)) + data

        def peer():
            c, _ = srv.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            got["auth"] = b"Authorization: Bearer t0k" in req
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            c.sendall(frame(0x9, b"hi") + frame(0x1, "第一".encode(), fin=False) + frame(0x0, "段".encode())
                      + frame(0x8, struct.pack(">H", 1000)))
            b0, b1 = c.recv(2)                          # 客户端回的 pong，带掩码
            got["pong"] = (b0 & 0x0F, b1 & 0x80, b1 & 0x7F)
            c.recv(4096)
            c.close()

        t = threading.Thread(target=peer)
        t.start()
        ws = listen.WS("127.0.0.1", srv.getsockname()[1], headers=["Authorization: Bearer t0k"], timeout=5)
        self.assertEqual(ws.recv(), "第一段")
        with self.assertRaises(listen.Closed):
            ws.recv()
        ws.close()
        t.join(5)
        srv.close()
        self.assertTrue(got["auth"])
        self.assertEqual(got["pong"], (0xA, 0x80, 2))


class BackfillTest(unittest.TestCase):
    """补录：按 message_id 对齐、floor 之后；行的格式和 listen 一致；用假协议端跑一次 sync（缺的补上、已有的不重写）。"""

    def test_missing(self):
        from . import backfill
        msgs = [{"message_id": 1, "time": 100}, {"message_id": 2, "time": 200},
                {"message_id": 3, "time": 50}, {"time": 300}]
        self.assertEqual([m["message_id"] for m in backfill.missing(msgs, {2}, 60)], [1])

    def test_row(self):
        from . import backfill
        msg = {"message_id": 1, "time": 1793474702}
        self.assertEqual(json.loads(backfill.row(msg)),
                         {"received": "2026-10-31T19:25:02.000+00:00", "event": msg})

    def test_floor(self):
        from . import backfill
        now = datetime.now(timezone.utc).timestamp()
        self.assertAlmostEqual(backfill.floor_of([now - 60]), now - 60, delta=3)
        self.assertAlmostEqual(backfill.floor_of([now - 86400 * 10], days=3), now - 86400 * 3, delta=5)

    def test_sync(self):
        import socket
        import struct
        import threading
        from unittest import mock
        from . import backfill, listen
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        now = int(datetime.now(timezone.utc).timestamp())        # 时间要落在 floor 之后，不然 days 一改就不算缺
        msgs = [{"message_id": 2, "time": now - 60, "raw_message": "旧的"},
                {"message_id": 7, "time": now - 30, "raw_message": "缺的"}]

        def frame(op, data):
            n = len(data)
            if n < 126:
                head = struct.pack(">BB", 0x80 | op, n)
            elif n < 1 << 16:
                head = struct.pack(">BBH", 0x80 | op, 126, n)
            else:
                head = struct.pack(">BBQ", 0x80 | op, 127, n)
            return head + data

        def peer():
            c, _ = srv.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            b0, b1 = c.recv(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", c.recv(2))[0]
            mask = c.recv(4)
            data = b""
            while len(data) < n:
                data += c.recv(n - len(data))
            ask = json.loads(bytes(b ^ mask[i % 4] for i, b in enumerate(data)))
            reply = {"status": "ok", "retcode": 0, "data": {"messages": msgs}, "echo": ask["echo"]}
            c.sendall(frame(0x1, json.dumps(reply).encode()))
            c.recv(4096)
            c.close()

        t = threading.Thread(target=peer)
        t.start()
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "listen-onebot.jsonl"
            path.write_text(json.dumps({"received": "x", "event": {"message_id": 2, "time": now - 60}}) + "\n",
                            encoding="utf-8")
            out = io.StringIO()
            with mock.patch.object(listen, "LOG", path), \
                    mock.patch.object(listen, "config", return_value=(None, f"127.0.0.1:{port}", port, "t0k")), \
                    mock.patch.object(listen, "groups", return_value={100000001}), \
                    mock.patch.object(listen, "private", return_value=()), \
                    mock.patch.object(backfill, "free_port", return_value=port):
                self.assertEqual(backfill.sync(write=True, days=3650, log=out.write), 1)
            rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        t.join(5)
        srv.close()
        self.assertEqual([r["event"]["message_id"] for r in rows], [2, 7])
        self.assertIn("缺 1 条", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class ExpressionOperatorTest(unittest.TestCase):
    def test_rules_version_and_instance_names(self):
        from unittest.mock import patch
        from ... import express
        module = observe
        step = 'rough'
        with patch.object(module, 'register', return_value=1) as register:
            with patch.object(module, 'USER_NAME', '青禾'):
                a = module.operator(None, step)[1]
            with patch.object(module, 'USER_NAME', '松风'):
                self.assertEqual(a, module.operator(None, step)[1])
            self.assertEqual(register.call_args.kwargs['params']['expression'], express.signature())
            with patch('mh.express.VERSION', 'test-next'):
                self.assertNotEqual(a, module.operator(None, step)[1])
