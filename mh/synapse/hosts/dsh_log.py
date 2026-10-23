"""读 dsh 的会话日志（~/.dsh/sessions/<workspace>/<session>/session*.jsonl.zstd）：联想读上下文、命中找工具调用都用它。
Synapse 自己读宿主文件，不经 Engram 的采集（重构 A5：各宿主的解析器归 observe，Synapse 不 import observe）。"""
import json
import subprocess
from pathlib import Path

ROOT = Path.home() / ".dsh/sessions"
RANK = {"session.v4.jsonl.zstd": 4, "session.v3.jsonl.zstd": 3, "session.jsonl.zstd": 1}   # 同一会话只认最高版本


def events(path):
    # 正在写入的文件尾帧可能不完整：zstd 返回非零，但已解出的完整行照常使用
    out = subprocess.run(["zstd", "-d", "-c", "--", str(path)], capture_output=True).stdout
    for line in out.decode("utf-8", errors="replace").splitlines():
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


DSH_CONTEXT = ("user/message", "assistant/message", "tool/call", "tool/result", "compaction/summary")


def read_context(session, prompt):
    """dsh 会话日志（~/.dsh/sessions/*/<会话>/session*.jsonl.zstd）里最后一次压缩之后的内容，同 claude.read_context 的返回。
    dsh 没有 transcript_path；宿主注入的轮次由插件按消息来源认出，不在这里判断（插件只把 source.kind 为 user 的话送来）。"""
    logs = [p for p in ROOT.glob(f"*/{session}/session*.jsonl.zstd") if p.name in RANK] if session else []
    if not logs:
        return "", None, False
    evs = list(events(max(logs, key=lambda p: RANK[p.name])))
    start = max((i for i, e in enumerate(evs) if e.get("type") == "compaction/summary"), default=0)
    want, texts, found = prompt.strip(), [], False

    def strings(x):
        if isinstance(x, str):
            yield x
        elif isinstance(x, list):
            for y in x:
                yield from strings(y)
        elif isinstance(x, dict):
            if x.get("type") == "reasoning":
                return
            for y in x.values():
                yield from strings(y)

    for e in evs[start:]:
        if e.get("type") not in DSH_CONTEXT:
            continue
        d = e.get("data") or {}
        if e["type"] == "user/message" and (d.get("source") or {}).get("kind") == "user":
            said = "\n".join(b.get("text", "") for b in d.get("content") or [] if isinstance(b, dict)).strip()
            if said == want or (said and said in want):
                found = True
                continue                                 # 这一句本身不算上下文
        texts.extend(strings(d.get("content") if e["type"] == "user/message" else d))
    return "\n".join(texts), None, found
