"""python3 -m mh.synapse.hosts.codex <start|associate|bind|event|mcp>。"""

import io
import json
import os
import sys
from datetime import datetime

# codex 把钩子的附加上下文当成一条 developer 消息放进来，模型读到的文字里没有“来自钩子”的标签（Claude Code 有）；
# 2026-11-26 服务机上 gpt-6-luna 把手记说成“你贴出的”、说没收到推送。标签外面补一句来源，标签里的正文不动（送达记的是正文）。
SOURCE = "(Pushed by memory-harness via codex hook)\n"     # 英文、只说来源（用户 2026-11-26）


def label(out):
    """给钩子输出的附加上下文前面加来源说明；没有附加上下文的原样返回。"""
    spec = (out or {}).get("hookSpecificOutput") or {}
    if spec.get("additionalContext"):
        spec["additionalContext"] = SOURCE + spec["additionalContext"]
    return out


def main():
    action = sys.argv[1]
    if action == "mcp":
        # 会话来自每次 PreToolUse 的参数；启动 Codex 的外层会话环境不能拿来归属足迹。
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ["MH_HOST"] = "codex"
        from ...pull.mcp import serve
        serve()
        return
    d = {}
    try:
        d = json.load(sys.stdin)
        d["host"] = "codex"
        if action == "bind":
            args = d.get("tool_input") or {}
            if isinstance(args, str):
                args = json.loads(args)
            args = dict(args, session=d["session_id"], cwd=d.get("cwd", ""))
            json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                               "updatedInput": args}},
                      sys.stdout, ensure_ascii=False)
        elif action == "event":
            from ..events import hook
            hook(io.StringIO(json.dumps(d)))
        elif action in ("start", "associate"):
            if d.get("agent_id") or d.get("agent_type"):
                return
            if action == "start":
                from ... import library
                from ...push import hook
                conn = library()
                try:
                    out = hook(conn, d)
                finally:
                    conn.close()
                json.dump(label(out), sys.stdout, ensure_ascii=False)
            else:
                from ...push.associate import hook
                buf = io.StringIO()
                code = hook(io.StringIO(json.dumps(d)), buf)
                text = buf.getvalue()
                if text and not code:
                    text = json.dumps(label(json.loads(text)), ensure_ascii=False)
                sys.stdout.write(text)
                if code:                                  # 联想开关的命令：codex 以退出码 2 拦下
                    sys.exit(code)
        else:
            raise ValueError(f"未知操作 {action}")
    except Exception as e:
        # hook 故障留痕并退出 0，避免阻塞用户输入或工具。
        try:
            from ....engram.store import HOME
            folder = HOME / "events"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "codex-errors.log").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now().astimezone().isoformat()} {action} {d.get('session_id', '')} {e!r}\n")
        except Exception:
            pass


if __name__ == "__main__":
    from mh.synapse.hosts import forward       # 本机有转发配置就整个转给 mh 服务机（X7），不返回
    forward.maybe()
    main()
