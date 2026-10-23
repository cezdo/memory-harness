"""observe 开关的 UserPromptSubmit 钩子（Claude Code、codex 共用）：用户的输入正好是开关命令时，记下开关、拦下这次输入，
不调用模型。其余输入什么都不做。python3 -I -S hook.py claude|codex [记录文件]

记录追加到数据目录的 observe-switch.jsonl：时间、宿主、会话 ID、会话文件、开还是关、原话。
拦法：Claude Code 输出 {"decision": "block"}；codex 以退出码 2 加 stderr 的理由拦（codex 的约定）。
钩子失败也不挡用户：命令文件和 skill 还在，命令照常发给模型，会话记录里的原话由程序认。
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG = Path.home() / ".local/share/memory-harness/observe-switch.jsonl"
COMMANDS = {"/no-observe": "off", "$no-observe": "off", "/do-observe": "on", "$do-observe": "on"}
SAID = {"off": "memory-harness: 已关闭对本会话的观察，本会话的信息将不再进入记忆。可使用 /do-observe 恢复。",
        "on": "memory-harness: 已恢复对本会话的观察。"}


def main(argv):
    host = argv[1] if len(argv) > 1 else "claude"
    log = Path(argv[2]) if len(argv) > 2 else LOG
    try:
        d = json.load(sys.stdin)
    except ValueError:
        return 0
    state = COMMANDS.get((d.get("prompt") or "").strip())
    if not state:
        return 0
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "source": host,
                            "session": d.get("session_id") or "", "file": d.get("transcript_path") or "",
                            "state": state, "prompt": d.get("prompt")}, ensure_ascii=False) + "\n")
    if host == "codex":                     # codex 里恢复命令写作 $do-observe（skill）
        print(SAID[state].replace("/do-observe", "$do-observe"), file=sys.stderr)
        return 2
    print(json.dumps({"decision": "block", "reason": SAID[state]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
