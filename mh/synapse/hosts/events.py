"""宿主事件的影子记录：工具调用前后、一批工具结束、回复写完、子代理结束、压缩、换模型。只记录，不输出任何东西。

为什么要记（2026-10-10）：Cognition 递归层的函数（“A ⊬ B”）要在“我”快宣布 B 之前提醒，而不是等用户下一次开口。
各个 hook 的送达时机不同（tmp/live/调研-函数提醒通道-2026-10-10.md，本机 2.1.292 实测）：
- PostToolUse / PostToolUseFailure / PostToolBatch：工具结果之后、下一次模型调用之前，不打断；
- PreToolUse：上下文和工具结果一起到，想真拦住只能 deny；
- Stop / SubagentStop：回复写完以后，给了上下文就会多出一轮，用户看得见。
先把这些 hook 都接上影子版本，攒下真实的事件，到时候一次看清哪类内容适合哪个 hook。

纪律：不往 stdout 写任何东西（写了就可能被当成附加上下文）；任何错误都吞掉、退出码 0，绝不影响宿主。
每次调用都会走一遍，所以只用标准库、只追加一行 JSON；按天分文件，不碰 SQLite（并发的工具调用会同时触发）。
宿主直接跑这个文件（python3 -I -S …/events.py，约 30ms）；用 -m mh.synapse.hosts.events 会顺带导入 mh.synapse，要 110ms 左右。
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

DIR = Path(os.environ.get("MH_HOME", Path.home() / ".local/share/memory-harness")) / "events"
CUT = 400           # 每个字段留的头部字数
TAIL = 200          # 输出类字段另留的尾部字数（结论、退出码多在末尾）
MESSAGE = 3000      # Stop 的 last_assistant_message 留多少字：要看回复里有没有宣布 B，整条留得多些
KNOWN = {"session_id", "transcript_path", "cwd", "hook_event_name", "prompt_id", "permission_mode", "effort",
         "scratchpad_dir", "agent_id", "agent_type", "tool_name", "tool_input", "tool_response", "tool_use_id",
         "error", "tool_calls", "last_assistant_message", "stop_hook_active", "background_tasks", "session_crons",
         "duration_ms", "model", "trigger", "from_model", "to_model"}


def _cut(v, head=CUT, tail=0):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    if len(s) <= head + tail:
        return s
    return s[:head] + f"…〔共 {len(s)} 字〕…" + (s[-tail:] if tail else "")


def _fields(d, head=CUT, tail=0):
    """字典按字段截断（保留结构，便于以后按字段匹配触发模式）；不是字典就整体截断。"""
    if isinstance(d, dict):
        return {k: _cut(v, head, tail) if isinstance(v, (str, list, dict)) else v for k, v in d.items()}
    return _cut(d, head, tail) if d is not None else None


def record(d):
    ev = d.get("hook_event_name", "")
    rec = {"at": datetime.now().astimezone().isoformat(timespec="milliseconds"), "event": ev,
           "session": d.get("session_id", ""), "prompt_id": d.get("prompt_id"), "cwd": d.get("cwd", ""),
           "agent": d.get("agent_type") or d.get("agent_id")}
    if d.get("host"):
        rec["host"] = d["host"]
    if "tool_name" in d:
        rec["tool"] = d["tool_name"]
        rec["tool_use_id"] = d.get("tool_use_id")
        rec["input"] = _fields(d.get("tool_input"))
    if "tool_response" in d:
        rec["response"] = _fields(d["tool_response"], CUT, TAIL)
    if "error" in d:
        rec["error"] = _cut(d["error"], CUT, TAIL)
    if "duration_ms" in d:
        rec["ms"] = d["duration_ms"]
    if "tool_calls" in d:     # 一批里每个调用的详情已在 PostToolUse 里，这里只记这一批有哪些
        rec["batch"] = [[c.get("tool_name"), c.get("tool_use_id")] for c in d["tool_calls"] or []]
    if "last_assistant_message" in d:
        rec["message"] = _cut(d["last_assistant_message"] or "", MESSAGE, TAIL)
        rec["stop_hook_active"] = d.get("stop_hook_active")
        rec["background_tasks"] = _fields(d.get("background_tasks"))
        rec["session_crons"] = _fields(d.get("session_crons"))
    for k in ("model", "trigger", "from_model", "to_model"):
        if k in d:
            rec[k] = d[k]
    new = sorted(set(d) - KNOWN)
    if new:                   # 宿主新加的字段：留名字和一小段，升级后能发现
        rec["other"] = {k: _cut(d[k], 120) for k in new}
    return rec


def hook(stdin=sys.stdin):
    start = time.monotonic()
    try:
        rec = record(json.load(stdin))
        rec["hook_ms"] = round((time.monotonic() - start) * 1000, 1)
        DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        with open(DIR / f"{rec['at'][:10]}.jsonl", "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        try:
            DIR.mkdir(parents=True, exist_ok=True)
            with open(DIR / "errors.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().astimezone().isoformat()} {type(e).__name__}: {e}\n")
        except Exception:
            pass


if __name__ == "__main__":
    hook()
    sys.exit(0)
