"""rhythm：按墙钟时间驱动 Engram 的过程（design/rhythm/设计.md）。重构 A12 的骨架：观察循环。

节律只管三件事：什么时候醒、并行几个、额度。每个过程只给一个“跑一批”的接口，一批看哪些、怎么看由过程自己决定
（observe 的 IDLE 是 observe 的参数，不在这里）。

观察循环：采集 → observe 跑一批 → 没有新工作就休眠。新会话和再观察在 observe 里排同一个队列，先到先得。
形成循环（B5）：form 跑一批 → 没有新工作就休眠。和观察循环分成两个进程（两个服务），只通过库交换数据，互不拖住；
form 自己按 observe 的接口（progress 里待观测的）定门槛，跟在观察后面。
- 每个循环只有一个实例（数据目录里的文件锁 observe.lock、form.lock）。
- 有环境错误（额度、限流、连接……）这一批提前结束，休眠后再试；失败次数和待重做清单归各过程。
- 一轮出错不让常驻进程退出，记下来，休眠后接着跑。
以后加上睡眠（第 5 步）、额度的使用（quota.py 现在只读，供 mh quota 和长实验用）。
"""
import fcntl
import sys
import time
from datetime import datetime

from ..engram import form, observe, store
from ..engram.observe import collect

INTERVAL = 300          # 没有新工作时休眠几秒
LIMIT = 16              # 一批最多预览几段会话（一段会话的窗口接着预览完）
WORKERS = 16            # 精读、粗读的模型调用并行几个
PREVIEW_WORKERS = 2     # 预览并行几个：Sonnet 走 Claude，额度和交互会话共用（2026-10-17 确认）；Sol 走 codex
CORRECT_LIMIT = 4       # 一批最多纠正几组（点名申请，排在新原话后面；纠错要调模型、花额度，申请.md C1d）
FORM_LIMIT = 10         # 形成一批最多几个单位
FORM_WORKERS = 4        # pack、label 的模型调用并行几个（attach 按时间顺序一个个来）
PROVIDER = {"sol": "codex", "sonnet": "claude"}    # 预览池（observe.POOL）里的模型走哪家的额度
BUSY_5H = 80            # 5 小时额度用到这么多（%）就排到后面
QUOTA_TTL = 600         # 额度读一次管多久（秒）
TIDY_INTERVAL = 60      # 整理循环多久醒一次（秒）；每个任务按自己的周期决定这次跑不跑


def _status(conn, log):
    from ..synapse.push import status
    for ws in status.workspaces():
        status.write(ws)
    return ""                                        # 每 5 分钟一次，不记日志


def _check(conn, log):
    from ..synapse.push import status
    for ws in status.workspaces():
        status.run_check(ws)
    return ""                                        # 结果进项目现状，不记日志


def _backfill(conn, log):
    from ..synapse import track
    inner = track.backfill_internal(conn)
    return f"补足迹：form 内部 {inner}" if inner else ""


def _cite(conn, log):
    """〖〗回填（Synapse 设计足迹一节）：回复里用〖〗标出的转述 → 认出是哪一条记忆，写成 use。要调 Jev（TypeSafe 官方 API）。"""
    from ..synapse.track import cite
    wrote, failed = cite.backfill(conn)
    return f"〖〗回填 {wrote} 轮" + (f"，出错 {failed} 轮（下次再试）" if failed else "") if wrote or failed else ""


# 整理循环的任务（2026-10-20）：维护工作集，让开场推送这一侧跟上项目和记忆的实际状态。周期跟着成本走：
# 程序任务分钟到小时级，模型任务按轻重几个小时到一周。一个任务：(名字, 程序 | 模型, 周期秒数, 做什么)。
TIDY = [("检查", "程序", 1800, _check),           # 排在项目现状前面：第一次醒来编的现状就带上检查
        ("项目现状", "程序", 300, _status),
        ("补 form 内部足迹", "程序", 86400, _backfill),
        ("〖〗回填", "模型", 86400, _cite)]          # 调 Jev 判转述说的是哪一条（2026-10-22）


def lock(name, home=None):
    """拿数据目录里的文件锁；已经有别的实例拿着就返回 None。返回的文件对象要一直留着（关了锁就放了）。"""
    f = open((home or store.HOME) / f"{name}.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


_picked = {"at": 0.0, "order": None}


def pick(names, read=None):
    """预览先用哪个模型（2026-10-20：Sol、Sonnet 按额度并用）：周额度用得少的排前面；5 小时额度用到 BUSY_5H% 以上、
    或已经用不了的排后面；读不到额度的排最后。同样的按原来的先后。read：取额度（测试替换），默认 quota.read。"""
    from . import quota
    qs = {q["name"]: q for q in (read or quota.read)(sorted({PROVIDER[n] for n in names if n in PROVIDER}))}

    def key(item):
        i, n = item
        q = qs.get(PROVIDER.get(n))
        if not q or "error" in q:
            return (2, 0, i)
        used = {w["name"]: w["used"] for w in q["windows"]}
        return (int(not q.get("ok", True) or used.get("5h", 0) >= BUSY_5H), used.get("week", 0), i)
    return [n for _, n in sorted(enumerate(names), key=key)]


def _order():
    """observe 预览模型的先后，按额度，QUOTA_TTL 内不重读。"""
    if _picked["order"] is None or time.time() - _picked["at"] > QUOTA_TTL:
        _picked.update(at=time.time(), order=pick(list(observe.POOL["preview"])))
    return _picked["order"]


def observe_once(conn, log=sys.stderr, models=None, limit=LIMIT, workers=WORKERS, preview_workers=PREVIEW_WORKERS,
                 modules=None):
    """观察循环的一轮：采集，再让 observe 跑一批（新会话和再观察的申请排同一个队列），然后按点名申请纠正最多 CORRECT_LIMIT 组
    （申请.md C1d：发现它的那次就是一次用到，下一批观察就做，排在新原话后面）。精读的纠正只能在这里跑：dsh web 每个进程
    一个实例，端口被这个循环占着。返回 (新轮次, 处理了几件, 环境错误几次)。modules：替换采集来源（测试用）。"""
    got = collect.run(conn, log=log) if modules is None else collect.run(conn, sources=tuple(modules), log=log, modules=modules)
    new = sum(v["turns"] for v in got.values())
    order = None if models and "preview" in models else _order()
    done, env = observe.run(conn, limit=limit, workers=workers, preview_workers=preview_workers, cause="schedule",
                            log=log, models=models, order=order)
    if not env:
        fixed, env = observe.correct(conn, models=models, log=log, cause="request", limit=CORRECT_LIMIT)
        done += fixed
    return new, done, env


def form_once(conn, log=sys.stderr, models=None, limit=FORM_LIMIT, workers=FORM_WORKERS):
    """形成循环的一轮：form 跑一批。返回 (处理了几件, 环境错误几次)。"""
    return form.run(conn, limit=limit, workers=workers, cause="schedule", log=log, models=models)


def _loop(name, step, db, interval, log, once):
    """常驻循环的共同部分：拿锁，一轮轮跑 step(conn) → (说明, 忙不忙, 结果)，不忙就休眠。once：只跑一轮就返回。"""
    held = lock(name, (db.parent if db else None))
    if held is None:
        print(f"{name} 循环已经在运行", file=log)
        return None
    conn = store.connect(db or store.DEFAULT_DB)
    while True:
        try:
            note, busy, got = step(conn)
            if note:
                print(f"{datetime.now():%m-%d %H:%M} {note}", file=log, flush=True)
        except Exception as e:          # 一轮出错不让常驻进程退出
            print(f"本轮出错：{e!r}", file=log, flush=True)
            busy, got = False, None
        if once:
            held.close()
            return got
        if not busy:
            time.sleep(interval)


def tidy_once(conn, log=sys.stderr, tasks=None, state=None, now=None):
    """整理循环的一轮：到了周期的任务跑一次（上次跑的时刻记在数据目录 tidy-state.json）。一个任务出错不挡别的。返回跑了哪些。"""
    import json
    path = state or (store.HOME / "tidy-state.json")
    try:
        last = json.loads(path.read_text())
    except (OSError, ValueError):
        last = {}
    t = now or time.time()
    ran = []
    for name, kind, period, fn in tasks or TIDY:
        if name in last and t - last[name] < period:      # 从来没跑过的，第一次醒来就跑
            continue
        try:
            note = fn(conn, log)
            if note:
                print(f"{datetime.now():%m-%d %H:%M} [整理·{name}] {note}", file=log, flush=True)
        except Exception as e:
            print(f"[整理·{name}] 出错：{e!r}", file=log, flush=True)
        last[name] = t
        ran.append(name)
    path.write_text(json.dumps(last, ensure_ascii=False))
    return ran


def tidy_loop(db=None, interval=TIDY_INTERVAL, log=sys.stderr, once=False):
    """常驻的整理循环（memory-harness-tidy.service）。once：只跑一轮就返回跑了哪些任务。"""
    def step(conn):
        return "", False, tidy_once(conn, log=log)
    return _loop("tidy", step, db, interval, log, once)


def observe_loop(db=None, interval=INTERVAL, log=sys.stderr, once=False, **kw):
    """常驻的观察循环（memory-harness-observe.service）。once：只跑一轮就返回 (新轮次, 处理了几件, 环境错误几次)。"""
    models = kw.pop("models", None) or {step: observe.MODELS[name]() for step, (name, _) in observe.CURRENT.items()
                                        if step != "preview"}          # 预览按额度在池里选（pick）

    def step(conn):
        new, done, env = observe_once(conn, log=log, models=models, **kw)
        note = f"新轮次 {new}，处理 {done}" + (f"，环境错误 {env}" if env else "") if new or done else ""
        return note, bool(done and not env), (new, done, env)
    return _loop("observe", step, db, interval, log, once)


def form_loop(db=None, interval=INTERVAL, log=sys.stderr, once=False, **kw):
    """常驻的形成循环（memory-harness-form.service）。once：只跑一轮就返回 (处理了几件, 环境错误几次)。"""
    models = kw.pop("models", None) or {step: form.MODELS[name]() for step, (name, _) in form.CURRENT.items()}

    def step(conn):
        done, env = form_once(conn, log=log, models=models, **kw)
        note = f"形成 处理 {done}" + (f"，环境错误 {env}" if env else "") if done else ""
        return note, bool(done and not env), (done, env)
    return _loop("form", step, db, interval, log, once)
