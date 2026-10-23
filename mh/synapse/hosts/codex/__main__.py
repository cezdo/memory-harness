"""python3 -m mh.synapse.hosts.codex <start|associate|bind|event|mcp>。"""

import io
import json
import os
import sys
from datetime import datetime


def main():
    action = sys.argv[1]
    if action == "mcp":
        # 会话来自每次 PreToolUse 的参数；启动 Codex 的外层会话环境不能拿来归属足迹。
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
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
                json.dump(out, sys.stdout, ensure_ascii=False)
            else:
                from ...push.associate import hook
                hook(io.StringIO(json.dumps(d)), sys.stdout)
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
    main()
