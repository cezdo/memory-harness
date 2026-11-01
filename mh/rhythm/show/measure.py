"""量机器：实例配置 [show].hosts 里写了 os 的机器，各量一次在不在、开机多久、CPU、内存、GPU、磁盘（用户 2026-11-01）。
只读：本机直接读，别的机器经 ssh 跑系统自带的命令，不往那边写东西。慢（Windows 机器上的 PowerShell 约 4 秒），由 serve 在后台
每 MEASURE_EVERY 秒量一次写进数据目录 show/hosts.json，打开页面时只读这份。
每台 → {name, ok, at, err?, boot?, cpu?, cores?, load?, mem: [used, total]?, gpu?, vram?, unified?（GPU 用的是内存）, disks: [[挂载点, 总, 已用]]}，字节和 %。"""
import base64
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from ...engram.store import HOME
from ...engram.store.instance import SHOW

MEASURE_EVERY = 300
CACHE = HOME / "show" / "hosts.json"
SKIP_FS = ("tmpfs", "devtmpfs", "overlay", "squashfs", "efivarfs")

LINUX = (f"nproc; cat /proc/loadavg; cat /proc/uptime; head -1 /proc/stat; sleep 1; head -1 /proc/stat; "
         f"grep -E '^(MemTotal|MemAvailable):' /proc/meminfo; df -B1 --output=target,size,used {' '.join('-x ' + f for f in SKIP_FS)}")
MAC = ("sysctl -n hw.ncpu hw.memsize kern.boottime vm.loadavg; memory_pressure | tail -1; df -k /System/Volumes/Data | tail -1; "
       "ioreg -r -d 1 -c IOAccelerator | grep -o '\"Device Utilization %\"=[0-9]*\\|\"In use system memory\"=[0-9]*'; "
       "top -l 2 -n 0 -s 1 | grep 'CPU usage' | tail -1")
WINDOWS = r"""$ProgressPreference='SilentlyContinue'
$o=Get-CimInstance Win32_OperatingSystem
$c=(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$d=@(Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object { @($_.DeviceID,$_.Size,($_.Size-$_.FreeSpace)) })
$g=(Get-Counter '\GPU Engine(*)\Utilization Percentage' -ErrorAction SilentlyContinue).CounterSamples | Group-Object {($_.InstanceName -split '_engtype_')[1]} | ForEach-Object { ($_.Group | Measure-Object CookedValue -Sum).Sum } | Measure-Object -Maximum
$v=(Get-Counter '\GPU Adapter Memory(*)\Dedicated Usage' -ErrorAction SilentlyContinue).CounterSamples | Measure-Object CookedValue -Maximum
@{boot=$o.LastBootUpTime.ToUniversalTime().ToString('o');mem=@((($o.TotalVisibleMemorySize-$o.FreePhysicalMemory)*1024),($o.TotalVisibleMemorySize*1024));cpu=$c;disks=$d;gpu=$g.Maximum;vram=$v.Maximum;cores=[Environment]::ProcessorCount} | ConvertTo-Json -Compress -Depth 4"""


def run(cmd, ssh=None, timeout=40):
    a = ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", ssh, cmd] if ssh else ["bash", "-c", cmd]
    r = subprocess.run(a, capture_output=True, timeout=timeout)
    if r.returncode and not r.stdout:
        raise RuntimeError((r.stderr.decode("utf-8", "replace").strip().splitlines() or [f"退出码 {r.returncode}"])[-1][:120])
    return r.stdout.decode("utf-8", "replace")


def linux(out):
    ln = out.splitlines()
    cores, load, up = int(ln[0]), float(ln[1].split()[0]), float(ln[2].split()[0])
    a, b = ([int(x) for x in s.split()[1:]] for s in ln[3:5])
    busy = lambda v: (sum(v) - v[3] - v[4], sum(v))   # noqa: E731 —— idle、iowait 不算忙
    (b1, t1), (b2, t2) = busy(a), busy(b)
    mem = {k: int(v) * 1024 for k, v in re.findall(r"^(\w+):\s+(\d+)", "\n".join(ln[5:7]), re.M)}
    disks = [[p[0], int(p[1]), int(p[2])] for p in (s.split() for s in ln[8:]) if len(p) == 3 and p[1].isdigit()]
    return dict(cores=cores, load=load, boot=datetime.fromtimestamp(datetime.now().timestamp() - up, timezone.utc).isoformat(),
                cpu=round(100 * (b2 - b1) / max(t2 - t1, 1), 1), mem=[mem["MemTotal"] - mem["MemAvailable"], mem["MemTotal"]], disks=disks)


def mac(out):
    ln = out.splitlines()
    total = int(ln[1])
    free = int(re.search(r"(\d+)%", ln[4])[1])
    df = ln[5].split()
    gpu = re.search(r'"Device Utilization %"=(\d+)', out)
    vram = re.search(r'"In use system memory"=(\d+)', out)
    idle = re.search(r"([\d.]+)% idle", out)
    return dict(cores=int(ln[0]), load=float(ln[3].strip("{} ").split()[0]),
                boot=datetime.fromtimestamp(int(re.search(r"sec = (\d+)", ln[2])[1]), timezone.utc).isoformat(),
                cpu=round(100 - float(idle[1]), 1) if idle else None, mem=[round(total * (100 - free) / 100), total],
                gpu=int(gpu[1]) if gpu else None, vram=int(vram[1]) if vram else None, unified=True,
                disks=[["/", int(df[1]) * 1024, int(df[2]) * 1024]])


def windows(out):
    d = json.loads(out[out.index("{"):out.rindex("}") + 1])
    disks = d.get("disks") or []
    disks = [disks[i:i + 3] for i in range(0, len(disks), 3)] if disks and not isinstance(disks[0], list) else disks
    return dict(cores=d.get("cores"), boot=d["boot"], cpu=d.get("cpu"), mem=d["mem"],
                gpu=round(d["gpu"], 1) if d.get("gpu") is not None else None, vram=d.get("vram"), disks=[[a, int(b), int(c)] for a, b, c in disks])


def one(h):
    os_, ssh = h.get("os"), h.get("ssh")
    local = not ssh                    # 没有 ssh 就是本机：磁盘已经在“重启后看这里”，不重复列
    at = datetime.now(timezone.utc).isoformat()
    try:
        if os_ == "linux":
            r = linux(run(LINUX, ssh))
        elif os_ == "mac":
            r = mac(run(MAC, ssh))
        elif os_ == "windows":
            enc = base64.b64encode(WINDOWS.encode("utf-16-le")).decode()
            r = windows(run(f"powershell -NoProfile -EncodedCommand {enc}", ssh, timeout=60))
        else:
            return None
        return dict(name=h["name"], ok=True, at=at, local=local, **r)
    except Exception as e:
        return dict(name=h["name"], ok=False, at=at, local=local, err=f"量不到：{str(e)[:120] or e.__class__.__name__}")


def measure(cfg=SHOW):
    hosts = [h for h in cfg.get("hosts", []) if h.get("os")]
    with ThreadPoolExecutor(max(len(hosts), 1)) as ex:
        return [r for r in ex.map(one, hosts) if r]


def save(cfg=SHOW):
    rs = measure(cfg)
    CACHE.parent.mkdir(exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(rs, ensure_ascii=False))
    tmp.replace(CACHE)
    return rs
