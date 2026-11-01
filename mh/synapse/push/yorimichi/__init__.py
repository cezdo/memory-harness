"""yorimichi（寄り道，顺道拐一下；用户 2026-10-28）：这段对话现在允许联想拐几次。主会话（“我”）判断对话的阶段，
只在阶段变了的时候经 MCP 工具 `yorimichi` 改一次；联想的钩子每轮读它，决定给不给、给几条。

- width 0：在一个点上深挖，不给联想；1–3：在发散、轻松地聊，给几条。
- 按会话，最后一次说了算；没说过是 DEFAULT（1，用户 2026-11-02；10-28 起原来是 0，宁可少给）。用户的 `/no-souhatsu`（push/switch）优先：关了就不联想。
- 记录追加到数据目录的 yorimichi.jsonl：时间、宿主、会话 ID、条数、理由。读状态就是找这个会话最后一行（每轮要最新的）；
  观察循环的采集把它收成 turn 进库（observe 的 switch 来源，origin 是 main，即“我”；2026-10-30 runbook N3）。
- 读不到、写不进都不挡对话：读不到当 DEFAULT；没有会话号的（隔离的实验）当 0。
- 记下以后回给“我”现在的实情（2026-10-30）：送达还关着（联想的 DELIVER 是空的）就说这次只记下判断；用户关了这个会话的联想
  也说一声。“我”这边不另设联想开关：对“我”来说 0 就是关，用户的开关优先。
- 一段对话一直没判过的，联想在第 NUDGE_AT 句用户的话时提醒“我”一次（push/associate 的 nudge）。
"""
import json
from datetime import datetime, timezone

from ....engram.store import HOME

LOG = HOME / "yorimichi.jsonl"
WIDTH_MAX = 3
DEFAULT = 1          # 没判过的对话给几条（用户 2026-11-02 改成 1；之前是 0）


def check(width):
    """→ 0–3 的整数；不是就报错。"""
    try:
        width = int(width)
    except (TypeError, ValueError):
        width = -1
    if not 0 <= width <= WIDTH_MAX:
        raise ValueError(f"width 是 0–{WIDTH_MAX}：0 深挖不给联想，1–{WIDTH_MAX} 给几条")
    return width


def state(session, log=None):
    """这个会话现在的 → {"width"}（还有 time、why，说过的话）。没说过、读不到是 DEFAULT；没有会话号是 0。"""
    if not session:
        return {"width": 0}
    got = {"width": DEFAULT}
    try:
        with open(log or LOG, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("session") == session and isinstance(d.get("width"), int):
                    got = {k: d.get(k) for k in ("width", "time", "why")}
    except OSError:
        pass
    return got


def record(session, width, why="", host="", log=None):
    """记下一次变化 → 给“我”看的一句确认。没有会话号不记（记了也读不回来）。"""
    width = check(width)
    if not session:
        raise ValueError("拿不到会话号，记不下")
    log = log or LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "source": host, "session": session,
                            "width": width, "why": why or ""}, ensure_ascii=False) + "\n")
    return f"记下了：{label(width)}。" + now(session)


def now(session):
    """记下以后的实情：用户关了联想、送达还关着，各一句；都没有就是空。"""
    from ....engram.store.instance import USER_NAME
    from .. import switch
    from ..associate import DELIVER
    if switch.state(session) == "off":
        return f"{USER_NAME}关了这个会话的联想（/no-souhatsu），恢复以前不联想，这次只记下判断。"
    if not DELIVER:
        return f"联想的送达现在关着（Synapse 设计“联想”），这次只记下判断，{USER_NAME}在联想那行标题里看得到。"
    return ""


def label(width):
    """给人看的：Yorimichi 0 / Yorimichi 2（展示用首字母大写，标识符照旧小写）。"""
    return f"Yorimichi {width}"
