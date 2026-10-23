"""observe 开关：用户在会话里输入 /no-observe，这个会话就不观察；/do-observe 恢复（2026-10-17）。

- 判定是程序：一个会话里最后一次开关说了算，作用于整个会话、observe 的全流程（2026-10-17）：不预览、不读，
  已经产出的 E 也不再交给上一层；开关那一轮本身永远不观察。库只追加：E 不删；已经被 form 读过的，
  关掉时记 withdraw、恢复时记 restore（见 observe.mark_switches），没被 form 读过的两头都不记。
- 都不调用模型：
  - Claude Code、codex：UserPromptSubmit 钩子（hook.py）认出命令，记进数据目录的 observe-switch.jsonl，再拦下这次输入；
    这份记录由采集收成 turn（sources/switch.py），判定只看库。
    命令文件（claude/*.md）和 skill（codex/*/SKILL.md）留着，让命令出现在补全里；钩子失败时命令照常发给模型，
    会话记录里留下原话（<command-name>/no-observe</command-name>、$no-observe），程序同样认；
  - dsh：插件命令（dsh/，web profile 的 bundle），会话日志里是 command/run，采集记成原话为 /no-observe、没有回复的一轮。
- 只认整条消息就是命令的写法；讨论里提到 /no-observe 不算。
"""
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..sources.switch import SOURCE

HERE = Path(__file__).parent
_CLAUDE = re.compile(r"<command-name>\s*/(no|do)-observe\s*</command-name>")
_PLAIN = re.compile(r"[/$](no|do)-observe")
NAMES = ("no-observe", "do-observe")


def switch(user):
    """这一轮的原话是不是开关：'off'、'on'，不是返回 None。"""
    u = (user or "").strip()
    m = _CLAUDE.search(u) if u.startswith("<command-") else _PLAIN.fullmatch(u)
    return None if not m else ("off" if m.group(1) == "no" else "on")


def _when(t):
    try:
        d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def states(conn):
    """{(来源, 会话): 'off' | 'on'}：每个出现过开关的会话，最后一次开关的状态（见 latest）。"""
    return {k: v[0] for k, v in latest(conn).items()}


def latest(conn):
    """{(来源, 会话): (状态, 开关那一轮的 turn 编号)}：每个出现过开关的会话，按时间最后一次开关（同一时间按出现的先后）。只看库：
    会话记录里的原话（dsh 的插件命令、钩子没拦下而发给了模型的命令），和采集收进来的钩子记录（来源 switch，
    按宿主的会话文件对上库里的会话，对不上就按“宿主, 会话编号”）。"""
    files = {r["file"]: (r["source"], r["session"]) for r in conn.execute("SELECT source, session, file FROM session_detail")}
    seen = {}
    for n, r in enumerate(conn.execute("""SELECT d.occurrence, d.source, d.session, d.address, s.parent, x.user, o.started
                                          FROM turn_detail d JOIN turn_text x ON x.id=d.text JOIN occurrence o ON o.id=d.occurrence
                                          JOIN session_detail s ON s.source=d.source AND s.session=d.session
                                          WHERE x.user LIKE '%-observe%' ORDER BY d.source, d.session, d.ordinal""")):
        state = switch(r["user"])
        if not state:
            continue
        key = (r["source"], r["session"])
        if r["source"] == SOURCE:
            a = json.loads(r["address"])
            key = files.get(a.get("file")) or (a.get("host"), r["parent"])
        seen.setdefault(key, []).append((_when(r["started"]), n, state, r["occurrence"]))
    return {k: max(v)[2:] for k, v in seen.items()}


def _link(src, dst):
    if dst.is_symlink() and dst.resolve() == src.resolve():
        return None
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"{dst} 已存在且不是指向 {src} 的链接，先看清再处理")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(src)
    return f"{dst} → {src}"


def _hook(path, command, host):
    """把钩子加进宿主的 UserPromptSubmit（已经有就不动）；改之前留一份备份。"""
    data = json.loads(path.read_text()) if path.exists() else {}
    groups = data.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    if any(str(HERE / "hook.py") in h.get("command", "") for g in groups for h in g.get("hooks", [])):
        return None
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.bak-observe-switch-{datetime.now():%Y%m%d-%H%M%S}"))
    groups.append({"hooks": [{"type": "command", "command": command, "timeout": 5}]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return f"{path}：UserPromptSubmit 加了 {host} 的开关钩子"


def install(home=Path.home(), python="/usr/bin/python3"):
    """装 Claude Code 命令、codex skill（符号链接到仓库，改仓库即生效）和两边的开关钩子。codex 的新钩子要用户在 codex 里
    信任一次才会运行。dsh 的 bundle 要挂进 web profile、重启 dsh web，不在这里做（宿主由用户重启）。返回做了什么。"""
    done = []
    for name in NAMES:
        done.append(_link(HERE / "claude" / f"{name}.md", home / ".claude/commands" / f"{name}.md"))
        done.append(_link(HERE / "codex" / name, home / ".codex/skills" / name))
    hook = HERE / "hook.py"
    done.append(_hook(home / ".claude/settings.json", f"{python} -I -S {hook} claude", "Claude Code"))
    done.append(_hook(home / ".codex/hooks.json", f"{python} -I -S {hook} codex", "codex"))
    return [x for x in done if x]
