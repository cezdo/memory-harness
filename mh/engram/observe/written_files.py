"""每一轮写进了、读过哪些文件（2026-10-18：写进的文件由程序附进 form 的输入；2026-10-19：读过、写过的都附进粗读的输入，
观察者只看对话文本，看不出哪些说明是在复述文件）。纯程序，observe 的 written()、粗读的输入用它。

读过（read）：Read 工具的 file_path；Bash 里看文件的命令（sed、cat、head、tail、less、grep、rg、wc、ls、jq、diff、find、cd）
带的路径样的参数，cd 进去的目录也算。变量拼出来的文件名（$f-v0.1.md）认不出，只到目录。实验：tmp/window/touched.py。

写进（written）：

来源：会话的原始记录（Claude Code 的 jsonl、dsh 的 session.v4.jsonl.zstd、codex 的 rollout jsonl），加上工作区的 git 提交。
- Edit、Write、NotebookEdit、MultiEdit（dsh：write、edit）的 file_path；
- git 提交：时间窗里全部分支的提交，按提交时间落到轮次；只留哈希在本会话工具输出里出现过的，或那一轮跑过 git commit 的
  （并行的会话同一轮里也提交时可能混进来）。再用 git show 取这次提交的文件。工作区不是 git 仓库时只认写文件。
Bash 里用 sed、heredoc、python 写文件的认不出来；没提交的这类写入会漏掉。codex：exec 里的 apply_patch 当写文件、exec_command 当 Bash（2026-10-26）。
按工具调用的时间落到轮次上（轮次 i 从它的开始到下一轮的开始）。
路径按工作区换成相对路径（worktree 的前缀 .claude/worktrees/<名>/ 也去掉），家目录写 ~，和 E 里的写法对上。
打码不在这里做：调用方把这些行和 E 拼进同一份输入后整体处理，同一个文件在两边一样。
实验：tmp/logical-session/files.py（pack-v0.4 起）。以后可以改成采集时把路径存下来，接口不变。

碰过（touched）：工具调用里出现过的全部绝对路径，不分读写、不落轮次；Synapse 认跨工作区的会话用。
"""
import bisect
import json
import re
import shlex
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
_DSH = {"write": "Write", "edit": "Edit", "bash": "Bash"}
TAIL = timedelta(hours=6)          # 最后一轮没有下一轮的开始时间，提交的时间窗往后放这么久
HOME_DIR = str(Path.home())


def rel(path, cwd):
    p = path
    pre = (cwd or "").rstrip("/") + "/"
    if pre != "/" and p.startswith(pre):
        p = p[len(pre):]
    p = re.sub(r"^\.claude/worktrees/[^/]+/", "", p)
    return re.sub("^" + re.escape(HOME_DIR) + r"(?=/)", "~", p)


def _t(x):
    """各种时间写法 → UTC 的 ISO 字符串（毫秒，Z 结尾），好按字符串比较。"""
    if isinstance(x, (int, float)):
        d = datetime.fromtimestamp(x / 1000, timezone.utc)
    else:
        d = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
        d = (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _claude(path):
    uses, results, last = {}, {}, None
    with open(path, errors="replace") as f:
        lines = f.readlines()
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        last = d.get("timestamp") or last            # 有的行（subagent、压缩后的摘要）不带时间，沿用前一条
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "tool_use" and last:
                uses[c["id"]] = (_t(last), c.get("name"), c.get("input") or {})
            elif c.get("type") == "tool_result":
                r = c.get("content")
                if isinstance(r, list):
                    r = "\n".join(x.get("text", "") for x in r if isinstance(x, dict))
                results[c.get("tool_use_id")] = r or ""
    return uses, results


def _dsh(path):
    """dsh 的 tool-call：name 是小写的 write、edit、bash，参数是 JSON 字符串；结果不细分，整份记录当作输出查哈希。"""
    from .sources import dsh
    uses, raw = {}, []
    for e in dsh._events(path):
        raw.append(json.dumps(e, ensure_ascii=False))
        if e.get("type") != "assistant/message":
            continue
        for b in ((e.get("data") or {}).get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool-call" and b.get("name") in _DSH:
                try:
                    args = json.loads(b.get("arguments") or "{}")
                except ValueError:
                    args = {}
                uses[b["id"]] = (_t(e.get("time")), _DSH[b["name"]], args)
    return uses, {"all": "\n".join(raw)}


_CALL = re.compile(r"tools\.(exec_command|apply_patch)\(")
_STR = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`(?:[^`\\]|\\.)*`')
_PATCHED = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


def _js_str(src, at):
    """src[at:] 起的第一个 JS 字符串字面量 → 内容；不是字面量（变量）返回 None。"""
    m = _STR.match(src, at)
    if not m:
        return None
    raw = m.group(0)
    if raw[0] == '"':
        try:
            return json.loads(raw)
        except ValueError:
            pass
    return raw[1:-1].encode("utf-8", "backslashescape").decode("unicode_escape", "replace") if "\\" in raw else raw[1:-1]


def _codex(path):
    """codex 的工具调用都包在 exec（一段 JS）里：tools.exec_command({cmd:"…", workdir:"…"}) 当 Bash，
    tools.apply_patch("*** Update File: …") 里的文件当 Write（相对路径按那一轮 turn_context 的 cwd）。结果按 call_id。"""
    uses, results, cwd = {}, {}, None
    with open(path, errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
            if d.get("type") == "turn_context":
                cwd = p.get("cwd") or cwd
            elif p.get("type") == "custom_tool_call_output":
                out = p.get("output")
                results[p.get("call_id")] = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
            elif p.get("type") == "custom_tool_call" and p.get("name") == "exec" and d.get("timestamp"):
                js, ts = p.get("input") or "", _t(d["timestamp"])
                for n, m in enumerate(_CALL.finditer(js)):
                    key = f"{p.get('call_id')}#{n}"
                    if m.group(1) == "apply_patch":
                        patch = _js_str(js, m.end()) or ""
                        for i, (a, b) in enumerate(_PATCHED.findall(patch)):
                            f_ = a or b
                            uses[f"{key}.{i}"] = (ts, "Write", {"file_path": f_ if f_.startswith("/") or not cwd else str(Path(cwd) / f_)})
                        continue
                    args = js[m.end():m.end() + 20000]
                    c = re.match(r"\s*\{\s*cmd\s*:\s*", args)
                    cmd = _js_str(args, c.end()) if c else None
                    if cmd is None:
                        continue
                    w = re.search(r"\bworkdir\s*:\s*", args[:len(cmd) + 400])
                    wd = _js_str(args, w.end()) if w else None
                    uses[key] = (ts, "Bash", {"command": f"cd {wd} && {cmd}" if wd else cmd})
    return uses, results


READERS = {"claude": _claude, "dsh": _dsh, "codex": _codex}
LOOK = {"sed", "cat", "head", "tail", "less", "grep", "rg", "wc", "ls", "jq", "diff", "cd", "find"}
PATHY = re.compile(r"^[~\w.\-/]*(/|\.(py|md|json|jsonl|yml|yaml|toml|sh|txt|html|js|ts|csv))[\w.\-/]*$")


def bash_reads(cmd):
    """一条 Bash 命令里看了哪些文件、目录（路径样的参数）。"""
    out = []
    for part in re.split(r"&&|\|\||;|\|", cmd or ""):
        try:
            words = shlex.split(part)
        except ValueError:
            words = part.split()
        if not words or words[0] not in LOOK:
            continue
        out += [w for w in words[1:] if not w.startswith("-") and "$" not in w and PATHY.match(w) and not re.match(r"^\d+,\d+p$", w)]
    return out


def read(source, path, cwd, starts):
    """{轮次在 starts 里的位置: [读过的路径（相对工作区，目录以 / 结尾）]}。认不出的来源、找不到的原始记录返回空。"""
    reader = READERS.get(source)
    if not reader or not starts or not path or not Path(path).exists():
        return {}
    starts = [_t(s) for s in starts]
    uses, _ = reader(path)
    out = defaultdict(list)
    for ts, name, inp in sorted(uses.values(), key=lambda x: x[0]):
        turn = bisect.bisect_right(starts, ts) - 1
        if turn < 0:
            continue
        files = [inp["file_path"]] if name == "Read" and inp.get("file_path") else bash_reads(inp.get("command")) if name == "Bash" else []
        for f in files:
            f = rel(f, cwd)
            if "." not in f.rsplit("/", 1)[-1] and not f.endswith("/"):
                f += "/"
            if len(f.strip("/.~")) > 1 and f not in out[turn]:
                out[turn].append(f)
    return dict(out)


ABS = re.compile(r"(?<![\w.$~/-])(~?/[\w.\-/~@+]+)")
REL = re.compile(r"(?<![\w.$~/:-])((?:[\w.\-]+/)+[\w.\-]+)")      # 相对路径（mh/a.py）：按命令里前面最近的 cd，没有就按会话的 cwd
CD = re.compile(r"\bcd\s+[\"']?(~?/[^\s;&|\"']*)")


def _rel(cmd, cwd):
    """一条命令里的相对路径 → 绝对路径；只跟这条命令里的 cd（绝对路径），不跨命令。"""
    cds = [(m.start(), m.group(1).replace("~", HOME_DIR, 1)) for m in CD.finditer(cmd)]
    out = []
    for m in REL.finditer(cmd):
        if m.group(1).startswith(("http", "www.")):
            continue
        base = ([d for at, d in cds if at < m.start()] or [cwd])[-1]
        if base:
            out.append(str(Path(base) / m.group(1)))
    return out


def touches(source, path, cwd=None):
    """一段会话的工具调用里出现过的绝对路径（~ 展开）→ 最后一次出现的时刻（UTC ISO），不分读写：文件参数，加 Bash 命令里
    路径样的词（重定向、heredoc、变量赋值里的也算）。给 Synapse 认“在别的工作区开、动过这个工作区”的会话、没提交的改动是谁
    碰的（2026-10-26，别的项目的会话改本仓，Bash heredoc 写文件 written() 认不出）。认不出的来源、找不到的原始记录返回空。"""
    reader = READERS.get(source)
    if not reader or not path or not Path(path).exists():
        return {}
    uses, _ = reader(path)
    out = {}
    for ts, name, inp in uses.values():
        for v in (inp.get("file_path"), inp.get("path"), inp.get("command") if name == "Bash" else None):
            if isinstance(v, str):
                got = [m.replace("~", HOME_DIR, 1) if m.startswith("~") else m for m in ABS.findall(v)]
                got += _rel(v, cwd)
                for m in got:
                    out[m] = max(out.get(m, ts), ts)
    return out


def touched(source, path, cwd=None):
    """touches() 的路径集合。"""
    return set(touches(source, path, cwd))


def commit_files(repo, h):
    try:
        out = subprocess.run(["git", "-c", "core.quotepath=off", "-C", repo, "show", "--name-only", "--format=", h],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    return [x for x in out.splitlines() if x.strip()]


def commits(repo, since, until):
    """时间窗里全部分支的提交：[(哈希, UTC 时间, 说明)]，早的在前（同一轮的几次提交按先后列，最后一条是最后提交）。
    工作区不是 git 仓库时返回空。"""
    if not repo or not Path(repo).is_dir():
        return []
    if subprocess.run(["git", "-C", repo, "rev-parse"], capture_output=True).returncode:
        return []
    out = subprocess.run(["git", "-C", repo, "log", "--all", "--reverse", f"--since={since}", f"--until={until}",
                          "--date=format-local:%Y-%m-%dT%H:%M:%S.000Z", "--format=%H%x09%ad%x09%s"],
                         capture_output=True, text=True, env={"TZ": "UTC", "PATH": "/usr/bin:/bin"}).stdout
    return [tuple(x.split("\t", 2)) for x in out.splitlines() if x.count("\t") >= 2]


def written(source, path, cwd, starts):
    """{轮次在 starts 里的位置（从 0 起）: [一行一行的说明]}。starts：各轮的开始时间（按轮次排）。
    认不出的来源、找不到的原始记录返回空。"""
    reader = READERS.get(source)
    if not reader or not starts or not path or not Path(path).exists():
        return {}
    starts = [_t(s) for s in starts]
    uses, results = reader(path)
    out = defaultdict(list)
    for ts, name, inp in sorted(uses.values(), key=lambda x: x[0]):
        turn = bisect.bisect_right(starts, ts) - 1
        if turn < 0 or name not in WRITE_TOOLS or not inp.get("file_path"):
            continue
        line = f"写了 {rel(inp['file_path'], cwd)}"
        if line not in out[turn]:
            out[turn].append(line)
    committing = {bisect.bisect_right(starts, ts) - 1 for ts, name, inp in uses.values()
                  if name == "Bash" and "git commit" in (inp.get("command") or "")}
    said = "\n".join(results.values())
    until = _t((datetime.fromisoformat(starts[-1].replace("Z", "+00:00")) + TAIL).timestamp() * 1000)
    for h, when, subject in commits(cwd, starts[0], until):
        turn = bisect.bisect_right(starts, when) - 1
        if turn >= 0 and (h[:7] in said or turn in committing):
            out[turn].append(f"提交 {h[:8]}「{subject[:80]}」：" + "、".join(rel(f, cwd) for f in commit_files(cwd, h)))
    return dict(out)
