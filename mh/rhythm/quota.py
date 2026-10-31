"""三家订阅的额度（2026-10-13）：Codex（ChatGPT）、Claude（Claude Code 订阅）、OpenCode Go（Go 套餐）。只读，不发模型请求。
节律（rhythm）取额度用；长实验在额度用完时自动等重置（tmp/cexp/supervise.sh，经 python3 -m mh quota）。2026-10-17 从 mh/synapse/ 搬来（重构 A12）。

- codex：codex app-server 的 JSON-RPC account/rateLimits/read；独立 CODEX_HOME，里面只有认证的符号链接。
- claude：GET api.anthropic.com/api/oauth/usage，令牌取自 ~/.claude/.credentials.json（Claude Code 自己会刷新；
  过期时这里只报过期，不代为刷新）。接口未公开，要带 User-Agent claude-code/<版本>，否则容易 429。
  2026-10-25：这个接口按出口限流（不带令牌也 429），被限时也可能不回 429、在 TLS 握手时掐断（SSL EOF）。所以读到的
  结果落盘缓存 CLAUDE_TTL 秒，期间不再请求；读不到时退回上次读到的，注明是多久以前的（"stale"）。
- opencode-go：2026-10-31 起（ark 订阅用完，用户换成 OpenCode Go）。GET opencode.ai/zen/go/v1/usage，一次给三个窗口
  （rolling 5h、weekly、monthly）的已用百分比和重置时刻；要套餐的 API Key，只读账户状态，不算一次模型请求。
  密钥先看环境变量 OPENCODE_GO_API_KEY，再看 dsh 自己的凭据（$DSH_HOME/.credentials.yaml，默认 ~/.dsh/.credentials.yaml；
  dsh 把 key 存在 refs 里）；别拿 key 打推理接口探额度。

每家统一成 {"name", "plan", "windows": [{"name": "5h" | "week" | "month", "used": 0–100, "resets": unix 秒}], "ok": 能不能用}；
读不到时 {"name", "error"}；退回旧结果时多一个 "stale"（多久以前、为什么）。
"""
import json, os, re, subprocess, threading, time, urllib.error, urllib.request
from datetime import datetime
from pathlib import Path

from ..engram.store import HOME
from ..engram.invoke import NO_UPDATE, OFFLINE_FEATURES, catalog   # 启动 app-server 也要关掉插件、apps 和在线模型目录

from ..engram.store.instance import CODEX, DSH_TEST   # codex CLI 的路径、dsh 测试实例的位置：实例配置
CODEX_HOME = HOME / "codex-quota-home"
CLAUDE_CRED = Path.home() / ".claude/.credentials.json"
CLAUDE_CACHE = HOME / "quota-claude.json"
# dsh 把 provider 的 key 存在自己的凭据文件里；DSH_HOME 指到测试实例时先看那里，最后也看家目录那份
OPENCODE_URL = "https://opencode.ai/zen/go/v1/usage"
OPENCODE_CREDS = tuple(dict.fromkeys(
    p for p in (Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh") / ".credentials.yaml",
                Path.home() / ".dsh/.credentials.yaml",
                *([DSH_TEST / "home/.credentials.yaml"] if DSH_TEST else []))))
CODEX_TIMEOUT = 60      # 读 codex 额度最多等多久（秒）：app-server 不出声也不退出时，到点杀掉，记“读不到”，不卡住节律（审查 09，2026-10-28）
CLAUDE_TTL = 600        # Claude 额度读一次管多久（秒）：接口限流，不必高频查


def _app_server_args(cat=None):
    """启动 app-server 的参数。默认开着的插件、apps 和在线模型目录会让每次启动拉几十 MB（2026-10-28 实测 20MB），
    这里只读额度、不用这些；有本地模型目录（invoke.catalog）就指过去。"""
    return [CODEX, "app-server", *OFFLINE_FEATURES, *NO_UPDATE] + (["-c", f"model_catalog_json={cat}"] if cat else [])


def codex():
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    if not (CODEX_HOME / "auth.json").exists():
        (CODEX_HOME / "auth.json").symlink_to(Path.home() / ".codex/auth.json")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": "mh-quota", "version": "0.1"}}},
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"}]
    p = subprocess.Popen(_app_server_args(catalog()), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         env=dict(os.environ, CODEX_HOME=str(CODEX_HOME)), text=True)
    late = threading.Event()
    timer = threading.Timer(CODEX_TIMEOUT, lambda: (late.set(), p.kill()))
    timer.start()
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
            raise RuntimeError(f"app-server {CODEX_TIMEOUT} 秒没返回额度" if late.is_set() else "app-server 没有返回额度")
    finally:
        timer.cancel()
        p.kill()
        p.wait()
    rl = r["rateLimits"]
    ws = [{"name": {300: "5h", 10080: "week"}.get(w["windowDurationMins"], f"{w['windowDurationMins']}m"),
           "used": w["usedPercent"], "resets": w["resetsAt"]} for w in (rl.get("primary"), rl.get("secondary")) if w]
    return {"name": "codex", "plan": rl.get("planType"), "windows": ws,
            "ok": bool(r.get("ordinaryUsageAllowed", True)) and not rl.get("rateLimitReachedType")}


def _limited(e):
    """被限流：429，或握手被掐断（SSL EOF）。"""
    if isinstance(e, urllib.error.HTTPError):
        return e.code == 429
    return "UNEXPECTED_EOF" in str(e) or "EOF occurred" in str(e)


def claude(fetch=None, now=None):
    """先看缓存：CLAUDE_TTL 内直接用；过了才请求，读不到就退回缓存并注明。fetch、now：测试替换。"""
    now = time.time() if now is None else now
    try:
        cache = json.loads(CLAUDE_CACHE.read_text())
    except (OSError, ValueError):
        cache = None
    if cache and now - cache["at"] < CLAUDE_TTL:
        return cache["q"]
    try:
        q = (fetch or _claude_fetch)()
    except Exception as e:
        why = "接口限流，过几分钟再查" if _limited(e) else str(e)[:200]
        if not cache:
            raise RuntimeError(why) from e
        return dict(cache["q"], stale=f"{(now - cache['at']) / 60:.0f} 分钟前读到的；这次读不到：{why}")
    CLAUDE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CLAUDE_CACHE.write_text(json.dumps({"at": now, "q": q}, ensure_ascii=False))
    return q


def _claude_fetch():
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


def _opencode_key():
    """OpenCode 套餐的 key：环境变量优先，其次 dsh 的凭据文件（dsh 存在 refs 下，一行一个）。"""
    key = (os.environ.get("OPENCODE_GO_API_KEY") or "").strip()
    if key:
        return key
    for path in OPENCODE_CREDS:
        try:
            text = path.read_text()
        except OSError:
            continue
        m = re.search(r"^\s*OPENCODE_GO_API_KEY:\s*['\"]?([^\s'\"]+)['\"]?\s*$", text, re.M)
        if m:
            return m.group(1)
    raise RuntimeError(f"没有 OPENCODE_GO_API_KEY（环境变量或 dsh 的凭据 {OPENCODE_CREDS[0]}），查不了 OpenCode Go 的额度")


def _opencode_usage(d):
    """接口返回的三个窗口 → 统一的额度结构。没有 percent 的窗口跳过；一个都没有算读不到。"""
    ws = []
    for key, name in (("rolling", "5h"), ("weekly", "week"), ("monthly", "month")):
        w = (d.get("usage") or {}).get(key) or {}
        if w.get("percent") is None:
            continue
        reset = w.get("resetsAt")
        try:
            resets = int(datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()) if reset else None
        except (AttributeError, ValueError):
            resets = None
        ws.append({"name": name, "used": round(float(w["percent"]), 1), "resets": resets})
    if not ws:
        raise RuntimeError(f"额度接口没给三个窗口：{str(d)[:200]}")
    return {"name": "opencode-go", "plan": "go", "windows": ws, "ok": all(w["used"] < 100 for w in ws)}


def _opencode_fetch():
    # Cloudflare 不认 urllib 默认的 User-Agent（error 1010），随便带一个自己的
    req = urllib.request.Request(OPENCODE_URL, headers={"Authorization": f"Bearer {_opencode_key()}",
                                                       "User-Agent": "mh-quota/0.1"})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OpenCode Go 额度接口返回 {e.code}：{e.read()[:300].decode('utf-8', 'replace')}") from e
    return _opencode_usage(json.loads(raw))


def opencode_go(fetch=None):
    """读一次 OpenCode Go 的额度。fetch：测试替换。"""
    return (fetch or _opencode_fetch)()


PROVIDERS = {"codex": codex, "claude": claude, "opencode-go": opencode_go}


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
        lines.append(f"{q['name']}（{q['plan']}）：{ws}" + ("" if q["ok"] else "　← 用完了")
                     + (f"（{q['stale']}）" if q.get("stale") else ""))
    return "\n".join(lines)
