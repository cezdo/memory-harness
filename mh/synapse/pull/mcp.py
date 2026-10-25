"""拉取工具的 MCP 服务（stdio）：evoke、relive、roam（v0.12 以前叫 search、get、map）做成工具，“我”直接调用，不再手写 `PYTHONPATH=… python3 -m mh get --session …`
（2026-10-15：推送用 hook，拉取用自己写的 MCP 服务，尽量减少手写命令的损耗）。

- 不依赖 MCP SDK：stdio 传输就是一行一条 JSON-RPC 消息，只需 initialize、tools/list、tools/call、ping 四种，自己写，不新建 venv。
- 会话编号取宿主给子进程的环境变量 CLAUDE_CODE_SESSION_ID，足迹照旧记在这段会话上（目的 interactive）；
  取不到时，工具参数里可以给 session，再不给就记为 dev。
- 库：新库 engram.db，经 mh/synapse/pull 取（2026-10-19 换库，E1a；旧库 E4 归档）。环境变量 MH_DB 可以换（测试用拷贝，不在正式库上留足迹）。
- 进程常驻于一段会话：pull 的读视图按库的变化缓存，get、map 不用每次重建。
- 出错不让会话断：返回 isError 和错误文字，进程继续。
- feedback（2026-10-19）：和拉取工具并列的反馈入口，写进新库 engram.db（Engram 的 form.feedback，Synapse 只是转交）；
  主会话的模型由调用方填。现在只接话题（会话入口，T5）。
注册（用户级，所有工作区都能用；观察者的 claude -p 带 --strict-mcp-config 和独立配置目录，不会加载它）：
  claude mcp add --scope user mh -- env PYTHONPATH=<仓库目录> python3 -m mh.synapse.pull.mcp
dsh（2026-10-15）：一个 dsh 进程里所有会话共用一个服务进程，环境变量也会被宿主清洗，拿不到会话编号；
  所以不走 dsh 自带的 MCP 客户端，由 mh/synapse/hosts/dsh/ 插件自己连这个服务，每次调用把会话编号放进参数 session，
  并去掉子进程环境里的 CLAUDE_CODE_SESSION_ID（dsh web 若从 Claude Code 里启动，会继承到那段 Claude 会话的编号）。
"""
import json
import os
import sys
import traceback
from pathlib import Path

from .. import library
from ...engram.store.instance import USER_NAME

NAME, PROTOCOL = "mh", "2025-06-18"

WINDOW = {      # 三个拉取工具共用的参数
    "limit": {"type": "integer", "description": "每页几条，默认 8，最多 30"},
    "page": {"type": "integer", "description": "第几页，从 1 起"},
    "note": {"type": "string", "description": "一句话意见"}}

TOOLS = [
    {"name": "evoke",
     "description": "按词找记忆，按相关度排，命中给全文：线 L、话题 L:T、骨架 k- 在前，陈述 L:T:S 在后，旧的 J 放在最后的“联想”里。"
                    "几个词一起给，不必同时出现，带上换个说法的词。",
     "inputSchema": {"type": "object", "properties": {
         "words": {"type": "array", "items": {"type": "string"}, "description": "要查的词"},
         "grain": {"type": "string", "enum": ["L", "T", "S"], "description": "最小给到哪一层：L 线和骨架，T 话题，S 陈述（默认）"},
         **WINDOW},
         "required": ["words"]}},
    {"name": "relive",
     "description": "按句柄读记忆库（不是文件），不按相关度：陈述给它前后各 3 条（▶ 是要的那条），话题从头按顺序读，线、骨架给下一层，E、J 给正文。"
                    "句柄：线 L3，话题 L3:T5 或 T5，陈述 L3:T5:S1 或 T5:S1（一段 L3:T5:S1-S8），骨架 k-，E5，J5；名字逐字唯一也认。",
     "inputSchema": {"type": "object", "properties": {
         "handles": {"type": "array", "items": {"type": "string"}, "description": "句柄，一个或几个"},
         "source": {"type": "boolean", "description": "来源：陈述依据的 E；J 的旧会话"},
         "said": {"type": "boolean", "description": "走到原话"},
         "recall": {"type": "string", "description": "历史：all 或 N（陈述何时写出、还在不在；话题的旧版；J 的关系链）"},
         **WINDOW},
         "required": ["handles"]}},
    {"name": "roam",
     "description": "一层层走记忆的地图：工作区 → 骨架 → 线 → 话题。",
     "inputSchema": {"type": "object", "properties": {
         "node": {"type": "string", "description": "工作区名或句柄；不给就列出各工作区"},
         **WINDOW}}},
    {"name": "feedback",
     "description": "话题记录有误时改写或申请重做；scope=presentation 时给 use 编号与理由，只记录呈现反馈，保留来源记录。",
     "inputSchema": {"type": "object", "properties": {
         "handle": {"type": "string", "description": "scope=record 时必填，话题句柄，如 T5"},
         "scope": {"type": "string", "enum": ["record", "presentation"], "description": "反馈对象，默认 record；presentation 指交付措辞"},
         "use": {"type": "integer", "description": "scope=presentation 时必填，送达的 use 编号"},
         "reason": {"type": "string", "description": "为什么不对"},
         "name": {"type": "string", "description": "改好的名字"},
         "topic": {"type": "string", "description": "改好的主题"},
         "stops": {"type": "array", "items": {"type": "string"}, "description": "改好的停在，可以带“（陈述3、陈述4）”指出依据"},
         "holder": {"type": "string", "description": f"谁说的：我（默认）或{USER_NAME}"},
         "model": {"type": "string", "description": "你的模型名"}},
         "required": ["reason"]}},
]


def tools():
    return TOOLS


def _session(args):
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or args.get("session") or ""


HIDDEN = ("session", "cwd")       # 宿主、测试传的，不在 schema 里


class ArgError(ValueError):
    """参数不对：直接回给调用方，不带调用栈（2026-10-24：relive 写成 handle 时什么都不回，看不出错在哪）。"""


def check_args(name, args):
    spec = next((t["inputSchema"] for t in TOOLS if t["name"] == name), None)
    if spec is None:
        return
    props = spec["properties"]
    extra = [k for k in args if k not in props and k not in HIDDEN]
    missing = [k for k in spec.get("required", []) if args.get(k) in (None, "", [])]
    if extra or missing:
        bad = "；".join([*(f"没有参数 {k}" for k in extra), *(f"缺 {k}" for k in missing)])
        want = "，".join(f"{k}（{v.get('description', v.get('type', ''))}）" if k in spec.get("required", []) else k
                        for k, v in props.items())
        raise ArgError(f"{name} 参数不对：{bad}。参数有：{want}")


def call(conn, name, args):
    """执行一个工具，返回文字。"""
    check_args(name, args)
    kw = dict(session=_session(args), purpose="interactive", cwd=args.get("cwd") or os.getcwd(), note=args.get("note") or "")
    if name in ("evoke", "relive", "roam"):
        return _call_new(conn, name, args, kw)
    if name == "feedback":
        scope = args.get("scope", "record")
        if scope == "presentation":
            from .. import track
            from . import VERSION
            return track.presentation_feedback(conn, VERSION, args.get("use"), str(args.get("reason") or ""), **{k: kw[k] for k in ("session", "cwd", "note")})
        if scope != "record" or not args.get("handle") or not args.get("reason"):
            raise ValueError("记录反馈需要 handle 与 reason；呈现反馈需要 scope=presentation、use 与 reason")
        from ...engram import form, store as estore
        econn = estore.connect(estore.DEFAULT_DB)
        try:
            stops = args.get("stops")
            stops = [stops] if isinstance(stops, str) else stops
            outward = lambda t: t.replace("会话入口（T 编号", "话题（T 编号")      # 对外会话入口叫话题
            try:
                return outward(form.feedback(econn, estore.main_operator(econn, args.get("model") or estore.UNKNOWN),
                                             str(args.get("handle", "")),
                                             args.get("reason") or "", name=args.get("name"), topic=args.get("topic"),
                                             stops=stops, holder=args.get("holder") or "我"))
            except ValueError as e:
                raise ValueError(outward(str(e))) from None
        finally:
            econn.close()
    raise ValueError(f"没有工具 {name}")


def _call_new(conn, name, args, kw):
    from .. import pull
    win = {"limit": args.get("limit"), "page": args.get("page")}
    if name == "evoke":
        words = args.get("words") or []
        words = words.split() if isinstance(words, str) else [str(w) for w in words]
        return pull.evoke(conn, words, grain=args.get("grain"), **win, **kw)
    if name == "relive":
        hs = args.get("handles") or []
        hs = hs.split() if isinstance(hs, str) else [str(h) for h in hs]
        recall = args.get("recall")
        recall = None if recall in (None, "", False) else str(recall)
        return pull.relive(conn, hs, said=bool(args.get("said")), source=bool(args.get("source")), recall=recall, **win, **kw)
    return pull.roam(conn, args.get("node") or None, **win, **kw)


def handle(conn, msg):
    """一条 JSON-RPC 消息 → 回复（通知返回 None）。"""
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:                         # 通知（notifications/initialized 等）不回
        return None
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        from .. import pull
        result = {"protocolVersion": want, "capabilities": {"tools": {}},
                  "serverInfo": {"name": NAME, "version": pull.VERSION}}             # 不给 instructions：用法在手记的“工具”里，这里再写一遍就重复了（用户 2026-10-25）
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tools()}
    elif method == "tools/call":
        p = msg.get("params") or {}
        try:
            text, err = call(conn, p.get("name"), p.get("arguments") or {}), False
        except ArgError as e:
            text, err = str(e), True
        except Exception as e:
            text, err = f"出错：{e!r}\n{traceback.format_exc(limit=3)}", True
        result = {"content": [{"type": "text", "text": text}], "isError": err}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"不支持 {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=sys.stdin, stdout=sys.stdout):
    conn = library()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "不是 JSON"}}
        else:
            out = handle(conn, msg) if isinstance(msg, dict) else None
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve()
