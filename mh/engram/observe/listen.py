"""监听（G7，design/rhythm/shelved/群聊.md）：连 QQ 协议端（NapCat）的 OneBot 11 正向 WS，事件原样追加进数据目录的
listen-onebot.jsonl。只收不发；常驻，memory-harness-listen 服务跑它（python3 -m mh listen），不开库。

协议端在别的机器上、只听那台机器的本机时，自己起 ssh 隧道（实例配置 [listen]）；隧道或 WS 断了就整个重来，
等待从 RETRY 起翻倍，到 RETRY_MAX 为止，连上后复位。

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


def serve():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))      # 服务停止时走 finally，把隧道一起收掉
    ssh, remote, local, token = config()
    wait = RETRY
    while True:
        wait = RETRY if once(ssh, remote, local, token, groups(), private()) else min(wait * 2, RETRY_MAX)
        log(f"{wait} 秒后重连")
        time.sleep(wait)
