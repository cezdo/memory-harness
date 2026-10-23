"""observe 开关的测试：认得出三种宿主的写法、讨论里提到不算、最后一次开关说了算、关掉的会话和开关那一轮不挑、
dsh 的 command/run 记成一轮、钩子记下开关并拦下输入、钩子的记录由采集收成 turn 再参与判定、安装不覆盖别人的东西。
python3 -m unittest mh.engram.observe.switch.test
"""
import io
import json
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path

from ..sources import dsh, switch as source
from ..sources.turn import Session, Turn
from ... import store as st
from .. import collect, ready, operator
from . import HERE, switch, states, install


class Source:
    def __init__(self, sessions):
        self.sessions = sessions          # {路径: (Session, [Turn])}

    def files(self):
        return list(self.sessions)

    def parse(self, path):
        return self.sessions[path]


def turn(user, assistant="好的"):
    return Turn(user=user, assistant=assistant, closed=True)


class SwitchTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.conn = st.connect(self.dir / "engram.db")
        self.saved, source.LOG = source.LOG, self.dir / "none.jsonl"      # 不读真实数据目录里的开关记录

    def tearDown(self):
        source.LOG = self.saved

    def _collect(self, **sessions):
        src = {}
        for i, (sid, turns) in enumerate(sessions.items()):
            f = self.dir / f"{sid}.jsonl"
            f.write_text("x")
            src[f] = (Session("dsh", sid, str(f), started=f"2026-01-0{i + 1}T00:00:00Z", cwd="/w"), turns)
        collect.run(self.conn, sources=("dsh",), force=True, log=io.StringIO(), modules={"dsh": Source(src)})

    def test_switch(self):
        self.assertEqual(switch("/no-observe"), "off")
        self.assertEqual(switch("  $do-observe\n"), "on")
        self.assertEqual(switch("<command-message>no-observe</command-message>\n<command-name>/no-observe</command-name>"), "off")
        self.assertIsNone(switch("比如/no-observe和/do-observe"))
        self.assertIsNone(switch("/no-observe 这个会话别记"))
        self.assertIsNone(switch("<command-name>/model</command-name>"))

    def test_pending(self):
        self._collect(a=[turn("你好"), turn("/no-observe", ""), turn("随便问问")],
                      b=[turn("开始"), turn("/no-observe", ""), turn("/do-observe", ""), turn("继续")],
                      c=[turn("比如/no-observe和/do-observe"), turn("下一句")])
        self.assertEqual(states(self.conn), {("dsh", "a"): "off", ("dsh", "b"): "on"})
        op, _ = operator(self.conn)
        windows = {rows[0]["session"]: [r["ordinal"] for r in rows] for rows, _ in ready(self.conn, op)}
        self.assertNotIn("a", windows)                                      # 关掉的整个会话不预览，前面的也不
        self.assertEqual(windows["b"], [0])                                 # 恢复了；窗口到开关那一轮为止，开关本身不看
        self.assertEqual(windows["c"], [0, 1])                              # 讨论里提到不算

    def test_dsh_command(self):
        events = [{"type": "session", "id": "s1", "cwd": "/w", "createdAt": 1},
                  {"type": "user/message", "seq": 1, "data": {"source": {"kind": "user"}, "content": [{"type": "text", "text": "你好"}]}},
                  {"type": "command/run", "seq": 2, "data": {"name": "no-observe", "args": "", "source": {"kind": "user"}}},
                  {"type": "assistant/message", "seq": 3, "data": {"message": {"content": [{"type": "text", "text": "你好呀"}]}}},
                  {"type": "turn/end", "seq": 4, "data": {"reason": {"kind": "completed"}}},
                  {"type": "command/run", "seq": 5, "data": {"name": "permission", "args": " read-only"}},
                  {"type": "command/run", "seq": 6, "data": {"name": "no-observe", "args": " 多余参数"}},       # 插件报用法错误，不算
                  {"type": "command/run", "seq": 7, "data": {"name": "do-observe", "args": ""}}]
        d = self.dir / "ws" / "s1"
        d.mkdir(parents=True)
        f = d / "session.v4.jsonl.zstd"
        raw = "\n".join(json.dumps(e) for e in events).encode()
        f.write_bytes(subprocess.run(["zstd", "-q", "-c"], input=raw, capture_output=True, check=True).stdout)
        _, turns = dsh.parse(f)
        self.assertEqual([(t.user, t.assistant) for t in turns],
                         [("你好", "你好呀"), ("/no-observe", ""), ("/do-observe", "")])   # 进行中的一轮先收完

    def _hook(self, host, prompt, log):
        p = subprocess.run([sys.executable, "-I", "-S", str(HERE / "hook.py"), host, str(log)], capture_output=True, text=True,
                           input=json.dumps({"session_id": "s9", "transcript_path": str(self.dir / "s9.jsonl"), "prompt": prompt}))
        return p.returncode, p.stdout, p.stderr

    def test_hook(self):
        log = self.dir / "switch.jsonl"
        code, out, _ = self._hook("claude", "/no-observe", log)
        self.assertEqual((code, json.loads(out)["decision"]), (0, "block"))
        self.assertEqual(json.loads(out)["reason"], "memory-harness: 已关闭对本会话的观察，本会话的信息将不再进入记忆。可使用 /do-observe 恢复。")
        code, out, err = self._hook("codex", "$do-observe", log)
        self.assertEqual((code, out), (2, ""))                              # codex：退出码 2 拦下，理由在 stderr
        self.assertEqual(err.strip(), "memory-harness: 已恢复对本会话的观察。")
        self.assertEqual(self._hook("claude", "比如 /no-observe", log), (0, "", ""))    # 别的输入什么都不做
        rows = [json.loads(l) for l in open(log)]
        self.assertEqual([(r["source"], r["session"], r["state"]) for r in rows], [("claude", "s9", "off"), ("codex", "s9", "on")])

    def test_states_log(self):
        self._collect(a=[turn("你好"), turn("再问一句")], b=[turn("开始"), turn("/no-observe", "好，这个会话不观察了。")])
        source.LOG = self.dir / "switch.jsonl"
        f = str(self.dir / "a.jsonl")
        records = [
            {"time": "2027-01-01T00:00:00+00:00", "source": "dsh", "session": "x", "file": f, "state": "off", "prompt": "/no-observe"},   # 按会话文件对上 a
            {"time": "2027-01-01T00:00:00+00:00", "source": "dsh", "session": "b", "file": "", "state": "on", "prompt": "/do-observe"},  # 比原话晚：b 恢复
            {"time": "2027-01-01T00:00:00+00:00", "source": "codex", "session": "c", "file": "", "state": "off", "prompt": "$no-observe"},
            {"time": "2027-01-01T00:00:01+00:00", "source": "codex", "session": "c", "file": "", "state": "on", "prompt": "$do-observe"}]
        source.LOG.write_text("\n".join(json.dumps(e) for e in records[:3]) + "\n坏的一行\n")
        self.assertEqual(states(self.conn), {("dsh", "b"): "off"})            # 只看库：没采集之前钩子的记录不算
        collect.run(self.conn, sources=("switch",), log=io.StringIO())
        self.assertEqual(states(self.conn), {("dsh", "a"): "off", ("dsh", "b"): "on", ("codex", "c"): "off"})
        rows = self.conn.execute("SELECT d.session, d.origin, d.host_kind, d.closed, x.user, x.assistant FROM turn_detail d "
                                 "JOIN turn_text x ON x.id=d.text WHERE d.source='switch' ORDER BY d.session, d.ordinal").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("codex:c", "human", "command", 1, "$no-observe", ""),
                                                    (f"dsh:b", "human", "command", 1, "/do-observe", ""),
                                                    ("dsh:x", "human", "command", 1, "/no-observe", "")])
        with open(source.LOG, "a") as out:                                  # 追加一条：只补新的
            out.write(json.dumps(records[3]) + "\n")
        got = collect.run(self.conn, sources=("switch",), log=io.StringIO())
        self.assertEqual(got["switch"]["turns"], 1)
        self.assertEqual(states(self.conn)[("codex", "c")], "on")
        op, _ = operator(self.conn)
        self.assertFalse([rows for rows, _ in ready(self.conn, op) if rows[0]["source"] == "switch"])   # 开关那一轮不预览

    def test_install(self):
        home = self.dir / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude/settings.json").write_text(json.dumps({"model": "x", "hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "synapse"}]}]}}))
        done = install(home)
        self.assertEqual(len(done), 6)
        self.assertTrue((home / ".claude/commands/no-observe.md").read_text().startswith("---"))
        self.assertTrue((home / ".codex/skills/do-observe/SKILL.md").exists())
        cfg = json.loads((home / ".claude/settings.json").read_text())
        self.assertEqual(cfg["model"], "x")                                  # 别的设置原样留着
        self.assertEqual([g["hooks"][0]["command"].split()[-1] for g in cfg["hooks"]["UserPromptSubmit"]], ["synapse", "claude"])
        self.assertEqual(len(list((home / ".claude").glob("settings.json.bak-observe-switch-*"))), 1)
        self.assertTrue(json.loads((home / ".codex/hooks.json").read_text())["hooks"]["UserPromptSubmit"])
        self.assertEqual(install(home), [])                                # 再装一次什么都不做
        (home / ".claude/commands/do-observe.md").unlink()
        (home / ".claude/commands/do-observe.md").write_text("别人的")
        with self.assertRaises(FileExistsError):                           # 不覆盖不是自己装的文件
            install(home)

if __name__ == "__main__":
    unittest.main()
