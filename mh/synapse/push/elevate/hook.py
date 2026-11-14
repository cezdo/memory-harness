"""旧路径的垫片（2026-11-13 elevate 搬到 mh/engram/elevate/）：宿主配置里写着这个路径，正开着的会话要靠它不断。
宿主配置改完、装了旧路径的会话都结束以后删（runbook I7 一揽子迁移）。"""
import runpy
import sys
from pathlib import Path

NEW = Path(__file__).resolve().parents[3] / "engram/elevate/hook.py"
sys.argv[0] = str(NEW)
runpy.run_path(str(NEW), run_name="__main__")
