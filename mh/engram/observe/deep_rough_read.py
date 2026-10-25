"""精读、粗读：预览定下要读的几轮，交给模型读，拆成 event（design/engram/observe/设计.md“内部做法”）。

这里放精读、粗读这一步的 Operator 定义（哪个模型、经哪个宿主、叠哪些补丁、怎样隔离）和给模型的输入；调用本身在 mh/engram/invoke/。
2026-10-17 起（tmp/observe-e4/）：
- 精读 deepseek-ark-web-deep：常驻 dsh web（端口 3085），minimal 预设原样带 bash，只放行字数命令（白名单外判失败），high；
- 粗读 deepseek-ark-web-rough：另起的 dsh web（端口 3086），bare 预设去掉全部工具，low（到 2026-10-19）。
2026-10-19 起（tmp/window/ 实验三到六，用户：粗读是常态、精读是精品，各用所长）：
- 粗读 luna-codex-rough：codex exec 上的 Luna，low，用了工具就作废；提示词 rough-read-v0.1。
  一次最多读 ROUGH_CAP 字的回复（写窗口：一次读得越多写得越粗），超出的组按轮切开分几次读（切开的各段都带这组的计划）；
  提示词给一个条数范围（有内容的段落数 × ROUGH_RHO，上下浮动 ROUGH_FLOAT），不给字数；
  每一轮开头附上这一轮“我”读过、写过的文件和提交（程序从工具调用里认，written_files），复述文件的说明只写一条指路的。
- 精读不变：Luna 写得省、跟提示词走，DeepSeek 写得细、几乎不翻转说话人，细的放在分量重的少数轮上。
补丁在 patches/：极简层（关掉记忆工具等，dsh-minimal）、模型与推理档位（dsh-web）、粗读的 bare 预设（dsh-rough）。
补丁的内容进隔离标记，改了就是新的 Operator。
"""
import json
from pathlib import Path

import re

from ..invoke import Codex, DshWeb
from ..store import HOME, absent, blocks
from . import guard, label

PATCHES = Path(__file__).parent / "patches"
MINIMAL_PATCH = PATCHES / "dsh-minimal.patch.yml"
WEB_PATCH = PATCHES / "dsh-web.patch.yml"
ROUGH_PATCH = PATCHES / "dsh-rough.patch.yml"
MODEL = "deepseek-v4-1-flash-260910"          # 模型名写在 dsh-web.patch.yml，这里是给人看的

MODELS = {
    "deepseek-ark-web-deep": lambda: DshWeb(MODEL, "dsh web 常驻（测试实例，ark，minimal 预设，high，bash 只放行 mh-count）",
                                            patches=(MINIMAL_PATCH, WEB_PATCH), allow=guard.allow, allow_tag="mh-count"),
    "deepseek-ark-web-rough": lambda: DshWeb(MODEL, "dsh web 常驻（测试实例，ark，bare 预设无工具，low）",
                                             port=3086, box=HOME / "dsh-web-rough", preset="bare",
                                             patches=(MINIMAL_PATCH, WEB_PATCH, ROUGH_PATCH)),
    "luna-codex-rough": lambda: Codex("gpt-6-luna", "low", isolation="codex-home-v1-low", no_tools=True),
}
# 粗读的写窗口（2026-10-19，tmp/window/：Luna 4000 字在细和不丢之间，8000 起丢掉判断；数字没验证过，按使用调）
ROUGH_CAP = 4000             # 一次粗读大约读这么多字的回复：超过 ROUGH_MAX 的组按它估段数、平均切（一轮超过的自己一段）
ROUGH_MIN, ROUGH_MAX = 3000, 5000   # 切开后每段平均落在这个范围（2026-10-19：3000–5000 都可以）
ROUGH_RHO = 1.0              # 条数范围的中心：每个有内容的段落大约一条（Luna 不给范围时约 0.7，给 1.2 时约 0.9–1.2 都放得开）
ROUGH_FLOAT = 0.3            # 范围上下浮动三成（用户：只给一个数要求太高）
CONTENT_MIN = 40             # 有内容的段落：至少这么多字
LONG_CAP = 53                # 单轮自成一窗的条数中心上限（临时兜底，2026-10-25）：ROUGH_MAX × 有内容段落 p90 密度 10.6/千字；runbook I7a 长轮预览上线后撤
STEPS = {"精读": "deep", "粗读": "rough"}       # 读法 → 步骤；不读的不调用
TOOLS = {"deep": f"\n环境里唯一能用的命令是 {guard.COUNT}（数字数：参数或 here-doc 给文字）；别的命令都会让这次观察作废。\n",
         "rough": "\n不要使用任何工具或命令：用了，这次观察就作废。\n"}


def reply_chars(row):
    return len(row["assistant"] or "") + sum(len(x) for x in json.loads(row["commentary"] or "[]"))


def split(group, rows, cap=ROUGH_CAP, lo=ROUGH_MIN, hi=ROUGH_MAX):
    """粗读的一组按回复字数切成几段，尽量平均（2026-10-19：不要好几个 4000、最后一个 1000；3000–5000 都可以，不硬切，
    保证完整的轮）。不超过 hi 的不切；超过的按 cap 估段数，平均落在 lo–hi 之间，在最接近均分点的轮边界切开。返回 [组]。"""
    sizes = [reply_chars(r) for r in rows]
    total = sum(sizes)
    if total <= hi or len(rows) < 2:
        return [group]
    k = max(2, round(total / cap))
    while k < len(rows) and total / k > hi:
        k += 1
    while k > 2 and total / k < lo:
        k -= 1
    k = min(k, len(rows))
    cum = [sum(sizes[:i + 1]) for i in range(len(sizes))]
    cuts, start = [], 0                    # cuts：每段最后一轮的下标
    for j in range(1, k):
        target = total * j / k
        best = min(range(start, len(rows) - (k - j)), key=lambda i: abs(cum[i] - target))
        cuts.append(best)
        start = best + 1
    bounds = [-1] + cuts + [len(rows) - 1]
    parts = [rows[bounds[i] + 1:bounds[i + 1] + 1] for i in range(k)]
    return [{**group, "from": p[0]["ordinal"] + 1, "to": p[-1]["ordinal"] + 1, "turns": [r["occurrence"] for r in p],
             "part": f"{i + 1}/{k}"} for i, p in enumerate(parts)]


_PATH = re.compile(r"[\w./~@+-]*[\w~@+-]")


def file_names(files):
    """一轮附上的读写文件 → 认段落用的名字：文件取带扩展名的文件名，目录取至少 4 个字的目录名（太短的会误认普通的词）。"""
    names = set()
    for x in (files or {}).get("read", []) + (files or {}).get("wrote", []):
        for p in _PATH.findall(x.split("」：", 1)[-1] if x.startswith("提交 ") else x):
            if "/" not in p and "." not in p:
                continue
            base = p.rstrip("/").rsplit("/", 1)[-1]
            if ("." in base.strip(".") and len(base) >= 3) or ("." not in base and len(base) >= 4):
                names.add(base)
    return names


def amount(tables, files=None):
    """条数范围 (a, b)：有内容的段落数 × ROUGH_RHO，上下浮动 ROUGH_FLOAT；至少 1 条、至少差 1。
    “我”读过、写过文件的轮，提到这些文件的“我”的段落多半在复述文件，合算 1 条指路（2026-10-19：范围按全部段落算，
    下限会逼着把文件的内容写回来）；没提到的照常算（2026-10-25 修：原来整轮只算 1 条，读代码来回答问题的讨论也被压掉，
    tmp/observe-fast-v2/ 的 3290ad：33 个段落只有 7 个提到附上的文件，范围却是 4–8）。"""
    def count(n, t):
        # 用户说了话的轮至少算 1 条（用户的话常常很短，不到 CONTENT_MIN，2026-10-19 不算时用户的条目少了三成多）
        user = max(1 if any(k.startswith("U") and v.strip() for k, v in t.items()) else 0,
                   sum(1 for k, v in t.items() if k.startswith("U") and len(v) >= CONTENT_MIN))
        mine = [v for k, v in t.items() if k.startswith("A") and len(v) >= CONTENT_MIN]
        names = file_names((files or {}).get(n))
        about = [v for v in mine if any(x in v for x in names)]
        return user + len(mine) - len(about) + (1 if about or (files or {}).get(n) and not mine else 0)
    n = max(1, round(ROUGH_RHO * sum(count(n, t) for n, t in tables.items())))
    if len(tables) == 1:
        n = min(n, LONG_CAP)
    lo = max(1, round((1 - ROUGH_FLOAT) * n))
    return lo, max(lo + 1, round((1 + ROUGH_FLOAT) * n))


def file_note(files):
    """这一轮“我”读过、写过的文件：{"read": [...], "wrote": [...]} → 写进这一轮开头的说明行。"""
    if not files:
        return []
    out = []
    if files.get("read"):
        out.append("这一轮“我”读过：" + "、".join(files["read"]))
    if files.get("wrote"):
        out.append("这一轮“我”写过：" + "；".join(files["wrote"]))
    return out


POINTER = re.compile(r"(详见|参见|细节见|具体见|见于|见当轮|见该|记在|写在)")    # 只认指路的说法（“在讨论中”这类不算）


_SWITCH = "据此|承认|说明|提出|指出|认为|同意|决定|表示|回应|随后|接着|列出|解释|建议|要求|纠正|确认"


def _mixed(text):
    """一条里中途换了说话人（“用户指出……；我承认……”）。"""
    other = "我" if text.startswith(label.USER_NAME) else label.USER_NAME if text.startswith("我") else None
    return bool(other) and re.search(rf"[；。，]\s*{other}({_SWITCH})", text) is not None


def checks(events, tables, files):
    """程序检查（只标出，不改）：以用户开头却只引用“我”的段落（说话人可能整体翻转，tmp/window/ 实验四）；
    写了“见……”这类指路的，但这几轮程序没认出读写过任何文件（指向不存在的出处，实验六）；
    一条里中途换了说话人（每条只说一件事：谁做了什么；新粗读压得紧时约 2%，实验七）。"""
    suspect = [i for i, e in enumerate(events) if e["text"].startswith(label.USER_NAME) and e["segments"]
               and all(s.split(":", 1)[-1].startswith("A") for s in e["segments"])]
    any_files = any(files.get(n) for n in tables) if files else False
    dangling = [] if any_files else [i for i, e in enumerate(events) if POINTER.search(e["text"])]
    return {"speaker_suspect": suspect, "pointer_without_file": dangling,
            "mixed_speaker": [i for i, e in enumerate(events) if _mixed(e["text"])]}


def _ending(row):
    return None if absent(row["ending"]) or row["ending"] == "normal" else row["ending"]


def turn_blocks(row, extra=()):
    """一轮的全文，段落编号前面加轮号（3:A2）。extra：附在这一轮开头说明里的行（粗读的读写文件）。返回 (段落表, 文本)。"""
    n = row["ordinal"] + 1
    table, _ = blocks.number({"user": label.user_text(row["user"])[0], "assistant": row["assistant"],
                              "commentary": json.loads(row["commentary"]), "ending": _ending(row)})
    lines = [f"[{n}:{k}] {blocks._clip(v) if len(v) > blocks.BLOCK_CAP else v}" for k, v in table.items()]
    notes = label.notes(row["user"], row["assistant"], row["closed"], None, False, _ending(row))
    if label.relayed(row["user"]) is not None:
        notes.append(f"这一轮{label.USER_NAME}贴了一段转来的长文本：带 Markdown 结构的那部分不是{label.USER_NAME}自己写的。")
    notes += list(extra)
    return table, f"## 第 {n} 轮" + ("\n" + "\n".join(notes) if notes else "") + "\n" + "\n\n".join(lines)


def build(head, env, group, rows, files=None):
    """一组轮次的输入：提示词（不打码）+ 打码后的环境、预览写的计划、全文。files：{轮号: 读写的文件}，给了就附在各轮开头（粗读）；
    提示词里的 {amount} 换成条数范围。返回 (各轮段落表, 打码表, 文本)。"""
    mask = guard.Mask()
    tables, parts = {}, [env, f"## 观察计划（第 {group['from']}–{group['to']} 轮）\n{group['plan']}"]
    for row in rows:
        table, text = turn_blocks(row, file_note((files or {}).get(row["ordinal"] + 1)))
        tables[row["ordinal"] + 1] = (row["occurrence"], table)
        parts.append(text)
    if "{amount}" in head:
        lo, hi = amount({n: t for n, (_, t) in tables.items()}, files)
        head = head.replace("{amount}", f"{lo}–{hi}")
    return tables, mask, head + "\n\n" + mask("\n\n".join(parts))
