"""观察者的 bash 白名单与路径打码（2026-10-17，实验 tmp/observe-e4/）。

白名单（精读）：只放行字数命令 mh-count，参数或 here-doc 给文字；带管道、分号、命令替换之类的一律不放行，
白名单外的调用判失败。dsh 必须用原生 minimal 预设带 bash，去掉工具推理会变长、能力下降，所以不拿掉 bash，只管它调什么。
打码（精读、粗读）：观察者去摸环境的根源是原文里的路径，所以给观察者的材料里，路径、文件名、链接换成 path_A、file_A.md、
url_A；观察者输出后再换回去。"""
import re
import shlex
import os

COUNT = os.environ.get("MH_COUNT", "/usr/local/mh-tools/mh-count/mh-count")
NAMES = {COUNT, "mh-count"}
OPS = set(";&|()<>")


def allow(cmd):
    cmd = cmd.strip()
    first, _, rest = cmd.partition("\n")
    m = re.fullmatch(r"(\S+)\s*<<-?\s*(['\"]?)(\w+)\2\s*", first)
    if m:                                   # here-doc：首行只有命令和 <<'EOF'，之后是正文和结束行，结束行后面不能再有东西
        lines = rest.split("\n")
        ends = [i for i, x in enumerate(lines) if x.strip() == m.group(3)]
        return m.group(1) in NAMES and bool(ends) and not "".join(lines[ends[0] + 1:]).strip() \
            and bool(m.group(2) or not re.search(r"\$\(|`|\$\{?\w", rest))
    if "$(" in cmd or "`" in cmd:
        return False
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        return False
    return bool(toks) and toks[0] in NAMES and not any(t and set(t) <= OPS for t in toks)


CJK = "一-鿿"
EXT = r"(?:md|py|html?|jsonl?|ya?ml|js|ts|mjs|sh|txt|db|toml|csv|zstd|log|patch|ini|cfg|conf|service|timer|png|jpe?g|pdf|ahk|ps1|bat|exe|zip)"
URL = re.compile(r"https?://[^\s<>\"'`)\]}，。；、）」]+")
A = r"[A-Za-z0-9_.\-@+]"                    # 路径的一节：只认 ASCII（Python 的 \w 也匹配中文）
F = rf"[A-Za-z0-9_\-{CJK}]"                  # 文件名可以有中文
PATH = re.compile(
    rf"(?<![A-Za-z0-9_./\-@{CJK}])"
    rf"(?:(?:~|\.{{1,2}})/{A}+(?:/[A-Za-z0-9_.\-@+{CJK}]+)*/?"         # ~/、./
    rf"|/{A}+(?:/[A-Za-z0-9_.\-@+{CJK}]+)+/?"                         # 绝对路径，至少两节（/Domain 这种单节不算）
    rf"|{A}+(?:/{A}+)+/"                                             # 相对目录，以 / 结尾
    rf"|{A}+(?:/{A}+)*/[A-Za-z0-9_.\-{CJK}]*{F}\.{EXT}"                 # 相对路径，最后是带扩展名的文件
    rf"|{F}+(?:\.[A-Za-z0-9_\-]+)*\.{EXT})"                             # 单独的文件名
    rf"(?![A-Za-z0-9_/{CJK}])")
TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:path|file|url)_[A-Z]+(?![A-Za-z0-9])")


def _name(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


class Mask:
    def __init__(self):
        self.fwd, self.back = {}, {}

    def _tok(self, kind, real):
        key = (kind, real)
        if key not in self.fwd:
            tok = f"{kind}_{_name(sum(1 for k in self.fwd if k[0] == kind))}"
            self.fwd[key], self.back[tok] = tok, real
        return self.fwd[key]

    def _path(self, m):
        p = m.group(0)
        if not re.search(r"[A-Za-z" + CJK + "]", p.replace("~", "")):     # 2026/10/17、3/4 这种不是路径
            return p
        if "/" not in p.strip("/") and not p.startswith(("/", "~", ".")) and not re.search(r"\." + EXT + "$", p):
            return p
        trail = "/" if p.endswith("/") and len(p) > 1 else ""
        p = p[:-1] if trail else p
        d, _, base = p.rpartition("/")
        if "/" in p and not re.search(r"\." + EXT + "$", base):        # 最后一节不像文件：整条当目录
            return self._tok("path", p) + trail
        out = []
        if "/" in p:
            out.append(self._tok("path", d) + "/" if d else "/")
        stem, dot, ext = base.rpartition(".")
        out.append(self._tok("file", stem) + dot + ext)
        return "".join(out) + trail

    def __call__(self, text):
        text = URL.sub(lambda m: self._tok("url", m.group(0)), text)
        return PATH.sub(self._path, text)

    def restore(self, text):
        return TOKEN.sub(lambda m: self.back.get(m.group(0), m.group(0)), text)
