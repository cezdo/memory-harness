"""联想（souhatsu）的开关（用户 2026-10-28）：专心做任务时关掉，轻松讨论时打开。

`/no-souhatsu` 关、`/do-souhatsu` 开（codex 里写成 `$no-souhatsu`、`$do-souhatsu`，skill）。联想的 UserPromptSubmit 钩子认出
整句命令，记下、拦下，不调用模型（照 observe 开关的路子）。最后一次说了算，按会话，默认开。关掉的只是联想；“查不到的词”照旧。

记录追加到数据目录的 souhatsu-switch.jsonl：时间、宿主、会话 ID、开还是关、原话。读状态就是找这个会话最后一行。
钩子读不到、写不进都不挡用户：命令文件和 skill 还在，命令照常发给模型，模型只回一句确认。
"""
import json
from datetime import datetime, timezone

from ....engram.store import HOME

LOG = HOME / "souhatsu-switch.jsonl"
COMMANDS = {"/no-souhatsu": "off", "$no-souhatsu": "off", "/do-souhatsu": "on", "$do-souhatsu": "on"}
SAID = {"off": "memory-harness: 已关闭本会话的联想（souhatsu），“查不到的词”照旧提示。可使用 /do-souhatsu 恢复。",
        "on": "memory-harness: 已恢复本会话的联想。"}


def command(prompt):
    """整句正好是开关命令才算（讨论里提到、带参数的都不算）→ "on" / "off" / None。"""
    return COMMANDS.get((prompt or "").strip())


def state(session, log=None):
    """这个会话现在的状态："on" 或 "off"。没有会话号、没有记录、读不到都是开。"""
    if not session:
        return "on"
    got = "on"
    try:
        with open(log or LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("session") == session:
                    got = d.get("state", got)
    except OSError:
        pass
    return got


def record(state_, host, session, prompt, log=None):
    log = log or LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "source": host, "session": session or "",
                            "state": state_, "prompt": prompt}, ensure_ascii=False) + "\n")


def handle(d, host, stdout, stderr, log=None):
    """钩子收到的 JSON 是开关命令就记下、拦下 → 退出码；不是命令返回 None，由联想接着做。
    拦法：Claude Code 输出 {"decision": "block"}；codex 以退出码 2 加 stderr 的理由拦（codex 的约定）。"""
    want = command(d.get("prompt"))
    if not want:
        return None
    record(want, host, d.get("session_id"), d.get("prompt"), log)
    if host == "codex":
        print(SAID[want].replace("/do-souhatsu", "$do-souhatsu"), file=stderr)
        return 2
    json.dump({"decision": "block", "reason": SAID[want]}, stdout, ensure_ascii=False)
    return 0
