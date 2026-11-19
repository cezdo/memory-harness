"""python3 -m mh.synapse.hosts.agy <start|associate|bind|mcp|acp-session|acp-prompt>。acp-* 给 Zeron 调（ACP 下钩子注入不了），约定见 Synapse 设计“宿主接入”agy。"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from . import INJECTED, extract_user, is_acp, is_first_invocation, remember


def _norm(raw):
    cwd = raw.get("cwd") or (raw.get("workspacePaths") or [""])[0] or ""
    if "sessionId" in raw:          # Zeron 调 acp-session / acp-prompt：ACP 的字段名，没有 transcriptPath
        sid = raw.get("sessionId") or ""
        return {"host": "agy", "session_id": sid, "cwd": cwd, "transcript_path": acp_transcript(sid),
                "model": "", "prompt": INJECTED.sub("", raw.get("prompt") or "").strip(), "invocation_num": None}
    model = raw.get("modelName") or raw.get("model") or ""
    return {
        "host": "agy",
        "session_id": raw.get("conversationId") or raw.get("session_id") or "",
        "cwd": cwd,
        "transcript_path": raw.get("transcriptPath") or raw.get("transcript_path") or "",
        "model": "" if model == "auto" else model,
        "prompt": extract_user(raw.get("lastUserInput") or raw.get("prompt") or ""),
        "invocation_num": raw.get("invocationNum"),
    }


ACP_ROOT = Path.home() / ".gemini/antigravity-acp"


def acp_transcript(session_id):
    """ACP 会话可读的记录（brain/<id>/.system_generated/logs/）；新会话还没有时给空。"""
    if not session_id:
        return ""
    logs = ACP_ROOT / "brain" / session_id / ".system_generated/logs"
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        if (logs / name).exists():
            return str(logs / name)
    return ""


def acp_session(d):
    """建会话、续接时的手记 → {"systemPrompt": …}；Zeron 放进 _meta.antigravity.systemPrompt.append。"""
    from ... import library
    from ...push import hook
    conn = library()
    try:
        out = hook(conn, d)
    finally:
        conn.close()
    text = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
    return {"systemPrompt": text} if text else {}


def _steps(out):
    """agy 钩子格式的输出 → (给用户的一句话 或 None, 给模型的文字 或 None)。"""
    if not out:
        return None, None
    stop = ctx = None
    for st in out.get("injectSteps") or []:
        if st.get("errorMessage"):
            stop = st["errorMessage"].get("userMessage") or ""
        elif st.get("systemMessage"):
            ctx = st["systemMessage"].get("systemMessage") or ""
    return (stop if out.get("terminationBehavior") == "terminate" else None), ctx


def acp_prompt(d):
    """用户每说一句（Zeron 转发 session/prompt 之前）：observe 开关 → 联想开关 → /mhcore → 联想，
    一次判完 → {"stop": 一句话}（不转发）/ {"prepend": "<memory-harness>…"}（加在最前面）/ {}。
    CLI 下这几件分在 PreInvocation 的三个钩子里；ACP 下那三个钩子见 .db 路径就不做事。"""
    from ....engram.store.instance import unplugged
    prompt, session, cwd = d["prompt"], d["session_id"], d["cwd"]
    if not prompt or unplugged(cwd):
        return {}
    from ....engram.observe.switch import hook as observe_switch
    state = observe_switch.decide("agy", prompt, session, d["transcript_path"], cwd)
    if state:
        return {"stop": observe_switch.SAID[state]}
    from ...push import switch
    want = switch.command(prompt)
    if want:
        switch.record(want, "agy", session, prompt)
        return {"stop": switch.SAID[want]}
    from ....engram.store import HOME
    from ....engram.elevate import Kernel, handle as elevate
    from ...push import CORE
    stop, granted = _steps(elevate(Kernel(CORE, HOME), "agy", "prompt", {"conversationId": session, "cwd": cwd,
                                                                            "lastUserInput": prompt})[0])
    if stop is not None:
        return {"stop": stop}
    parts = [f"<memory-harness>\n{granted}\n</memory-harness>"] if granted else []
    from ... import library
    from ...push.associate import associate
    conn = library()
    try:
        got = associate(conn, prompt, session, cwd, d["transcript_path"] or None, host="agy")
    finally:
        conn.close()
    if got and got[0]:
        parts.append(got[0])
    return {"prepend": "\n\n".join(parts) + "\n\n"} if parts else {}


def _remember(tool, args, d):
    """ACP 不认 overwrite：把会话记一笔旁路，MCP 按工具名和参数取走（CLI 下 overwrite 照样生效）。"""
    if tool and d["session_id"]:
        try:
            remember(tool, args, d["session_id"], d["cwd"])
        except OSError:
            pass


def main():
    action = sys.argv[1]
    if action == "mcp":
        # 会话来自每次 PreToolUse 的参数；启动 agy 的外层会话环境不能拿来归属足迹。
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ["MH_HOST"] = "agy"
        from ...pull.mcp import serve
        serve()
        return
    raw, d = {}, {}
    try:
        raw = json.load(sys.stdin)
        d = _norm(raw)
        if action == "bind":
            tc = raw.get("toolCall") or {}
            name = tc.get("name") or raw.get("tool_name") or ""
            args = tc.get("args") if "args" in tc else raw.get("tool_input") or {}
            if isinstance(args, str):
                args = json.loads(args)
            # 带外壳（ServerName、ToolName、Arguments）的：CLI 懒加载时工具名是 call_mcp_tool；ACP 交给钩子的工具名
            # 已经是裸的 roam，参数却还是整个外壳（2026-11-20 直接驱动 adapter 抓到的）。两种都按外壳拆
            if isinstance(args, dict) and ("ServerName" in args or "server_name" in args):
                srv = args.get("ServerName") or args.get("server_name") or ""
                if srv != "mh":
                    json.dump({}, sys.stdout, ensure_ascii=False)
                    return
                key = "Arguments" if "Arguments" in args else ("arguments" if "arguments" in args else "Arguments")
                inner = args.get(key)
                was_str = isinstance(inner, str)
                if was_str:
                    inner = json.loads(inner) if inner.strip() else {}
                elif isinstance(inner, dict):
                    inner = dict(inner)
                else:
                    inner = {}
                _remember(args.get("ToolName") or args.get("tool_name") or name, inner, d)
                if name != "call_mcp_tool":              # ACP 不认 overwrite，只记旁路
                    json.dump({"decision": "allow"}, sys.stdout, ensure_ascii=False)
                    return
                inner["session"] = d["session_id"]
                inner["cwd"] = d["cwd"]
                patched = dict(args)
                patched[key] = json.dumps(inner, ensure_ascii=False) if was_str else inner
                json.dump({"decision": "allow", "overwrite": patched}, sys.stdout, ensure_ascii=False)
                return
            if not re.match(r"^mcp__?mh_", name):         # 裸工具名、不带外壳：只记旁路
                _remember(name, args, d)
                json.dump({"decision": "allow"}, sys.stdout, ensure_ascii=False)
                return
            _remember(re.sub(r"^mcp__?mh__?", "", name), args, d)
            args = dict(args, session=d["session_id"], cwd=d["cwd"])
            json.dump({"decision": "allow", "overwrite": args}, sys.stdout, ensure_ascii=False)
        elif action == "start":
            from ... import library
            from ...push import hook
            conn = library()
            try:
                out = hook(conn, d)
            finally:
                conn.close()
            text = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
            res = {"injectSteps": [{"systemMessage": {"systemMessage": text}}]} if text else {}
            json.dump(res, sys.stdout, ensure_ascii=False)
        elif action == "associate":
            if is_acp(d["transcript_path"]):      # ACP 不给原话、不认注入：联想由 Zeron 调 acp-prompt
                json.dump({}, sys.stdout, ensure_ascii=False)
                return
            if not is_first_invocation(d["transcript_path"], d["invocation_num"], d["session_id"]):
                json.dump({}, sys.stdout, ensure_ascii=False)
                return
            from ....engram.store.instance import unplugged
            if unplugged(d["cwd"]):
                json.dump({}, sys.stdout, ensure_ascii=False)
                return
            from ...push import switch
            code = switch.handle(d, "agy", sys.stdout, sys.stderr)
            if code is not None:
                return
            from ... import library
            from ...push.associate import associate
            conn = library()
            try:
                got = associate(conn, d["prompt"], d["session_id"], d["cwd"], d["transcript_path"], host="agy")
            finally:
                conn.close()
            if got:
                ctx, _ = got
                # 不带 metadata.user_markdown：带了 agy CLI 会把整条 systemMessage 丢掉，模型看不到、记录里也没有
                # （2026-11-20 探针 tmp/agy-probe-2026-11-20，和它自己的文档不符）；给用户看的那一行在 agy 里就不显示了
                json.dump({"injectSteps": [{"systemMessage": {"systemMessage": ctx}}]}, sys.stdout, ensure_ascii=False)
            else:
                json.dump({}, sys.stdout, ensure_ascii=False)
        elif action == "acp-session":
            json.dump(acp_session(d), sys.stdout, ensure_ascii=False)
        elif action == "acp-prompt":
            json.dump(acp_prompt(d), sys.stdout, ensure_ascii=False)
        else:
            raise ValueError(f"未知操作 {action}")
    except Exception as e:
        # hook 故障留痕并退出 0，避免阻塞用户输入或工具。
        try:
            from ....engram.store import HOME
            folder = HOME / "events"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "agy-errors.log").open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now().astimezone().isoformat()} {action} {d.get('session_id', '')} {e!r}\n")
        except Exception:
            pass
        json.dump({}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
