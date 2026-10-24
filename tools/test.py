"""在隔离的配置和数据目录中运行合成数据测试。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = [
    "mh.express.test",
    "mh.engram.store.test", "mh.engram.invoke.test", "mh.engram.observe.test",
    "mh.engram.observe.switch.test", "mh.engram.form.test", "mh.engram.test",
    "mh.engram.cognize.test", "mh.rhythm.test", "mh.synapse.pull.test",
    "mh.synapse.track.test", "mh.synapse.push.test", "mh.synapse.push.elevate.test",
    "mh.synapse.hosts.codex.test",
]

with tempfile.TemporaryDirectory(prefix="mh-tests-") as tmp:
    env = dict(os.environ, HOME=tmp, MH_HOME=str(Path(tmp) / "data"), MH_DB=str(Path(tmp) / "data/engram.db"),
               MH_INSTANCE=str(Path(tmp) / "instance.toml"), PYTHONPATH=str(ROOT))
    raise SystemExit(subprocess.run([sys.executable, "-m", "unittest", *MODULES], cwd=ROOT, env=env).returncode)
