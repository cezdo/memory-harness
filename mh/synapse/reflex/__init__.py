"""反射（reflex，design/synapse/reflex/设计.md）：高重复、失败可感知的程序性事实的一句话现行答案。

归 Synapse 离线层（派生物，带句柄回到来路）。按作用域分两级（2026-11-19）：
- 环境级反射：跨工作区通用（发文件、机器地址、模型服务端口等），正本默认在代码目录 `mh/synapse/reflex/env.md`
  （实例配置 `[resources].reflex` 可覆盖），对所有接入 mh 的工作区常驻送达；
- 工作区级反射：留在该工作区的 `workspace-status/<工作区>.md` 的 `## 反射` 一节，只在该工作区送达。
"""
import re
from pathlib import Path
from ...engram.store.instance import resource

ENV = resource("reflex", Path(__file__).with_name("env.md"), "reflex.md")
TITLE = "### 反射（一句话现行答案；失效当场改源文件那一行、换新句柄）"
_HANDLE = re.compile(r"(?<![A-Za-z0-9_:])L(\d+|\?):T(\d+)(?![0-9])")


def parse(text):
    """从 Markdown 文本摘出反射条目（`- ...` 行）：有 `## 反射` 标题的只取那一节，没有的取全文的列表行。"""
    if "## 反射" in text:
        _, _, rest = text.partition("## 反射")
        text, _, _ = rest.partition("\n## ")
    return [l.strip() for l in text.splitlines() if l.strip().startswith("- ")]


def env(path=None):
    """环境级反射条目列表。"""
    p = Path(path if path is not None else ENV)
    return parse(p.read_text()) if p.is_file() else []


def valid_for(items, conn=None):
    """若给了库连接，过滤掉句柄在库里不存在的条目（避免测试库或无效句柄写进 use.inputs 破坏引用不变量）。"""
    if conn is None or not hasattr(conn, "execute"):
        return list(items)
    out = []
    for line in items:
        ok = True
        for lid, tid in _HANDLE.findall(line):
            if lid != "?" and not conn.execute("SELECT 1 FROM topic_line WHERE id=?", (int(lid),)).fetchone():
                ok = False
            if not conn.execute("SELECT 1 FROM session_entry WHERE id=?", (int(tid),)).fetchone():
                ok = False
        if ok:
            out.append(line)
    return out


def lines(ws_reflex=(), path=None, conn=None):
    """合并环境级反射与当前工作区反射（环境级在前，去重）。"""
    merged = list(dict.fromkeys([*env(path), *(ws_reflex or ())]))
    return valid_for(merged, conn)


def section(ws_reflex=(), path=None, conn=None):
    """手记近况里的反射小节行列表；没有条目时返回 []。"""
    got = lines(ws_reflex, path=path, conn=conn)
    return [TITLE, *got] if got else []
