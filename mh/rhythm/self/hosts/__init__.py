"""self 的宿主接入（design/rhythm/self/接入.md）：一个宿主一个模块，照接入标准实现同一组函数，由那个宿主里的“我”自己写。

- NAME：便签头里 `host: <NAME>` 认它；不写就是 DEFAULT
- MODEL：用这个宿主醒来时的模型；宿主 + 模型就是这次由“我”的哪部分醒来，记进状态和核查
- ready() → 空串 = 现在能叫；否则是为什么不能（额度、没装）
- new_session() → 新会话编号；宿主不能事先指定的返回 ""，跑完由 outcome 读出来
- resumable(session) → 技术上是否能续接（记录还在、不太长）；不判断缓存或成本
- launch(root, text, session, resumed, logs) → 后台进程（新进程组）；cwd 是 self，输入是 text 文件，日志写进 logs
- outcome(logs) → (正常结束没有, 最后一句, 会话编号)
- actions(logs) → [("write", 路径) | ("run", 命令)]：这次会话（含子代理）自己做的，给核查用
"""
from . import claude, codex, dsh

HOSTS = {m.NAME: m for m in (claude, codex, dsh)}
DEFAULT = "claude"
