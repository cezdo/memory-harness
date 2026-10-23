"""实例配置（重构 H，2026-10-22）：这一个用户、这一台机器的设定，不写在代码里。

读 `~/.config/memory-harness/instance.toml`（环境变量 MH_INSTANCE 可换），不进仓库；没有这个文件时用通用默认，
写不对时在 stderr 说一声、也用默认：Synapse 挂在宿主上，宿主不能因为它报错。格式见 instance.example.toml。
"""
import os
import sys
import tomllib
from pathlib import Path

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

CODEX = _tools.get("codex", "codex")                     # codex CLI（额度查询用）
DSH_TEST = Path(_dir(_tools["dsh_test"])) if _tools.get("dsh_test") else None   # dsh 更新测试实例（精读、粗读用）


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
