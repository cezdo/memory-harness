"""Codex 的 hook 适配：推送、联想、拉取会话绑定。"""

import json
from pathlib import Path


def read_context(path, prompt):
    """读取 rollout 的可见消息和工具结果；压缩后从 replacement_history 重新开始。"""
    texts, found = [], False
    want = prompt.strip()

    def item(p):
        nonlocal found
        kind = p.get("type")
        if kind == "message":
            content = p.get("content") or []
            parts = [b.get("text", "") for b in content if isinstance(b, dict)
                     and b.get("type") in ("input_text", "output_text", "text")]
            said = "\n".join(parts).strip()
            if p.get("role") == "user" and said == want:
                found = True
                return
            texts.extend(parts)
        elif kind in ("function_call", "custom_tool_call"):
            value = p.get("arguments") if kind == "function_call" else p.get("input")
            if isinstance(value, str):
                texts.append(value)
        elif kind in ("function_call_output", "custom_tool_call_output"):
            value = p.get("output")
            if isinstance(value, str):
                texts.append(value)
            elif isinstance(value, list):
                texts.extend(b.get("text", "") for b in value if isinstance(b, dict))

    try:
        with Path(path).open(encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                p = d.get("payload") or {}
                if d.get("type") == "compacted":
                    texts.clear()
                    found = False
                    history = p.get("replacement_history")
                    if isinstance(history, list):
                        for entry in history:
                            item(entry)
                    elif isinstance(p.get("message"), str):
                        texts.append(p["message"])
                elif d.get("type") == "response_item":
                    item(p)
    except (OSError, TypeError):
        return "", None, False
    return "\n".join(texts), None, found
