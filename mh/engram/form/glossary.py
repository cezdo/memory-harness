"""从 design/global/词典.md 的词头读出“同一个东西的几种叫法”（2026-10-18：词典该发挥作用，名字用全名，简写只当别名）。纯程序。

词头的写法见词典“怎么维护”：“/”只连同一个东西的叫法，全名在前；并列的不同东西用“、”；“；”分开相关但不同的东西；
括号里只放说明，整段去掉；反引号里的内容整体算一个名字。实验：tmp/form-attach/glossary.py。
- entries()：[{"full": 全名, "names": [这一个东西的全部叫法], "head": 词头}]
- used(text)：这段文字里用到了哪些词条（叫法逐字出现；ASCII 的叫法前后不能是字母数字；单个汉字的叫法不算）
- aliases(word)：这个词所在那一个东西的全部叫法（全名在前）；另补本实例已确认的个人称呼候选；通用角色词不并入个人组
"""
import re
from pathlib import Path
from ..store import instance
from ..store.instance import resource

PATH = resource("glossary", Path(__file__).resolve().parents[3] / "design/global/词典.md", "glossary.md")
_cache = {}


def entries(path=PATH):
    path = Path(path)
    stamp = (str(path), path.stat().st_mtime if path.exists() else 0)
    if stamp not in _cache:
        out = []
        for line in (path.read_text().splitlines() if path.exists() else []):
            m = re.match(r"^\*\*(.+)\*\*\s*$", line.strip())
            if not m:
                continue
            head = re.sub(r"（[^）]*）|\([^)]*\)", "", m.group(1))            # 括号只放说明
            ticks = {}

            def hold(x):
                k = f"\0{len(ticks)}\0"
                ticks[k] = x.group(1)
                return k
            head = re.sub(r"`([^`]+)`", hold, head)
            for part in re.split(r"[；;]", head):
                for thing in part.split("、"):                                 # 并列的不同东西各算一个
                    names = [ticks.get(n.strip(), n.strip()) for n in thing.split("/")]   # “/”连同一个东西的叫法
                    names = [n.replace("`", "").strip() for n in names]
                    names = [n for n in names if n and len(n) <= 30]
                    if names:
                        out.append({"full": names[0], "names": names, "head": m.group(1)})
        _cache.clear()
        _cache[stamp] = out
    return _cache[stamp]


def _hit(name, text):
    if len(name) < 2 and not name.isascii():
        return False                     # 单个汉字（“线”）到处都是，不算用到
    if name.isascii():
        return re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", text) is not None
    return name in text


def used(text, path=PATH):
    """用到的词条（同一个东西只算一次）。"""
    out, seen = [], set()
    for e in entries(path):
        key = (e["head"], e["full"])
        if key not in seen and any(_hit(n, text) for n in e["names"]):
            out.append(e)
            seen.add(key)
    return out


def personal_names():
    """实例提供的称呼候选；通用角色词不加入个人组。"""
    return list(dict.fromkeys(n for n in instance.USER_ALIASES if n.casefold() not in ("用户", "user")))


def aliases(word, path=PATH):
    w = word.strip().lower()
    names = personal_names()
    private = names if w in {n.lower() for n in names} else []
    for e in entries(path):
        if w in {n.lower() for n in e["names"]}:
            return list(dict.fromkeys([*e["names"], *private]))
    return private


def text(record, path=PATH):
    """附给模型的一节：记录里用到、又有别的叫法的词条（起名用全名）。没有就是空串。"""
    out = [f"- {e['full']}（也叫：{'、'.join(e['names'][1:])}）" for e in used(record, path) if len(e["names"]) > 1]
    result = ("\n\n## 词典（记录里用到的词，前面是全名）\n\n" + "\n".join(out)) if out else ""
    names = personal_names()
    if len(names) > 1 and any(_hit(n, record) for n in names):
        result += "\n\n## 本实例的称呼线索\n\n" + "、".join(names) + "。这些叫法供检索和理解参考；同名也可能指机器、目录或第三方，按上下文判断，不据此合并主体。"
    return result


INDEX_START, INDEX_END = "<!-- 词头索引：程序生成（glossary.index），别手改 -->", "<!-- 词头索引完 -->"


def index(path=PATH):
    """词典开头的词头索引（2026-10-21：减量，给上下文让路）：按章节列出全部词头，读者先看索引，再按词头搜正文。"""
    out, section = [], None
    for line in Path(path).read_text().split(INDEX_END)[-1].splitlines():
        if re.match(r"^#{2,3} ", line):
            section = line.lstrip("#").strip()
            out.append(f"\n- {section}：")
        m = re.match(r"^\*\*(.+)\*\*\s*$", line.strip())
        if m and section:
            out[-1] += ("" if out[-1].endswith("：") else " · ") + m.group(1)
    return "\n".join([INDEX_START, "## 词头索引", "".join(o for o in out if not o.endswith("：")), INDEX_END])


def write_index(path=PATH):
    """把索引写进词典（放在“怎么维护”之前；已有的就换掉）。"""
    path = Path(path)
    s = path.read_text()
    if INDEX_START in s:
        s = s[:s.index(INDEX_START)] + s[s.index(INDEX_END) + len(INDEX_END) + 2:]
    i = s.index("## 怎么维护")
    path.write_text(s[:i] + index(path) + "\n\n" + s[i:])
