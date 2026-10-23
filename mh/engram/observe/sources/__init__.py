"""采集来源：各宿主的会话文件 → 归一化的会话和轮次（turn.py 的 Session、Turn）。纯程序，不调用模型。

每个来源一个模块，给 files()（要看哪些文件）和 parse(path)（一个文件 → (会话, 轮次)）。
一个文件里有几个会话的来源（开关记录）给 parse_all(path) → [(会话, 轮次)…]。

新会话先等一等：会话开始不满 FRESH 就不收，也不标记文件，下一轮再看。测试、误开的会话被删掉时，
采集还没碰过它，就不会被引进来（2026-10-13）。会话开始时间认不出的照常收。
"""
from datetime import datetime, timedelta, timezone

from . import claude, codex, dsh, switch

SOURCES = {"codex": codex, "dsh": dsh, "claude": claude, "switch": switch}
FRESH = timedelta(minutes=30)     # 会话开始满这么久才收；0 = 不等（2026-10-13；10-16 由 10 分钟改 30，留出对照实验删掉“关”那一份的时间）


def _time(text):
    """来源记下的会话开始时间；认不出（空、格式不认）返回 None。"""
    try:
        t = datetime.fromisoformat((text or "").replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def young(session, now, fresh=FRESH):
    """这个会话是不是还没到收进来的时间（开始不满 fresh）。时间认不出的不算新。"""
    started = _time(session.started)
    return bool(fresh) and started is not None and now - started < fresh
