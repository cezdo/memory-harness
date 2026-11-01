"""原文段落编号：把一轮对话切成编号段落（U1… / A1…），供模型按编号指认来源，读的时候按同样的切法找回段落。纯程序，随时能重算。"""
import re

BLOCK_CAP = 1500        # 单段展示上限；超出部分中间省略，原文仍在库里
_HEADING = re.compile(r"^#{1,6}\s")
_FENCE = re.compile(r"^\s*(```|~~~)")


def split(text):
    blocks, cur, fenced = [], [], False

    def flush():
        if cur and "".join(cur).strip():
            blocks.append("\n".join(cur).strip("\n"))
        cur.clear()

    for line in text.splitlines():
        if _FENCE.match(line):
            if not fenced:
                flush()
            cur.append(line)
            fenced = not fenced
            if not fenced:
                flush()
            continue
        if fenced:
            cur.append(line)
        elif not line.strip():
            flush()
        elif _HEADING.match(line):
            flush()
            cur.append(line)
            flush()
        else:
            cur.append(line)
    flush()
    return blocks


def _clip(text):
    if len(text) <= BLOCK_CAP:
        return text
    keep = BLOCK_CAP // 2
    return f"{text[:keep]}\n…（中间省略 {len(text) - 2 * keep} 字）…\n{text[-keep:]}"


def number(turn, marks=None):
    """返回 {编号: 原文段落} 以及可直接放进提示词的文本。
    extra：别的会话发来的消息（turn_detail.asides，2026-11-01）：排在 U 后面，每条前面写明是谁、不是用户的话；
    轮后发来的（after），“我”的回答 reply 排在 A 最后。marks 给一个 dict 时，把这些编号的说明写进去，
    调用方按它渲染（原文段落本身不改，回溯还按原话找）。"""
    user = split(turn["user"])
    process = [b for c in turn["commentary"] for b in split(c)]
    final = split(turn["assistant"])
    table, lines = {}, []
    n = 0
    for b in user:
        n += 1
        table[f"U{n}"] = b
        lines.append(f"[U{n}] {_clip(b)}")
    replies = []
    for x in turn.get("extra") or ():
        mark = f"（{'这一轮结束之后' if x.get('after') else ''}{x['who']}发来消息，不是用户的话）"
        for b in split(x["text"]):
            n += 1
            table[f"U{n}"] = b
            lines.append(f"[U{n}] {mark}{_clip(b)}")
            if marks is not None:
                marks[f"U{n}"] = mark
        if x.get("reply"):
            replies.append((x["who"], x["reply"]))
    lines.append("")
    if turn.get("ending"):          # 中止、出错：没有最终回答（2026-10-11）
        lines.append("（这一轮没有正常结束，A 都是过程中的话）")
    elif process:
        lines.append(f"（A1–A{len(process)} 是过程中的话，A{len(process) + 1} 起是最终回答）")
    i = 0
    for i, b in enumerate(process + final, 1):
        table[f"A{i}"] = b
        lines.append(f"[A{i}] {_clip(b)}")
    for who, text in replies:
        mark = f"（这一轮结束之后“我”对{who}的回答）"
        for b in split(text):
            i += 1
            table[f"A{i}"] = b
            lines.append(f"[A{i}] {mark}{_clip(b)}")
            if marks is not None:
                marks[f"A{i}"] = mark
    return table, "\n\n".join(lines)
