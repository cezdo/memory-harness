"""数据迁移：旧服务端导出一个包，新服务端导入（design/deploy/设计.md“数据迁移”）。纯程序，不调用模型。

    python3 -m mh.deploy migrate export <包目录> [--live]   # 旧端：循环停着才导（单写）；--live 只给试跑
    python3 -m mh.deploy migrate import <包目录> [--force]  # 新端：目标已有库或同名文件不同时停下；--force 先改名再放

包里四样：
- data/：数据目录里带走的部分，库是 SQLite backup 出的一致快照；
- home/：宿主会话原文，按相对家目录的路径放（采集按会话号认会话，换了位置只会整体重读一遍，不会重复入库）；
- config/：实例配置和它引用的密钥文件（备份 .bak* 不带）；
- manifest.json：清单（每项怎么分）、各表行数、导出时的提交、配置里的绝对路径。导入后按行数核对。
"""
import fnmatch
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tomllib
from datetime import datetime
from pathlib import Path

VERSION = 1
LOOPS = ("observe", "form", "tidy")      # 写库的三个循环：各自常驻时拿着数据目录里的 <名>.lock（rhythm.lock）

# 数据目录顶层每一项怎么处理，按顺序第一个对上的算（fnmatch）。没对上的停下来，先在这里补一行再导。
TAKE = (                                  # 正本、原始记录和状态：带走
    "engram.db",                          # 库（单独做 backup，不直接拷）
    "events", "imports", "archive",       # 钩子事件与错误日志、导入的对话、旧记忆系统的存档
    "observe-switch.jsonl", "souhatsu-switch.jsonl", "yorimichi.jsonl",   # 开关记录（采集的一个来源）
    "cleaned.jsonl",                      # 清理记录
    "kernel", "kernel-log.jsonl", "names.json",                           # 内核态、名单变体
    "tidy-state.json", "status",          # 整理循环的状态与现状
    "engram-check-snapshot.json",         # check 和上一次比的基线
)
# 写它的服务不一定跟着搬（2026-11-25：只迁 observe、form、tidy，show、tend、listen、reply 留在旧机器，self 以后再搬）：
# 对应的服务在 moving 里才带走，不在就留在原机，不然两台机器各长一份。
KEEP = {
    "listen-onebot.jsonl": "listen", "reply-log.jsonl": "reply",         # 聊天原文、回复记录（observe、form 不读）
    "tend": "tend", "show": "show",                                       # 照看、看板的状态与历史
    "self.json": "self", "self-runs": "self",                             # self 的状态与每次醒来
}
REBUILD = (                               # 派生的缓存：新机器上自己算
    "assoc-cache-*", "codex-models.json", "quota-*.json", "reply-system.md",
)
STAY = (                                  # 本机运行时：留在原机
    "*-home",                             # 观察者、self、unplug 的隔离 home（带着这台机器的登录）
    "dsh-web*", "experiments", "run", "run.log", "review.html",   # 精读的 dsh web 实例、9 月的实验和旧审阅页
    "*.lock", "*.sock", "*.wake", "pos.log", "engram.db-*", "engram.db.*",
)


def classify(name, moving=LOOPS):
    """数据目录顶层一项 → "take" | "rebuild" | "stay" | None（不认识）。moving：跟着搬的服务。"""
    if name in KEEP:
        return "take" if KEEP[name] in moving else "stay"
    for kind, patterns in (("take", TAKE), ("rebuild", REBUILD), ("stay", STAY)):
        if any(fnmatch.fnmatchcase(name, p) for p in patterns):
            return kind
    return None


def plan(data, moving=LOOPS):
    """{kind: [名字…]}，含 None（不认识的）。"""
    out = {"take": [], "rebuild": [], "stay": [], None: []}
    for p in sorted(Path(data).iterdir()):
        out[classify(p.name, moving)].append(p.name)
    return out


def source_roots(home=None, data=None):
    """各采集来源读的目录里，在家目录下、不在数据目录里的（数据目录里的随隔离 home 留下）。按来源模块自己的 ROOT / ROOTS，不另抄一份。"""
    from ..engram import store
    from ..engram.observe.sources import SOURCES
    home, data = Path(home or Path.home()), Path(data or store.HOME)
    roots = []
    for mod in SOURCES.values():
        for r in [getattr(mod, "ROOT", None), *getattr(mod, "ROOTS", ())]:
            if r is None:
                continue
            r = Path(r)
            if r.is_relative_to(home) and not r.is_relative_to(data) and r not in roots:
                roots.append(r)
    return roots


def counts(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


# 写的是别的机器上的位置，不是这台要读的：展示页“东西都在哪”的说明；客户端各自的家目录（[deploy].clients，X7 cwd 对不上的修法）；
# 精读交给别的机器时提示词里的字数命令（[tools].mh_count）
ELSEWHERE = ("show.paths", "deploy.clients", "tools.mh_count")


def absolute_paths(config):
    """实例配置里写成绝对路径的值（键路径, 值）：搬到另一台机器上要逐条看。"""
    try:
        d = tomllib.loads(Path(config).read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out = []

    def walk(v, key):
        if key.startswith(ELSEWHERE):
            return
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{key}.{k}" if key else k)
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{key}[{i}]")
        elif isinstance(v, str) and v.startswith("/") and not any(c in v for c in " 、，；"):   # 说明文字里提到的路径不算
            out.append((key, v))
    walk(d, "")
    return out


def _held(data):
    """哪几个写库的循环正拿着锁（在跑）。"""
    import fcntl
    held = []
    for name in LOOPS:
        p = Path(data) / f"{name}.lock"
        if not p.exists():
            continue
        with open(p) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
            except BlockingIOError:
                held.append(name)
    return held


def _commit(repo):
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() or None


def _copy(src, dst):
    """拷一个文件或目录（保留时间戳）；目录合并进已有的。"""
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=False)


def export(dest, *, data=None, home=None, config=None, roots=None, live=False, moving=LOOPS, log=sys.stdout):
    """导出到 dest（不存在或为空）。返回清单。"""
    from ..engram import store
    from ..engram.store import instance
    data, home = Path(data or store.HOME), Path(home or Path.home())
    config = Path(config or instance.PATH.parent)
    roots = source_roots(home, data) if roots is None else [Path(r) for r in roots]
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit(f"包目录不是空的：{dest}")
    held = _held(data)
    if held and not live:
        raise SystemExit(f"这几个循环还在写库：{'、'.join(held)}。先停掉（单写）；只是试跑可以加 --live")
    p = plan(data, moving)
    if p[None]:
        raise SystemExit("数据目录里有清单不认识的：" + "、".join(p[None]) + "。先在 mh/deploy/migrate.py 的清单里补上怎么处理")
    (dest / "data").mkdir(parents=True)
    for name in p["take"]:
        src = data / name
        if name == "engram.db":
            print(f"库：backup {src}", file=log)
            s, t = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(dest / "data" / name)
            try:
                s.backup(t)
            finally:
                s.close()
                t.close()
        else:
            _copy(src, dest / "data" / name)
    originals = []
    for r in roots:
        if r.exists():
            rel = r.relative_to(home)
            print(f"原文：~/{rel}", file=log)
            _copy(r, dest / "home" / rel)
            originals.append(str(rel))
    configs = []
    if config.is_dir():
        for f in sorted(config.iterdir()):
            if f.is_file() and ".bak" not in f.name and f.name != "forward.toml":   # forward.toml 是客户端的，不随服务端走
                _copy(f, dest / "config" / f.name)
                configs.append(f.name)
    db = dest / "data" / "engram.db"
    manifest = {
        "version": VERSION, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": socket.gethostname(), "home": str(home), "data": str(data), "live": bool(held),
        "moving": list(moving),
        "commit": _commit(Path(__file__).resolve().parents[2]),
        "plan": {k: v for k, v in p.items() if k}, "originals": originals, "config": configs,
        "absolute_paths": absolute_paths(config / "instance.toml"),
        "counts": counts(db) if db.exists() else {},
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    return manifest


def _conflicts(src, dst):
    """src 里的文件在 dst 已有、内容（大小或时间）不同的。"""
    out = []
    files = [src] if src.is_file() else [p for p in src.rglob("*") if p.is_file()]
    for f in files:
        t = dst / f.relative_to(src) if src.is_dir() else dst
        if t.exists():
            a, b = f.stat(), t.stat()
            if (a.st_size, int(a.st_mtime)) != (b.st_size, int(b.st_mtime)):
                out.append(t)
    return out


def import_(src, *, data=None, home=None, config=None, force=False, log=sys.stdout):
    """把包放进这台机器。目标已有库、或有同名而内容不同的文件时停下；force 时先把它们改名成 <名>.before-import-<时刻>。"""
    from ..engram import store
    from ..engram.store import instance
    data, home = Path(data or store.HOME), Path(home or Path.home())
    config = Path(config or instance.PATH.parent)
    src = Path(src)
    manifest = json.loads((src / "manifest.json").read_text())
    if manifest.get("version") != VERSION:
        raise SystemExit(f"包的版本是 {manifest.get('version')}，这里认 {VERSION}")
    if _held(data):
        raise SystemExit("这台机器的循环在跑，先停掉再导入")
    pairs = [(src / "data" / n, data / n) for n in manifest["plan"]["take"]]
    pairs += [(src / "home" / r, home / r) for r in manifest["originals"]]
    pairs += [(src / "config" / n, config / n) for n in manifest["config"]]
    clash = [d for s, d in pairs if s.name == "engram.db" and d.exists()]
    clash += [c for s, d in pairs if s.name != "engram.db" and s.exists() for c in _conflicts(s, d)]
    if clash and not force:
        raise SystemExit("目标已有、内容不同的（--force 会先改名留底）：\n" + "\n".join(f"  {c}" for c in clash))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for c in clash:
        c.rename(c.with_name(f"{c.name}.before-import-{stamp}"))
        print(f"留底：{c} → {c.name}.before-import-{stamp}", file=log)
    for s, d in pairs:
        if s.exists():
            _copy(s, d)
    got = counts(data / "engram.db")
    diff = {k: (v, got.get(k)) for k, v in manifest["counts"].items() if got.get(k) != v}
    if diff:
        raise SystemExit("库的行数和导出时对不上：" + json.dumps(diff, ensure_ascii=False))
    print(f"导入完：库 {len(got)} 张表行数与导出时一致；数据目录 {len(manifest['plan']['take'])} 项、原文 {len(manifest['originals'])} 处、配置 {len(manifest['config'])} 个", file=log)
    if manifest["plan"]["rebuild"]:
        print("新机器上自己重建的缓存：" + "、".join(manifest["plan"]["rebuild"]), file=log)
    old = manifest.get("home")
    for key, value in manifest["absolute_paths"]:
        if not Path(value).exists():
            mark = "（旧机器家目录下）" if old and value.startswith(old.rstrip("/") + "/") else ""
            print(f"实例配置 {key} = {value}：这台机器上没有{mark}，要改", file=log)
    return manifest


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m mh.deploy migrate", description="数据迁移：export 在旧服务端导出，import 在新服务端导入")
    ap.add_argument("action", choices=["export", "import", "plan"])
    ap.add_argument("bundle", nargs="?", help="包目录（plan 不用）")
    ap.add_argument("--live", action="store_true", help="export：循环在跑也导（只给试跑：之后写进旧库的不在包里）")
    ap.add_argument("--force", action="store_true", help="import：目标已有的先改名留底再放")
    ap.add_argument("--with", dest="also", default="", help="export、plan：另外跟着搬的服务，逗号分开（" + "、".join(sorted(set(KEEP.values()))) + "）；默认只有 " + "、".join(LOOPS))
    ap.add_argument("--home", type=Path, help="import：放进另一个家目录（试跑；数据目录、实例配置取它下面的默认位置）")
    a = ap.parse_args(argv)
    also = [x for x in a.also.split(",") if x]
    bad = [x for x in also if x not in KEEP.values()]
    if bad:
        ap.error("不认识的服务：" + "、".join(bad))
    moving = (*LOOPS, *also)
    if a.action == "plan":
        from ..engram import store
        print("跟着搬的服务：" + "、".join(moving))
        for kind, names in plan(store.HOME, moving).items():
            print(f"{kind or '不认识'}：{'、'.join(names) or '—'}")
        print("原文：" + "、".join(f"~/{r.relative_to(Path.home())}" for r in source_roots()))
        return
    if not a.bundle:
        ap.error("给包目录")
    if a.action == "export":
        m = export(a.bundle, live=a.live, moving=moving)
        total = sum(m["counts"].values())
        print(f"导出完：{a.bundle}，库 {len(m['counts'])} 张表 {total} 行，提交 {m['commit'] and m['commit'][:8]}" + ("（--live：循环在跑）" if m["live"] else ""))
        for key, value in m["absolute_paths"]:
            print(f"  实例配置里的绝对路径 {key} = {value}")
    elif a.home:
        h = a.home.expanduser().resolve()
        import_(a.bundle, home=h, data=h / ".local/share/memory-harness", config=h / ".config/memory-harness", force=a.force)
    else:
        import_(a.bundle, force=a.force)
