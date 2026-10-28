"""调用接口：dsh（headless、web）、claude -p、codex exec 的接入与隔离（重构 A4，2026-10-17：单独一个文件夹）。

每个调用只做一件事：给提示词，拿回文本和调用信息。宿主的适配和更新都在这里，换模型、宿主升级只动这里。
具体用哪个模型、哪份提示词、叠哪些补丁，是各过程自己的 Operator 定义（observe 的在 mh/engram/observe/preview.py、deep_rough_read.py），不放在这里。
密钥只从各宿主自己的配置读。隔离标记（isolation）由各类按构造参数算出，进 Operator：隔离方式变了就是新的 Operator。
"""
import concurrent.futures
import ctypes
import hashlib
import http.client
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from ..store.instance import DSH_TEST

from ..store import HOME

# 单次调用的超时（秒）。读一轮的步骤（细粒度、整理）和读整段会话的步骤（会话记录、Formation）工作量不对等，
# 后者翻倍（2026-10-08）。超时是执行参数，不进 Operator。
TURN_TIMEOUT = 900
WHOLE_TIMEOUT = 1800


def whole(model):
    """给读整段会话的步骤用：放宽超时。"""
    model.timeout = WHOLE_TIMEOUT
    return model


# 默认开着的插件与 apps 每次调用都会同步 apps 工具表（实测约 6MB），插件目录过期后再拉 26MB；
# 观察者只读、从不用这些工具，关掉后单次约 0.8MB（2026-10-08）。视为不影响观察，isolation 仍是 v1（用户裁决）
# 2026-10-28 复测：插件目录长到 48MB JSON，默认特性下一次调用 42MB（插件包 26.7MB + apps 工具表 13.2MB）；
# 关掉后 0.7MB，剩下的几乎全是每个进程启动都拉的在线模型目录（见 catalog）。
# --strict-config：写错的键直接报错；之前手写的 plugins_enabled 就是被静默忽略的无效键
OFFLINE_FEATURES = [a for f in ("plugins", "apps", "remote_plugin") for a in ("-c", f"features.{f}=false")]
# 隔离的 home 没有宿主那份配置，更新检查得单独关（宿主 ~/.codex/config.toml 里是 check_for_update_on_startup=false）
NO_UPDATE = ["-c", "check_for_update_on_startup=false"]

# 模型目录：codex 每次进程启动都会在线拉一份（实测约 0.66MB，2026-10-28）。观察、形成每次调用都是一个新进程，
# 一天上千次就是几百 MB。把目录落盘，调用时用 model_catalog_json 指过去：过期（CATALOG_DAYS）或里面没有要用的
# 模型时刷新一次（codex debug models，一次约 0.66MB），之后只剩真正要发的那一请求。
CATALOG = HOME / "codex-models.json"
CATALOG_DAYS = 7
_catalog_lock = threading.Lock()
_catalog_seen = {"key": None, "models": set()}


def _models_of(path):
    """目录里的模型名（按 mtime、大小缓存，不每次解析 600KB JSON）。"""
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    if _catalog_seen["key"] != key:
        data = json.loads(path.read_text())
        _catalog_seen["key"] = key
        _catalog_seen["models"] = {m.get("slug") for m in data.get("models", []) if isinstance(m, dict)}
    return _catalog_seen["models"]


def _ensure_home(home):
    """隔离的 codex home：目录、认证（只放一个指向宿主 auth.json 的符号链接）。不存在时 codex 直接拒绝启动。"""
    home.mkdir(parents=True, exist_ok=True)
    if not (home / "auth.json").exists():
        (home / "auth.json").symlink_to(Path.home() / ".codex/auth.json")
    return home


def _debug_models():
    """codex debug models 的产出，就是落盘的模型目录。用隔离的 home，不读宿主的缓存和配置。"""
    p = subprocess.run(["codex", "debug", "models", *OFFLINE_FEATURES, *NO_UPDATE],
                       capture_output=True, text=True, timeout=120,
                       env=dict(os.environ, CODEX_HOME=str(_ensure_home(Codex.home))))
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-300:] or f"codex debug models 退出 {p.returncode}")
    return p.stdout


def _off_path(path):
    """标记文件：这个 codex 版本不认 model_catalog_json 时记一笔，免得每次调用都白试一次（白试会多起一个进程、多拉一次在线目录）。"""
    return Path(path).with_name(Path(path).name + ".off")


def _marked_off(path, days):
    try:
        return time.time() - _off_path(path).stat().st_mtime < days * 86400
    except OSError:
        return False


def catalog(model=None, path=None, days=CATALOG_DAYS, run=None):
    """本地的模型目录：够新、里面有这个模型（model 不给就不看）就直接用；否则刷新一次。
    刷新不了、或这个版本不认它，返回 None，调用方退回在线目录（在线时每个进程启动拉一次）。"""
    path = Path(path or CATALOG)
    if _marked_off(path, days):
        return None

    def usable():
        try:
            return time.time() - path.stat().st_mtime < days * 86400 and (model is None or model in _models_of(path))
        except (OSError, ValueError):
            return False

    if usable():
        return path
    with _catalog_lock:
        if usable():                    # 并发的调用里可能已经有人刷过了
            return path
        try:
            text = (run or _debug_models)()
            models = {m.get("slug") for m in json.loads(text).get("models", []) if isinstance(m, dict)}
        except Exception:
            return None
        if not models:
            return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
            tmp.write_text(text)
            tmp.replace(path)           # 原子落盘：别的进程不会读到半个文件
        except OSError:
            return None
        _catalog_seen["key"] = None
        return path


class Codex:
    """GPT（默认 Luna），经本机 codex exec；临时空目录、只读、不留会话文件，免得被自己采回去。

    隔离：用独立的 CODEX_HOME，里面只有指向原认证的符号链接。否则全局 ~/.codex/AGENTS.md 会让观察者照宿主的规则行事
    （2026-10-06 的实际事故：Luna 把被观察的旧对话写进了旧记忆系统的日记；旧系统 2026-10-23 已归档）。"""
    timeout = TURN_TIMEOUT
    home = HOME / "codex-home"
    isolation = "codex-home-v1"

    def __init__(self, model="gpt-6-luna", effort="medium", platform="codex", isolation=None, no_tools=False):
        # 推理强度显式写死，等于 codex-home-v1 以来各模型实际用的默认值（Luna medium、Sol low）；
        # 改档位要换 isolation 标签，让它进 Operator 身份。
        # no_tools：codex 的只读沙箱拦不住读文件，用了工具就判失败、结果作废（观察者，2026-10-19；进 isolation 标签）
        self.model, self.effort, self.platform, self.no_tools = model, effort, platform, no_tools
        if isolation:
            self.isolation = isolation
        if no_tools:
            self.isolation += "-notools"

    def __call__(self, prompt):
        env = dict(os.environ, CODEX_HOME=str(_ensure_home(self.home)))
        cat = catalog(self.model)
        with tempfile.TemporaryDirectory(prefix="mh-codex-") as tmp:
            out = Path(tmp) / "last.txt"

            def run(cat):
                # 有本地模型目录就指过去，省掉这次进程启动的在线目录；没有（刷新失败）就照原样在线拉
                extra = ["-c", f"model_catalog_json={cat}"] if cat else []
                return subprocess.run(
                    ["codex", "exec", "-m", self.model, "-c", f"model_reasoning_effort={self.effort}", *OFFLINE_FEATURES, *NO_UPDATE, *extra,
                     "--strict-config", "--ephemeral", "--skip-git-repo-check",
                     "-s", "read-only", "-C", tmp, "-o", str(out), "--json", "-"],
                    input=prompt, capture_output=True, text=True, timeout=self.timeout, env=env)

            p = run(cat)
            if cat and p.returncode != 0 and "model_catalog_json" in p.stderr + p.stdout:
                # 版本换了、目录格式对不上：去掉它重来一次；坏文件删掉、记一笔，CATALOG_DAYS 内不再带
                # （不带就不认它的话，每次带都是白起一个进程、多拉一次在线目录）
                Path(cat).unlink(missing_ok=True)
                try:
                    _off_path(cat).write_text(f"{time.time()}\n")
                except OSError:
                    pass
                p = run(None)
            events = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
            if p.returncode != 0 or not out.exists():
                # 接口报错（如订阅到期时的“模型不支持”）在 stdout 的 error 事件里，stderr 是空的（2026-10-10）
                said = [e.get("message") or e.get("error", {}).get("message", "") for e in events if e.get("type") in ("error", "turn.failed")]
                raise RuntimeError(f"codex exec 失败（{p.returncode}）：{p.stderr[-500:]}{' '.join(said)[-500:]}")
            # 按条目类型认工具调用：只有回答、推理、出错这几类是文字。原先找 "command" 子串，回答里引用了
            # 「exec_command」这类词也会被当成用了工具（C 实验 i2 段 1312、1320，2026-10-15）
            tools = {(e.get("item") or {}).get("id") for e in events
                     if (e.get("item") or {}).get("type") not in (None, "agent_message", "reasoning", "error")}
            if self.no_tools and tools:
                raise RuntimeError(f"观察者调用了工具（{len(tools)} 次），结果作废")
            return out.read_text(), {"model": self.model, "tool_events": len(tools), "isolation": self.isolation}


class Dsh:
    """DeepSeek，经 dsh 更新测试实例的 headless 模式。模型与密钥由该实例的配置决定，换 key 不用动这里。
    会话写在测试实例自己的 home 里，不进 ~/.dsh，不会被采回。"""
    timeout = TURN_TIMEOUT
    root = DSH_TEST or Path.home() / "dsh-test"    # 测试实例的位置：实例配置

    def __init__(self, model, platform="dsh headless（测试实例）", profile=None, patches=(), isolation=None):
        # patches：叠加在 profile 之后的补丁（各过程自己的 Operator 定义）；isolation：隔离标记，进 Operator
        self.model, self.platform, self.profile = model, platform, profile
        self.extra = tuple(map(Path, patches))
        if isolation:
            self.isolation = isolation

    def __call__(self, prompt):
        env = dict(os.environ, DSH_HOME=str(self.root / "home"))
        with tempfile.TemporaryDirectory(prefix="mh-dsh-") as tmp:
            # 叠加某个 profile 的 patch，借用它的 provider 配置（如 web 里从 settings.yaml 导入的 ark）
            patch = ["--patch", str(self.root / f"home/profiles/{self.profile}/cordis.patch.yml")] if self.profile else []
            patch += [a for p in self.extra for a in ("--patch", str(p))]
            p = subprocess.run(["node", str(self.root / "env-next/lib/bin.js"), "headless", *patch, "--json", "-"],
                               input=prompt, capture_output=True, text=True, timeout=self.timeout, cwd=tmp, env=env)
        events = [json.loads(l) for l in p.stdout.splitlines() if l.startswith("{")]
        final = next((e["text"] for e in reversed(events) if e.get("type") == "final"), None)
        if p.returncode != 0 or final is None:
            raise RuntimeError(f"dsh headless 失败（{p.returncode}）：{p.stderr[-500:]}")
        session = next((e["sessionId"] for e in events if e.get("type") == "session"), "")
        meta = {"model": self.model, "session": session,
                "steps": sum(1 for e in events if e.get("phase") == "step_end"),
                "usage": [e["usage"] for e in events if e.get("phase") == "step_end"]}
        meta.update(self._actual(session))
        if meta["steps"] > 1:             # 观察者不该用工具：用了就作废（只是执行检查，不改模型看到的内容）
            raise RuntimeError(f"观察者调用了工具（{meta['steps']} 步），结果作废")
        return final, meta

    def _actual(self, session):
        """从测试实例的会话文件读出实际的提供方和模型。"""
        for f in (self.root / "home/sessions").glob(f"*/{session}/session*.zstd"):
            out = subprocess.run(["zstd", "-d", "-c", "--", str(f)], capture_output=True).stdout
            for line in out.decode("utf-8", "replace").splitlines():
                if '"request/context"' in line:
                    d = json.loads(line).get("data", {})
                    return {"model": d.get("model"), "provider": d.get("provider")}
        return {}


def _die_with_parent():
    """子进程里执行：父进程（常驻循环或命令行）退出时，内核给它发 SIGTERM，不留下占着端口的孤儿（Linux）。"""
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)       # PR_SET_PDEATHSIG


# PR_SET_PDEATHSIG 跟的是创建子进程的那个线程：在观察的线程池里拉起，线程池一收工 dsh web 就被杀了。
# 所以总在这个一直活着的线程里拉起。
_launcher = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="dsh-web-launcher")


class DshWeb(Dsh):
    """同一个 dsh 测试实例，改为常驻的 dsh web：一个进程开多个会话，每次观察新建一个会话（2026-10-09）。
    headless 每路一个约 600MB 的进程；常驻一个进程 48 路并发仍不到 1GB。

    会话用 dsh 自带的 minimal 预设，原样不改：人设一句、不带运行时上下文、只有一个 bash。
    web 的会话按预设组装插件，profile 层的极简层管不到预设，只管得到 profile 层挂进来的东西（如记忆工具）。
    观察者不该用工具：会话文件里一出现 tool/call 就取消这个会话，结果作废。作废之前命令可能已经执行，所以：
      - 整个 dsh web 跑在 bwrap 沙箱里：只看得见系统目录、node、dsh 自己的程序和测试 home（遮掉备份、附件、旧会话），
        /home 下的其他东西（仓库、宿主）都不存在；会话写进专用的空目录；
      - dsh 以只读权限运行（bash 写不了文件）；
      - 工作目录是每次新建的空临时目录。
    进程由第一次调用拉起，是常驻循环的子进程，随服务一起停；跑满 restart_every 次、手上没有调用时重启一次，
    免得会话越积越多。

    运行配置都可以在构造时给（2026-10-10：去掉硬编码）：port、box（沙箱里唯一能写的目录，日志也在这里）、
    preset、restart_every、poll、permission、mask、patches（各过程自己的补丁，按顺序叠加）。
    同一个端口的调用共用一个 dsh web 进程（按端口记状态），不同端口互不相干：实验另开端口，不碰观察服务那个。
    preset 计入 isolation（进 Operator）；端口、目录、轮询这些执行参数不计入。"""
    NODE = Path(shutil.which("node") or "node").resolve().parent.parent
    # 测试 home 里与观察无关、又可能被读到的东西，在沙箱里用空目录遮住（旧会话由 sessions 的专用目录遮住）
    MASK = (".git", "attachments", ".agent-presets")   # 旧记忆系统的两个备份目录 2026-10-23 随旧系统归档去掉
    _servers = {}                          # 端口 → 进程状态
    _servers_lock = threading.Lock()

    def __init__(self, model, platform="dsh web（测试实例）", *, port=3085, box=None, preset="minimal",
                 restart_every=400, poll=3, permission="read-only", mask=None, patches=(), allow=None, allow_tag=None):
        super().__init__(model, platform, patches=patches)
        self.port, self.preset, self.restart_every, self.poll, self.permission = port, preset, restart_every, poll, permission
        self.box = Path(box) if box else HOME / "dsh-web"    # sessions/（会话文件）、cwd/（每次的工作目录）、日志
        self.log = HOME / "dsh-web.log" if box is None else self.box / "dsh-web.log"
        self.mask = tuple(mask) if mask is not None else self.MASK
        # patches（已在 Dsh 里收下）：按顺序叠加在 web profile 之后的补丁（极简层、推理档位、另声明的预设……），哈希进 isolation；
        # 同一端口只该用一组。allow：bash 命令的白名单（command → 是否放行）；不给 = 一出现工具调用就作废
        self.allow = allow
        self.isolation = f"dsh-web-{preset}-box-" + hashlib.sha256(b"".join(p.read_bytes() for p in self.extra)).hexdigest()[:8] \
            + (f"-allow-{allow_tag}" if allow else "")
        with DshWeb._servers_lock:
            self.server = DshWeb._servers.setdefault(port, {"lock": threading.Lock(), "proc": None, "cookie": None,
                                                            "calls": 0, "inflight": 0})

    def _env(self):
        # 默认只读权限（dsh 的 read-only 预设）：bash 写不了任何文件
        return dict(os.environ, DSH_HOME=str(self.root / "home"), DSH_PERMISSION_MODE=self.permission, HOME="/tmp")

    def _box(self, argv):
        """把命令包进 bwrap：网络共享（要连 ark），其余命名空间全部隔开；父进程退出它也退出。"""
        home, box = self.root / "home", self.box
        args = ["bwrap", "--unshare-all", "--share-net", "--die-with-parent",
                "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc", "--ro-bind", "/run/systemd/resolve", "/run/systemd/resolve",
                "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                "--symlink", "usr/sbin", "/sbin", "--proc", "/proc", "--dev", "/dev", "--ro-bind", "/sys", "/sys", "--tmpfs", "/tmp",
                "--ro-bind", str(self.NODE), str(self.NODE), "--ro-bind", str(self.root / "env-next"), str(self.root / "env-next"),
                "--bind", str(home), str(home), "--bind", str(box / "sessions"), str(home / "sessions"),
                "--bind", str(box / "cwd"), str(box / "cwd"), "--ro-bind", str(box / "patches"), str(box / "patches")]
        for m in self.mask:
            if (home / m).exists():
                args += ["--tmpfs", str(home / m)]
        return args + ["--chdir", "/tmp", *argv]

    def _start(self):
        sv = self.server
        if sv["proc"] and sv["proc"].poll() is None:
            sv["proc"].terminate()
            sv["proc"].wait(30)
        log = self.log
        for d in ("sessions", "cwd", "patches"):
            (self.box / d).mkdir(parents=True, exist_ok=True)
        # 补丁复制进沙箱（仓库不进沙箱）；web profile 自己的补丁（ark provider）由 dsh 叠加
        patch = []
        for p in self.extra:
            (self.box / "patches" / p.name).write_bytes(p.read_bytes())
            patch += ["--patch", str(self.box / "patches" / p.name)]
        argv = [str(self.NODE / "bin/node"), str(self.root / "env-next/lib/bin.js"), "web", *patch,
                "--port", str(self.port), "--host", "127.0.0.1", "--no-open"]
        sv["proc"] = _launcher.submit(
            subprocess.Popen, self._box(argv), stdout=open(log, "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=self._env(), preexec_fn=_die_with_parent).result()
        token = None
        for _ in range(120):
            time.sleep(1)
            m = re.search(r"token=(\S+)", log.read_text(errors="replace"))
            if m:
                token = m.group(1)
                break
            if sv["proc"].poll() is not None:
                break
        if not token:
            raise RuntimeError(f"dsh web 没有起来：{log.read_text(errors='replace')[-500:]}")
        # 用一次性令牌换登录 cookie（303 跳转，不跟随）
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        conn.request("GET", f"/?token={token}")
        r = conn.getresponse()
        cookies = [c.split(";", 1)[0] for k, c in r.getheaders() if k.lower() == "set-cookie"]
        sv["cookie"] = "; ".join(c for c in cookies if c.startswith("dsh-auth"))
        if not sv["cookie"]:
            raise RuntimeError(f"dsh web 登录失败（HTTP {r.status}）")
        sv["calls"] = 0

    def _rpc(self, method, args):
        host = f"127.0.0.1:{self.port}"
        body = {"type": "client-request", "rpcId": "mh-" + uuid.uuid4().hex[:8], "method": method, "payload": {"args": args}}
        req = urllib.request.Request(f"http://{host}/api/{method}", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Cookie": self.server["cookie"],
                                              "Origin": f"http://{host}"})
        d = json.loads(urllib.request.urlopen(req, timeout=60).read())
        r = d.get("result") or {}
        if not r.get("ok"):
            raise RuntimeError(f"dsh web {method} 失败：{r.get('error') or d}")
        return r.get("value")

    def _events(self, sid):
        for f in (self.box / "sessions").glob(f"*/{sid}/session*.zstd"):
            out = subprocess.run(["zstd", "-dc", "--", str(f)], capture_output=True).stdout
            ev = []
            for line in out.decode("utf-8", "replace").splitlines():
                try:
                    ev.append(json.loads(line))
                except ValueError:
                    pass
            return ev
        return []

    def __call__(self, prompt):
        sv = self.server
        with sv["lock"]:
            alive = sv["proc"] and sv["proc"].poll() is None
            if not alive or (sv["calls"] >= self.restart_every and sv["inflight"] == 0):
                self._start()
            sv["calls"] += 1
            sv["inflight"] += 1
        try:
            return self._observe(prompt)
        finally:
            with sv["lock"]:
                sv["inflight"] -= 1

    def _observe(self, prompt):
        start = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="mh-dshweb-", dir=self.box / "cwd") as tmp:
            sid = self._rpc("session/create", {"request": {"cwd": tmp, "agentPreset": self.preset}})["sessionId"]
            self._rpc("session/prompt", {"request": {"requestId": "req-" + uuid.uuid4().hex[:12], "sessionId": sid,
                                                     "mode": "queue", "content": [{"type": "text", "text": prompt}]}})
            while True:
                time.sleep(self.poll)
                ev = self._events(sid)
                kinds = {e.get("type") for e in ev}
                bad = [c for c in self._tool_calls(ev) if not self._allowed(c)]
                if bad:                       # 观察者用了白名单外的工具：立刻取消，结果作废
                    try:
                        self._rpc("session/cancel", {"request": {"sessionId": sid}})
                    except Exception:
                        pass
                    raise RuntimeError(f"观察者调用了工具 {[self._show(c) for c in bad]}（会话 {sid}），已取消，结果作废")
                if "turn/end" in kinds:
                    break
                if time.monotonic() - start > self.timeout:
                    try:
                        self._rpc("session/cancel", {"request": {"sessionId": sid}})
                    except Exception:
                        pass
                    raise TimeoutError(f"dsh web 观察超时（会话 {sid}）")
        end = next(e["data"] for e in ev if e.get("type") == "turn/end")
        if (end.get("reason") or {}).get("kind") != "completed":
            raise RuntimeError(f"dsh web 没有正常结束：{end.get('reason')}（会话 {sid}）")
        final = ""
        usage = []
        for e in ev:
            if e.get("type") == "assistant/message":
                for c in e["data"]["message"]["content"]:
                    if c.get("type") == "text":
                        final = c.get("text", "")
                u = e["data"]["message"].get("usage") or e["data"].get("usage")
                if u:
                    usage.append(u)
        steps = sum(1 for e in ev if e.get("type") == "step/end")
        if steps > 1 and not self.allow:
            raise RuntimeError(f"观察者走了 {steps} 步（会话 {sid}），结果作废")
        ctx = next((e["data"] for e in ev if e.get("type") == "request/context"), {})
        hdr = next((e["data"]["header"] for e in ev if e.get("type") == "request/header"), {})
        return final, {"model": self.model, "session": sid, "steps": steps, "usage": usage,
                       "provider": ctx.get("provider"), "actual_model": ctx.get("model"), "preset": self.preset,
                       "effort": (hdr.get("config") or {}).get("reasoningEffort"),
                       "tools": [t.get("name") for t in hdr.get("tools", [])],
                       "calls": [self._show(c) for c in self._tool_calls(ev)]}

    @staticmethod
    def _tool_calls(ev):
        return [e["data"] for e in ev if e.get("type") == "tool/call"]

    def _allowed(self, call):
        if not self.allow or call.get("name") != "bash":
            return False
        try:
            return bool(self.allow(json.loads(call.get("arguments") or "{}").get("command") or ""))
        except ValueError:
            return False

    @staticmethod
    def _show(call):
        try:
            cmd = json.loads(call.get("arguments") or "{}").get("command")
        except ValueError:
            cmd = None
        return f"{call.get('name')}: {cmd[:80]}" if cmd else call.get("name")


class Claude:
    """Claude（默认 Sonnet），经本机 claude -p；与 Codex 同样的隔离：独立的 CLAUDE_CONFIG_DIR，里面只有指向原认证的
    符号链接，不读全局设置、CLAUDE.md、自动记忆和 MCP；不给任何工具；不留会话文件。"""
    timeout = TURN_TIMEOUT
    home = HOME / "claude-home"
    isolation = "claude-home-v1"
    system = "按用户消息里的要求完成这一次文本任务，只输出所要求的内容。"     # 替换 Claude Code 默认的编程助手提示（其中含记忆目录说明）

    def __init__(self, model="claude-sonnet-5-5", platform="claude -p", effort=None, isolation=None):
        # effort 不给 = CLI 默认档（claude-home-v1 以来一直如此）；给了就要换 isolation 标签，让它进 Operator 身份（同 Codex）
        self.model, self.platform, self.effort = model, platform, effort
        if isolation:
            self.isolation = isolation

    def __call__(self, prompt):
        self.home.mkdir(parents=True, exist_ok=True)
        if not (self.home / ".credentials.json").exists():
            (self.home / ".credentials.json").symlink_to(Path.home() / ".claude/.credentials.json")
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.home))
        with tempfile.TemporaryDirectory(prefix="mh-claude-") as tmp:
            p = subprocess.run(
                ["claude", "-p", "--model", self.model, "--tools", "", "--system-prompt", self.system, "--setting-sources", "",
                 "--strict-mcp-config", "--no-session-persistence", "--output-format", "json"]
                + (["--effort", self.effort] if self.effort else []),
                input=prompt, capture_output=True, text=True, timeout=self.timeout, cwd=tmp, env=env)
        try:
            d = json.loads(p.stdout)
        except ValueError:
            d = {}
        if p.returncode != 0 or d.get("is_error") or "result" not in d:
            raise RuntimeError(f"claude -p 失败（{p.returncode}）：{(p.stderr or p.stdout)[-500:]}")
        return d["result"], {"model": self.model, "usage": d.get("usage"), "turns": d.get("num_turns"),
                             "isolation": self.isolation, **({"effort": self.effort} if self.effort else {})}


def call(model, prompt):
    """调一次，量耗时。返回 (文本, 调用信息)；调用信息里的 seconds 是墙钟秒数。"""
    start = time.monotonic()
    text, meta = model(prompt)
    meta["seconds"] = round(time.monotonic() - start, 1)
    return text, meta
