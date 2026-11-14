"""拉取工具的 MCP 服务（stdio）：evoke、relive、roam（v0.12 以前叫 search、get、map）做成工具，“我”直接调用，不再手写 `PYTHONPATH=… python3 -m mh get --session …`
（2026-10-15：推送用 hook，拉取用自己写的 MCP 服务，尽量减少手写命令的损耗）。

- 不依赖 MCP SDK：stdio 传输就是一行一条 JSON-RPC 消息，只需 initialize、tools/list、tools/call、ping 四种，自己写，不新建 venv。
- 会话编号取宿主给子进程的环境变量 CLAUDE_CODE_SESSION_ID，足迹照旧记在这段会话上（目的 interactive）；
  取不到时，工具参数里可以给 session，再不给就记为 dev。
- 库：新库 engram.db，经 mh/synapse/pull 取（2026-10-19 换库，E1a；旧库 E4 归档）。环境变量 MH_DB 可以换（测试用拷贝，不在正式库上留足迹）。
- 进程常驻于一段会话：pull 的读视图按库的变化缓存，get、map 不用每次重建。
- 出错不让会话断：返回 isError 和错误文字，进程继续。
- observe-now（2026-10-30 加，2026-11-06 撤，v0.41）：“我”让观察现在把尾巴读掉；用户 11-06：快不该放在 SSD 层，连同快道一起撤。
  以后改成调用即标记，11-13 定名 shiori（见下）。
- yorimichi（2026-10-28）：“我”在对话阶段变了时记一次允许联想拐几次（1 平常，0 只在一被打断就容易出错时，2–3 发散；2026-11-02 改成以 1 为常态），联想的钩子读它；实现在 push/yorimichi。
- shiori（2026-11-13，runbook I7）：“我”给这一轮夹栞；调用本身留在会话日志里就是标记，服务这边不写任何东西（engram/observe/shiori）。
- feedback（2026-10-19 加，2026-11-02 撤，v0.36）：现阶段反馈少、集中、用户在场，主会话没用过这个工具（130 次反馈都是开发中直接调
  form 写的），撤出 MCP；form 的反馈函数和命令行 `mh engram feedback` 留着，以后阶段变了再接（Engram 设计“反馈”）。
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
from ...engram.store.instance import USER_NAME, unplugged

NAME, PROTOCOL = "mh", "2025-06-18"

NOTE = {"note": {"type": "string", "description": "一句话意见"}}
WINDOW = {      # relive、roam 的翻页：一页固定 8 条，没有 limit（evoke 连 page 也没有，v0.17；relive、roam 去掉 limit，v0.24）
    "page": {"type": "integer", "description": "第几页，从 1 起（一页 8 条）"}, **NOTE}

TOOLS = [
    {"name": "evoke",
     "description": "按词找记忆：给话题 L:T，少而准时先给陈述 L:T:S 全文。"
                    "查词按空格切成词，每个词单独认；同时含的词越多越靠前，不必全都含，换个说法的词也可以一起给。"
                    "结果多时只给话题；页头会说去掉哪个词能多命中。结果一页 6–8 条，同一轮里给过的不再给，再查就是接着往下。"
                    "用到了其中的内容，就在正文里用〖〗标出那半句（引用回填靠它连回是哪条）。",
     "inputSchema": {"type": "object", "properties": {
         "words": {"type": "array", "items": {"type": "string"}, "description": "要查的词"},
         **NOTE},
         "required": ["words"]}},
    {"name": "relive",
     "description": "按句柄读记忆库（不是文件），不按相关度：陈述给它自己，话题从头按顺序读（超过一页先给这段对话的结构），线、骨架给下一层，E、J 给正文。"
                    "句柄：线 L3，话题 L3:T5 或 T5，陈述 L3:T5:S1 或 T5:S1（一段 L3:T5:S1-S8），骨架 k-，E5，J5，认知 C5；名字逐字唯一也认。",
     "inputSchema": {"type": "object", "properties": {
         "handles": {"type": "array", "items": {"type": "string"}, "description": "句柄，一个或几个"},
         "source": {"type": "boolean", "description": "来源：陈述依据的 E；J 的旧会话"},
         "said": {"type": "boolean", "description": "走到原话"},
         "near": {"type": "boolean", "description": "陈述：连同所在话题里前后各 3 条（▶ 是要的那条）"},
         **WINDOW},
         "required": ["handles"]}},
    {"name": "roam",
     "description": "一层层走记忆的地图：工作区 → 骨架 → 线 → 话题。",
     "inputSchema": {"type": "object", "properties": {
         "node": {"type": "string", "description": "工作区名或句柄；不给就列出各工作区"},
         **WINDOW}}},
    {"name": "yorimichi",
     "description": "寄り道：这段对话允许联想拐几次。平常是 1（没判过就是 1）；只有收窄到一被打断就容易出错的一件事（排查一个故障、逐行改一段代码）才给 0，在干活本身不算；聊开了、话题在散给 2–3。阶段变了时调一次（没变不调）。",
     "inputSchema": {"type": "object", "properties": {
         "width": {"type": "integer", "description": "0 不给联想，1 平常，2–3 发散"},
         "why": {"type": "string", "description": "一句话：为什么这么判"}},
         "required": ["width"]}},
    {"name": "shiori",
     "description": "栞：在这一轮夹一枚书签，标出它要紧、以后要能想起来（定下的事、纠正、只在这里说过的理由）。"
                    "调用本身留在会话记录里就是标记：观察会精读这一轮；落库以前，手记近况的“未落库”一节列着它。不搬运内容，不用写正文。",
     "inputSchema": {"type": "object", "properties": {
         "why": {"type": "string", "description": "一句话：这一轮为什么要紧"}},
         "required": ["why"]}},
]


def tools():
    return TOOLS


def _session(args):
    """Claude Code：钩子按宿主进程记下的当前会话（/clear 以后环境变量是旧的），没有再用环境变量；别的宿主经参数给。"""
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):
        from ..hosts import claude
        return claude.current() or os.environ["CLAUDE_CODE_SESSION_ID"]
    return args.get("session") or ""


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


UNPLUGGED_SAID = "这个目录不接 mh（实例配置 [connect].unplugged），记忆工具在这里不用。"


def call(conn, name, args):
    """执行一个工具，返回文字。"""
    check_args(name, args)
    if unplugged(args.get("cwd") or os.getcwd()):
        return UNPLUGGED_SAID
    kw = dict(session=_session(args), purpose="interactive", cwd=args.get("cwd") or os.getcwd(), note=args.get("note") or "")
    if name in ("evoke", "relive", "roam"):
        return _call_new(conn, name, args, kw)
    if name == "yorimichi":     # 允许联想拐几次（push/yorimichi，2026-10-28）：联想的钩子按它给不给、给几条
        from ..push import yorimichi
        return yorimichi.record(kw["session"], args.get("width"), args.get("why") or "",
                           host="claude" if os.environ.get("CLAUDE_CODE_SESSION_ID") else "")
    if name == "shiori":        # 栞（I7，2026-11-13）：调用本身就是标记，这里什么都不写（engram/observe/shiori）
        return "夹好了。这一轮会被精读；落库以前，手记近况的“未落库”里看得到它。"
    raise ValueError(f"没有工具 {name}")


def _call_new(conn, name, args, kw):
    from .. import pull
    win = {"page": args.get("page")}
    if name == "evoke":
        words = args.get("words") or []
        words = words.split() if isinstance(words, str) else [str(w) for w in words]
        return pull.evoke(conn, words, **kw)      # grain 只走内部，不进 MCP（2026-11-02：主会话不该知道）
    if name == "relive":
        hs = args.get("handles") or []
        hs = hs.split() if isinstance(hs, str) else [str(h) for h in hs]
        recall = args.get("recall")
        recall = None if recall in (None, "", False) else str(recall)
        return pull.relive(conn, hs, said=bool(args.get("said")), source=bool(args.get("source")), recall=recall,
                           files=bool(args.get("files")), near=bool(args.get("near")), **win, **kw)
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
        result = {"tools": [] if unplugged(os.getcwd()) else tools()}     # 不接 mh 的目录里起的（Claude Code、codex 按会话起）：不给工具
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
