"""python3 -m mh.synapse.push.elevate [--apply]：装 /mhcore 的命令、skill 和钩子；不带 --apply 只列要做什么。
python3 -m mh.synapse.push.elevate log [-l info|notice|warn] [#编号]：看内核日志（dmesg 式，一条一行；给编号展开那一条）。"""
import sys

from . import LEVELS, detail, install, view
from ....engram.store import HOME

argv = sys.argv[1:]
if argv[:1] == ["log"]:
    log, rest = HOME / "kernel-log.jsonl", argv[1:]
    level = rest[rest.index("-l") + 1] if "-l" in rest else "info"
    if level not in LEVELS:
        raise SystemExit(f"-l 是 {'、'.join(LEVELS)}")
    picks = [int(x.lstrip("#")) for x in rest if x.lstrip("#").isdigit()]
    print("\n\n".join(detail(log, n) for n in picks) if picks else "\n".join(view(log, level)) or "没有记录")
else:
    done = install(apply="--apply" in argv)
    print("\n".join(done) if done else "都装好了")
    if done and "--apply" not in argv:
        print("（预览；加 --apply 才装。codex 的新钩子装完要在 /hooks 里信任一次）")
