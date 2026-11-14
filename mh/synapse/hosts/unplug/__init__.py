"""拔掉 mh 起宿主：测试用的宿主实例，不接 mh（用户 2026-11-14，design/synapse/设计.md“宿主接入”的“不接 mh 的实例”）。

`python3 -m mh unplug <claude|codex|dsh> [宿主参数…]`：在数据目录下的独立 home（<宿主>-unplugged-home）里起宿主。
设置每次从日常配置照抄一份，去掉 mh 的东西：钩子（命令里带仓库路径的）、MCP（名为 mh 的）、mh 装进去的 skill 和命令、
dsh profile 里 mh 的插件；认证用链接，不复制密钥。会话落在这个 home 里，采集不读这里，所以一轮都不进库。
"""
import json
import os
import re
import shutil
import sys
from pathlib import Path

from ....engram import store

REPO = Path(__file__).resolve().parents[4]
USER = Path.home()
HOSTS = ("claude", "codex", "dsh")
SKILLS = frozenset(p.name for p in (REPO / "mh/skills").iterdir() if p.is_dir())   # mh 装进宿主的 skill 和命令


def home(host, base=None):
    return Path(base or store.HOME / f"{host}-unplugged-home")


def mh_command(cmd):
    """这条钩子命令是不是 mh 的：命令里带仓库路径（三个宿主的装法都是绝对路径或 PYTHONPATH）。"""
    return str(REPO) in (cmd or "")


def _link(dst, src):
    """dst 指向 src；已经是链接就换，src 不在就不建。"""
    if dst.is_symlink():
        dst.unlink()
    elif dst.exists():
        return
    if src.exists() or src.is_symlink():
        dst.symlink_to(src)


def _link_dir(dst, src, skip=SKILLS):
    """目录逐项链接（skill、命令、插件）：跳过 mh 的那几项；dst 里已有的 mh 项、失效链接清掉。"""
    dst.mkdir(parents=True, exist_ok=True)
    for p in dst.iterdir():
        if p.is_symlink() and (p.name in skip or p.stem in skip or not p.exists()):
            p.unlink()
    for p in sorted(src.iterdir()) if src.is_dir() else ():
        if p.name in skip or p.stem in skip:
            continue
        _link(dst / p.name, p)


def strip_hooks(hooks):
    """钩子表去掉 mh 的命令，空了的组和事件一起去掉。返回 (新表, {(事件, 旧组, 旧条): (新组, 新条)})。"""
    out, moved = {}, {}
    for event, groups in (hooks or {}).items():
        kept = []
        for i, g in enumerate(groups):
            hs = [(j, h) for j, h in enumerate(g.get("hooks", [])) if not mh_command(h.get("command"))]
            if not hs:
                continue
            for n, (j, _) in enumerate(hs):
                moved[(event, i, j)] = (len(kept), n)
            kept.append(dict(g, hooks=[h for _, h in hs]))
        if kept:
            out[event] = kept
    return out, moved


# ---- Claude Code：CLAUDE_CONFIG_DIR

def claude_home(base=None, src=USER / ".claude", state=USER / ".claude.json"):
    """配置目录：settings.json 照抄去掉 mh 的钩子；.claude.json 第一次建时照抄登录和界面状态（不带 mcpServers、projects），
    以后保留宿主自己写的，只再去一次 mh 的 MCP；凭据、按键、插件、非 mh 的 skill 用链接。"""
    h = home("claude", base)
    h.mkdir(parents=True, exist_ok=True)
    _link(h / ".credentials.json", src / ".credentials.json")
    _link(h / "keybindings.json", src / "keybindings.json")
    _link(h / "plugins", src / "plugins")
    _link_dir(h / "skills", src / "skills")
    _link_dir(h / "commands", src / "commands")
    settings = json.loads((src / "settings.json").read_text()) if (src / "settings.json").exists() else {}
    settings["hooks"], _ = strip_hooks(settings.get("hooks"))
    (h / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=1))
    mine = h / ".claude.json"
    if mine.exists():
        d = json.loads(mine.read_text() or "{}")
    else:
        d = {k: v for k, v in (json.loads(state.read_text()) if state.exists() else {}).items() if k not in ("mcpServers", "projects")}
    d.get("mcpServers", {}).pop("mh", None)
    for p in d.get("projects", {}).values():
        p.get("mcpServers", {}).pop("mh", None)
    mine.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    return h


# ---- Codex：CODEX_HOME

_SECTION = re.compile(r"^\[(.+)\]\s*(#.*)?$")
_STATE = re.compile(r'^hooks\.state\."(.+):([a-z_]+):(\d+):(\d+)"$')


def _snake(event):
    return re.sub(r"(?<!^)([A-Z])", r"_\1", event).lower()


def codex_config(text, moved, old_hooks, new_hooks):
    """config.toml 按节过滤：去掉 [mcp_servers.mh…]；[hooks.state."<旧 hooks.json>:事件:组:条"] 换成新位置（信任照旧），
    mh 的那几条去掉。其余原样。"""
    events = {_snake(e): e for e in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PermissionRequest",
                                     "Stop", "SessionEnd", "Interrupt", "PostCompact", "SubagentStart", "SubagentStop")}
    out, keep = [], True
    for line in text.splitlines():
        m = _SECTION.match(line.strip())
        if m:
            name = m.group(1).strip()
            keep = not (name == "mcp_servers.mh" or name.startswith("mcp_servers.mh."))
            s = _STATE.match(name)
            if s:
                event = events.get(s.group(2))
                key = (event, int(s.group(3)), int(s.group(4)))
                keep = key in moved
                if keep:
                    g, n = moved[key]
                    line = f'[hooks.state."{new_hooks}:{s.group(2)}:{g}:{n}"]'
        if keep:
            out.append(line)
    return "\n".join(out) + "\n"


def codex_home(base=None, src=USER / ".codex"):
    """CODEX_HOME：config.toml、hooks.json 照抄去掉 mh；认证、全局说明、非 mh 的 skill 用链接。"""
    h = home("codex", base)
    h.mkdir(parents=True, exist_ok=True)
    _link(h / "auth.json", src / "auth.json")
    _link(h / "AGENTS.md", src / "AGENTS.md")
    _link_dir(h / "skills", src / "skills")
    hooks = json.loads((src / "hooks.json").read_text()) if (src / "hooks.json").exists() else {}
    stripped, moved = strip_hooks(hooks.get("hooks"))
    (h / "hooks.json").write_text(json.dumps(dict(hooks, hooks=stripped), ensure_ascii=False, indent=2))
    cfg = (src / "config.toml").read_text() if (src / "config.toml").exists() else ""
    (h / "config.toml").write_text(codex_config(cfg, moved, src / "hooks.json", h / "hooks.json"))
    return h


# ---- dsh：DSH_HOME

def dsh_profile(pkg):
    """profile 的 package.json 去掉 mh 的插件：依赖里链到仓库的，和 bundles 里同名的。"""
    deps = pkg.get("dependencies", {})
    mine = {k for k, v in deps.items() if mh_command(v)}
    pkg = dict(pkg, dependencies={k: v for k, v in deps.items() if k not in mine})
    prof = pkg.get("dsh", {}).get("profile")
    if prof:
        pkg["dsh"] = dict(pkg["dsh"], profile=dict(prof, bundles=[b for b in prof.get("bundles", []) if b not in mine]))
    return pkg


def _copy(dst, src):
    """profile 里的东西拷贝，不链接：dsh 的插件管理照新的 package.json 修剪依赖时，不能动到日常那份（会删掉 mh-synapse）。
    文件每次照抄；目录（node_modules，几 MB）只在没有时拷一次，里面的链接原样保留。"""
    if src.is_dir() and not src.is_symlink():
        if not dst.exists():
            shutil.copytree(src, dst, symlinks=True)
    else:
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        shutil.copy2(src, dst, follow_symlinks=False)


def dsh_home(base=None, src=USER / ".dsh"):
    """DSH_HOME：每个 profile 拷一份，package.json 去掉 mh 插件（node_modules 里 mh 的包还在，不在 bundles 里就不加载），
    插件管理的日志和备份不拷；凭据、.env、全局说明、预设、非 mh 的 skill 用链接。"""
    h = home("dsh", base)
    h.mkdir(parents=True, exist_ok=True)
    for name in (".credentials.yaml", ".env", "AGENTS.md", ".agent-presets"):
        _link(h / name, src / name)
    _link_dir(h / "skills", src / "skills")
    profiles = src / "profiles"
    for prof in sorted(profiles.iterdir()) if profiles.is_dir() else ():
        dst = h / "profiles" / prof.name
        if prof.name == "node_modules":
            _copy(dst, prof)
            continue
        if not prof.is_dir():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for p in prof.iterdir():
            if p.name in ("package.json", ".plugin-manager") or ".bak" in p.name:
                continue
            _copy(dst / p.name, p)
        if (prof / "package.json").exists():
            (dst / "package.json").write_text(json.dumps(dsh_profile(json.loads((prof / "package.json").read_text())),
                                                         ensure_ascii=False, indent=2))
    return h


BUILD = {"claude": (claude_home, "CLAUDE_CONFIG_DIR"), "codex": (codex_home, "CODEX_HOME"), "dsh": (dsh_home, "DSH_HOME")}


def main(argv):
    if not argv or argv[0] not in HOSTS:
        print("用法：python3 -m mh unplug <claude|codex|dsh> [宿主参数…]\n"
              "在不接 mh 的独立 home 里起宿主：没有手记、联想、mh 的 MCP 和 skill，会话不进记忆。\n"
              "--home 只打印 home 的路径（照日常配置重写一遍），不起宿主。", file=sys.stderr)
        sys.exit(2)
    host, rest = argv[0], argv[1:]
    build, var = BUILD[host]
    h = build()
    if rest[:1] == ["--home"]:
        print(h)
        return
    env = dict(os.environ, **{var: str(h)})
    for key in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):     # 外层会话的身份不传进去
        env.pop(key, None)
    exe = shutil.which(host)
    if not exe:
        sys.exit(f"{host} 没装")
    os.execvpe(exe, [host, *rest], env)
