"""拉取工具的 MCP 服务（stdio）：evoke、relive、roam（v0.12 以前叫 search、get、map）做成工具，“我”直接调用，不再手写 `PYTHONPATH=… python3 -m mh get --session …`
（2026-10-15：推送用 hook，拉取用自己写的 MCP 服务，尽量减少手写命令的损耗）。

- 不依赖 MCP SDK：stdio 传输就是一行一条 JSON-RPC 消息，只需 initialize、tools/list、tools/call、ping 四种，自己写，不新建 venv。
- Claude Code 从环境和钩子记录取会话号，其余宿主经内部参数或 agy 旁路传入；宿主随请求记进足迹。
  交互拉取没有会话号时记为 dev。
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
import concurrent.futures
import contextvars
import io
import json
import os
import socket
import sys
import threading
import traceback
from pathlib import Path

from .. import library
from ...engram.store.instance import USER_NAME, unplugged

NAME, PROTOCOL = "mh", "2025-06-18"
CODEX_HINT = "memory-harness memory tools (evoke, relive, roam, yorimichi, shiori); usage is in the handnote's tools section."

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


# 这条连接是谁（2026-11-26 试 relay）：直接起的进程就是本进程自己；经 relay 连进常驻服务的，用 relay 开头报的那份。
# 只认这几个环境变量和 cwd、父进程号：认宿主、会话、目录用（_host、_session、unplugged）。
ENV_KEYS = ("MH_HOST", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "MH_HOST_PID", "MH_DEVICE", "MH_CLIENT_HOME")
_CTX = contextvars.ContextVar("mcp_ctx", default=None)


def _env(k):
    ctx = _CTX.get()
    return (ctx["env"].get(k) if ctx else os.environ.get(k)) or None


def _cwd():
    ctx = _CTX.get()
    return ctx["cwd"] if ctx else os.getcwd()


def _ppid():
    ctx = _CTX.get()
    return ctx["ppid"] if ctx else os.getppid()


def _host(args):
    return (args.get("host") or _env("MH_HOST")
            or ("claude" if _env("CLAUDE_CODE_SESSION_ID") else "")
            or ("codex" if _env("CODEX_THREAD_ID") else ""))


def _session(args, host):
    """Claude Code：钩子按宿主进程记下的当前会话（/clear 以后环境变量是旧的），没有再用环境变量；别的宿主经参数给。"""
    if host == "claude" and _env("CLAUDE_CODE_SESSION_ID"):
        from ..hosts import claude
        return claude.current(_env("MH_HOST_PID") or _ppid()) or _env("CLAUDE_CODE_SESSION_ID")
    return args.get("session") or ""


HIDDEN = ("session", "cwd", "host")       # 宿主、测试传的，不在 schema 里


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
    host = _host(args)
    if host == "agy":     # ACP 不认 bind 的 overwrite：按旁路认会话（hosts/agy，2026-11-20）
        from ..hosts.agy import take
        got = take(name, args)
        if got and not args.get("session"):
            args = dict(args, session=got.get("session") or "", cwd=args.get("cwd") or got.get("cwd") or "")
    if unplugged(args.get("cwd") or _cwd()):
        return UNPLUGGED_SAID
    kw = dict(session=_session(args, host), host=host, purpose="interactive", cwd=args.get("cwd") or _cwd(), note=args.get("note") or "")
    if name in ("evoke", "relive", "roam"):
        return _call_new(conn, name, args, kw)
    if name == "yorimichi":     # 允许联想拐几次（push/yorimichi，2026-10-28）：联想的钩子按它给不给、给几条
        from ..push import yorimichi
        return yorimichi.record(kw["session"], args.get("width"), args.get("why") or "", host=host)
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
        if _env("MH_HOST") == "codex":    # codex 延迟加载只列命名空间和这句说明，空着模型认不出 mh 的工具（2026-11-26）；只给一句指路，不写用法
            result["instructions"] = CODEX_HINT
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [] if unplugged(_cwd()) else tools()}     # 不接 mh 的目录里起的（Claude Code、codex 按会话起）：不给工具
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


def serve(stdin=sys.stdin, stdout=sys.stdout, run=None):
    """run(msg) → 回复：不给就在本进程开库、直接 handle（stdio 时）；常驻服务给的是交给它那个工作线程去做。"""
    if run is None:
        conn = library()
        run = lambda m: handle(conn, m)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "不是 JSON"}}
        else:
            out = run(msg) if isinstance(msg, dict) else None
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()


SOCKET = Path(os.environ.get("MH_HOME", Path.home() / ".local/share/memory-harness")) / "run" / "mcp.sock"   # 同 store.HOME


def _context(hello):
    """relay 开头那行 → 这条连接的 ctx。只留 ENV_KEYS；宿主不是 Claude 的（codex、agy 在 MCP 配置里写 MH_HOST），
    同它们自己的 mcp 入口：会话来自工具参数，外层 Claude 会话漏进来的 CLAUDE_CODE_SESSION_ID 不算。"""
    env = {k: v for k, v in (hello.get("env") or {}).items() if k in ENV_KEYS and v}
    if env.get("MH_HOST", "claude") != "claude":
        env.pop("CLAUDE_CODE_SESSION_ID", None)
    return {"env": env, "cwd": hello.get("cwd") or "", "ppid": hello.get("ppid") or 0}


def _with_env(ctx):
    """钩子在工作线程里跑时，临时把 relay 带来的环境变量放进 os.environ（用完还原）。
    本机调时没带 MH_HOST_PID，按 relay 的 ppid 往上找 claude 进程号，让 claude.bind() 认得出。"""
    env = dict(ctx.get("env") or {})
    if not env.get("MH_HOST_PID") and ctx.get("ppid"):
        from ..hosts import claude
        pid = claude.host_pid(ctx["ppid"])
        if pid:
            env["MH_HOST_PID"] = str(pid)
    saved = {k: os.environ.get(k) for k in ENV_KEYS}
    for k in ENV_KEYS:
        if env.get(k):
            os.environ[k] = str(env[k])
        else:
            os.environ.pop(k, None)
    return saved


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def handle_hook(conn, action, raw_stdin, ctx):
    """常驻服务代跑一次性钩子（inject / associate / codex-start / codex-associate）→ (exit_code, stdout_str, stderr_str)。"""
    import io
    saved = _with_env(ctx)
    out, err, code = io.StringIO(), io.StringIO(), 0
    old_err = sys.stderr
    try:
        sys.stderr = err
        if action == "inject":
            from .. import push
            out.write(json.dumps(push.hook(conn, json.loads(raw_stdin)), ensure_ascii=False) + "\n")
        elif action == "associate":
            from ..push import associate
            code = associate.hook(io.StringIO(raw_stdin), out) or 0
        elif action in ("codex-start", "codex-associate"):
            from ..hosts.codex import __main__ as codex_main
            d = json.loads(raw_stdin)
            d["host"] = "codex"
            if not (d.get("agent_id") or d.get("agent_type")):
                if action == "codex-start":
                    from .. import push
                    json.dump(codex_main.label(push.hook(conn, d)), out, ensure_ascii=False)
                else:
                    from ..push import associate
                    buf = io.StringIO()
                    c = associate.hook(io.StringIO(json.dumps(d)), buf)
                    text = buf.getvalue()
                    if text and not c:
                        text = json.dumps(codex_main.label(json.loads(text)), ensure_ascii=False)
                    out.write(text)
                    code = c or 0
        else:
            err.write(f"mh-mcp: 不支持的钩子 {action}\n")
            code = 1
    except Exception as e:
        err.write(f"mh-mcp hook {action} 出错：{e!r}\n")
        code = 1
    finally:
        sys.stderr = old_err
        _restore_env(saved)
    return code, out.getvalue(), err.getvalue()


def daemon(path=SOCKET):
    """常驻的 MCP + 钩子服务（2026-11-26 试：原来每段会话起一个 Python 进程，各 30M；改成各会话起一个几 MB 的 relay 连到这里）。
    每条连接：第一行是 relay 报的 {"env": {…}, "cwd": …, "ppid": …}；
    - 无 mode / mode="mcp"：之后是一行一条 JSON-RPC；
    - mode="hook"：之后是钩子 stdin 原文，回复 `<exit_code> <stdout_bytes>\n<stdout><stderr>`。
    所有调用交给一个工作线程，一次做一个：库连接和 pull 的读视图缓存（按库连接存）只有一份，各会话共用，
    启动后立即异步预热读视图缓存，消除首次 evoke 的冷启动尖刺。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    os.chmod(path, 0o600)
    srv.listen()
    state = {}
    worker = concurrent.futures.ThreadPoolExecutor(1, initializer=lambda: state.setdefault("conn", library()))
    def _warm():
        from . import layers
        layers(state["conn"])

    worker.submit(_warm)                               # 启动后预热 Layers 缓存，消除首次 evoke 5s 冷启动

    def run(ctx, msg):
        def job():
            _CTX.set(ctx)
            return handle(state["conn"], msg)
        return worker.submit(contextvars.copy_context().run, job).result()

    def run_hook(ctx, action, raw_stdin):
        def job():
            _CTX.set(ctx)
            return handle_hook(state["conn"], action, raw_stdin, ctx)
        return worker.submit(contextvars.copy_context().run, job).result()

    def one(sock):
        with sock, sock.makefile("rb") as rf, sock.makefile("wb") as wf:
            try:
                head = rf.readline()
                hello = json.loads(head.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                return
            ctx = _context(hello)
            if hello.get("mode") == "hook":
                try:
                    raw = rf.read().decode("utf-8", errors="replace")
                    code, out_s, err_s = run_hook(ctx, hello.get("action") or "", raw)
                    ob, eb = out_s.encode("utf-8"), err_s.encode("utf-8")
                    wf.write(f"{code} {len(ob)}\n".encode("ascii") + ob + eb)
                    wf.flush()
                except OSError:
                    pass
                return
            try:
                serve(io.TextIOWrapper(rf, encoding="utf-8"), io.TextIOWrapper(wf, encoding="utf-8"),
                      run=lambda m: run(ctx, m))
            except (OSError, ValueError):
                pass

    while True:
        sock, _ = srv.accept()
        threading.Thread(target=one, args=(sock,), daemon=True).start()


if __name__ == "__main__":
    if sys.argv[1:2] == ["daemon"]:
        daemon()
    else:
        from mh.synapse.hosts import forward       # 本机有转发配置就整个转给 mh 服务机（X7），不返回
        forward.maybe()
        serve()
