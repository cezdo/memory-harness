"""程序标注：宿主注入、IDE 标签、非正常结束、转述，都由程序认出、标注，不让模型去猜（2026-10-09）。原文不改，
只在组装观察输入时处理；采集时也用它定每一轮的来源（collect.origin）。

采集的切轮方式不变：后台通知、打断标记、命令回显、压缩总结仍各自算一“轮”。它们不是用户的话：
  通知  <task-notification>…：后台任务结束时宿主转来的通知
  打断  [Request interrupted by user…]：用户按 Esc 后宿主写的标记
  命令  <command-name>…、<local-command-stdout>…、<bash-input>/<bash-stdout>…：斜杠命令、! 命令与它们的回显
  压缩  “This session is being continued from a previous conversation…”：上下文压缩后宿主插入的总结（写的人是之前的“我”）
IDE 标签（<ide_opened_file>…、<ide_selection>…）加在用户的话前面，从用户的话里拿掉，写进环境。
后台通知去掉格式，只留主要信息（2026-10-20：不要让模型看那么多没用的格式化内容）：摘要、状态、Monitor 的输出（截短），
task-id、tool-use-id、output-file 和固定说明的 note 不给。预览、精读粗读、回溯的段落编号都经 user_text，切法一致。
压缩总结只留一个标记，正文不给（2026-10-20：对话的上下文是充足的，信息量只在压缩后的回复里）；以前读过总结的观察
留下的段落编号照旧认（回溯按那次观察的 Operator 参数决定切法，show.segments）。
转述：用户贴进来的别处文本（另一个模型的回复、别处起草的题目），由 relayed() 认出，说话人写“转来的文本”（2026-10-08；
2026-10-17 起由 observe 标在 E 上，form 从 E 读）。
"""
import re

from ..store.instance import USER_NAME, USER_ALIASES   # 称呼在实例配置里：写记录用 USER_NAME，匹配时认 USER_ALIASES 里任何一个
PLATFORMS = {"codex": "Codex", "dsh": "dsh（DeepSeek Harness）", "claude": "Claude Code"}

_IDE = re.compile(r"<(ide_opened_file|ide_selection)>(.*?)</\1>\s*", re.S)
_CMD = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
_NOTICE = re.compile(r"<task-notification>(.*?)</task-notification>", re.S)
COMPACTION = "This session is being continued from a previous conversation"
COMPACTION_MARK = "（上下文压缩：宿主插入了之前的“我”写的总结，内容略）"
EVENT_CAP = 300         # 通知里 Monitor 输出（<event>）最多给几个字


def kind(user):
    """这一轮开头是什么：None = 用户的话；否则是宿主注入的种类。"""
    u = _IDE.sub("", user or "").lstrip()
    if u.startswith("<task-notification>"):
        return "通知"
    if u.startswith("[Request interrupted by user"):
        return "打断"
    if u.startswith(("<command-name>", "<command-message>", "<local-command-stdout>", "<local-command-stderr>",
                     "<local-command-caveat>", "<bash-input>", "<bash-stdout>", "<bash-stderr>")):
        return "命令"
    if u.startswith("This session is being continued from a previous conversation"):
        return "压缩"
    return None


def wanted(user, assistant):
    """可以观察的内容：“我”写出了最终回答。没有最终回答的（被截断、被主动结束）不观察：往往意义不大，
    只给用户的话和工具清单，观察者会去调 bash 找缺的内容（6 轮各作废 3 次）（2026-10-10）。"""
    return bool(assistant)


def strip_ide(user):
    """拿掉 IDE 标签，返回 (用户的话, IDE 说明列表)。"""
    notes = []
    for tag, body in _IDE.findall(user or ""):
        body = re.sub(r"\s+", " ", body).strip()
        m = re.match(r"The user opened the file (.+?) in the IDE", body)
        body = m.group(1) if m else re.sub(r"^The user selected the (following )?", "", body)
        notes.append((f"{USER_NAME}当时在 IDE 里打开着：" if tag == "ide_opened_file" else f"{USER_NAME}当时在 IDE 里选中了：") + body[:200])
    return _IDE.sub("", user or "").strip(), notes


def condense(user, compaction=True):
    """后台通知只留主要信息：“后台任务通知（状态）：摘要”，有 Monitor 输出的接一句“输出：……”（截到 EVENT_CAP）；
    整段不留空行，段落编号和原文一样是一段。compaction：压缩总结只留标记（COMPACTION_MARK）。其余原样返回。"""
    if compaction and (user or "").lstrip().startswith(COMPACTION):
        return COMPACTION_MARK
    def one(m):
        body = m.group(1)
        get = lambda tag: " ".join(re.findall(rf"<{tag}>(.*?)</{tag}>", body, re.S)).strip()
        summary, status, event = get("summary"), get("status"), re.sub(r"\s+", " ", get("event"))
        out = "后台任务通知" + (f"（{status}）" if status else "") + f"：{summary or '（没有摘要）'}"
        if event:
            out += "；输出：" + (event[:EVENT_CAP] + f"……（共 {len(event)} 字）" if len(event) > EVENT_CAP else event)
        return out
    return _NOTICE.sub(one, user or "")


def user_text(user, compaction=True):
    """组装观察输入时用户这一侧的文字：拿掉 IDE 标签，后台通知只留主要信息，压缩总结只留标记（compaction=False 时给全文，
    回溯以前读过总结的观察用）。返回 (文字, IDE 说明列表)。原文不改。"""
    text, notes = strip_ide(user)
    return condense(text, compaction).strip(), notes


def environment(row, model):
    """输入里的“环境”一节：对话平台、工作目录、当时的“我”、回看的“我”（总设计“我是谁”的两层环境）。"""
    unknown = lambda v: not v or str(v).startswith("?")
    return (f"## 环境\n对话平台：{PLATFORMS.get(row['source'], row['source'])}，"
            f"工作目录 {'未知' if unknown(row['cwd']) else row['cwd']}，"
            f"当时的“我”：{'未知模型' if unknown(row['model']) else row['model']}，"
            f"现在回看的“我”：{model.model}（经 {model.platform}）")


def notes(user, assistant, closed, nxt=None, rewound=False, ending=None):
    """写进环境一节的标注。nxt：同一会话下一轮的开头（判断是不是用户打断的）。
    rewound：后面还有对话，但没有一轮接在这一轮之后——用户回退到更早的地方重新输入了（Claude Code 回退改写）。
    ending：来源记下的非正常结束（store 的 occurrences.ending）。"""
    out = []
    k = kind(user)
    if k == "通知":
        out.append(f"这一轮开头不是{USER_NAME}的输入，是宿主转来的后台任务通知；U 段落都不是{USER_NAME}的话。")
    elif k == "打断":
        out.append(f"这一轮开头是宿主写的打断标记（{USER_NAME}按 Esc 打断了上一轮），不是{USER_NAME}的话。")
    elif k == "命令":
        m = _CMD.search(user or "")
        out.append(f"这一轮开头是{USER_NAME}执行的命令{(' ' + m.group(1)) if m else ''}，其余是宿主的输出，不是{USER_NAME}说的话。")
    elif k == "压缩":
        out.append(f"这一轮开头是宿主插入的上下文压缩总结，写的人是之前的“我”，不是{USER_NAME}。")
    out += strip_ide(user)[1]
    if ending:
        how, _, detail = ending.partition(":")
        how = {"aborted": f"{USER_NAME}中止了它" if detail == "user" else "被中止了",
               "error": f"模型调用出错（{detail}）" if detail else "模型调用出错"}.get(how, f"被中断了（{ending}）")
        out.append(f"这一轮没有正常结束：{how}。最后一段 A 是停下前的过程话，不是最终回答；这一轮停在哪里，以它为准。")
    if rewound:
        out.append(("这一轮没有正常结束。" if not closed else "") +
                   f"之后{USER_NAME}回退到这一轮之前重新输入，这一轮所在的分支没有再继续；另一条分支另有记录。")
    elif not closed:
        out.append("这一轮没有正常结束，被下一次输入打断了" + (f"：是{USER_NAME}按 Esc 打断的。" if kind(nxt) == "打断" else "。"))
    return out


RELAY_CHARS = 1000       # 转述：用户一轮里超长、完整的 Markdown 文本（2026-10-08）；长度下限（旋钮）
_MARKUP = {"标题": (r"(?m)^#{1,6} ", 2), "加粗": (r"\*\*[^*\n]+\*\*", 4), "表格": (r"(?m)^\|.*\|\s*$", 3),
           "代码块": (r"(?m)^\s*```", 2), "列表": (r"(?m)^\s*(?:[-*]|\d+\.) ", 8)}
_MARKUP_LINE = re.compile(r"^\s*(?:#{1,6} |\||```|[-*] |\d+\. |>)|\*\*")


def relayed(text):
    """用户贴进来的别处文本（另一个模型的回复、别处起草的题目）开头未必说明是拷贝的，由程序识别：
    超长，且至少用了两种 Markdown 结构。终端输出、日志、HTML 没有这些结构，不算。
    返回转述前用户自己写的引语（Markdown 开始之前的第一句，可能为空）；不是转述返回 None。"""
    text = text or ""
    if len(text) < RELAY_CHARS or sum(len(re.findall(p, text)) >= n for p, n in _MARKUP.values()) < 2:
        return None
    head = []
    for line in text.splitlines():
        if _MARKUP_LINE.search(line):
            break
        head.append(line)
    preface = re.match(r"[^。！？：:]*[。！？：:]?", "\n".join(head).strip()).group(0)   # 引语常和转述连在同一行，截到第一句
    return preface if len(preface) <= 300 else ""
