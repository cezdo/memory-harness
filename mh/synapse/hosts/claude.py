"""Claude Code 的会话记录（transcript）。"""
import json
from pathlib import Path


def read_context(path, prompt):
    """transcript 里最后一次压缩之后的内容（模型现在看得到的），以及这一句是不是宿主注入的。
    返回 (上下文文本, 宿主注入的类型或 None, transcript 里有没有找到这一句)。"""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, TypeError):
        return "", None, False
    start = max((i for i, l in enumerate(lines) if '"compact_boundary"' in l), default=0)
    texts, host, found, last = [], None, False, None
    want = prompt.strip()

    def strings(x, skip=("thinking", "signature")):
        if isinstance(x, str):
            yield x
        elif isinstance(x, list):
            for y in x:
                yield from strings(y)
        elif isinstance(x, dict):
            if x.get("type") in skip:
                return
            for k, y in x.items():
                if k not in skip:
                    yield from strings(y)

    for l in lines[start:]:
        try:
            d = json.loads(l)
        except ValueError:
            continue
        t = d.get("type")
        if t == "system":
            last = d.get("subtype")
        elif t == "user":
            c = d.get("message", {}).get("content")
            said = c if isinstance(c, str) else "".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
            if said.strip() == want:
                found = True
                kind = (d.get("origin") or {}).get("kind")
                host = ("宿主（isMeta）" if d.get("isMeta") else
                        f"宿主（{kind}）" if kind is not None and kind != "human" else None)
                continue                                 # 这一句本身不算上下文
            last = "user"
            texts.extend(strings(d.get("message", {}).get("content")))
        elif t == "assistant":
            texts.extend(strings(d.get("message", {}).get("content")))
        elif t == "attachment":
            a = d.get("attachment") or {}
            if a.get("type") != "hook_system_message":    # 联想那一行只给用户看，模型看不到
                texts.extend(strings(a))
    if not found and last == "scheduled_task_fire":
        host = "定时任务"
    return "\n".join(texts), host, found
