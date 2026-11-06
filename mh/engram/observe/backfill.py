"""补录（G7）：把 listen-onebot.jsonl 里缺的消息从 OneBot 历史接口拉回来。

监听只在连着的时候收得到：断线、协议端掉线、进程崩了这段时间没有事件推过来，文件里就是空的。
补录不猜断线时刻，按内容对齐——向协议端要一份最近的历史，凡是 message_id 不在文件里的就算缺，
按 listen 的原格式（收到的时刻用消息原本的时间）追加；幂等，重复跑不会重复写。

协议端只留最近一段历史（2026-11-01 那次：群 383 条、私聊 9 条，到前一天 18:32），
要得比它留的早补不了；补到哪一条为止会打出来，不假装补全。

用法：python3 -m mh backfill [--write] [--days 3] [--count 2000]
"""
import json
import socket
from datetime import datetime, timedelta, timezone

from . import listen


def human(ts):
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%m-%d %H:%M:%S")


def floor_of(times, days=3):
    """往回看到哪：文件里最早那条和 days 天前，取晚的。"""
    ago = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    return max(min(times) if times else 0, ago)


def missing(msgs, ids, floor):
    """历史里、floor 之后、文件里没有的那些（按 time 排序）。"""
    out = [m for m in msgs
           if m.get("message_id") and m["message_id"] not in ids and m.get("time", 0) >= floor]
    out.sort(key=lambda m: m.get("time", 0))
    return out


def row(msg):
    """一条历史消息 → 文件里的一行（和 listen.record 的格式一致）。"""
    at = datetime.fromtimestamp(msg["time"], timezone.utc).isoformat(timespec="milliseconds")
    return json.dumps({"received": at, "event": msg}, ensure_ascii=False)


def load(path=None):
    """文件里已有的 message_id 和事件时间。"""
    path = path or listen.LOG
    ids, times = set(), []
    if not path.exists():
        return ids, times
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line).get("event") or {}
        except ValueError:
            continue
        if ev.get("message_id"):
            ids.add(ev["message_id"])
        if ev.get("time"):
            times.append(ev["time"])
    return ids, times


def free_port(start):
    for p in range(start, start + 50):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("找不到空闲端口")


def targets():
    """要补哪些：(种类, 编号, 动作, 参数)。配置了 groups 就补那些群和 private 里的私聊。"""
    out = [("群", g, "get_group_msg_history", {"group_id": g}) for g in sorted(listen.groups() or ())]
    out += [("私聊", u, "get_friend_msg_history", {"user_id": u}) for u in sorted(listen.private())]
    return out


def sync(write=False, days=3, count=2000, log=print):
    """补一次；返回查到几条缺的（write 时也就是补了几条）。"""
    ids, times = load()
    floor = floor_of(times, days)
    log(f"文件里 {len(ids)} 条消息，最早 {human(min(times)) if times else '-'}；看 {human(floor)} 之后")

    ssh, remote, local, token = listen.config()
    port = free_port(local + 100)
    proc = listen.tunnel(ssh, remote, port) if ssh else None
    if ssh and not proc:
        raise RuntimeError("隧道起不来")
    ws = listen.WS("127.0.0.1" if ssh else remote.rsplit(":", 1)[0], port,
                   headers=[f"Authorization: Bearer {token}"] if token else [])
    got, reach = [], []
    try:
        for n, (kind, key, action, base) in enumerate(targets()):
            echo = f"bf{n}"
            ws.send({"action": action, "params": dict(base, count=count), "echo": echo})
            while True:
                msg = json.loads(ws.recv())
                if msg.get("echo") == echo:
                    break
            if msg.get("status") != "ok":
                log(f"{kind} {key}：接口失败 {msg.get('retcode')} {msg.get('message')}")
                continue
            msgs = (msg.get("data") or {}).get("messages") or []
            new = missing(msgs, ids, floor)
            ids.update(m["message_id"] for m in new)
            got.extend(new)
            if msgs:
                oldest = min(m["time"] for m in msgs)
                reach.append((kind, key, oldest))
                span = f"{human(oldest)} → {human(max(m['time'] for m in msgs))}"
            else:
                span = "-"
            log(f"{kind} {key}：拿到 {len(msgs)} 条（{span}），缺 {len(new)} 条")
    finally:
        try:
            ws.close()
        finally:
            if proc:
                proc.terminate()
                try:
                    proc.wait(5)
                except Exception:
                    proc.kill()

    for kind, key, oldest in reach:
        if oldest > floor + 60:
            log(f"注意：{kind} {key} 的历史只到 {human(oldest)}，再早的协议端没留，补不了")

    got.sort(key=lambda m: m.get("time", 0))
    if not got:
        log("没有缺的")
        return 0
    log(f"共缺 {len(got)} 条：{human(got[0]['time'])} → {human(got[-1]['time'])}")
    for m in got[:5] + (got[-5:] if len(got) > 5 else []):
        log(f"  {human(m['time'])} {m.get('user_id')} {str(m.get('raw_message'))[:40]}")
    if not write:
        log("（只统计，加 --write 才写）")
        return len(got)
    with listen.LOG.open("a", encoding="utf-8") as f:
        for m in got:
            f.write(row(m) + "\n")
    log(f"已追加 {len(got)} 行到 {listen.LOG}")
    return len(got)
