"""ChatGPT 静态导出：只读实例配置显式列出的 JSON 文件，沿活动分支还原轮次。"""
import json
from datetime import datetime, timezone
from pathlib import Path

from .turn import Session, Turn
from ...store import instance


def files():
    seen = set()
    for entry in instance.CHATGPT_EXPORTS:
        path = Path(entry["file"]).expanduser().resolve()
        if path in seen:
            raise ValueError(f"ChatGPT 导出重复配置：{path}")
        seen.add(path)
        yield path


def settings(path):
    for entry in instance.CHATGPT_EXPORTS:
        if Path(entry["file"]).expanduser().resolve() == path.resolve():
            if not entry.get("cwd"):
                raise ValueError("ChatGPT 导出必须显式指定 cwd，不从项目标签推断工作区")
            return entry
    raise ValueError(f"未配置的 ChatGPT 导出：{path}")


def signature(path):
    return json.dumps(settings(path), sort_keys=True, ensure_ascii=False)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts is not None else ""


def text(content):
    kind = content.get("content_type")
    if kind in ("text", "multimodal_text"):
        return "".join(p if isinstance(p, str) else f"［附件：{p.get('asset_pointer') or '未提供正文'}］"
                       for p in content.get("parts") or [])
    if kind == "reasoning_recap":
        return content.get("content") or ""
    if kind == "thoughts":
        out = []
        for thought in content.get("thoughts") or []:
            if thought.get("summary"):
                out.append(thought["summary"])
            chunks = "\n".join(c["content"] for c in thought.get("chunks") or [] if c.get("content"))
            if chunks or thought.get("content"):
                out.append(chunks or thought["content"])
        return "\n\n".join(out)
    raise ValueError(f"未适配的 ChatGPT 消息类型：{kind}")


def parse_conversation(conv, path, cwd):
    cid = conv["id"]
    session = Session("chatgpt", cid, str(path), cwd=str(Path(cwd).expanduser()), title=conv["title"],
                      started=_iso(conv.get("create_time")), model=conv.get("default_model_slug") or "")
    mapping, node = conv["mapping"], conv.get("current_node")
    if not node:
        raise ValueError(f"{cid} 没有 current_node")
    chain, seen = [], set()
    while node:
        if node in seen or node not in mapping:
            raise ValueError(f"{cid} 的活动分支断链或成环：{node}")
        seen.add(node)
        entry = mapping[node]
        if entry.get("message"):
            chain.append((node, entry["message"]))
        node = entry.get("parent")
    chain.reverse()
    turns, cur, outputs = [], None, []

    def close(closed):
        if cur is None:
            return
        cur.closed = closed
        if outputs:
            cur.assistant, cur.commentary = outputs[-1], outputs[:-1]
        turns.append(cur)

    for node, msg in chain:
        role = msg["author"]["role"]
        meta, content = msg.get("metadata") or {}, msg.get("content") or {}
        if role == "user":
            close(True)
            cur = Turn(user=text(content), started=_iso(msg.get("create_time")),
                       address={"conversation": cid, "nodes": [], "messages": [], "attachments": [],
                                "models": [], "snapshot": True}, prev=len(turns) - 1)
            outputs = []
        elif role not in ("assistant", "system"):
            raise ValueError(f"{cid} 存在未适配的角色：{role}")
        elif role == "system":
            continue                 # 宿主系统提示不冒充用户原话；原始对象保留在文件里
        if cur is None:
            raise ValueError(f"{cid} 活动分支在用户输入之前出现助手消息")
        cur.address["nodes"].append(node)
        cur.address["messages"].append(msg["id"])
        for item in meta.get("attachments") or []:
            cur.address["attachments"].append({"message": msg["id"], **item})
        if meta.get("error_metadata"):
            cur.ending = "error:" + str(meta["error_metadata"].get("error_code") or "unknown")
        if role == "assistant":
            cur.note_model(meta.get("resolved_model_slug") or meta.get("model_slug"))
            cur.address["models"].append({"message": msg["id"], "model_slug": meta.get("model_slug"),
                                         "resolved_model_slug": meta.get("resolved_model_slug")})
            body = text(content)
            if content.get("content_type") in ("thoughts", "reasoning_recap"):
                if body:
                    cur.reasoning.append(body)
            elif body.strip():
                outputs.append(body)
    # 静态快照的末轮没有运行时结束标记；有回答正文才收，输入里另说明此来源限制。
    close(bool(outputs))
    return session, turns


def parse_all(path):
    config = settings(path)
    conversations = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(conversations, list):
        raise ValueError("ChatGPT 导出应为会话对象数组")
    ids = [c["id"] for c in conversations]
    if len(ids) != len(set(ids)):
        raise ValueError("ChatGPT 导出存在重复 conversation ID")
    return [parse_conversation(c, path, config["cwd"]) for c in conversations]
