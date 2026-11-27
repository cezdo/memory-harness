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
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

HOME = Path(os.environ.get("MH_HOME", Path.home() / ".local/share/memory-harness"))    # 同 store.HOME
LOG = HOME / "observe-switch.jsonl"
WAKE = HOME / "observe.wake"                # 同 rhythm 的叫醒文件（{循环名}.wake）
COMMANDS = {"/no-observe": "off", "$no-observe": "off", "/do-observe": "on", "$do-observe": "on"}
SAID = {"off": "memory-harness: 已关闭对本会话的观察，本会话的信息将不再进入记忆。可使用 /do-observe 恢复。",
        "on": "memory-harness: 已恢复对本会话的观察。"}
_USER_REQUEST = re.compile(r"<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>", re.S)


def _extract_user(text):
    s = text or ""
    m = _USER_REQUEST.search(s)
    return (m.group(1) if m else s).strip()


def _full_transcript(path, session=""):
    if not path:
        return ""
    p = Path(path)
    if _is_acp(p):
        cid = session or p.stem
        logs = p.parent.parent / "brain" / cid / ".system_generated/logs"
        for name in ("transcript_full.jsonl", "transcript.jsonl"):
            cand = logs / name
            if cand.exists():
                return str(cand)
        return ""
    full = p.with_name("transcript_full.jsonl") if p.name == "transcript.jsonl" else p
    return str(full if full.exists() else p)


def _agy_first_invocation(path, invocation_num=None):
    if path and not str(path).endswith(".db") and Path(path).exists():
        last_user, after = False, False
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(d, dict):
                        continue
                    t = d.get("type")
                    if t == "USER_INPUT":
                        last_user, after = True, False
                    elif last_user and t in ("PLANNER_RESPONSE", "GENERIC", "RUN_COMMAND", "VIEW_FILE", "CODE_ACTION", "MCP_TOOL"):
                        after = True
            if last_user:
                return not after
        except OSError:
            pass
    return invocation_num in (None, 0, 1)


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


def _is_acp(path):
    """同 synapse/hosts/agy.is_acp（这个钩子用 -I -S 跑，不 import 仓库）。"""
    p = Path(path or "")
    return p.suffix == ".db" or (p.parent.name == "conversations" and p.suffix != ".jsonl")


def decide(host, prompt, session, transcript, cwd, log=LOG):
    """整句是开关命令就记下 → "on" / "off"；不是命令、不接 mh 的目录（[connect].unplugged，命令照常发给模型）→ None。"""
    state = COMMANDS.get(prompt)
    if not state or unplugged(cwd):
        return None
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "source": host,
                            "session": session, "file": transcript,
                            "state": state, "prompt": prompt}, ensure_ascii=False) + "\n")
    if state == "on":                       # /do-observe：恢复观察，循环早点醒来记 restore（读掉尾巴 11-06 随快道撤了）
        wake(log.parent / WAKE.name)
    return state


def main(argv):
    host = argv[1] if len(argv) > 1 else "claude"
    if argv[2:3] == ["stop"]:
        wake()
        if host == "agy":
            print("{}")
        return 0
    log = Path(argv[2]) if len(argv) > 2 else LOG
    try:
        d = json.load(sys.stdin)
    except ValueError:
        return 0
    raw_prompt = d.get("prompt") if "prompt" in d else d.get("lastUserInput") or ""
    prompt = _extract_user(raw_prompt) if host == "agy" else (raw_prompt or "").strip()
    session = d.get("session_id") or d.get("conversationId") or ""
    transcript = _full_transcript(d.get("transcript_path") or d.get("transcriptPath") or "", session)
    cwd = d.get("cwd") or (d.get("workspacePaths") or [""])[0] or ""
    acp = host == "agy" and _is_acp(d.get("transcript_path") or d.get("transcriptPath") or "")
    if acp or (host == "agy" and not _agy_first_invocation(transcript, d.get("invocationNum"))):
        print("{}")                         # ACP 不给用户原话：开关由 Zeron 调 acp-prompt 判（synapse/hosts/agy）
        return 0
    state = decide(host, prompt, session, transcript, cwd, log)
    if not state:
        if host == "agy":
            print("{}")
        return 0
    if host == "codex":                     # codex 里恢复命令写作 $do-observe（skill）
        print(SAID[state].replace("/do-observe", "$do-observe"), file=sys.stderr)
        return 2
    if host == "agy":
        print(json.dumps({"injectSteps": [{"errorMessage": {"userMessage": SAID[state]}}],
                          "terminationBehavior": "terminate"}, ensure_ascii=False))
        return 0
    print(json.dumps({"decision": "block", "reason": SAID[state]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))     # 仓库根（-I -S 下按路径跑）
    from mh.synapse.hosts import forward       # 本机有转发配置就整个转给 mh 服务机（X7），不返回
    forward.maybe()
    sys.exit(main(sys.argv))
