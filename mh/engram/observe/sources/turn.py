"""三个来源共同的归一化单位：一轮对话。"""
from dataclasses import dataclass, field
import hashlib


@dataclass
class Session:
    source: str            # codex | dsh | claude
    session: str           # 来源内的会话 ID
    file: str              # 原始文件路径
    kind: str = "user"     # user | subagent | review
    parent: str = ""       # 父会话 / 根会话
    cwd: str = ""
    model: str = ""
    title: str = ""
    started: str = ""      # ISO 时间


@dataclass
class Turn:
    user: str                                        # 用户本轮的真实输入（已去掉宿主注入的上下文块）
    assistant: str = ""                              # 本轮最终回答
    commentary: list = field(default_factory=list)   # 中途的叙述性文字
    reasoning: list = field(default_factory=list)    # 可读的推理文字（来源提供明文时）
    tools: list = field(default_factory=list)        # 调用过的工具名
    started: str = ""
    address: dict = field(default_factory=dict)      # 原文位置：行号 / seq 范围
    closed: bool = False                             # 来源已标记本轮结束
    prev: int = None                                 # 接在第几轮（序号）之后；空 = 上一轮。回退改写时指回分叉点，-1 = 回到开头
    ending: str = ""                                 # 来源记下的非正常结束：aborted:user、error:<消息>、interrupted；空 = 正常或来源不记（目前只有 dsh 记）

    @property
    def id(self):
        """内容哈希；fork 或迁移造成的同一轮复制会落到同一个 ID。"""
        h = hashlib.sha256((self.user + "\x1e" + self.assistant).encode("utf-8"))
        return h.hexdigest()[:24]
