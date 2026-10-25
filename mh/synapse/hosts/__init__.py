"""hosts：各宿主的接入（Claude Code、Codex、dsh），适配层，不是过程（design/synapse/设计.md“hosts”）。
- 宿主的入口：Claude Code 和 dsh 调 `python3 -m mh inject`（手记推送）、`-m mh.synapse.push.associate`（联想）、`-m mh.synapse.pull.mcp`
  （拉取）；Codex 经 `-m mh.synapse.hosts.codex`；dsh 的插件在 dsh/，流水经 `-m mh.synapse.track.pushes`。
- 读上下文：各宿主自己的会话记录各一个 read_context（claude.py、codex/、dsh_log.py），返回 (上下文文本, 宿主注入的类型或 None, 找到这一句没有)。
- 这里：认出宿主注入的轮次（不是用户在说话），记下是哪类宿主事件。
"""
import re

HOST = (("<task-notification", "后台任务"), ("<command-", "命令"), ("<local-command", "命令输出"), ("<bash-", "shell"),
        ("This session is being continued", "压缩总结"), ("[Request interrupted", "打断"), ("Caveat:", "命令输出"),
        # 外部脚本/定时任务发起的轮次（2026-10-16）：走 `claude -p` 或 dsh headless 时既没有 isMeta、
        # 也没有 scheduled_task_fire，source.kind 也是 user，只能靠 prompt 自己的约定 —— 开头写「（定时任务」。
        # 这类轮次不是用户在说话，不联想（也不算“同一段对话里给过”），只按宿主事件留一条足迹。
        ("（定时任务", "定时任务（脚本发起）"))
# 宿主注入的轮次不做话题联想，但记下是哪类事件：任务完成、定时任务这些时刻，以后可能是函数提醒的触发点
# （“exit 0 ⊬ 结果正确”这类；2026-10-10，见 tmp/live/交接-cognition重新定义-2026-10-10.md）

COMMAND = re.compile(r"^(/[\w:-]+(\s|$)|! )")   # 斜杠命令、! 开头的 shell 命令：是命令，不是说话
TAG = re.compile(r"<(status|summary|command-name)>(.*?)</\1>", re.S)

def host_event(text, kind):
    """宿主事件的类型和程序能直接读出的字段（后台任务的状态、摘要；命令名）。"""
    ev = {"kind": kind}
    for k, v in TAG.findall(text):
        ev[k] = v.strip()[:200]
    return ev
