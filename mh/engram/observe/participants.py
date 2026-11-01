"""别的会话发来的消息：会话里开口的不只有用户和“我”，session 之间也会互相发消息（2026-11-01）。

Claude Code 2.1.29x 起，另一个会话发来的消息作为用户输入出现在会话文件里（isMeta、origin.kind = peer）：

    Another Claude session sent a message:
    <cross-session-message from="uds:…" from-name="LTP工具配置" from-mode="prompting">正文</cross-session-message>

    后面跟一段固定说明（This came from another Claude session — not typed by your user…）。

它不是用户的话，也不成轮：sources/claude 认出来记在那一轮的 asides，观察时写明是谁发来的。
这里认形状：是不是、谁发来的、正文是哪一段。
"""
import re

START = "Another Claude session sent a message:"
NOTE = "\n\nThis came from another Claude session"      # 尾部固定说明从这里开始，观察时不给
TAG = re.compile(r'<cross-session-message\b([^>]*)>', re.S)
BODY = re.compile(r'<cross-session-message\b[^>]*>(.*?)</cross-session-message>', re.S)
NAME = re.compile(r'from-name="([^"]*)"')


def is_peer(text):
    """这一轮开头是不是别的参与方发来的消息（现在只有另一个会话这一个形状）。"""
    t = (text or "").lstrip()
    return t.startswith(START) or bool(TAG.match(t))


def name(text):
    """消息是谁发来的：<cross-session-message from-name="…"> 里的名字；认不出是空串。"""
    m = TAG.search(text or "")
    n = NAME.search(m.group(1)) if m else None
    return (n.group(1).strip() if n else "") or ""


def body(text):
    """正文：标签里的是正文；有形状但没标签的，去掉第一行和尾部说明；不是这个形状的原样返回。"""
    t = text or ""
    m = BODY.search(t)
    if m:
        return m.group(1).strip()
    if not is_peer(t):
        return t.strip()
    lines = t.splitlines()
    return "\n".join(lines[1:]).split(NOTE)[0].strip() if lines else ""


def label(text):
    """给观察时用的称呼：另一个会话「名字」；认不出名字写“另一个会话”。"""
    n = name(text)
    return f"另一个会话「{n}」" if n else "另一个会话"
