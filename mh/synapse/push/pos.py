"""分词、词性与依存句法（LTP small），给联想挑名词段用（依存的 ATT 分开“会话记录”这样的定中和“判断内容”这样的动宾）。

LTP 加载要八秒多、常驻约 680MB，联想每轮起一个进程不行，常驻又占内存，所以按需常驻：
第一次用时由客户端在后台拉起（用 LTP 的 venv 跑本文件），空闲 IDLE 秒后自己退出；开场 hook 顺手预热。
还没加载好的那一轮，tag() 返回 None，联想这一轮不做（不退回旧的不分词做法）。

本文件两用：mh 里 import 它当客户端（只用标准库）；用 LTP 的 venv 直接运行它就是服务端（`pos.py serve`）。
协议：Unix socket，一行 JSON {"texts": [...]}，回一行 JSON {"cws", "pos", "head", "label"}，每项一句一个列表。
"""
import fcntl
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

VENV = os.environ.get("MH_LTP_PYTHON", "/usr/local/mh-tools/ltp/venv/bin/python")
MODEL = os.environ.get("MH_LTP_MODEL", "/usr/local/mh-tools/ltp/model")
HOME = Path(os.environ.get("MH_HOME", Path.home() / ".local/share/memory-harness"))   # 同 collect.store.HOME（服务端不 import mh）
SOCK, LOCK, LOG = HOME / "pos.sock", HOME / "pos.lock", HOME / "pos.log"
IDLE = 3600          # 空闲多久退出（秒）
THREADS = 2          # torch 线程数：单句 20ms 左右，不跟观察、形成抢 CPU


def _ask(texts, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(SOCK))
        s.sendall(json.dumps({"texts": texts}, ensure_ascii=False).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            got = s.recv(65536)
            if not got:
                break
            buf += got
    return json.loads(buf)


def loading():
    """服务端正在加载或在跑：它一直拿着锁。"""
    try:
        with open(LOCK, "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def ensure():
    """没在跑就在后台拉起，不等。返回是否已经能用。"""
    try:
        _ask([], 1)
        return True
    except (OSError, ValueError):
        pass
    if not loading() and Path(VENV).exists():
        HOME.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as log:
            subprocess.Popen([VENV, str(Path(__file__).resolve()), "serve"], stdin=subprocess.DEVNULL, stdout=log,
                             stderr=log, start_new_session=True, cwd="/")
    return False


def tag(texts, timeout=5):
    """[(词, 词性, 依存的中心词序号（从 1 起，0 是根）, 依存关系), ...] 的列表，一句一个；服务没就绪返回 None（并顺手拉起）。"""
    try:
        r = _ask(list(texts), timeout)
        return [list(zip(*x)) for x in zip(r["cws"], r["pos"], r["head"], r["label"])]
    except (OSError, ValueError, KeyError):
        ensure()
        return None


def serve():
    HOME.mkdir(parents=True, exist_ok=True)
    lock = open(LOCK, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return                                  # 已经有一个在加载或在跑
    import torch
    from ltp import LTP
    torch.set_num_threads(THREADS)
    t = time.time()
    m = LTP(MODEL)
    print(f"{time.strftime('%F %T')} 加载 {time.time() - t:.1f}s", flush=True)
    if SOCK.exists():
        SOCK.unlink()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCK))
    os.chmod(SOCK, 0o600)
    srv.listen(8)
    srv.settimeout(IDLE)
    try:
        while True:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                break
            with conn:
                conn.settimeout(10)
                try:
                    buf = b""
                    while not buf.endswith(b"\n"):
                        got = conn.recv(65536)
                        if not got:
                            break
                        buf += got
                    texts = [x for x in json.loads(buf)["texts"]]
                    if texts:
                        r = m.pipeline(texts, tasks=["cws", "pos", "dep"])
                        out = {"cws": [[w.strip() for w in ws] for ws in r.cws], "pos": r.pos,
                               "head": [d["head"] for d in r.dep], "label": [d["label"] for d in r.dep]}
                    else:
                        out = {"cws": [], "pos": [], "head": [], "label": []}
                    conn.sendall(json.dumps(out, ensure_ascii=False).encode() + b"\n")
                except Exception as e:                  # 一次坏请求不让服务退出
                    print(f"{time.strftime('%F %T')} 出错 {e!r}", flush=True)
    finally:
        srv.close()
        SOCK.unlink(missing_ok=True)
        print(f"{time.strftime('%F %T')} 空闲 {IDLE}s，退出", flush=True)


if __name__ == "__main__" and sys.argv[1:] == ["serve"]:
    serve()
