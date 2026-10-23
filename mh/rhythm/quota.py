"""三家订阅的额度（2026-10-13）：Codex（ChatGPT）、Claude（Claude Code 订阅）、ark（火山方舟 Agent Plan）。只读，不发模型请求。
节律（rhythm）取额度用；长实验在额度用完时自动等重置（tmp/cexp/supervise.sh，经 python3 -m mh quota）。2026-10-17 从 mh/synapse/ 搬来（重构 A12）。

- codex：codex app-server 的 JSON-RPC account/rateLimits/read；独立 CODEX_HOME，里面只有认证的符号链接。
- claude：GET api.anthropic.com/api/oauth/usage，令牌取自 ~/.claude/.credentials.json（Claude Code 自己会刷新；
  过期时这里只报过期，不代为刷新）。接口未公开，要带 User-Agent claude-code/<版本>，否则容易 429。
- ark：2026-10-13：没有 AK/SK 就不查（不大量用用不完）。接口是火山引擎 OpenAPI GetAFPUsage（Agent Plan；Coding Plan 是 GetCodingPlanUsage），要账号 AccessKey（AK/SK，
  只需 ArkReadOnlyAccess），不能用 API Key。AK/SK 放 ~/.config/memory-harness/volc.json：{"ak": "...", "sk": "..."}。
  不要用 Plan 的 API Key 直接打推理接口探额度：方舟说明 Plan 只能在 AI 编程工具里用。

每家统一成 {"name", "plan", "windows": [{"name": "5h" | "week" | "month", "used": 0–100, "resets": unix 秒}], "ok": 能不能用}；
读不到时 {"name", "error"}。
"""
import hashlib, hmac, json, os, subprocess, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ..engram.store import HOME

from ..engram.store.instance import CODEX              # codex CLI 的路径：实例配置（这台机器上绕过 ~/.local/bin/codex 的包装）
CODEX_HOME = HOME / "codex-quota-home"
CLAUDE_CRED = Path.home() / ".claude/.credentials.json"
VOLC = Path.home() / ".config/memory-harness/volc.json"


def codex():
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    if not (CODEX_HOME / "auth.json").exists():
        (CODEX_HOME / "auth.json").symlink_to(Path.home() / ".codex/auth.json")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "mh-quota", "version": "0.1"}}},
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"}]
    p = subprocess.Popen([CODEX, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         env=dict(os.environ, CODEX_HOME=str(CODEX_HOME)), text=True)
    try:
        p.stdin.write("".join(json.dumps(m) + "\n" for m in msgs))
        p.stdin.flush()
        for line in p.stdout:
            m = json.loads(line)
            if m.get("id") == 2:
                if "error" in m:
                    raise RuntimeError(m["error"])
                r = m["result"]
                break
        else:
            raise RuntimeError("app-server 没有返回额度")
    finally:
        p.kill()
    rl = r["rateLimits"]
    ws = [{"name": {300: "5h", 10080: "week"}.get(w["windowDurationMins"], f"{w['windowDurationMins']}m"),
           "used": w["usedPercent"], "resets": w["resetsAt"]} for w in (rl.get("primary"), rl.get("secondary")) if w]
    return {"name": "codex", "plan": rl.get("planType"), "windows": ws,
            "ok": bool(r.get("ordinaryUsageAllowed", True)) and not rl.get("rateLimitReachedType")}


def claude():
    cred = json.loads(CLAUDE_CRED.read_text())["claudeAiOauth"]
    if cred.get("expiresAt", 0) / 1000 < time.time():
        raise RuntimeError("Claude Code 的令牌已过期（打开一次 Claude Code 会自动刷新）")
    ver = subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.split()[0] or "2.1.0"
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": f"Bearer {cred['accessToken']}", "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": f"claude-code/{ver}", "Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=30).read())
    ws = []
    for key, name in (("five_hour", "5h"), ("seven_day", "week"), ("seven_day_opus", "week-opus"), ("seven_day_sonnet", "week-sonnet")):
        w = r.get(key)
        if w and w.get("resets_at"):
            ws.append({"name": name, "used": w["utilization"],
                       "resets": int(datetime.fromisoformat(w["resets_at"]).timestamp())})
    return {"name": "claude", "plan": cred.get("subscriptionType"), "windows": ws, "ok": all(w["used"] < 100 for w in ws)}


def _volc_call(action, ak, sk, host="open.volcengineapi.com", service="ark", region="cn-beijing", version="2024-01-01"):
    """火山引擎 OpenAPI 签名（HMAC-SHA256，POST 空 body）。"""
    query = "&".join(f"{k}={urllib.parse.quote(v, safe='-_.~')}" for k, v in
                     sorted({"Action": action, "Region": region, "Version": version}.items()))
    now = datetime.now(timezone.utc)
    xdate, short = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    bh = hashlib.sha256(b"").hexdigest()
    ct = "application/json; charset=utf-8"
    headers = f"host:{host}\nx-date:{xdate}\nx-content-sha256:{bh}\ncontent-type:{ct}\n"
    signed = "host;x-date;x-content-sha256;content-type"
    creq = f"POST\n/\n{query}\n{headers}\n{signed}\n{bh}"
    scope = f"{short}/{region}/{service}/request"
    sts = f"HMAC-SHA256\n{xdate}\n{scope}\n{hashlib.sha256(creq.encode()).hexdigest()}"
    k = sk.encode()
    for part in (short, region, service, "request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    req = urllib.request.Request(f"https://{host}/?{query}", data=b"", method="POST", headers={
        "X-Date": xdate, "X-Content-Sha256": bh, "Content-Type": ct,
        "Authorization": f"HMAC-SHA256 Credential={ak}/{scope}, SignedHeaders={signed}, Signature={sig}"})
    try:
        d = json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        d = json.loads(e.read() or b"{}")
    err = (d.get("ResponseMetadata") or {}).get("Error")
    if err:
        raise RuntimeError(f"{err.get('Code')}: {err.get('Message')}")
    return d.get("Result") or d


def ark():
    if not VOLC.exists():
        raise RuntimeError(f"没有火山引擎 AK/SK（{VOLC}），查不了 Agent Plan 额度")
    c = json.loads(VOLC.read_text())
    r = _volc_call("GetAFPUsage", c["ak"], c["sk"])
    ws = []
    for key, name in (("AFPFiveHour", "5h"), ("AFPWeekly", "week"), ("AFPMonthly", "month")):
        w = r.get(key)
        if w and float(w.get("Quota") or 0) > 0:
            ws.append({"name": name, "used": round(min(float(w.get("Used") or 0) / float(w["Quota"]) * 100, 100), 1),
                       "resets": int(w["ResetTime"]) if str(w.get("ResetTime") or "").lstrip("-").isdigit() else None})
    return {"name": "ark", "plan": r.get("PlanType"), "windows": ws, "ok": all(w["used"] < 100 for w in ws)}


PROVIDERS = {"codex": codex, "claude": claude, "ark": ark}


def read(names=None):
    out = []
    for n in names or PROVIDERS:
        try:
            out.append(PROVIDERS[n]())
        except Exception as e:
            out.append({"name": n, "error": str(e)[:300]})
    return out


def wait_until(q):
    """额度用完时返回要等到的 unix 秒（满了的窗口里最晚的那个重置）；没用完返回 0；读不到返回 None。"""
    if "error" in q:
        return None
    full = [w["resets"] for w in q["windows"] if w["used"] >= 100 and w["resets"]]
    if full:
        return max(full)
    if not q["ok"]:
        return min((w["resets"] for w in q["windows"] if w["resets"]), default=None)
    return 0


def text(qs):
    lines = []
    for q in qs:
        if "error" in q:
            lines.append(f"{q['name']}：读不到（{q['error']}）")
            continue
        ws = "；".join(f"{w['name']} 已用 {w['used']:g}%" + (f"，{datetime.fromtimestamp(w['resets']):%m-%d %H:%M} 重置" if w["resets"] else "")
                      for w in q["windows"])
        lines.append(f"{q['name']}（{q['plan']}）：{ws}" + ("" if q["ok"] else "　← 用完了"))
    return "\n".join(lines)
