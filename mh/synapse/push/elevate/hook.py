"""内核态的钩子（Claude Code、codex 共用）：python3 -I -S hook.py claude|codex prompt|pre|post|stop

每次调工具都会跑（post），所以只 import 标准库和实例配置。出错一律放行：错误记进数据目录的 events/kernel-errors.log，
漏拦的改动由下一次 post 的比对兜住。用法见 __init__.py。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))


def main(argv):
    host, event = argv[1], argv[2]
    d = {}
    try:
        d = json.load(sys.stdin)
        if d.get("agent_id") or d.get("agent_type"):      # 子代理：提权只给用户直接对话的那一层
            if event in ("prompt", "stop"):
                return 0
        from mh.engram.store import HOME
        from mh.synapse.push import CORE as STANDING
        from mh.synapse.push.elevate import Kernel, handle
        out, code = handle(Kernel(STANDING, HOME), host, event, d)
        if host == "codex" and out and out.get("decision") == "block":     # codex 拦输入：退出码 2 加 stderr 的理由
            print(out["reason"].replace("/mhcore", "$mhcore"), file=sys.stderr)
            return 2
        if out is not None:
            json.dump(out, sys.stdout, ensure_ascii=False)
        return code
    except Exception as e:
        try:
            from mh.engram.store import HOME
            (HOME / "events").mkdir(parents=True, exist_ok=True)
            with open(HOME / "events/kernel-errors.log", "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().astimezone().isoformat()} {host} {event} {d.get('session_id', '')} {e!r}\n")
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
