"""原文推送：客户端把宿主会话原文推到服务机的 ~ 下同样的相对位置（design/deploy/设计.md“安装器”）。

    python3 -m mh deploy push [--once]      # 常驻：Linux 上用 inotify 监听变化（防抖 2s + 5 分钟兜底），别处每 INTERVAL 秒推一次

服务机从 forward.toml 读（和宿主入口转发同一台）。推的是采集来源的目录（migrate.source_roots），按相对家目录的路径；
本机没有的目录不推。
只推不删：服务机上的文件（它自己的会话、以前推过的）一概不碰（2026-11-26）。
空会话（打开了还没开口的）不推；同一个进程每小时顺带清一次本机的空会话（clean，2026-11-27），规则见 engram/observe/clean。
"""
import ctypes
import os
import select
import subprocess
import sys
import time
from pathlib import Path

INTERVAL = 60
CLEAN_EVERY = 3600                  # 顺带清客户端空会话的间隔
FALLBACK = 300                     # inotify 模式下兜底全量同步间隔（新建深层子目录等）
DEBOUNCE = 2.0                      # 连续写入防抖：合并成一次 rsync
SSH = "ssh -o BatchMode=yes -o ConnectTimeout=5 -o ControlMaster=auto -o ControlPath=~/.ssh/cm-mh-%C -o ControlPersist=600"
_IN_MASK = 0x00000002 | 0x00000008 | 0x00000080 | 0x00000100   # MODIFY | CLOSE_WRITE | MOVED_TO | CREATE


def _watch_dirs(roots):
    """要监听的目录（根 + 现有子目录）。"""
    out = []
    for r in roots:
        if not r.is_dir():
            continue
        out.append(r)
        for cur, dirs, _ in os.walk(r):
            for d in dirs:
                out.append(Path(cur) / d)
    return out


class _Inotify:
    """纯标准库 ctypes 调 Linux inotify（不引入第三方依赖）；非 Linux 或不可用时抛 OSError。"""
    def __init__(self, roots):
        if not sys.platform.startswith("linux"):
            raise OSError("not linux")
        self._libc = ctypes.CDLL("libc.so.6", use_errno=True)
        self.fd = self._libc.inotify_init1(0o0004000 | 0o2000000)   # IN_NONBLOCK | IN_CLOEXEC
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        self._watched = set()
        self.refresh(roots)

    def refresh(self, roots):
        for d in _watch_dirs(roots):
            s = str(d)
            if s not in self._watched:
                if self._libc.inotify_add_watch(self.fd, os.fsencode(s), _IN_MASK) >= 0:
                    self._watched.add(s)

    def wait(self, timeout, debounce=DEBOUNCE):
        """等任意变化（返回 True）或超时（返回 False）；有变化后短等 debounce 合并连续写。"""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return False
        deadline = time.monotonic() + debounce
        while True:
            try:
                while os.read(self.fd, 65536):
                    pass
            except BlockingIOError:
                pass
            left = deadline - time.monotonic()
            if left <= 0:
                return True
            if not select.select([self.fd], [], [], left)[0]:
                return True

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def commands(server, roots, home=None, skip=None):
    """→ 要跑的命令（列表的列表）：先在服务机上建好目录，再逐个 rsync。skip：root → 相对它、这次不推的路径（空会话）。"""
    home = Path(home or Path.home())
    roots = [Path(r) for r in roots if Path(r).is_dir()]
    if not roots:
        return []
    rels = [str(r.relative_to(home)) for r in roots]
    mk = ["ssh", *SSH.split()[1:], server, "mkdir -p " + " ".join(rels)]
    return [mk] + [["rsync", "-a", *[f"--exclude=/{x}" for x in sorted((skip or {}).get(r, ()))],
                    "-e", SSH, f"{r}/", f"{server}:{rel}/"] for r, rel in zip(roots, rels)]


def _skip(roots, log):
    """空会话不推（2026-11-27）：出错就照常全推，不挡推送。"""
    from ..engram.observe import clean as c
    try:
        return c.excludes(roots)
    except Exception as e:                                  # noqa: BLE001
        print(f"判空会话失败，这次全推：{str(e)[:200]}", file=log, flush=True)
        return {}


def once(server, log=sys.stdout):
    from .migrate import source_roots
    rc = 0
    roots = source_roots()
    for cmd in commands(server, roots, skip=_skip(roots, log)):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            rc = r.returncode
            print(f"推送失败（{r.returncode}）：{' '.join(cmd[-2:])}：{(r.stderr or r.stdout).strip()[:300]}", file=log, flush=True)
    return rc


def clean(log=sys.stdout):
    """顺带清客户端上的空会话（engram/observe/clean.clean_client；2026-11-27）：本机判、本机删，规则和服务机清自己的一样；
    空会话推送时就跳过了，服务机上没有它的镜像。出错只记一行，不挡推送。"""
    from ..engram.observe import clean as c
    from .migrate import source_roots
    try:
        got = c.clean_client(source_roots(), apply=True)
    except Exception as e:                                  # noqa: BLE001
        print(f"清理空会话失败：{str(e)[:200]}", file=log, flush=True)
        return
    if got:
        print(f"清理空会话 {len(got)} 个：" + "、".join(f"{s}:{i[:12]}" for s, i, _ in got), file=log, flush=True)


def main(argv):
    from ..synapse.hosts import forward
    from .migrate import source_roots
    cfg = forward.config()
    if not cfg or not cfg.get("ssh"):
        raise SystemExit(f"没有转发配置（{forward.CONFIG}）：先 install client --server <服务机>")
    if "--once" in argv:
        raise SystemExit(once(cfg["ssh"]))
    try:
        watcher = _Inotify(source_roots())
    except OSError:
        watcher = None
    last_clean = 0.0
    try:
        while True:
            once(cfg["ssh"])
            if time.time() - last_clean >= CLEAN_EVERY:
                last_clean = time.time()
                clean()
            if watcher is not None:
                watcher.refresh(source_roots())
                watcher.wait(FALLBACK)
            else:
                time.sleep(INTERVAL)
    finally:
        if watcher is not None:
            watcher.close()
