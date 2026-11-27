"""监听（design/reply/设计.md）：连 QQ 协议端（NapCat）的 OneBot 11 正向 WS，事件原样追加进数据目录的
listen-onebot.jsonl。只收不发；常驻，memory-harness-listen 服务跑它（python3 -m mh listen），不开库。

协议端在别的机器上、只听那台机器的本机时，自己起 ssh 隧道（实例配置 [listen]）；隧道或 WS 断了就整个重来，
等待从 RETRY 起翻倍，到 RETRY_MAX 为止，连上后复位。配置了 [listen].revive（ssh 主机上的计划任务名）和
revive_dir（协议端装在哪），连不上满 REVIVE_AFTER、协议端进程不在，就跑那个任务；只认快速登录，要扫码就关掉它、
喊用户（SCAN），连上之前不再拉。

每行：{"received": 收到的时刻（UTC ISO）, "event": 协议端发来的原样 JSON}。心跳（meta_event/heartbeat）不记，
连上时的 lifecycle 照记，断开、重连只进日志；配置了 groups 就只记这些群（keep）。采集读这份文件是以后的事（采集加一个来源）。
"""
import base64
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..store import HOME
from ..store.instance import LISTEN, _dir

LOG = HOME / "listen-onebot.jsonl"
RETRY, RETRY_MAX = 5, 300
READ_TIMEOUT = 90           # 协议端心跳 30 秒一次，这么久什么都没收到就当断了
REVIVE_AFTER, REVIVE_GAP = 180, 900     # 连不上满 3 分钟去拉协议端；拉过后 15 分钟内不再拉
LOGIN_WAIT = 120                        # 拉起后等快速登录这么久
SCAN = HOME / "listen-scan.md"          # 要扫码时写的这一张（看板读、发预览站）；连上就删

LINK = re.compile(r"https?://([^/\s]+)\S*")
SHORT = 4                   # 名字尽可能短：留前几个字
PLACE = {"image": "[图片]", "face": "[表情]", "mface": "[表情]", "record": "[语音]",
         "video": "[视频]", "file": "[文件]", "forward": "[聊天记录]"}


def text_of(message, names=None, links=True, quote=None):
    """OneBot 的消息段数组 → 一行文字。@ 写成 @名字（names 认不出写号），图片写 [图片]、表情包写它带的描述；
    links：链接只留网站名（回复的模型用，省字；采集留全文）；quote：{被回复的消息 id: 那一行的文字}，给了就把回复
    写成“[回复 某人：喵喵]”。回复和采集共用这一份。"""
    if isinstance(message, str):
        return message
    names, out = names or {}, []
    for seg in message or ():
        t, d = seg.get("type"), seg.get("data") or {}
        if t == "text":
            text = d.get("text") or ""
            out.append(LINK.sub(r"[链接 \1]", text) if links else text)
        elif t == "at":
            q = str(d.get("qq"))
            out.append("@全体成员" if q == "all" else f"@{names.get(q, q)}")
        elif t == "reply":
            q = (quote or {}).get(str(d.get("id")))
            out.append(f"[回复 {q}]" if q else "[回复]")
        elif t in ("image", "mface") and d.get("summary", "").strip("[]") not in ("", "图片", "动画表情"):
            out.append(f"[表情：{d['summary'].strip('[]')}]")      # 表情包带一句描述，扫一眼就能看到
        else:
            out.append(PLACE.get(t, f"[{t}]"))
    return "".join(out).strip()


def short(name, n=SHORT):
    """名字尽可能短：去掉符号和装饰，留前 n 个字。"""
    s = re.sub(r"[^\w一-鿿぀-ヿ]", "", name or "")
    return s[:n] or (name or "?")[:n]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


class Closed(Exception):
    pass


class WS:
    """最小的 WebSocket 客户端（RFC 6455）：文本帧、分片、ping/pong、close；不做扩展和子协议。"""

    def __init__(self, host, port, path="/", headers=(), timeout=READ_TIMEOUT):
        self.sock = socket.create_connection((host, port), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        req = [f"GET {path} HTTP/1.1", f"Host: {host}:{port}", "Upgrade: websocket", "Connection: Upgrade",
               f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", *headers, "", ""]
        self.sock.sendall("\r\n".join(req).encode())
        self.buf = b""
        self.lock = threading.Lock()            # 收和发可以在不同线程（回复进程），发要串起来
        while b"\r\n\r\n" not in self.buf:
            self.buf += self._recv()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101 " not in status + " ":
            raise Closed(f"握手失败：{status}")
        self.sock.settimeout(timeout)

    def _recv(self):
        b = self.sock.recv(65536)
        if not b:
            raise Closed("对端关了连接")
        return b

    def _need(self, n):
        while len(self.buf) < n:
            self.buf += self._recv()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send(self, op, data=b""):
        n, mask = len(data), os.urandom(4)
        if n < 126:
            head = struct.pack(">BB", 0x80 | op, 0x80 | n)
        elif n < 1 << 16:
            head = struct.pack(">BBH", 0x80 | op, 0x80 | 126, n)
        else:
            head = struct.pack(">BBQ", 0x80 | op, 0x80 | 127, n)
        with self.lock:
            self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send(self, obj):
        self._send(0x1, json.dumps(obj, ensure_ascii=False).encode())

    def recv(self):
        """下一条完整的文本消息（str）；控制帧在这里处理掉。"""
        parts = []
        while True:
            b0, b1 = self._need(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._need(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._need(8))[0]
            mask = self._need(4) if b1 & 0x80 else None
            data = self._need(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x8:
                raise Closed(f"对端发来 close {struct.unpack('>H', data[:2])[0] if len(data) >= 2 else ''}")
            if op == 0x9:
                self._send(0xA, data)
                continue
            if op == 0xA:
                continue
            parts.append(data)
            if b0 & 0x80:
                return b"".join(parts).decode("utf-8")

    def close(self):
        try:
            self._send(0x8, struct.pack(">H", 1000))
        except OSError:
            pass
        self.sock.close()


def keep(event, groups=None, private=()):
    """记不记这条事件。心跳不记；配置了 groups（实例配置 [listen].groups）就只记这些群的事件、private 里那些号的
    私聊（双向：对方发的看 user_id，“我”发的看 target_id）和协议端自己的元事件（连上、断开）；别的私聊、别的群、
    加好友请求都不记（用户 10-31）；接口的回执（有 echo）不记。"""
    if not isinstance(event, dict):
        return True
    if event.get("meta_event_type") == "heartbeat" or "echo" in event:
        return False
    if groups is None or event.get("post_type") == "meta_event":
        return True
    if event.get("message_type") == "private" and "group_id" not in event:
        return event.get("user_id") in private or event.get("target_id") in private
    return event.get("group_id") in groups


def record(raw, path=LOG, now=None, groups=None, private=()):
    """一条消息 → 要不要记、记成哪一行（str 或 None）。不是 JSON 的照原文记在 raw 里。"""
    try:
        event = json.loads(raw)
    except ValueError:
        event = None
    if not keep(event, groups, private):
        return None
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="milliseconds")
    row = {"received": at, "event": event} if event is not None else {"received": at, "raw": raw}
    return json.dumps(row, ensure_ascii=False)


def append(line, path=LOG):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def groups(cfg=LISTEN):
    """只记哪些群（集合）；没配置就是 None，全记。"""
    return set(int(g) for g in cfg["groups"]) if "groups" in cfg else None


def private(cfg=LISTEN):
    """配置了 groups 时，还记哪些号的私聊。"""
    return {int(u) for u in cfg.get("private", ())}


def config(cfg=LISTEN):
    """实例配置 [listen] → (ssh 主机或 None, 远端 host:port, 本机端口, token)。"""
    remote = cfg.get("remote", "127.0.0.1:3001")
    ssh = cfg.get("ssh") or None
    local = int(cfg.get("local", 13001)) if ssh else int(remote.rsplit(":", 1)[1])
    token = ""
    if cfg.get("token_file"):
        token = Path(_dir(cfg["token_file"])).read_text().strip()
    return ssh, remote, local, token


def tunnel(ssh, remote, local):
    """起 ssh 隧道，等本机端口能连上；起不来返回 None。"""
    p = subprocess.Popen(["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
                          "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                          "-L", f"127.0.0.1:{local}:{remote}", ssh],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(40):
        if p.poll() is not None:
            log(f"隧道起不来（退出码 {p.returncode}）：{p.stderr.read().decode(errors='replace').strip()[-300:]}")
            return None
        try:
            socket.create_connection(("127.0.0.1", local), timeout=1).close()
            return p
        except OSError:
            time.sleep(0.5)
    p.kill()
    log("隧道 20 秒内没通")
    return None


def once(ssh, remote, local, token, only=None, peers=()):
    """连一次，收到断为止；连上过返回 True。"""
    p = tunnel(ssh, remote, local) if ssh else None
    if ssh and not p:
        return False
    host = "127.0.0.1" if ssh else remote.rsplit(":", 1)[0]
    ws = None
    try:
        ws = WS(host, local, headers=[f"Authorization: Bearer {token}"] if token else [])
        log(f"已连上 {ssh + ':' if ssh else ''}{remote}")
        while True:
            line = record(ws.recv(), groups=only, private=peers)
            if line:
                append(line)
    except (OSError, Closed) as e:
        log(f"断开：{type(e).__name__} {e}")
        return ws is not None
    finally:
        if ws:
            ws.close()
        if p:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()


def due(down, tried, now, after=REVIVE_AFTER, gap=REVIVE_GAP):
    """该不该去拉协议端：从 down（断开的时刻）起一直没连上满 after 秒，且离上次拉（tried，没拉过是 None）满 gap 秒。"""
    return now - down >= after and (tried is None or now - tried >= gap)


def ps(ssh, script):
    """在 ssh 主机（Windows）上跑一段 PowerShell，返回标准输出（那边是 GBK）。"""
    enc = base64.b64encode(f"$ErrorActionPreference='SilentlyContinue'; {script}".encode("utf-16le")).decode()
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", ssh, f"powershell -NoProfile -EncodedCommand {enc}"],
                       capture_output=True, timeout=60)
    return r.stdout.decode("gbk", "replace").strip()


def probe(ssh, home, port):
    """协议端那边的样子：{now: 那边的 UTC 时刻, qr: 二维码最后写入的 UTC 时刻（没有是空）, up: 端口在听,
    procs: 装在 home 下的进程数（只认这个目录的，系统 QQ 不算）}。时刻都是 PowerShell 的 'o' 格式，能按字符串比。"""
    out = ps(ssh, f"$q=(Get-Item '{home}\\versions\\*\\resources\\app\\napcat\\cache\\qrcode.png' | sort LastWriteTime"
                  f" | select -Last 1).LastWriteTimeUtc.ToString('o'); $l=[bool](Get-NetTCPConnection -LocalPort {port} -State Listen);"
                  f" $p=@(Get-Process | ? {{ $_.Path -like '{home}\\*' }}).Count;"
                  f" \"$((Get-Date).ToUniversalTime().ToString('o'))|$q|$l|$p\"")
    now, qr, up, procs = out.splitlines()[-1].split("|")
    return {"now": now, "qr": qr, "up": up == "True", "procs": int(procs)}


def stop(ssh, task, home):
    """结束计划任务和装在 home 下的进程（系统 QQ 不动）。"""
    ps(ssh, f"schtasks /end /tn {task} | Out-Null; Get-Process | ? {{ $_.Path -like '{home}\\*' }} | Stop-Process -Force")


def revive(ssh, task, home, port, look=probe, end=stop, sleep=time.sleep, wait=LOGIN_WAIT):
    """协议端进程不在就跑它的计划任务（用户 11-24：关掉登录窗口后小号离线了快一天），只认快速登录：端口起来算登上；
    写了新二维码（登录状态失效）或 wait 秒没登上，就把它关掉，不停在等扫码（用户 11-24）。进程还在（可能是用户
    开着窗口在扫）就不动。→ (这一次的经过, 要不要喊用户扫码)。"""
    try:
        p = look(ssh, home, port)
        if p["procs"]:
            return f"协议端进程还在（{p['procs']} 个），不拉", False
        t0 = p["now"]
        subprocess.run(["ssh", "-o", "BatchMode=yes", ssh, f"schtasks /run /tn {task}"], capture_output=True, timeout=60)
        for _ in range(wait // 5):
            sleep(5)
            p = look(ssh, home, port)
            if p["up"]:
                return "拉起协议端，快速登录上了", False
            if p["qr"] > t0:
                end(ssh, task, home)
                return "拉起协议端，快速登录失效、要扫码，已关掉", True
        end(ssh, task, home)
        return f"拉起协议端，{wait} 秒没登上，已关掉", True
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return f"拉起协议端没成：{type(e).__name__} {e}", False


def call(note, path=SCAN, cfg=LISTEN):
    """喊用户扫码：写 path（看板读它，连上后删），发到预览站「给用户」分类（实例配置 [show].review_ctl；没配就只写文件）。"""
    from ..store.instance import SHOW, USER_NAME
    at = datetime.now().astimezone().strftime("%m-%d %H:%M")
    path.write_text(f"# QQ 要扫码（{at}）\n\n{note}。{cfg.get('scan_hint', '')}\n", encoding="utf-8")
    ctl = SHOW.get("review_ctl")
    if not ctl:
        return "没配预览站，只写了文件"
    try:
        r = subprocess.run([sys.executable, str(Path(ctl).expanduser()), "publish", "--category", f"给{USER_NAME}",
                            "--name", "QQ要扫码.md", str(path)], capture_output=True, text=True, timeout=120)
        return f"发到预览站，退出码 {r.returncode}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"发预览站没成：{type(e).__name__} {e}"


def serve(cfg=LISTEN):
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))      # 服务停止时走 finally，把隧道一起收掉
    ssh, remote, local, token = config(cfg)
    task, home = (cfg.get("revive"), cfg.get("revive_dir")) if ssh else (None, None)
    port = int(remote.rsplit(":", 1)[1])
    wait, down, tried = RETRY, time.time(), None
    while True:
        if once(ssh, remote, local, token, groups(cfg), private(cfg)):
            wait, down = RETRY, time.time()
            SCAN.unlink(missing_ok=True)                        # 连上了：扫过码了，或自己登上了
        else:
            wait = min(wait * 2, RETRY_MAX)
            if task and home and not SCAN.exists() and due(down, tried, time.time()):
                tried = time.time()                             # 已经喊过扫码就不再拉，等用户
                what, scan = revive(ssh, task, home, port)
                log(what)
                if scan:
                    log(call(what))
                wait = RETRY
        log(f"{wait} 秒后重连")
        time.sleep(wait)
