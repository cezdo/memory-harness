"""yorimichi（寄り道，顺道拐一下；用户 2026-10-28）：这段对话现在允许联想拐几次。主会话（“我”）判断对话的阶段，
只在阶段变了的时候经 MCP 工具 `yorimichi` 改一次；联想的钩子每轮读它，决定给不给、给几条。

- width 0：在一个点上深挖，不给联想；1–3：在发散、轻松地聊，给几条。
- 按会话，最后一次说了算；没说过就是 0（宁可少给）。用户的 `/no-souhatsu`（push/switch）优先：关了就不联想。
- 记录追加到数据目录的 yorimichi.jsonl：时间、宿主、会话 ID、条数、理由。读状态就是找这个会话最后一行。
- 读不到、写不进都不挡对话：读不到当 0。
"""
import json
from datetime import datetime, timezone

from ....engram.store import HOME

LOG = HOME / "yorimichi.jsonl"
WIDTH_MAX = 3


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
    """这个会话现在的 → {"width"}（还有 time、why，说过的话）。没有会话号、没说过、读不到都是 0。"""
    got = {"width": 0}
    if not session:
        return got
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
    return f"记下了：{label(width)}。"


def label(width):
    """给人看的：Yorimichi 0 / Yorimichi 2（展示用首字母大写，标识符照旧小写）。"""
    return f"Yorimichi {width}"
