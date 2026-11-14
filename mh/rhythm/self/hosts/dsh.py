"""self 的 dsh 接入（design/rhythm/self/接入.md“dsh”，“我”在 dsh 里写，2026-11-08）。

接口照接入标准：NAME、ready、new_session、resumable、launch、outcome、actions。
用 dsh 的 headless profile（dsh --profile headless）：一次任务、打印结果、退出，没有 Host 和浏览器层。
独立 DSH_HOME（数据目录下 dsh-self-home）：会话不落在交互用的 ~/.dsh 里，采集多读这一处（observe/sources/dsh.SELF_ROOT）。
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from ....engram import store
from .. import confine

NAME = "dsh"
REPO = Path(__file__).resolve().parents[4]
PROVIDER = "opencode-go"
MODEL = "deepseek-v4.1-flash"
RESUME_BYTES = 3_000_000           # 上一段会话的记录超过这么大就另开一段（同 Claude、Codex，没调过）
WRITES = ("write", "edit")         # apply_patch 的输入是补丁文本，没有单个 file_path，第一版不拆
SESSIONS = "sessions"              # session-persistence-jsonl 的 root：$DSH_HOME/sessions


def _patch(creds):
    """headless profile 的 patch：模型走 OpenCode Go（和宿主一样），恢复凭据存储（读用户那份，不复制），
    关掉给会话起标题的那次模型请求。"""
    return f"""# self 的 headless profile（generated，mh/rhythm/self/hosts/dsh.py）
- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: {PROVIDER}
    model: {MODEL}
    reasoningEffort: high
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      {PROVIDER}:
        apiKeyEnv: OPENCODE_GO_API_KEY
        models:
          - id: {MODEL}
            name: oc·DeepSeek V4.1 Flash
            contextWindow: 1000000
            maxTokens: 65536
            input:
              - text
              - image
# 默认的 headless 关掉凭据存储，模型就读不到 key；恢复它，只读用户那份（不复制密钥）。
- id: credentials
  name: '@deepseek-ai/dsh-credentials-local'
  disabled: false
  config:
    path: {json.dumps(str(creds))}
- id: session-title-llm
  disabled: true
"""


def home(base=None):
    """独立 DSH_HOME（数据目录下 dsh-self-home）：headless profile 照代码重写，mh-synapse 用链接装进来，
    凭据读用户那份 ~/.dsh/.credentials.yaml。会话落在这个 home 的 sessions/ 下，采集多读这一处。"""
    h = Path(base or store.HOME / "dsh-self-home")
    prof = h / "profiles" / "headless"
    (prof / "node_modules").mkdir(parents=True, exist_ok=True)
    (h / SESSIONS).mkdir(parents=True, exist_ok=True)
    synapse = REPO / "mh/synapse/hosts/dsh"
    link = prof / "node_modules" / "mh-synapse"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(synapse)
    (prof / "package.json").write_text(json.dumps({
        "name": "dsh-profile-headless", "private": True,
        "dependencies": {"mh-synapse": "link:" + str(synapse)},
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless", "mh-synapse"]}}},
        ensure_ascii=False, indent=2))
    (prof / "pnpm-workspace.yaml").write_text("packages:\n  - .\n\nnodeLinker: hoisted\nautoInstallPeers: false\n")
    creds = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh") / ".credentials.yaml"
    if not creds.exists():
        creds = Path.home() / ".dsh/.credentials.yaml"
    (prof / "cordis.patch.yml").write_text(_patch(creds))
    return h


def ready():
    """能不能叫：没装、没登录（凭据读不到）不叫；有 quota 时 OpenCode Go 吃紧不叫。"""
    if not shutil.which("dsh"):
        return "dsh 没装"
    if not (Path.home() / ".dsh/.credentials.yaml").exists():
        return "dsh 未配置凭据"
    try:
        from ... import BUSY_5H, quota
    except ImportError:
        return ""                       # 公开包不带 quota：交给宿主报额度错误
    q = quota.read([PROVIDER])[0]
    if "error" in q:
        return "OpenCode Go 额度读不到：" + q["error"]
    used = {w["name"]: w["used"] for w in q["windows"]}
    return "OpenCode Go 额度吃紧" if not q.get("ok", True) or used.get("5h", 0) >= BUSY_5H else ""


def new_session():
    """新会话的编号：headless 的 --session-id 只能续接已有的会话（未知编号直接报错），新编号由它自己分配，
    跑完从 final 事件读出来。"""
    return ""


def _files(session, h=None):
    """这段会话的记录：会话目录以编号命名，里面是 session*.jsonl.zstd。"""
    return sorted((Path(h or store.HOME / "dsh-self-home") / SESSIONS).glob(f"*/{session}/session*.jsonl.zstd"))


def resumable(session, h=None):
    """这段会话能不能接着：记录还在、没超过 RESUME_BYTES。"""
    return bool(session) and any(p.stat().st_size < RESUME_BYTES for p in _files(session, h))


def _lines(path):
    """会话记录是 zstd：正在写入时尾部可能不完整，zstd 返回非零，已解出的完整行照常使用。"""
    out = subprocess.run(["zstd", "-d", "-c", "--", str(path)], capture_output=True).stdout
    return out.decode("utf-8", "replace").splitlines()


def _events_after(path, skip=0, limit=None):
    for i, line in enumerate(_lines(path)):
        if i < skip:
            continue
        if limit is not None and i - skip >= limit:
            return
        try:
            yield json.loads(line)
        except ValueError:
            continue


def launch(root, text, session, resumed, logs):
    """在后台起 dsh headless：cwd 是 self，输入是 text 文件，stdout 是 JSON 事件流（logs/run.jsonl）。
    danger-full-access + approval never：读得到家目录，写哪里由 self 约定和跑后核查保证，中间不等人点头。
    外层会话的身份变量不传进去（DSH_SESSION_ID 等），免得把后台会话挂到当前这段会话上。"""
    h = home()
    logs = Path(logs)
    # 记下每个会话记录的行数起点：续接时核查不重算上一轮，子代理的新文件从 0 读。
    (logs / "dsh.json").write_text(json.dumps({"home": str(h), "root": str(Path(root).resolve()),
        "offsets": {str(p): len(_lines(p)) for p in Path(h, SESSIONS).glob("*/*/session*.jsonl.zstd")}}))
    args = ["dsh", "--profile", "headless", "--json"]
    if resumed and session:
        args += ["--session-id", session]          # 续接：adopt 这段已有的会话
    args += ["--permission-mode", "danger-full-access", "--approval-policy", "never"]
    env = dict(os.environ, DSH_HOME=str(h))
    for key in ("DSH_SESSION_ID", "DSH_PROFILE", "DSH_PROFILE_DIR", "DSH_WEB_URL", "DSH_SHELL"):
        env.pop(key, None)
    with open(text) as stdin, (logs / "run.jsonl").open("w") as out, (logs / "run.err").open("w") as err:
        return subprocess.Popen(confine.wrap(args, root, logs, [h]), stdin=stdin, stdout=out, stderr=err, cwd=root,
                                env=env, start_new_session=True)


def _events(path, skip=0):
    """run.jsonl（JSON 事件流，未压缩）。"""
    if not path.exists():
        return
    for i, line in enumerate(path.read_text(errors="replace").splitlines()):
        if i < skip:
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def outcome(logs):
    """(正常结束没有, 最后一句, 会话编号)：读最后的 final 事件；reason 是 null 或 completed 算正常结束。"""
    ok, said, session = False, "", ""
    for e in _events(Path(logs) / "run.jsonl"):
        if e.get("type") == "session":
            session = e.get("sessionId") or session
        elif e.get("type") == "final":
            ok = (e.get("reason") or {}).get("kind") in (None, "completed")
            said = (e.get("text") or "").strip()
            session = e.get("sessionId") or session
    return ok, said, session


def _heads(h):
    """每个会话记录的首行（session 事件）：[(文件, id, 父会话, cwd)]。"""
    out = []
    for p in sorted(Path(h).glob(f"{SESSIONS}/*/*/session*.jsonl.zstd")):
        for e in _events_after(p, 0, 1):
            if e.get("type") == "session":
                out.append((p, e.get("id"), e.get("parentSession"), e.get("cwd")))
            break
    return out


def _actions(event, root):
    if event.get("type") != "tool/call":
        return []
    data = event.get("data") or {}
    name = data.get("name")
    try:
        args = json.loads(data.get("arguments") or "{}")
    except ValueError:
        return []
    if name in WRITES and isinstance(args.get("file_path"), str):
        return [("write", str((Path(root) / args["file_path"]).resolve()))]
    if name == "bash" and isinstance(args.get("command"), str):
        return [("run", " ".join(args["command"].split())[:200])]
    return []


def actions(logs):
    """这次会话（含子代理）自己做的：[("write", 路径) | ("run", 命令)]。

    读会话记录里的 tool/call（不是从模型文字猜）：主会话按编号，子代理按 parentSession 递归；
    启动前记了每个文件的行数，续接不重算上一轮、以后回看也不混入后轮。
    脚本内部写文件只能列出命令，不能还原任意程序的文件副作用。"""
    logs = Path(logs)
    try:
        meta = json.loads((logs / "dsh.json").read_text())
    except (OSError, ValueError):
        return []
    session = outcome(logs)[2]
    records = _heads(meta["home"])
    selected, out = {session} if session else set(), []
    while True:
        added = {sid for _, sid, parent, _ in records if parent in selected} - selected
        if not added:
            break
        selected.update(added)
    for path, sid, _, cwd in records:
        if sid not in selected:
            continue
        for e in _events_after(path, meta.get("offsets", {}).get(str(path), 0)):
            out.extend(_actions(e, cwd or meta["root"]))
    return list(dict.fromkeys(out))
