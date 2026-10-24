"""python3 -m mh.synapse.push.elevate [--apply]：装 /mhcore 的命令、skill 和钩子；不带 --apply 只列要做什么。"""
import sys

from . import install

done = install(apply="--apply" in sys.argv[1:])
print("\n".join(done) if done else "都装好了")
if done and "--apply" not in sys.argv[1:]:
    print("（预览；加 --apply 才装。codex 的新钩子装完要在 /hooks 里信任一次）")
