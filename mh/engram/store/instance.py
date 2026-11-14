"""实例配置（重构 H，2026-10-22）：这一个用户、这一台机器的设定，不写在代码里。

读 `~/.config/memory-harness/instance.toml`（环境变量 MH_INSTANCE 可换），不进仓库；没有这个文件时用通用默认，
写不对时在 stderr 说一声、也用默认：Synapse 挂在宿主上，宿主不能因为它报错。格式见 instance.example.toml。
"""
import os
import sys
import tomllib
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

PATH = Path(os.environ.get("MH_INSTANCE") or Path.home() / ".config/memory-harness/instance.toml")


def _load(path):
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"[mh] 实例配置读不了，用默认：{path}：{e}", file=sys.stderr)
        return {}


def _dir(s):
    return str(Path(s).expanduser())


_D = _load(PATH)
_user, _ws, _tools = _D.get("user", {}), _D.get("workspace", {}), _D.get("tools", {})

USER_NAME = _user.get("name", "用户")                    # 程序写记录、填提示词时用的称呼
USER_ALIASES = tuple(dict.fromkeys([USER_NAME, *_user.get("aliases", ())]))   # 匹配时认其中任何一个；第一个是 USER_NAME

PROJECTS = Path(_dir(_D["projects"])) if _D.get("projects") else None          # 放项目的目录（Synapse 认工作区用）

ENGRAM_DIRS = {_dir(d): w for d, w in _ws.get("engram", {}).items()}           # Engram：按目录更正工作区（最长前缀）
SYNAPSE_DIRS = {Path(_dir(d)): w for d, w in _ws.get("synapse", {}).items()}   # Synapse：改名、删除、别名的目录
MANAGING = {w: _dir(d) for w, d in _ws.get("managing", {}).items()}            # 管理性质的工作区 → 目录
SESSIONS = {(s["source"], s["session"]): s["workspace"] for s in _ws.get("sessions", ())}   # 按会话更正工作区

QUIET = frozenset(w.lower() for w in _D.get("synapse", {}).get("quiet", ()))   # Synapse 不当“查不到的词”送的词（手动名单）
# 会话接 mh 到哪一步（[connect]，用户 2026-11-14 把按位置的开关并到一节）：默认全接；unwatched 接 mh、原文照收、不观察；
# unplugged 不接 mh、原文也不收。两项都是路径的通配符（见 under）。旧写法 [observe].skip_workspaces 是工作区名，照认成 */名字。
_connect = _D.get("connect", {})


def _patterns(items):
    return tuple(dict.fromkeys((_dir(p) if p.startswith("~") else p).rstrip("/") or "/" for p in items if p))


UNWATCHED = _patterns(_connect.get("unwatched", ["*/" + w for w in _D.get("observe", {}).get("skip_workspaces", ())]))   # 默认不观察（observe，2026-11-07）
UNPLUGGED = _patterns(_connect.get("unplugged", ()))     # 不接 mh（2026-11-14），见 unplugged


def under(cwd, patterns):
    """路径本身或任何一层上级对得上其中一个通配符（fnmatch：`*` 也跨 `/`，`?`、`[...]`；~ 开头的先展开），子目录因此都算；
    用户 2026-11-14 要按模式写（`*tmp`）。只看路径，目录删了也一样；没有 cwd 的不算。"""
    c = (cwd or "").rstrip("/")
    if not c or not patterns:
        return False
    p = PurePosixPath(c)
    return any(fnmatchcase(str(x), pat) for x in (p, *p.parents) if str(x) != "/" for pat in patterns)


def unplugged(cwd):
    """会话的目录在不在 [connect].unplugged 里：在的话手记、联想、拉取、开关、内核态都不出声，采集不收。"""
    return under(cwd, UNPLUGGED)


PAUSE_SOURCES = frozenset(_D.get("engram", {}).get("pause_sources", ()))   # 暂停的来源：observe 不预览不读，form 全部步骤跳过；已有 E、陈述留在库里但撤下、读不到（2026-11-11，chatgpt 导入）

VIEWS = dict(_D.get("engram", {}).get("views", {}))   # 来源 → 视角：导入的对话带着视角读，observe 先按视角筛 E（observe/select），form 的 pack 加视角要求（2026-11-11）

CHATGPT_EXPORTS = tuple(_D.get("observe", {}).get("chatgpt_exports", ()))  # 显式导出文件与工作目录；不扫描 archive

CODEX = _tools.get("codex", "codex")                     # codex CLI（额度查询用）
DSH_TEST = Path(_dir(_tools["dsh_test"])) if _tools.get("dsh_test") else None   # mh 专用的 dsh 实例（精读用；不要指到 dsh 升级的测试环境，它会被 teardown）
INVOKE = _D.get("invoke", {})                            # 远端调用：listen、token、remote（invoke/serve.py）
LISTEN = _D.get("listen", {})                            # 监听 QQ 协议端：ssh、remote、local、token_file（observe/listen.py）
SHOW = _D.get("show", {})                                # 展示自己的窗口：listen、allow、local_model、入口、机器、路径（rhythm/show）
REVIEW_URL = _D.get("review", {}).get("url", "")             # 给用户看的预览站入口：发布的页面链接不进记录（form 去掉，2026-11-04）
VECTOR = _D.get("vector", {})                            # 检索面的向量一路：url、key_file（synapse/pull/vector.py）；没配就只有字面


def resource(name, development, filename):
    """私有资源可显式配置；开发目录沿用现有来源，发行包从实例配置旁读取。"""
    configured = _D.get("resources", {}).get(name)
    if configured:
        return Path(_dir(configured))
    return development if development.exists() else PATH.parent / filename


def expression(purpose, *, author="source", addressee="source", reader="model"):
    """当次表达上下文快照；调用方保存到 act/use，原始文本仍照实记录。"""
    from ... import express
    return express.context(USER_NAME, USER_ALIASES, purpose=purpose, author=author, addressee=addressee, reader=reader)
