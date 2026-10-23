"""生成配置；--apply 时备份并安装到当前 CODEX_HOME（默认 ~/.codex）。"""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from datetime import datetime

ROOT = Path(__file__).resolve().parents[4]
PREFIX = f"env PYTHONPATH={shlex.quote(str(ROOT))} /usr/bin/python3 -m mh.synapse.hosts.codex "
OURS = re.compile(r"-m mh\.synapse\.(hosts\.)?codex ")   # 本适配器的 handler（E2 之前是 mh.synapse.codex）


def configuration():
    def group(action, **extra):
        handler = {"type": "command", "command": PREFIX + action,
                   "timeout": 30 if action in ("start", "associate") else 5}
        if action in ("start", "associate"):
            handler["additionalContextLimit"] = 5000
        return dict(hooks=[handler], **extra)

    # 宿主事件的影子（event）2026-10-19 关了，不装（design/synapse/设计.md“hosts”）
    return {"hooks": {"SessionStart": [group("start")], "UserPromptSubmit": [group("associate")],
                      "PreToolUse": [group("bind", matcher="^mcp__mh__(evoke|relive|roam)$")]}}


def install(home):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "hooks.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    hooks = current.setdefault("hooks", {})
    for event, groups in configuration()["hooks"].items():
        # 只更新本适配器拥有的 handler，保留用户的其他 hooks。
        keep = []
        for group in hooks.get(event, []):
            handlers = [h for h in group.get("hooks", [])
                        if not OURS.search(h.get("command", ""))]
            if handlers:
                keep.append(dict(group, hooks=handlers))
        hooks[event] = keep + groups
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    for old in (path, home / "config.toml"):
        if old.exists():
            shutil.copy2(old, old.with_name(old.name + ".bak-synapse-" + stamp))
    subprocess.run(["codex", "mcp", "add", "mh", "--env", f"PYTHONPATH={ROOT}",
                    "--", "/usr/bin/python3", "-m", "mh.synapse.hosts.codex", "mcp"],
                   check=True, env=dict(os.environ, CODEX_HOME=str(home)))
    config = home / "config.toml"
    text = config.read_text()
    block = re.search(r"(?ms)^\[mcp_servers\.mh\]\n(.*?)(?=^\[|\Z)", text)
    if block and not re.search(r"(?m)^default_tools_approval_mode\s*=", block[1]):
        text = text[:block.start(1)] + 'default_tools_approval_mode = "approve"\n' + text[block.start(1):]
        config.write_text(text)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    print(f"已安装到 {home}；在 Codex /hooks 中审阅并信任 Synapse hooks。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        install(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    else:
        print(json.dumps(configuration(), ensure_ascii=False, indent=2))
