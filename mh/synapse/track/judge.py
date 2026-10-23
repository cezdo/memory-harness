"""Jev（TypeSafe 的判别模型）：〖〗回填用它判“转述说的是哪一条”（design/synapse/设计.md 足迹一节，2026-10-22）。
本文件两用：mh 里 import 它当客户端（只用标准库，子进程调 venv）；用 typesafe 的 venv 直接运行它就是调 API 的那一端
（标准输入一行 JSON：{"state", "instructions", "criteria"}，标准输出一行 JSON：{"value", "probabilities", "model"} 或 {"error"}）。
密钥在 ~/.config/memory-harness/typesafe.env（TYPESAFE_API_KEY、TYPESAFE_MODEL），任何输出都不带密钥。"""
import json
import os
import subprocess
import sys
from pathlib import Path

VENV = os.environ.get("MH_TYPESAFE_PYTHON", "/usr/local/mh-tools/typesafe/venv/bin/python")
ENV = Path.home() / ".config/memory-harness/typesafe.env"
TIMEOUT = 90


def _config():
    out = {}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out["TYPESAFE_API_KEY"], out.get("TYPESAFE_MODEL") or "jev-latest"


def choose(state, instructions, criteria):
    """问 Jev 一道单选题（criteria：{选项: 说明}）→ {"value", "probabilities", "model"}；出错抛 RuntimeError（不带密钥）。"""
    r = subprocess.run([VENV, str(Path(__file__).resolve())], input=json.dumps(
        {"state": state, "instructions": instructions, "criteria": criteria}, ensure_ascii=False),
        capture_output=True, text=True, timeout=TIMEOUT)
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError(f"Jev 没有给出结果（退出码 {r.returncode}）")
    if "error" in out:
        raise RuntimeError(out["error"])
    return out


def _serve():
    from typesafe_sdk import Choice, TypeSafeClient, TypeSafeError
    job = json.loads(sys.stdin.read())
    key, model = _config()
    try:
        resp = TypeSafeClient(api_key=key, timeout=60).system_one(
            job["state"], {"q": Choice(instructions=job["instructions"], criteria=job["criteria"])}, model=model).model_dump()
        a = resp["answers"]["q"]
        print(json.dumps({"value": a.get(a.get("type")), "probabilities": a.get("probabilities"), "model": resp.get("model")},
                         ensure_ascii=False))
    except TypeSafeError as e:
        print(json.dumps({"error": f"Jev API 调用失败：{str(e).replace(key, '***')[:300]}"}, ensure_ascii=False))


if __name__ == "__main__":
    _serve()
