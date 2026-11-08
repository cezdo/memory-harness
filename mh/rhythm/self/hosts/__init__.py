"""self 的宿主接入（design/rhythm/self/接入.md）：一个宿主一个模块，照接入标准实现同一组函数，由那个宿主里的“我”自己写。

- NAME：便签头里 `谁：<NAME>` 认它；不写就是 DEFAULT
- ready() → 空串 = 现在能叫；否则是为什么不能（额度、没装）
- new_session() → 新会话编号；宿主不能事先指定的返回 ""，跑完由 outcome 读出来
- resumable(session) → 这段会话能不能接着（记录还在、不太长）
- launch(root, text, session, resumed, logs) → 后台进程（新进程组）；cwd 是 self，输入是 text 文件，日志写进 logs
- outcome(logs) → (正常结束没有, 最后一句, 会话编号)
- actions(logs) → [("write", 路径) | ("run", 命令)]：这次会话（含子代理）自己做的，给核查用
"""
from . import claude

HOSTS = {m.NAME: m for m in (claude,)}
DEFAULT = "claude"
