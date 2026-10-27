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
语言：这一轮用的语言和记录语言（中文）不同、或者“我”和上一轮换了语言的，由 language_note() 写进这一轮的说明（2026-10-26）。
转述：用户贴进来的别处文本（另一个模型的回复、别处起草的题目），由 relayed() 认出，说话人写“转来的文本”（2026-10-08；
2026-10-17 起由 observe 标在 E 上，form 从 E 读）。
"""
import json
import re

from . import guard
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


# 语言（2026-10-26，起因：中文提问、英文回答的轮次，E 全译成中文，原来的提法和“用英文答了”这件事都没留下）。
RECORD_LANGUAGE = "中文"     # 记录语言：提示词和 E 用的语言（runbook H6 做成实例配置前写死在这里）
# 一段话用什么语言，按句子的骨架认，不按字数（用户 2026-10-26）：谓语、修饰这些语法算子是骨干语言，名词、术语、引的原话
# 可能更长，但不算在用的语言（“我把 replay 停了”是中文，“I stopped the 回放”是英文）。骨架词是封闭的一类：
# 中文取语法算子坐标表 v0.2 的表层锚点（design/engram/form/shelved/语法算子.md），加上判断词、处置、被动；
# 英文、日文按同样的算子配各自的虚词、助动词、系词（没有坐标表，按算子的分组列）。锚点落在实词里也会算上，只用来比哪种多。
SKELETON = {
    "中文": """不 没 未 非 而且 并且 又 或者 或 要么 如果 就 只要 只有 才 因为 所以 由于 因此 为了 以便 好让 但是 不过 而
虽然 即使 也 有的 某个 每 所有 都 各 分别 一半 多数 少数 通常 一般 只 仅 唯独 还 同样 连 甚至 有点 稍微 很 非常 更 比 不如
一样 最 至少 至多 超过 不到 约 大约 差不多 将近 越 愈 可能 也许 或许 一定 必定 必然 可以 不妨 必须 应当 应该 得 能 能够
愿意 想要 希望 认为 觉得 看来 据说 听说 现在 当时 之前 之后 同时 先 再 了 正在 正 在 着 过 曾经 开始 变得 不再 仍然 继续
重新 经常 偶尔 总是 使 让 令 从 到 向 往 地 的 所 吗 是否 什么 谁 请 别 吧 是 把 被 呢 已经""".split(),
    "英文": """not no never n't nor and or if unless because so since therefore for to but however although though while
even some any every all each both either neither only just also too very quite rather more less than as most least about
around nearly almost may might maybe perhaps probably could must should ought need can cannot able will would shall want
hope wish think believe seem seems apparently now then before after when until already still yet again often always usually
sometimes ever make makes let lets in on at from into onto of by with without through over under the a an this that these
those which who whom whose what where why how whether please is are was were be been being am do does did have has had""".split(),
    "日文": """は が を に で と へ から まで より の も や か ね よ な ない ず ません ます です でした だ だった た て ている
いる ある れる られる せる させる たい ので のに けど けれど でも しかし また もし なら ば たら ても だけ しか さえ ほど くらい
ように こと もの""".split(),
}
SKELETON_MIN = 5             # 骨架词至少这么多个才判断，短的不判
SKELETON_LEAD = 2.0          # 最多的那种语言至少是第二多的这么多倍；骨架本身混得分不清的不判
_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)
_HAN_RUN = re.compile(r"[㐀-鿿]+")
_HIRA_RUN = re.compile(r"[ぁ-ゟ]+")
_WORD = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]+(?:'[A-Za-z]+)?(?![A-Za-z0-9_])")    # A1、v3 这种编号不是词
_ZH = re.compile("|".join(sorted(SKELETON["中文"], key=len, reverse=True)))
_JA = re.compile("|".join(sorted(SKELETON["日文"], key=len, reverse=True)))
_EN = set(SKELETON["英文"])
CODEX_REQUEST = "## My request:"   # codex 附件外壳“# Files mentioned by the user: … ## My request:”之后才是用户的话
_CODEY = re.compile(r"[=(){}<>|$;\[\]]|^\s{2,}|^\S+:\s")    # 贴进来的命令、代码、日志行（同 Synapse 联想的认法）


def skeleton(text):
    """各语言的骨架词个数：{语言: 个数}。中文在汉字串里、日文在平假名串里按长的优先数，英文按词（n't 单算）。"""
    words = [w.lower() for w in _WORD.findall(text) if w != "A"]       # 大写的单个 A 多是“A 和 B”的代号，不当冠词
    return {"中文": sum(len(_ZH.findall(r)) for r in _HAN_RUN.findall(text)),
            "英文": sum(w in _EN for w in words) + sum(w.endswith("n't") for w in words),
            "日文": sum(len(_JA.findall(r)) for r in _HIRA_RUN.findall(text))}


def _json(text):
    t = (text or "").strip()
    if not t.startswith(("{", "[")):
        return False
    try:
        json.loads(t)
        return True
    except ValueError:
        return False


def language(text, pasted=False):
    """一段话用什么语言写（按骨架，见 SKELETON）：中文、日文、英文；骨架词太少或分不清返回 None。代码块、行内代码、链接、路径不算，
    整段是 JSON 的不判。pasted：用户这一侧，再去掉像命令、代码、日志的行（贴进来的，不是用户自己写的话）。"""
    if _json(text):
        return None
    t = _CODE.sub(" ", text or "")
    if pasted:
        t = "\n".join(l for l in t.splitlines() if not _CODEY.search(l))
    t = guard.PATH.sub(" ", guard.URL.sub(" ", t))
    (top, n), (_, second) = sorted(skeleton(t).items(), key=lambda x: -x[1])[:2]
    return top if n >= SKELETON_MIN and n >= SKELETON_LEAD * second else None


def language_note(user, assistant, before=None, steady=True):
    """这一轮用的语言，一句话；都是记录语言、也没换语言的返回 None。
    before：同一会话前面最近一轮判得出语言的“我”的回复（None = 前面没有，按记录语言算）；“我”和它用的语言不同，写“换成”。
    steady=False：接着用同一种语言的轮不写（只写换了语言的，和用户用的不是记录语言的）。
    用户这一侧只看整段都是用户自己写的话：宿主注入、转述的不判，有一行像贴进来的命令、日志也不判（2026-10-26：贴日志的轮被读成“用户用英文说”）。"""
    mine = language(assistant)
    u = user_text(user)[0]
    u = u.split(CODEX_REQUEST, 1)[1] if CODEX_REQUEST in u else u     # codex 带附件时宿主套的英文外壳，只看用户的话
    own = kind(user) is None and relayed(user) is None and not any(_CODEY.search(l) for l in u.splitlines())
    theirs = language(u) if own else None
    prev = RECORD_LANGUAGE if before is None else language(before)
    switched = bool(mine and prev and mine != prev)
    out = []
    if theirs and (theirs != RECORD_LANGUAGE or (mine and mine != theirs and (steady or switched))):
        out.append(f"{USER_NAME}用{theirs}说")
    if switched:
        out.append(f"我换成{mine}回复（上一轮用{prev}）")
    elif steady and mine and mine != RECORD_LANGUAGE:
        out.append(f"我用{mine}回复")
    return "这一轮" + "，".join(out) + "。" if out else None
