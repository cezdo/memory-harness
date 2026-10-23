"""Synapse：记忆的读取侧（design/synapse/设计.md）：推送 push、拉取 pull、足迹 track，宿主接入 hosts（E2，2026-10-22：按 hosts / push / pull / track 拆好）。
重构 E4（2026-10-20）：旧库 journal.db 归档（数据目录 archive/），旧库上的做法（旧推送、旧拉取、地图 Atlas、命中、回放、
需求片段、记忆时刻）随之删掉，留在 git 历史里；Atlas 以后在 cognize 里重构（D2）。"""
import os
from pathlib import Path

PURPOSES = ("interactive", "prefetch", "replay", "dev")   # 足迹的目的（track）；replay：离线回放


def library():
    """Synapse 读的库：新库 engram.db；环境变量 MH_DB 可以换成拷贝（测试时不在正式库上留足迹）。"""
    from ..engram import store
    return store.connect(Path(os.environ["MH_DB"]) if os.environ.get("MH_DB") else store.DEFAULT_DB)
