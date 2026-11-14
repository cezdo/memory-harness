"""联想（souhatsu）的开关（用户 2026-10-28）：专心做任务时关掉，轻松讨论时打开。

`/no-souhatsu` 关、`/do-souhatsu` 开（codex 里写成 `$no-souhatsu`、`$do-souhatsu`，skill）。联想的 UserPromptSubmit 钩子认出
整句命令，记下、拦下，不调用模型（照 observe 开关的路子）。最后一次说了算，按会话，默认开。关掉的只是联想；“查不到的词”照旧。

记录追加到数据目录的 souhatsu-switch.jsonl：时间、宿主、会话 ID、开还是关、原话。读状态就是找这个会话最后一行（每轮要最新的）；
观察循环的采集把它收成 turn 进库（observe 的 switch 来源，origin 是用户，2026-10-30 runbook N3），足迹、检查从库里看。
钩子读不到、写不进都不挡用户：命令文件和 skill 还在，命令照常发给模型，模型只回一句确认。

命令文件在 `mh/skills/<名字>/claude/<名字>.md`、`codex/<名字>/SKILL.md`（2026-11-14 从 `push/switch/` 搬来），
`install()` 把它们链接到两个宿主（`~/.claude/commands/`、`~/.codex/skills/`），旧位置的链接就地换掉。
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ....engram.store import HOME

HERE = Path(__file__).parent
SKILL = HERE.parents[2] / "skills"      # 命令和 skill 的源文件（2026-11-14 归 mh/skills/）
OLD = ("mh/synapse/push/switch/",)      # 搬家前的位置：宿主里指向它的链接就地换成新的，不并存（11-14）
NAMES = ("no-souhatsu", "do-souhatsu")

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


def _link(src, dst, apply):
    if dst.is_symlink() and dst.resolve() == src.resolve():
        return None
    if dst.is_symlink() and any(o in str(Path(os.readlink(dst))) for o in OLD):    # 指向搬家前的位置：换掉
        if apply:
            dst.unlink()
            dst.symlink_to(src)
        return f"{dst} → {src}（原来指向旧位置）"
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"{dst} 已存在且不是指向 {src} 的链接，先看清再处理")
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.symlink_to(src)
    return f"{dst} → {src}"


def install(home=Path.home(), apply=False):
    """装联想开关的 Claude Code 命令、codex skill（符号链接到 `mh/skills/`，改仓库即生效）。apply=False 只列要做什么；
    宿主里指向搬家前位置的链接就地换掉。钩子（`push/associate.py`）由宿主接入单独装，不在这里做。返回做了什么。"""
    done = []
    for name in NAMES:
        done.append(_link(SKILL / name / "claude" / f"{name}.md", home / ".claude/commands" / f"{name}.md", apply))
        done.append(_link(SKILL / name / "codex" / name, home / ".codex/skills" / name, apply))
    return [x for x in done if x]
