"""agy（Google Antigravity CLI）的 hook 适配：推送、联想、拉取会话绑定。"""

import hashlib
import json
import os
from pathlib import Path
import re
import time

_USER_REQUEST = re.compile(r"<USER_REQUEST>\n?(.*?)\n?</USER_REQUEST>", re.S)
INJECTED = re.compile(r"<memory-harness>.*?</memory-harness>\s*", re.S)   # ACP 下 Zeron 加在用户输入最前面的联想（acp-prompt 的 prepend）
# 工具输出所在的 step：CLI 记成 GENERIC，ACP 按工具分成几种
OUTPUTS = ("GENERIC", "RUN_COMMAND", "VIEW_FILE", "CODE_ACTION", "MCP_TOOL")


def extract_user(text):
    """从 agy 的 <USER_REQUEST>…</USER_REQUEST> 包装里取出用户原话，去掉 mh 注入的段；没有标签时原样去空白。"""
    s = text or ""
    m = _USER_REQUEST.search(s)
    return INJECTED.sub("", m.group(1) if m else s).strip()


def is_acp(path):
    """ACP（Zeron 经 antigravity-acp）给的 transcriptPath 是 conversations/<id>.db；CLI 给的是 jsonl。"""
    p = Path(path or "")
    return p.suffix == ".db" or (p.parent.name == "conversations" and p.suffix != ".jsonl")


# —— MCP 会话归属的旁路（ACP 不认 overwrite）——
BIND_TTL = 120          # 秒：bind 记下到 MCP 取走之间最多隔多久；agy 调工具紧跟着 PreToolUse，两分钟足够


def bind_dir():
    home = Path(os.environ.get("MH_HOME", Path.home() / ".local/share/memory-harness"))   # 同 store.HOME，钩子不 import 库
    return home / "run/agy-bind"


def bind_key(tool, args):
    """(工具名, 去掉 session、cwd 的参数) → 文件名。"""
    clean = {k: v for k, v in (args or {}).items() if k not in ("session", "cwd")}
    raw = tool + "\n" + json.dumps(clean, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def remember(tool, args, session, cwd, folder=None, now=None):
    folder = folder or bind_dir()
    now = time.time() if now is None else now
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.json"):
        try:
            if now - old.stat().st_mtime > BIND_TTL:
                old.unlink()
        except OSError:
            pass
    tmp = folder / f".{bind_key(tool, args)}.{os.getpid()}"
    tmp.write_text(json.dumps({"session": session, "cwd": cwd, "at": now}, ensure_ascii=False))
    tmp.replace(folder / f"{bind_key(tool, args)}.json")


def take(tool, args, folder=None, now=None):
    """取走 bind 记下的 {session, cwd}；没有或过期 → {}。"""
    f = (folder or bind_dir()) / f"{bind_key(tool, args)}.json"
    now = time.time() if now is None else now
    try:
        d = json.loads(f.read_text())
        f.unlink()
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) and now - d.get("at", 0) <= BIND_TTL else {}


def full_transcript(path, session_id=""):
    """agy 的钩子给的是截断版 transcript.jsonl（ACP 给的是 conversations/<id>.db）。
    优先返回同目录或 brain/<id>/.system_generated/logs/ 下的 transcript_full.jsonl；绝不返回二进制 .db。"""
    if not path:
        return None
    p = Path(path)
    if is_acp(p):
        cid = session_id or p.stem
        logs = p.parent.parent / "brain" / cid / ".system_generated/logs"
        for name in ("transcript_full.jsonl", "transcript.jsonl"):
            cand = logs / name
            if cand.exists():
                return cand
        return None
    full = p.with_name("transcript_full.jsonl") if p.name == "transcript.jsonl" else p
    return full if full.exists() else (p if p.exists() else None)


def is_first_invocation(path, invocation_num=None, session_id=""):
    """PreInvocation 在同一轮的每次模型调用前都会跑（含工具返回后的续调）。
    判断这次是不是用户刚说话后的第一次调用：看 transcript 里最后一条 USER_INPUT 之后有没有出现过 PLANNER_RESPONSE 或 GENERIC；
    读不到 transcript 时回退看 invocationNum（0 或 1）。"""
    p = full_transcript(path, session_id)
    if p is not None:
        last_user, after = False, False
        try:
            with p.open(encoding="utf-8", errors="replace") as f:
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
                    elif last_user and (t == "PLANNER_RESPONSE" or t in OUTPUTS):
                        after = True
            if last_user:
                return not after
        except OSError:
            pass
    return invocation_num in (None, 0, 1)


def read_context(path, prompt, session_id=""):
    """读取 transcript 的可见消息、工具调用参数和工具输出；遇到当前这句用户输入本身时记 found=True 并跳过。"""
    p = full_transcript(path, session_id)
    if p is None:
        return "", None, False
    texts, found = [], False
    want = extract_user(prompt)
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                t = d.get("type")
                if t == "USER_INPUT":
                    said = extract_user(d.get("content", ""))
                    if said == want:
                        found = True
                        continue
                    if said:
                        texts.append(said)
                elif t == "PLANNER_RESPONSE":
                    if d.get("content"):
                        texts.append(d["content"])
                    for tc in d.get("tool_calls") or []:
                        if isinstance(tc, dict) and tc.get("args"):
                            texts.append(json.dumps(tc["args"], ensure_ascii=False))
                elif t in OUTPUTS:
                    if d.get("content"):
                        texts.append(d["content"])
    except (OSError, TypeError):
        return "", None, False
    return "\n".join(texts), None, found
