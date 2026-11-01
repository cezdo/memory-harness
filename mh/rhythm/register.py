"""常驻服务的注册（design/rhythm/设计.md“常驻服务”，X1 2026-10-29）：三个循环在各平台上怎么注册、怎么读状态和出错行。
平台差异只在这里：Linux 用 systemd 用户服务（日志进 journal），macOS 用 launchd 的 LaunchAgent（日志写文件，
每行由 stamp() 补上时刻）。服务的定义只有 SERVICES 一份，unit / plist 由它生成，不再手写。

    python3 -m mh rhythm --register          # 看生成的文件和现在装着的有什么不同
    python3 -m mh rhythm --register --apply  # 写进去并登记开机自启；不重启正在跑的服务
"""
import difflib
import os
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# 名字 → (python3 -m mh 后面的参数, 说明)
SERVICES = {
    "memory-harness-observe": (["rhythm"], "mh rhythm：观察循环（采集 → observe 跑一批 → 休眠）"),
    "memory-harness-form": (["rhythm", "--form"], "mh rhythm --form：形成循环（form 跑一批 → 休眠）"),
    "memory-harness-tidy": (["rhythm", "--tidy"], "mh rhythm --tidy：整理循环（项目现状、检查、补足迹）"),
}
# 只在某台机器上装的（点名才生成）：远端调用的服务端，跑在能跑 DshWeb 的 Linux 上（X2）；监听 QQ 协议端、回复（G7）
EXTRA = {"memory-harness-invoke": (["engram", "serve"], "mh engram serve：远端调用，代跑精读的 dsh web"),
         "memory-harness-listen": (["engram", "listen"], "mh engram listen：监听 QQ 协议端，事件原样追加进数据目录（G7）"),
         "memory-harness-reply": (["reply"], "mh reply：在一个群里被叫到时回几句（G7）"),
         "memory-harness-show": (["rhythm", "--show"], "mh rhythm --show：展示自己的窗口，只读网页（N3）")}
ALL = {**SERVICES, **EXTRA}
# 循环要调的外部程序：服务的 PATH 由它们此刻所在的目录拼成（dsh 要 Node 24，系统自带的 node 可能太旧）
TOOLS = ("node", "claude", "codex")
RESTART_SEC = 30
STAMP_ENV = "MH_LOG_STAMP"           # launchd 下置 1：日志每行补上时刻
LOG_TAIL = 4 << 20                   # macOS 读日志文件末尾多少字节找出错行
PLATFORM = "launchd" if sys.platform == "darwin" else "systemd"


def _run(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def path_env():
    """服务的 PATH：TOOLS 所在的目录（按出现先后、去重）+ 系统目录。"""
    dirs = [str(Path(p).parent) for p in (shutil.which(t) for t in TOOLS) if p]
    return ":".join(dict.fromkeys(dirs + ["/usr/local/bin", "/usr/bin", "/bin"]))


def _logs():
    return Path.home() / "Library/Logs/memory-harness"


def unit(name, python=None, path=None):
    """systemd 用户服务的 unit 文本。"""
    args, desc = ALL[name]
    return "\n".join([
        "[Unit]", f"Description={desc}", "",
        "[Service]", f"WorkingDirectory={REPO}",
        f"Environment=PATH={path or path_env()}", "Environment=PYTHONUNBUFFERED=1",
        f"ExecStart={' '.join([python or sys.executable, '-m', 'mh', *args])}",
        "Restart=on-failure", f"RestartSec={RESTART_SEC}", "",
        "[Install]", "WantedBy=default.target", ""])


def plist(name, python=None, path=None):
    """launchd LaunchAgent 的 plist 文本。KeepAlive 只在非正常退出时拉起，对应 Restart=on-failure。"""
    args, desc = ALL[name]
    log = str(_logs() / f"{name}.log")
    return plistlib.dumps({
        "Label": name, "Comment": desc,
        "ProgramArguments": [python or sys.executable, "-m", "mh", *args],
        "WorkingDirectory": str(REPO),
        "EnvironmentVariables": {"PATH": path or path_env(), "PYTHONUNBUFFERED": "1", STAMP_ENV: "1"},
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": RESTART_SEC,
        "StandardOutPath": log, "StandardErrorPath": log,
    }).decode()


def target(name):
    """这个平台上服务文件放在哪。"""
    if PLATFORM == "launchd":
        return Path.home() / "Library/LaunchAgents" / f"{name}.plist"
    return Path.home() / ".config/systemd/user" / f"{name}.service"


def render(name, **kw):
    return (plist if PLATFORM == "launchd" else unit)(name, **kw)


def diff(name, **kw):
    """生成的和现在装着的差在哪（unified diff）；一样返回空串。"""
    want, path = render(name, **kw), target(name)
    have = path.read_text() if path.exists() else ""
    return "".join(difflib.unified_diff(have.splitlines(True), want.splitlines(True), str(path), "生成"))


def apply(names=None, log=sys.stdout):
    """写服务文件、登记开机自启。原文件不同时先备份成 .bak-register-<日期>。不启动、不重启正在跑的服务。"""
    for name in names or SERVICES:
        path, want = target(name), render(name)
        if path.exists() and path.read_text() == want:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_name(f"{path.name}.bak-register-{datetime.now():%Y%m%d}"))
        path.write_text(want)
        print(f"写了 {path}", file=log)
    if PLATFORM == "launchd":
        _logs().mkdir(parents=True, exist_ok=True)
        for name in names or SERVICES:          # 没登记过的才 bootstrap；登记过的要换文件，由人 bootout 再 bootstrap
            if state(name) in ("not-loaded", "not-found"):
                _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target(name))])
    else:
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", *(names or SERVICES)])


def state(name):
    """服务现在的状态：在跑是 "active"，没装是 "not-found"，装了没登记（launchd）是 "not-loaded"；其他照平台原样给
    （failed、inactive……）；读不到给空串。"""
    if PLATFORM == "launchd":
        out = _run(["launchctl", "print", f"gui/{os.getuid()}/{name}"])
        m = re.search(r"^\s*state = (\S+)", out, re.M)
        if not m:
            return "not-loaded" if target(name).exists() else "not-found"
        return "active" if m.group(1) == "running" else m.group(1)
    out = dict(x.partition("=")[::2] for x in _run(["systemctl", "--user", "show", name, "-p", "LoadState,ActiveState"]).splitlines())
    return "not-found" if out.get("LoadState") == "not-found" else out.get("ActiveState", "")


def lines(name, hours):
    """近 hours 小时的日志 → [(ISO 时刻, 正文)]，正文去掉平台加的前缀。"""
    if PLATFORM == "launchd":
        return _file_lines(_logs() / f"{name}.log", hours)
    out = _run(["journalctl", "--user", "-u", name, "--since", f"-{hours}h", "-o", "short-iso", "--no-pager"])
    got = []
    for x in out.splitlines():
        at, _, msg = x.partition(" ")
        if not re.match(r"\d{4}-\d\d-\d\dT", at):          # “-- No entries --”这类
            continue
        got.append((at, msg.split(": ", 1)[1] if ": " in msg else msg))     # 去掉“主机 进程[pid]: ”
    return got


def _file_lines(path, hours, now=None):
    """stamp() 写的日志文件：只读末尾 LOG_TAIL 字节，按行首时刻取近 hours 小时的。"""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, f.seek(0, 2) - LOG_TAIL))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    since = (now or datetime.now().astimezone()) - timedelta(hours=hours)
    got = []
    for x in text.splitlines():
        at, _, msg = x.partition(" ")
        try:
            if datetime.fromisoformat(at) >= since:
                got.append((at, msg))
        except ValueError:
            continue
    return got


class _Stamped:
    """每行开头补上本地时刻（ISO，带时区），格式同 journalctl -o short-iso 的时刻。"""
    def __init__(self, out):
        self.out, self.fresh = out, True

    def write(self, s):
        for part in s.splitlines(True):
            if self.fresh:
                self.out.write(datetime.now().astimezone().isoformat(timespec="seconds") + " ")
            self.out.write(part)
            self.fresh = part.endswith("\n")
        return len(s)

    def flush(self):
        self.out.flush()

    def __getattr__(self, k):
        return getattr(self.out, k)


def stamp():
    """服务进程开头调：launchd 下（STAMP_ENV=1）stdout、stderr 每行补上时刻；journal 自己记时刻，不动。"""
    if os.environ.get(STAMP_ENV) == "1":
        sys.stdout, sys.stderr = _Stamped(sys.stdout), _Stamped(sys.stderr)
