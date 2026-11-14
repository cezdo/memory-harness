"""observe 在宿主上的钩子（Claude Code、codex 共用），都不调用模型。

- 开关（UserPromptSubmit）：python3 -I -S hook.py claude|codex [记录文件]。用户的输入正好是开关命令时，记下开关、拦下这次输入；
  其余输入什么都不做。记录追加到数据目录的 observe-switch.jsonl：时间、宿主、会话 ID、会话文件、开还是关、原话。
  拦法：Claude Code 输出 {"decision": "block"}；codex 以退出码 2 加 stderr 的理由拦（codex 的约定）。
  钩子失败也不挡用户：命令文件和 skill 还在，命令照常发给模型，会话记录里的原话由程序认。
- 叫醒（Stop）：python3 -I -S hook.py claude|codex stop。一轮结束，碰一下数据目录的 observe.wake，休眠的观察循环提前醒
  （rhythm 的 _rest）；/do-observe 记下后同样叫醒。循环没在跑也不报错。
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("MH_HOME", Path.home() / ".local/share/memory-harness"))    # 同 store.HOME
LOG = HOME / "observe-switch.jsonl"
WAKE = HOME / "observe.wake"                # 同 rhythm 的叫醒文件（{循环名}.wake）
COMMANDS = {"/no-observe": "off", "$no-observe": "off", "/do-observe": "on", "$do-observe": "on"}
SAID = {"off": "memory-harness: 已关闭对本会话的观察，本会话的信息将不再进入记忆。可使用 /do-observe 恢复。",
        "on": "memory-harness: 已恢复对本会话的观察。"}


def unplugged(cwd):
    """[connect].unplugged 的判断在实例配置里（store/instance.unplugged）；只在真是开关命令时才去读，读不了当没配。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
        from mh.engram.store.instance import unplugged as check
        return check(cwd)
    except Exception:
        return False


def wake(path=WAKE):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        pass


def main(argv):
    host = argv[1] if len(argv) > 1 else "claude"
    if argv[2:3] == ["stop"]:
        wake()
        return 0
    log = Path(argv[2]) if len(argv) > 2 else LOG
    try:
        d = json.load(sys.stdin)
    except ValueError:
        return 0
    state = COMMANDS.get((d.get("prompt") or "").strip())
    if not state or unplugged(d.get("cwd")):  # 不接 mh 的目录（[connect].unplugged）：不记开关，命令照常发给模型
        return 0
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "source": host,
                            "session": d.get("session_id") or "", "file": d.get("transcript_path") or "",
                            "state": state, "prompt": d.get("prompt")}, ensure_ascii=False) + "\n")
    if state == "on":                       # /do-observe：恢复观察，循环早点醒来记 restore（读掉尾巴 11-06 随快道撤了）
        wake(log.parent / WAKE.name)
    if host == "codex":                     # codex 里恢复命令写作 $do-observe（skill）
        print(SAID[state].replace("/do-observe", "$do-observe"), file=sys.stderr)
        return 2
    print(json.dumps({"decision": "block", "reason": SAID[state]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
