"""回复（G7，design/reply/设计.md）：在一个群里被叫到时、或者白名单里的人私聊时，“我”用 Terra 回几句；群里有人复读，
程序跟一句。常驻，memory-harness-reply 服务跑它（python3 -m mh reply），不开库。

和监听（engram/observe/listen.py）各连一条 WS：监听只记，这里只管回；“我”发出去的话由协议端回传
（message_sent），监听照样记下，这里不另写聊天记录。每次回复的经过（提示词、模型原话、看了哪几条、token、耗时、
重写、截断）追加进数据目录的 reply-log.jsonl，挑拿不准的回复给用户看时用，也给“看过什么”留底。

模型看到的（用户 10-31）：系统提示是人设（工作区 AGENTS.md）+ 我怎么在这里说话（prompts/reply-v0.3.md），
换掉 codex 自带的写代码守则，关掉命令行等用不上的部分（LEAN）；用户消息是这一段聊天的完整记录，只往后追加
（缓存按前缀命中），会变的（现在几点、我上次说到哪、为什么要回）放在最后。程序只做简化和打码：连发、复读合成一行
标几条，图片写 [图片]、表情包写描述、链接只留网站名；“我”回话前看清过的，标在那句后面。撤回了的
（`group_recall`/`friend_recall`）从记录里拿掉、编号顺延，像没说过：不算叫到、不接话（用户 11-01）。

一次回复：被叫到（@ 我、回复我的消息、叫我的名字；私聊对方说了就算）→ 合批（安静 MERGE_QUIET 秒或最多等
MERGE_MAX 秒）→ 模型 → 可以申请看清几条（details：全名、号、时刻、全文、回复的哪条、里面的图），看一次再回
→ 去掉漏出来的标记 → 限长（超了重写一次，还超就截）→ 按换行拆成几条，隔 SEND_GAP 秒发出。
额度（用户 10-31）：用户叫我、私聊不计；只有 bot 叫我的，每小时最多 bot_hourly 次；其余（朋友叫我、跟复读、
主动接话发出去的）和 bot 那份合起来每小时最多 hourly_max 次；到了只记日志。
说完一句后的 active_minutes 分钟里，群里真人接着说话就算叫到（用户 11-01：活跃窗口）。
主动接话：群里“我”上次说话后又有 proactive_after 条别人的话、离上次主动至少 proactive_gap_minutes 分钟，就问模型一次
有没有想接的（可以不接）；按问的次数算 proactive_hourly 那份额度，省模型调用。
"""
import json
import re
import sys
import tempfile
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..engram.invoke import Codex
from ..engram.observe import listen
from ..engram.store import HOME
from ..engram.store.instance import _D, _dir

CFG = _D.get("reply", {})
LOG = HOME / "reply-log.jsonl"
SYSTEM = HOME / "reply-system.md"            # 拼好的系统提示（人设 + 规则），内容不变就不重写，codex 按路径读
RULES = Path(__file__).parent / "prompts" / "reply-v0.3.md"   # 11-01 v0.3：长度和语气有起伏
MERGE_QUIET, MERGE_MAX = 3, 8
SEND_GAP = 1.2
MAX_PARTS = 3
HISTORY = 400                    # 内存里每个会话留多少条（只用这一段的）
IMAGES, IMAGE_MAX = 3, 10 << 20  # 看清时最多附几张图、一张最大多少字节
SKIP = "（不回）"
LOOK = re.compile(r"^（看\s*([\d\s,，、]+)）$")
MEMO = re.compile(r"^\s*记下[：:]\s*(.+?)\s*$", re.M)          # 模型留给之后的自己的一行，不发出去
LEAK = re.compile(r"<\\?/?\|[a-z_]{1,20}\|>")         # 模型漏出来的结束标记（10-31 私聊：“咋啦<\/|final|>”）
ENDS = "。！？!?…～~"
ECHOABLE = {"text", "face", "image", "mface"}        # 复读时能原样跟的消息段
# codex 里用不上的部分全关（10-31 实测 13.7K → 6.9K token）：权限说明、协作模式、环境、技能列表、命令行（关了就读不了本机文件）、
# 画图、目标；系统提示另换成 SYSTEM。剩下多代理那段关不掉（约 700 token）
LEAN = ("include_permissions_instructions=false", "include_collaboration_mode_instructions=false",
        "include_environment_context=false", "skills.include_instructions=false",
        "features.shell_tool=false", "features.image_generation=false", "features.goals=false")
text_of, short = listen.text_of, listen.short     # 消息段 → 一行文字、名字截短：和采集共用一份（listen.py，2026-11-01）


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---- 纯函数：消息转文字、短名、切段、叫没叫到、记录、看清、复读、清理、限长、拆条 ----

def key_of(message):
    """判断两条是不是同一句（连发、复读）：文字按原文，图片、表情按文件，@ 和回复也算进去。"""
    parts = []
    for seg in message or ():
        d = seg.get("data") or {}
        parts.append((seg.get("type"), d.get("text") or d.get("file") or d.get("emoji_id") or d.get("id") or d.get("qq")))
    return json.dumps(parts, ensure_ascii=False)


def segment_start(times, now, day_hour=7, gap=1800):
    """这一段从第几条开始：本地时间每天 day_hour 点切一刀，隔 gap 秒以上也切（用户 10-31：7 点、30 分钟）。
    times 是升序的 unix 秒；now 也算一条（正要回的这一刻）。"""
    local = datetime.fromtimestamp(now).astimezone()
    cut = local.replace(hour=day_hour, minute=0, second=0, microsecond=0)
    if local < cut:
        cut -= timedelta(days=1)
    start, prev = len(times), now
    for i in range(len(times) - 1, -1, -1):
        if times[i] < cut.timestamp() or prev - times[i] > gap:
            break
        start, prev = i, times[i]
    return start


def called(event, self_id, names, mine):
    """这条消息叫没叫到我：@ 我、回复我发的消息、或者文字里有我的名字（不分大小写）。"""
    for seg in event.get("message") or ():
        d = seg.get("data") or {}
        if seg.get("type") == "at" and str(d.get("qq")) == str(self_id):
            return True
        if seg.get("type") == "reply" and str(d.get("id")) in mine:
            return True
    text = text_of(event.get("message")).lower()
    return any(n.lower() in text for n in names)


def view(seg, self_id, who):
    """这一段的完整记录，每条“编号 短名：文字”，“我”的写成我、后面带着回话前看清过什么；挨着的同一句合成一行：
    一个人连发标“连发 ×N”，几个人复读标“复读 ×N”。只往后追加：一条第一次写成什么样，以后还是什么样。"""
    names = {u: who(u) for u in {m["user_id"] for m in seg}}
    names[str(self_id)] = "我"
    lines, i = [], 0
    while i < len(seg):
        j = i
        while j + 1 < len(seg) and key_of(seg[j + 1]["message"]) == key_of(seg[i]["message"]):
            j += 1
        m = seg[i]
        text = text_of(m["message"], names)
        if j == i:
            lines.append(f"{i + 1} {names[m['user_id']]}：{text}{m.get('note', '')}")
        else:
            who_said = list(dict.fromkeys(names[x["user_id"]] for x in seg[i:j + 1]))
            tag = f"连发 ×{j - i + 1}" if len(who_said) == 1 else f"复读 ×{j - i + 1}"
            lines.append(f"{i + 1}-{j + 1} {'、'.join(who_said)}：{text}〔{tag}〕")
        i = j + 1
    return "\n".join(lines)


def echo_due(seg, self_id, after=2):
    """要不要跟着复读：最后挨着的几条是同一句、至少 after 个不同的人说了、“我”还没跟、而且是能原样发的消息段。
    该跟就给那条消息，否则 None。"""
    if after <= 0 or not seg:
        return None
    last = seg[-1]
    k = key_of(last["message"])
    run = []
    for m in reversed(seg):
        if key_of(m["message"]) != k:
            break
        run.append(m)
    speakers = {m["user_id"] for m in run}
    if str(self_id) in speakers or len(speakers) < after:
        return None
    if not last["message"] or any(s.get("type") not in ECHOABLE for s in last["message"]):
        return None
    return last


def echo_segments(message):
    """复读时发出去的消息段：文字、表情原样，图片用地址重发。"""
    out = []
    for s in message:
        d = dict(s.get("data") or {})
        if s.get("type") == "image":
            d = {"file": d.get("url") or d.get("file"), "summary": d.get("summary", "")}
        out.append({"type": s["type"], "data": d})
    return out


def image_urls(seg, picks):
    """看清的那几条里的图（地址），按出现先后，最多 IMAGES 张。"""
    urls = []
    for n in picks:
        if 1 <= n <= len(seg):
            urls += [(s.get("data") or {}).get("url") for s in seg[n - 1]["message"] if s.get("type") in ("image", "mface")]
    return [u for u in urls if u][:IMAGES]


def fetch(urls, folder, opener=urllib.request.urlopen):
    """把图下到 folder → 下好的文件路径（下不来的、太大的跳过，记日志）。"""
    paths = []
    for i, u in enumerate(urls):
        try:
            with opener(u, timeout=10) as r:
                data = r.read(IMAGE_MAX + 1)
            if len(data) > IMAGE_MAX:
                log(f"图太大，跳过：{u[:80]}")
                continue
            ext = {b"\x89PNG": ".png", b"GIF8": ".gif", b"RIFF": ".webp"}.get(data[:4], ".jpg")
            p = Path(folder) / f"{i + 1}{ext}"
            p.write_bytes(data)
            paths.append(str(p))
        except Exception as e:                      # 地址过期、网络断了：照旧只写 [图片]
            log(f"图下不来：{type(e).__name__} {u[:80]}")
    return paths


def details(seg, picks, self_id, who, by_id):
    """申请看清的那几条：全名（群名片、昵称）、号、时刻、全文、回复的是哪条。"""
    names = {m["user_id"]: who(m["user_id"]) for m in seg}
    names[str(self_id)] = "我"
    out = []
    for n in picks:
        if not 1 <= n <= len(seg):
            out.append(f"{n}：没有这一条")
            continue
        m = seg[n - 1]
        at = datetime.fromtimestamp(m["time"]).astimezone().strftime("%m-%d %H:%M:%S")
        full = "我" if m["user_id"] == str(self_id) else \
            "，".join(x for x in (f"群名片 {m['card']}" if m["card"] else "", f"昵称 {m['nick']}", f"QQ {m['user_id']}") if x)
        out.append(f"{n}：{names[m['user_id']]}（{full}），{at}\n   全文：{text_of(m['message'], names)}")
        for s in m["message"]:
            if s.get("type") == "reply":
                q = by_id.get(str((s.get("data") or {}).get("id")))
                out.append(f"   回复的是：{names.get(q['user_id'], who(q['user_id'])) if q else '?'}："
                           f"{text_of(q['message'], names) if q else '（这一段里找不到）'}")
    return "\n".join(out)


def look_note(picks, images):
    return f"〔回这句前看了 {'、'.join(map(str, picks))} 的详情" + (f"和 {images} 张图" if images else "") + "〕"


def picks_of(text):
    """模型的输出是不是申请看清：（看 3 5）→ [3, 5]；不是给 None。"""
    m = LOOK.match(text.strip())
    return [int(x) for x in re.findall(r"\d+", m.group(1))] if m else None


def clean(text):
    """去掉模型漏出来的标记。"""
    return LEAK.sub("", text).strip()


def memos_of(text):
    """把“记下：……”那几行拿出来 → (要发的话, [记下的])。"""
    return MEMO.sub("", text).strip(), [m.strip() for m in MEMO.findall(text) if m.strip()]


def visible(text):
    return len(re.sub(r"\s", "", text))


def clip(text, limit):
    """截到 limit 字以内（不算空白）：优先停在最后一个句末或换行，没有就硬截。"""
    out, n, last = [], 0, 0
    for c in text:
        if not c.isspace():
            if n >= limit:
                break
            n += 1
        out.append(c)
        if c in ENDS or c == "\n":
            last = len(out)
    s = "".join(out)
    return (s[:last] if last else s).strip()


def split(text, max_parts=MAX_PARTS):
    """按模型写的换行拆成几条；多了就把最短的相邻两条并起来（参考 dsh-onebot-qq 的 splitBubbles）。"""
    parts = [x.strip() for x in text.splitlines() if x.strip()]
    while len(parts) > max_parts:
        i = min(range(len(parts) - 1), key=lambda k: len(parts[k]) + len(parts[k + 1]))
        parts[i:i + 2] = [parts[i] + " " + parts[i + 1]]
    return parts


def system(persona, hint, limit, rules=RULES):
    """系统提示：人设 + 我怎么在这里说话。"""
    return persona.strip() + "\n\n" + rules.read_text().format(hint=hint, limit=limit, parts=MAX_PARTS).strip() + "\n"


def prompt(seen, scene, cue, last_mine=None, memos=()):
    """用户消息：完整记录在前（只往后追加，前缀不变），“我记下的”接在后面，会变的放最后。"""
    now = datetime.now().astimezone().strftime("%m-%d %H:%M")
    where = f"你上次说话是第 {last_mine} 条。" if last_mine else "这一段里你还没说过话。"
    noted = "".join(f"- （第 {m['after']} 条时）{m['text']}\n" for m in memos)
    noted = f"\n我记下的：\n{noted}" if noted else ""
    return f"{scene}这一段的聊天：\n\n{seen}\n{noted}\n———— 现在 {now}。{where}{cue}\n"


# ---- 常驻 ----

class Reply:
    def __init__(self, cfg=CFG, call=None, log_path=LOG, system_path=SYSTEM):
        self.group = int(cfg["group"])
        self.private = {str(x) for x in cfg.get("private", ())}     # 只回这些号的私聊（用户 10-31：只开用户的号）
        self.names = list(cfg.get("names", ()))
        self.ignore = {str(x) for x in cfg.get("ignore", ())}
        self.people = {str(k): v for k, v in cfg.get("people", {}).items()}
        self.owner = {str(x) for x in cfg.get("owner", ())}       # 用户的号：叫我、私聊都不计额度
        self.bots = {str(x) for x in cfg.get("bots", ())}         # 群里的 bot：只有它叫我的，算 bot 那份
        self.limits = {"total": int(cfg.get("hourly_max", 30)), "bot": int(cfg.get("bot_hourly", 10)),
                       "proactive": int(cfg.get("proactive_hourly", 4))}
        self.pro_gap = int(cfg.get("proactive_gap_minutes", 10)) * 60
        self.pro_after = int(cfg.get("proactive_after", 8))
        self.active = int(cfg.get("active_minutes", 5)) * 60   # 说完一句后的活跃窗口（用户 11-01：5 分钟）
        self.last_pro = 0.0
        self.said = {}               # 会话 → 最后一次发出话的时刻（活跃窗口从这里算）
        self.echo_after = int(cfg.get("echo_after", 2))             # 几个不同的人复读了就跟（0 不跟）
        self.hint, self.limit = int(cfg.get("hint_chars", 60)), int(cfg.get("max_chars", 100))
        self.day_hour, self.gap = int(cfg.get("day_hour", 7)), int(cfg.get("gap_minutes", 30)) * 60
        self.persona_path = Path(_dir(cfg.get("persona", "~/chat/AGENTS.md")))
        self.log_path, self.system_path = log_path, system_path
        model, effort = cfg.get("model", "gpt-6.1-terra"), cfg.get("effort", "medium")
        self.call = call or Codex(model, effort, isolation=f"codex-chat-v1-{effort}", no_tools=True,
                                  allow={"web_search"}, config=LEAN, instructions=system_path)
        self.self_id = None
        self.group_name = str(self.group)
        self.history = {}            # 会话 → 最近的消息；会话是 ("group", 群号) 或 ("private", 对方的号)
        self.by_id, self.mine, self.member = {}, set(), {}
        self.recalled = set()        # 撤回过的消息号：先到撤回、后到消息时也不算（群和私聊都有）
        self.notes = {}              # “我”要发的那句 → 回话前看清过什么（等协议端回传那条时标上）
        self.echoed = {}             # 会话 → 最近跟过的那句（同一串复读只跟一次）
        self.memos = {}              # 会话 → “我”记下的 [{time, after: 当时记录到第几条, text}]
        self.sent = {k: deque() for k in self.limits}   # 每份额度这一小时用掉的时刻，各会话共用
        self.ws = None
        self.cv = threading.Condition()
        self.pending = {}            # 会话 → {"at": 被叫到的时刻, "who": 谁叫的（主动接话是 {"proactive"}）}
        self.last = {}               # 会话 → 最近一条消息的时刻

    def who(self, uid):
        return self.people.get(str(uid)) or short(self.member.get(str(uid), str(uid)))

    def chat_of(self, e):
        """这条消息属于哪个要照看的会话；不归我们管给 None。私聊里“我”发的，对方在 target_id。"""
        if e.get("post_type") not in ("message", "message_sent"):
            return None
        if e.get("group_id") == self.group:
            return ("group", str(self.group))
        if e.get("message_type") == "private":
            mine = e.get("post_type") == "message_sent" or str(e.get("user_id")) == str(self.self_id)
            peer = str(e.get("target_id") if mine else e.get("user_id"))
            return ("private", peer) if peer in self.private else None
        return None

    def scene(self, chat):
        return f"群「{self.group_name}」" if chat[0] == "group" else f"和{self.who(chat[1])}私聊"

    def load(self, path=listen.LOG):
        """起来时补上最近的消息，接得上这一段；回话前看清过什么从 reply-log 里补。"""
        if self.log_path.exists():
            with open(self.log_path, encoding="utf-8") as f:
                for line in deque(f, maxlen=500):
                    r = json.loads(line)
                    if r.get("look") and r.get("sent"):
                        self.notes[r["sent"][0]] = look_note(r["look"], r.get("images"))
                    for text in r.get("memo") or ():
                        at = datetime.fromisoformat(r["at"]).timestamp()
                        self.memos.setdefault(tuple(r["chat"]), []).append({"time": at, "after": r.get("after"), "text": text})
        if not path.exists():
            return
        with open(path, encoding="utf-8") as f:
            for line in deque(f, maxlen=3000):
                e = json.loads(line).get("event") or {}
                self.note(e, live=False)

    def note(self, e, live=True):
        """记下一条消息；live 时判断要不要回：群里叫到我才回，私聊对方说了就回；群里有人复读就跟。
        撤回的不当消息：从记录里拿掉，像没说过。"""
        if e.get("post_type") == "notice":
            if e.get("notice_type") in ("group_recall", "friend_recall"):
                self.recall(e)
            return
        chat = self.chat_of(e)
        if not chat:
            return
        mid = str(e.get("message_id"))
        if mid in self.by_id or mid in self.recalled:
            return
        sender = e.get("sender") or {}
        uid = str(e.get("user_id"))
        mine = e.get("post_type") == "message_sent" or uid == str(self.self_id)
        self.member[uid] = sender.get("card") or sender.get("nickname") or uid
        m = {"id": mid, "chat": list(chat), "time": e.get("time") or time.time(), "user_id": uid,
             "card": sender.get("card", ""),
             "nick": sender.get("nickname", ""), "message": e.get("message") or []}
        if mine:
            self.mine.add(mid)
            if (n := self.notes.pop(text_of(m["message"]), None)):
                m["note"] = n
        self.history.setdefault(chat, deque(maxlen=HISTORY)).append(m)
        self.by_id[mid] = m
        if not live:
            return
        if not mine and chat[0] == "group":
            self.echo(chat)
        with self.cv:
            self.last[chat] = time.time()
            if mine or uid in self.ignore:
                pass
            elif chat[0] == "private" or called(e, self.self_id, self.names, self.mine):
                log(f"{'私聊' if chat[0] == 'private' else '被叫到'}：{self.who(uid)}（{uid}）{text_of(m['message'])[:40]}")
                p = self.pending.setdefault(chat, {"at": time.time(), "who": set()})
                p["who"].discard("proactive")
                p["who"].add(uid)
            elif chat[0] == "group" and chat not in self.pending and self.live(chat, uid):
                log(f"接着聊：{self.who(uid)}（{uid}）{text_of(m['message'])[:40]}")
                p = self.pending.setdefault(chat, {"at": time.time(), "who": set()})
                p["who"].add(uid)
                p["active"] = True
            elif chat[0] == "group" and chat not in self.pending:
                self.proactive(chat)
            self.cv.notify()

    def recall(self, e):
        """撤回了哪条：从记录里拿掉（编号顺延），像没说过；正在等的这次如果没有别的新消息就不回了。"""
        mid = str(e.get("message_id"))
        self.recalled.add(mid)
        m = self.by_id.pop(mid, None)
        if not m:
            return
        self.mine.discard(mid)
        chat = tuple(m.get("chat") or ())
        with self.cv:
            h = self.history.get(chat)
            if h:
                try:
                    h.remove(m)
                except ValueError:
                    pass
            if self.pending.get(chat):
                seg = self.segment(chat)
                last = max((i for i, x in enumerate(seg) if x["user_id"] == str(self.self_id)), default=-1)
                if not any(x["user_id"] != str(self.self_id) for x in seg[last + 1:]):
                    self.pending.pop(chat, None)
        log(f"撤回：{self.who(m['user_id'])} 的一句话不看了")

    def live(self, chat, uid):
        """刚说完一句后的活跃窗口：真人接着说话也算叫到（用户 11-01：说完的 5 分钟内保持活跃）。"""
        return (self.active > 0 and uid not in self.bots
                and time.time() - self.said.get(chat, 0) < self.active)

    def proactive(self, chat):
        """主动接话的时机（程序挑，模型定接不接）：“我”上次说话后又有 pro_after 条别人（不算 bot）的话，
        离上次主动够久，额度还有。"""
        now = time.time()
        if self.limits["proactive"] <= 0 or now - self.last_pro < self.pro_gap:
            return
        seg = self.segment(chat)
        last = max((i for i, m in enumerate(seg) if m["user_id"] == str(self.self_id)), default=-1)
        if sum(m["user_id"] not in self.bots for m in seg[last + 1:]) < self.pro_after or not self.allow("proactive"):
            return
        self.last_pro = now
        self.pending[chat] = {"at": now, "who": {"proactive"}}
        log("主动看一眼")

    def echo(self, chat):
        """有人复读就跟一句（程序做，不调模型；用户 10-31：复读也可以参与）。"""
        m = echo_due(self.segment(chat), self.self_id, self.echo_after)
        if not m or self.echoed.get(chat) == key_of(m["message"]) or not self.ws:
            return
        self.echoed[chat] = key_of(m["message"])
        if not self.allow("total"):
            log("该跟复读，但额度用完了")
            return
        self.spend("total")
        text = text_of(m["message"])
        log(f"跟复读（{self.scene(chat)}）：{text}")
        self.record({"chat": list(chat), "echo": True, "sent": [text]})
        try:
            self.ws.send({"action": "send_group_msg", "echo": f"echo-{time.time():.3f}",
                          "params": {"group_id": int(chat[1]), "message": echo_segments(m["message"])}})
            self.said[chat] = time.time()            # 跟复读也算说完一句
        except OSError as e:
            log(f"发送失败：{e}")

    def segment(self, chat=None):
        h = list(self.history.get(chat or ("group", str(self.group)), ()))
        return h[segment_start([m["time"] for m in h], time.time(), self.day_hour, self.gap):]

    def write_system(self):
        persona = self.persona_path.read_text() if self.persona_path.exists() else ""
        text = system(persona, self.hint, self.limit)
        if not self.system_path.exists() or self.system_path.read_text() != text:
            self.system_path.parent.mkdir(parents=True, exist_ok=True)
            self.system_path.write_text(text)
        return text

    def compose(self, chat=None, proactive=False, active=False):
        """→ (要发的话，或 SKIP / 空), 这次的经过（进 reply-log）。"""
        chat = chat or ("group", str(self.group))
        sys_text = self.write_system()
        seg = self.segment(chat)
        cue = ("没人叫你。看看你上次说话之后的这些里，有没有你真想接的；有就短短接一句，没有就只输出（不回）。" if proactive else
               "你刚说完话，群里还在接着聊。顺着刚才的话头接一句；真插不上才输出（不回）。" if active else
               "新消息里有人叫到了你。" if chat[0] == "group" else f"{self.who(chat[1])}在私聊你。")
        last_mine = max((i + 1 for i, m in enumerate(seg) if m["user_id"] == str(self.self_id)), default=None)
        since = seg[0]["time"] if seg else time.time()
        noted = [m for m in self.memos.get(chat, ()) if m["time"] >= since]       # 只给这一段里记下的
        p = prompt(view(seg, self.self_id, self.who), self.scene(chat), cue, last_mine, noted)
        trail = {"chat": list(chat), "system": sys_text, "prompt": p, "outputs": [], "calls": [], "after": len(seg)}
        images = []                                 # 看清时下好的图，之后的调用都附上
        memo = []

        def ask(q):
            text, meta = self.call(q, images=images) if images else self.call(q)
            trail["calls"].append({k: meta.get(k) for k in ("usage", "seconds")} if isinstance(meta, dict) else {})
            trail["outputs"].append(text.strip())
            text, got = memos_of(clean(text))
            memo.extend(x for x in got if x not in memo)
            return text or (SKIP if got else "")

        try:
            return self._compose(chat, seg, p, ask, trail, images)
        finally:
            if memo:
                trail["memo"] = memo
                self.memos.setdefault(chat, []).extend({"time": time.time(), "after": len(seg), "text": x} for x in memo)
                log(f"记下：{' / '.join(memo)}")

    def _compose(self, chat, seg, p, ask, trail, images):
        with tempfile.TemporaryDirectory(prefix="mh-reply-") as tmp:
            text = ask(p)
            picks = picks_of(text)
            if picks:
                trail["look"] = picks
                images.extend(fetch(image_urls(seg, picks), tmp))      # 原地加：ask 读的是 compose 里那个列表
                trail["images"] = len(images)
                log(f"看清：{picks}，附图 {len(images)} 张")
                seen = "（里面的图按出现先后附上了。）\n" if images else ""
                p += (f"\n你要看的：\n\n{details(seg, picks, self.self_id, self.who, self.by_id)}\n{seen}\n"
                      f"现在写你要发的话（或者{SKIP}），不能再看了。看到的值得留给之后的自己，最后另起一行写“记下：……”。\n")
                text = ask(p)
                if picks_of(text):
                    text = SKIP
            return self._fit(text, p, ask, trail)

    def _fit(self, text, p, ask, trail):
        """限长：超了重写一次，还超就截。"""
        if text != SKIP and visible(text) > self.limit:
            log(f"超字数 {visible(text)}/{self.limit}，重写一次")
            trail["rewrite"] = True
            text = ask(p + f"\n你刚才写了 {visible(text)} 字，太长了。压到 {self.hint} 字以内重写：\n{text}\n")
            if visible(text) > self.limit:
                log(f"还是超字数 {visible(text)}/{self.limit}，截断")
                trail["clipped"] = True
                text = clip(text, self.limit)
        return text, trail

    def record(self, trail):
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                    **trail}, ensure_ascii=False) + "\n")
        except OSError as e:
            log(f"reply-log 写不进去：{e}")

    def send(self, chat, parts):
        kind, to = chat
        for i, part in enumerate(parts):
            if i:
                time.sleep(SEND_GAP)
            msg = [{"type": "text", "data": {"text": part}}]
            self.ws.send({"action": "send_group_msg", "echo": f"reply-{time.time():.3f}",
                          "params": {"group_id": int(to), "message": msg}} if kind == "group" else
                         {"action": "send_private_msg", "echo": f"reply-{time.time():.3f}",
                          "params": {"user_id": int(to), "message": msg}})

    def kind_of(self, who):
        """这次回复算哪份额度：主动接话 "proactive"；有用户叫 None（不计）；只有 bot 叫 "bot"；其余 "total"。"""
        if "proactive" in who:
            return "proactive"
        if who & self.owner:
            return None
        if who and who <= self.bots:
            return "bot"
        return "total"

    def _used(self, k):
        q, now = self.sent[k], time.time()
        while q and now - q[0] > 3600:
            q.popleft()
        return len(q)

    def allow(self, kind):
        """这份额度还有没有；bot、主动接话还要看总量。"""
        if kind is None:
            return True
        return all(self._used(k) < self.limits[k] for k in {kind, "total"})

    def spend(self, kind, sent=True):
        """用掉一次：bot 记 bot 和总量；主动接话问的时候记 proactive（sent=False），发出去再记总量。"""
        now = time.time()
        if kind is None:
            return
        if kind != "proactive" or not sent:
            self.sent[kind].append(now)
        if kind != "total" and sent:
            self.sent["total"].append(now)

    def due(self, now):
        """合批等够了的会话（那个会话安静了，或者等满了）和还要等多久：(会话或 None, 秒)。"""
        wait = None
        for chat, p in self.pending.items():
            left = min(self.last.get(chat, 0) + MERGE_QUIET, p["at"] + MERGE_MAX) - now
            if left <= 0:
                return chat, 0
            wait = left if wait is None else min(wait, left)
        return None, wait

    def work(self):
        """另一条线程：等被叫到，合批，回；几个会话轮流，一次回一个。"""
        while True:
            with self.cv:
                while True:
                    chat, wait = self.due(time.time())
                    if chat:
                        break
                    self.cv.wait(wait)
                p = self.pending.pop(chat)
                kind = self.kind_of(p["who"])
                active = p.get("active", False)
            if not self.allow(kind):
                log(f"{kind} 额度这一小时用完了（{self._used(kind)}/{self.limits[kind]}，总量 "
                    f"{self._used('total')}/{self.limits['total']}），这次不回")
                continue
            if not self.ws:
                log("没连上，这次不回")
                continue
            if kind == "proactive":
                self.spend(kind, sent=False)        # 主动接话按问的次数算
            try:
                text, trail = self.compose(chat, proactive=kind == "proactive", active=active)
                trail["quota"] = kind
            except Exception as e:                  # 模型失败不拖垮进程，下次叫到再说
                log(f"模型失败：{type(e).__name__} {str(e)[:300]}")
                self.record({"chat": list(chat), "error": f"{type(e).__name__} {str(e)[:500]}"})
                continue
            if not text or text == SKIP:
                log("模型选了不回")
                self.record({**trail, "sent": []})
                continue
            parts = split(text)
            if trail.get("look"):
                self.notes[parts[0]] = look_note(trail["look"], trail.get("images"))
            self.spend(kind)
            log(f"回（{self.scene(chat)}{'，主动' if kind == 'proactive' else ''}）：" + " / ".join(parts))
            self.record({**trail, "sent": parts})
            try:
                self.send(chat, parts)
                self.said[chat] = time.time()        # 说完一句，开活跃窗口
            except OSError as e:
                log(f"发送失败：{e}")

    def on(self, raw):
        e = json.loads(raw)
        if e.get("echo") == "login":
            d = e.get("data") or {}
            self.self_id = d.get("user_id")
            self.member.setdefault(str(self.self_id), d.get("nickname") or str(self.self_id))
            log(f"我是 {self.self_id}")
        elif e.get("echo") == "group":
            self.group_name = (e.get("data") or {}).get("group_name") or self.group_name
            log(f"群名：{self.group_name}")
        elif "echo" in e and e.get("status") != "ok":
            log(f"接口失败：{json.dumps(e, ensure_ascii=False)[:300]}")
        elif "echo" not in e:
            self.note(e)

    def once(self, ssh, remote, local, token):
        p = listen.tunnel(ssh, remote, local) if ssh else None
        if ssh and not p:
            return False
        host = "127.0.0.1" if ssh else remote.rsplit(":", 1)[0]
        ws = None
        try:
            ws = listen.WS(host, local, headers=[f"Authorization: Bearer {token}"] if token else [])
            ws.send({"action": "get_login_info", "echo": "login"})
            ws.send({"action": "get_group_info", "params": {"group_id": self.group}, "echo": "group"})
            self.ws = ws
            log(f"已连上，群 {self.group}，私聊 {sorted(self.private)}")
            while True:
                self.on(ws.recv())
        except (OSError, listen.Closed) as e:
            log(f"断开：{type(e).__name__} {e}")
            return ws is not None
        finally:
            self.ws = None
            if ws:
                ws.close()
            if p:
                p.terminate()
                p.wait(5)


def serve():
    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    ssh, remote, _, token = listen.config()
    local = int(CFG.get("local", 13002))
    r = Reply()
    r.load()
    threading.Thread(target=r.work, daemon=True).start()
    wait = listen.RETRY
    while True:
        wait = listen.RETRY if r.once(ssh, remote, local, token) else min(wait * 2, listen.RETRY_MAX)
        log(f"{wait} 秒后重连")
        time.sleep(wait)
