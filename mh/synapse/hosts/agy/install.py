"""生成 agy 配置；--apply 时备份并安装到 ~/.gemini/config。"""

import argparse
import json
from datetime import datetime
from pathlib import Path
import re
import shlex
import shutil

ROOT = Path(__file__).resolve().parents[4]
PREFIX = f"env PYTHONPATH={shlex.quote(str(ROOT))} /usr/bin/python3 -m mh.synapse.hosts.agy "
OURS = re.compile(r"-m mh\.synapse\.hosts\.agy ")
GROUP = "memory-harness"
SKILLS = ("mh-zhengming", "mh-describe")
# CLI 里 mh 的工具叫 mcp_mh_<工具>，懒加载时是 call_mcp_tool；ACP 交给钩子的是裸工具名（server_name 另给，钩子看不到；2026-11-20）
BIND_MATCHER = "^mcp__?mh_.*|^call_mcp_tool$|^(evoke|relive|roam|yorimichi|shiori)$"


def configuration():
    def flat(action):
        return {"type": "command", "command": PREFIX + action, "timeout": 30}

    def grouped(action, matcher):
        return {"matcher": matcher, "hooks": [{"type": "command", "command": PREFIX + action, "timeout": 5}]}

    return {
        GROUP: {
            "SessionStart": [flat("start")],
            "PreInvocation": [flat("associate")],
            "PreToolUse": [grouped("bind", BIND_MATCHER)],
        }
    }


def mcp_server():
    return {
        "command": "/usr/bin/python3",
        "args": ["-m", "mh.synapse.hosts.agy", "mcp"],
        "env": {"PYTHONPATH": str(ROOT)},
    }


def _link_skill(src, dst):
    if dst.is_symlink() and dst.resolve() == src.resolve():
        return
    if dst.exists() or dst.is_symlink():
        raise FileExistsError(f"{dst} 已存在且不是指向 {src} 的链接，先看清再处理")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(src)


def install(home=Path.home() / ".gemini/config"):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    hooks_path = home / "hooks.json"
    mcp_path = home / "mcp_config.json"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for old in (hooks_path, mcp_path):
        if old.exists():
            shutil.copy2(old, old.with_name(old.name + ".bak-synapse-" + stamp))

    current = json.loads(hooks_path.read_text().strip() or "{}") if hooks_path.exists() else {}
    group = current.setdefault(GROUP, {})
    for event, entries in configuration()[GROUP].items():
        if event in ("PreToolUse", "PostToolUse"):
            keep = []
            for g in group.get(event, []):
                hs = [h for h in g.get("hooks", []) if not OURS.search(h.get("command", ""))]
                if hs:
                    keep.append(dict(g, hooks=hs))
            group[event] = keep + entries
        else:
            keep = [h for h in group.get(event, []) if not OURS.search(h.get("command", ""))]
            group[event] = keep + entries
    hooks_path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n")

    mcp = json.loads(mcp_path.read_text().strip() or "{}") if mcp_path.exists() else {}
    mcp.setdefault("mcpServers", {})["mh"] = mcp_server()
    mcp_path.write_text(json.dumps(mcp, ensure_ascii=False, indent=2) + "\n")

    for name in SKILLS:
        _link_skill(ROOT / "mh/skills" / name, home / "skills" / name)
    print(f"已安装到 {home}。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        install()
    else:
        print(json.dumps(configuration(), ensure_ascii=False, indent=2))
