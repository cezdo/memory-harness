"""隧道（X7，design/deploy“macOS 服务端连局域网走隧道”）：服务机按实例配置 [deploy.tunnel] 起一条 ssh，
把要连的局域网端口映射到本机回环，同时当这台机器 ssh 到那边的复用主连接。

为什么要它：macOS 的本地网络权限拦 launchd 起的第三方程序连局域网（`No route to host`），系统设置里的开关管不到，
也不弹授权；Apple 签名的程序（/usr/bin/ssh、curl、nc）不受限。launchd 起的 python exec 成 /usr/bin/ssh 也照样能连
（2026-11-26 在 macOS 服务机上试过），所以服务仍是 `python -m mh deploy tunnel`，由 rhythm.register 生成，exec 之后就是 ssh。

    [deploy.tunnel]
    ssh = "laptop"                                   # ssh 名字（~/.ssh/config 里的 Host）
    forward = ["127.0.0.1:17890:127.0.0.1:7890"]     # 每条照 ssh -L 的写法
    control = "~/.ssh/cm-laptop-tunnel"             # 可选：同时当 ssh 的复用主连接；~/.ssh/config 那个 Host 的 ControlPath 指这里，
                                                     # 服务里起的 ssh 就走它（只碰本机的 unix socket，不受本地网络权限限制）
"""
import os
import sys
from pathlib import Path

SSH = "/usr/bin/ssh"
OPTS = ["-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]


def config():
    from ..engram.store.instance import _D
    return _D.get("deploy", {}).get("tunnel") or {}


def command(cfg):
    """[deploy.tunnel] → ssh 的参数表；没配 ssh 给 None。"""
    if not cfg.get("ssh"):
        return None
    argv = [SSH, *OPTS]
    if cfg.get("control"):
        argv += ["-o", "ControlMaster=yes", "-o", f"ControlPath={Path(cfg['control']).expanduser()}"]
    for f in cfg.get("forward", []):
        argv += ["-L", f]
    return argv + [cfg["ssh"]]


def main(argv):
    cmd = command(config())
    if not cmd:
        raise SystemExit("实例配置没有 [deploy.tunnel] 的 ssh：这台机器不需要隧道")
    if argv[:1] == ["--print"]:
        print(" ".join(cmd))
        return
    if cfg_control := config().get("control"):
        Path(cfg_control).expanduser().unlink(missing_ok=True)      # 上一次没收走的 socket，留着 ssh 会拒绝当主连接
    sys.stdout.flush()
    os.execv(SSH, cmd)
