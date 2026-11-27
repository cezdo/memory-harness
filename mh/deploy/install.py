"""安装器：把一台机器装成 mh 的服务端或客户端（design/deploy/设计.md“安装器”）。纯程序，不调用模型。

    python3 -m mh.deploy install server [--apply] [--from oldhost:/usr/local/mh-tools] [--pip-index URL] [--services]
    python3 -m mh.deploy install client --server <服务机> [--apply]

不带 --apply 只看：一步一行，已经好的、缺的、要人做的。带 --apply 才动手，每一步先看再做，已经好的跳过，能重跑。
先装代码：`git clone <仓库> ~/memory-harness`，在检出里跑本命令；数据从旧服务端搬过来用 `migrate import`，
放在装工具和注册服务之间（服务一注册，launchd 就起了；单写，旧端停了再起新端，所以 --services 要另外点名）。
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OK, TODO, HUMAN = "好", "缺", "人"        # 一步的状态：已经好、--apply 能补、要人来做


@dataclass
class Tool:
    """一个第三方工具在工具根下怎么装：probe 都在就算装好；venv 给 pip 包；copy 是从旧机器拷的子目录。"""
    name: str
    probe: list
    pip: list = field(default_factory=list)
    copy: list = field(default_factory=list)
    used_by: str = ""


# 版本照 Linux、macOS 两台机器上实测能用的（11-25；LTP 4.2.14 不兼容 transformers 5）
TOOLS = [
    Tool("ltp", ["venv/bin/python", "model"], pip=["torch==2.14.1", "ltp==4.2.14", "transformers==4.55.4",
                                                    "tokenizers==0.21.4", "huggingface-hub==0.36.2"],
         copy=["model"], used_by="联想的分词（synapse/push/pos.py）"),
    Tool("lexicon", ["."], copy=["."], used_by="联想的日常词（synapse/push/associate.py）"),
    Tool("typesafe", ["venv/bin/python"], pip=["typesafe-sdk==0.7.0", "python-dotenv"], used_by="〖〗回填（synapse/track/judge.py）"),
]


@dataclass
class Step:
    name: str
    state: str
    detail: str
    do: object = None          # --apply 时调用；None 表示没有能自动做的


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def _python_for_venv():
    """建 venv 用的解释器：macOS 上 Homebrew 的 python@3.12（系统的太旧），别处用当前解释器。"""
    brew = Path("/opt/homebrew/opt/python@3.12/bin/python3.12")
    return str(brew) if sys.platform == "darwin" and brew.exists() else sys.executable


def env_step():
    miss = [t for t in ("git", "rsync", "ssh") if not shutil.which(t)]
    bad = []
    if sys.version_info < (3, 11):
        bad.append(f"Python {sys.version.split()[0]}（要 3.11 以上，tomllib）")
    if miss or bad:
        return Step("环境", HUMAN, "缺 " + "、".join(bad + miss))
    return Step("环境", OK, f"Python {sys.version.split()[0]}，SQLite {sqlite3.sqlite_version}，{sys.platform}")


def repo_step(repo=REPO):
    r = _run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"])
    if r.returncode:
        return Step("代码", HUMAN, f"{repo} 不是 git 检出：先 git clone 仓库到 ~/memory-harness 再在里面跑")
    branch = r.stdout.strip()
    head = _run(["git", "-C", str(repo), "log", "-1", "--format=%h %cs"]).stdout.strip()
    dirty = bool(_run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"]).stdout.strip())
    note = ("；工作树有改动" if dirty else "") + ("" if branch == "main" else f"；在 {branch} 上（服务跑的是 main）")
    return Step("代码", OK if not note else HUMAN, f"{repo}，{branch} {head}{note}")


def cli_step(which=shutil.which):
    """观察、形成调模型用的宿主 CLI。缺了也能跑：实例配置 [invoke].forward 指向有它们的机器，交给那边代跑。
    服务的 PATH 在注册那一刻按它们所在的目录拼（rhythm/register.path_env），要先装再 --services。"""
    from ..engram.store.instance import INVOKE
    from ..rhythm.register import TOOLS as CLIS
    have = {t: which(t) for t in CLIS}
    miss = [t for t, p in have.items() if not p]
    if not miss:
        return Step("宿主 CLI", OK, "、".join(have))
    found = ("有 " + "、".join(t for t, p in have.items() if p) + "；") if len(miss) < len(have) else ""
    if INVOKE.get("forward"):
        return Step("宿主 CLI", OK, f"{found}缺 {'、'.join(miss)}，模型调用经 [invoke].forward 交给 {INVOKE['forward']}")
    return Step("宿主 CLI", HUMAN, f"{found}缺 {'、'.join(miss)}：装上并登录（先装再注册服务），或在实例配置写 [invoke].forward 交给有它们的机器代跑")


def tool_steps(root, source=None, pip_index=None):
    out = []
    for t in TOOLS:
        d = root / t.name
        missing = [p for p in t.probe if not (d / p).exists() or (p == "." and not any(d.iterdir()))]
        if not missing:
            out.append(Step(f"工具 {t.name}", OK, f"{d}（{t.used_by}）"))
            continue
        need_copy = [c for c in t.copy if c in missing or "." in missing]
        if need_copy and not source:
            out.append(Step(f"工具 {t.name}", HUMAN, f"缺 {'、'.join(missing)}：要从旧机器拷，给 --from <主机>:<工具根>"))
            continue

        def do(t=t, d=d, need_copy=need_copy):
            d.mkdir(parents=True, exist_ok=True)
            for c in need_copy:
                base = f"{source.rstrip('/')}/{t.name}/"
                _check(["rsync", "-a", base, f"{d}/"] if c == "." else ["rsync", "-a", base + c, f"{d}/"])
            if t.pip and not (d / "venv/bin/python").exists():
                _check([_python_for_venv(), "-m", "venv", str(d / "venv")])
                idx = ["-i", pip_index] if pip_index else []
                _check([str(d / "venv/bin/pip"), "install", "--no-cache-dir", *idx, *t.pip],
                       env=dict(os.environ, **({"no_proxy": "*"} if pip_index else {})))
        out.append(Step(f"工具 {t.name}", TODO, f"缺 {'、'.join(missing)}：" + "，".join(
            ([f"从 {source} 拷 {'、'.join(need_copy)}"] if need_copy else []) + (["建 venv、pip 装 " + " ".join(t.pip)] if t.pip else [])), do))
    return out


RELAY_SRC = REPO / "mh/synapse/hosts/relay"


def relay_step(root, which=shutil.which):
    """mh-relay（X10）：从仓库里的 crate 编，二进制放工具根下 relay/mh-relay（编译产物不进仓库）；源码比它新就重编。"""
    d = root / "relay"
    out = d / "mh-relay"
    srcs = [RELAY_SRC / "Cargo.toml", RELAY_SRC / "Cargo.lock", *sorted((RELAY_SRC / "src").glob("*.rs"))]
    newest = max(p.stat().st_mtime for p in srcs if p.exists())
    if out.is_file() and out.stat().st_mtime >= newest:
        return Step("工具 relay", OK, f"{out}（宿主的 MCP 入口，连常驻服务）")
    cargo = which("cargo") or next((str(p) for p in [Path.home() / ".cargo/bin/cargo"] if p.exists()), None)
    if not cargo:
        return Step("工具 relay", HUMAN, "没有 cargo：装 Rust（rustup）再来；不装也能用，宿主照旧每会话起 Python MCP")

    def do():
        _check([cargo, "build", "--release", "--manifest-path", str(RELAY_SRC / "Cargo.toml"), "--target-dir", str(d / "target")])
        shutil.copy2(d / "target/release/mh-relay", out)
    return Step("工具 relay", TODO, f"{'源码比二进制新' if out.exists() else '没编'}：cargo build --release，放 {out}", do)


def mcp_step(start=False):
    """常驻的 MCP 服务（X10）：只读库加写足迹，和 relay 配套；不是单写的三个循环，注册了就起。"""
    from ..rhythm import register
    name = "memory-harness-mcp"
    state, changed = register.state(name), bool(register.diff(name))
    if state == "active" and not changed:
        return Step("MCP 服务", OK, "在跑")
    if not start:
        return Step("MCP 服务", HUMAN, f"{state}：和三个循环一起加 --services 注册")
    return Step("MCP 服务", TODO, f"{state}：注册 {name}（各会话的 mh-relay 连它的 run/mcp.sock）", lambda: _start(name))


def tunnel_step(start=False, cfg=None):
    """隧道（X7）：实例配置有 [deploy.tunnel] 的服务机才要（macOS 上 launchd 起的第三方程序连不了局域网）。不碍单写，注册了就起。"""
    from . import tunnel
    from ..rhythm import register
    cfg = tunnel.config() if cfg is None else cfg
    if not tunnel.command(cfg):
        return None
    name = "memory-harness-tunnel"
    state, changed = register.state(name), bool(register.diff(name))
    if state == "active" and not changed:
        return Step("隧道", OK, f"在跑：{cfg['ssh']}，{len(cfg.get('forward', []))} 条转发" + ("，兼当 ssh 主连接" if cfg.get("control") else ""))
    if not start:
        return Step("隧道", HUMAN, f"{state}：和三个循环一起加 --services 注册")
    return Step("隧道", TODO, f"{state}：注册 {name}", lambda: _start(name))


def _check(args, **kw):
    print("  $ " + " ".join(args), flush=True)
    subprocess.run(args, check=True, **kw)


def config_steps(path):
    from . import migrate
    if not path.exists():
        example = REPO / "mh/engram/store/instance.example.toml"

        def do():
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(example, path)
        return [Step("实例配置", TODO, f"没有 {path}：从旧端 migrate import 带过来；全新部署时 --apply 照示例抄一份再改", do)]
    gone = [f"{k} = {v}" for k, v in migrate.absolute_paths(path) if not Path(v).exists()]
    if gone:
        return [Step("实例配置", HUMAN, f"{path}；这台机器上没有的路径：" + "；".join(gone))]
    return [Step("实例配置", OK, str(path))]


def data_step(db, fresh=False):
    if db.exists():
        from . import migrate
        c = migrate.counts(db)
        return Step("数据", OK, f"{db}，{len(c)} 张表 {sum(c.values())} 行")
    if fresh:
        def do():
            from ..engram import store
            store.connect(db).close()
        return Step("数据", TODO, f"没有 {db}：--fresh，建一个空库", do)
    return Step("数据", HUMAN, f"没有 {db}：先在旧端 migrate export、这里 migrate import；全新部署加 --fresh")


def service_steps(start=False, data_ok=True):
    from ..rhythm import register
    out = []
    for name in register.SERVICES:
        state, changed = register.state(name), bool(register.diff(name))
        if state == "active" and not changed:
            out.append(Step(f"服务 {name}", OK, "在跑"))
            continue
        why = f"{state or '读不到状态'}" + ("，服务文件和生成的不同" if changed else "")
        if not start:
            out.append(Step(f"服务 {name}", HUMAN, f"{why}：单写，旧端停了再加 --services 注册（launchd 一注册就起）"))
        elif not data_ok:
            out.append(Step(f"服务 {name}", HUMAN, f"{why}：数据还没就位，先导入"))
        else:
            out.append(Step(f"服务 {name}", TODO, f"{why}：注册并起", lambda name=name: _start(name)))
    return out


def _start(name):
    from ..rhythm import register
    register.apply([name])
    if register.PLATFORM == "systemd":
        _check(["systemctl", "--user", "restart", name])
    elif register.state(name) != "active":
        _check(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{name}"])


def test_modules(repo=REPO):
    """全部单元测试模块：mh/ 下每个 test.py、*_test.py。"""
    mods = []
    for p in sorted((repo / "mh").rglob("*test.py")):
        package = all((d / "__init__.py").exists() for d in p.parents if d.is_relative_to(repo / "mh"))   # 公开包的 assets 之类不是包
        if package and (p.name == "test.py" or p.name.endswith("_test.py")):
            mods.append(".".join(p.relative_to(repo).with_suffix("").parts))
    return mods


def verify(db):
    """验收：全部单元测试，有库时再跑 check。"""
    print("验收：单元测试", flush=True)
    r = subprocess.run([sys.executable, "-m", "unittest", *test_modules()], cwd=REPO, capture_output=True, text=True)
    tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
    print("  " + "\n  ".join(tail))
    ok = r.returncode == 0
    if db.exists():
        print("验收：mh check --brief", flush=True)
        c = subprocess.run([sys.executable, "-m", "mh", "check", "--brief"], cwd=REPO, capture_output=True, text=True)
        print("  " + (c.stdout.strip().splitlines() or ["（没有输出）"])[0])
        ok = ok and c.returncode == 0
    return ok


def server_steps(source=None, pip_index=None, services=False, fresh=False):
    from ..engram import store
    from ..engram.store import instance
    steps = [env_step(), repo_step(), cli_step(), *tool_steps(instance.TOOLS, source, pip_index), relay_step(instance.TOOLS),
             *config_steps(instance.PATH)]
    data = data_step(store.DEFAULT_DB, fresh)
    steps.append(data)
    tun = tunnel_step(services)                   # 排在循环前面：循环的代理、远端调用可能要经它
    steps += [tun] if tun else []
    steps += service_steps(services, data.state == OK)
    steps.append(mcp_step(services and data.state == OK))
    return steps


# ---- 客户端 ----

def forward_step(server, python=None, path=None):
    from ..synapse.hosts import forward
    path = path or forward.CONFIG
    import socket
    want = {"ssh": server, "repo": "memory-harness", "python": python or "python3", "device": socket.gethostname()}
    have = forward.config(path) or {}
    if all(have.get(k) == v for k, v in want.items()):
        return Step("转发", OK, f"{path} → {server}")
    text = "".join(f'{k} = {json.dumps(v)}\n' for k, v in want.items())

    def do():
        r = _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", server,
                  f"cd memory-harness && {want['python']} -c 'import mh.engram.store as s; print(s.DEFAULT_DB.exists())'"])
        if r.returncode or r.stdout.strip() != "True":
            raise SystemExit(f"服务机 {server} 上没装好（~/memory-harness 的检出或库）：{(r.stderr or r.stdout).strip()}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# 宿主入口转给 mh 服务机（mh/synapse/hosts/forward.py）；删掉即回本机\n" + text)
    return Step("转发", TODO, f"写 {path}：{text.strip().replace(chr(10), '，')}", do)


def host_steps():
    """三家宿主的钩子、MCP 在不在（装法各宿主自己的 install，这里只看）。"""
    out = []
    claude = Path.home() / ".claude/settings.json"
    has = claude.exists() and "mh inject" in claude.read_text()
    out.append(Step("宿主 Claude Code", OK if has else HUMAN, str(claude) if has else f"{claude} 里没有 mh 的钩子"))
    codex = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json"
    has = codex.exists() and "mh.synapse.hosts.codex" in codex.read_text()
    out.append(Step("宿主 Codex", OK if has else HUMAN, str(codex) if has else "python3 -m mh.synapse.hosts.codex.install --apply"))
    return out


def push_step(start=False):
    from ..rhythm import register
    name = "memory-harness-push"
    state, changed = register.state(name), bool(register.diff(name))
    if state == "active" and not changed:
        return Step("原文推送", OK, "在跑")
    return Step("原文推送", TODO, f"{state}：注册 {name}（每分钟把宿主原文推到服务机的 ~）", lambda: _start(name))


def client_steps(server):
    return [env_step(), repo_step(), forward_step(server), *host_steps(), push_step()]


def show(steps):
    for s in steps:
        print(f"[{s.state}] {s.name}：{s.detail}")


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m mh.deploy install", description="把这台机器装成 mh 的服务端或客户端；不带 --apply 只看")
    ap.add_argument("role", choices=["server", "client"])
    ap.add_argument("--apply", action="store_true", help="动手：做“缺”的那些步")
    ap.add_argument("--from", dest="source", help="server：缺的工具数据从哪拷，<主机>:<工具根>，如 oldhost:/usr/local/mh-tools")
    ap.add_argument("--pip-index", help="server：pip 的镜像（国内网络走镜像快得多）")
    ap.add_argument("--services", action="store_true", help="server：注册并起三个循环（单写：旧端停了再点）")
    ap.add_argument("--fresh", action="store_true", help="server：没有库时建一个空库（全新部署，不从旧端搬）")
    ap.add_argument("--no-verify", action="store_true", help="server：--apply 后不跑测试和 check")
    ap.add_argument("--server", help="client：服务机的 ssh 名字")
    a = ap.parse_args(argv)
    if a.role == "client" and not a.server:
        ap.error("client 要给 --server")
    build = (lambda: server_steps(a.source, a.pip_index, a.services, a.fresh)) if a.role == "server" else (lambda: client_steps(a.server))
    steps = build()
    show(steps)
    if not a.apply:
        if any(s.state == TODO for s in steps):
            print("\n加 --apply 做“缺”的那些步；“人”的要先处理")
        return
    for s in steps:
        if s.state == TODO and s.do:
            print(f"\n做：{s.name}", flush=True)
            s.do()
    print("\n装完再看一遍：")
    steps = build()
    show(steps)
    if a.role == "server" and not a.no_verify:
        print()
        if not verify(Path(__import__("mh.engram.store", fromlist=["DEFAULT_DB"]).DEFAULT_DB)):
            raise SystemExit("验收没过")
