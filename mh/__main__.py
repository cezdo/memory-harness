import argparse
import json
import os
import sys
from pathlib import Path

from . import synapse


def main():
    ap = argparse.ArgumentParser(prog="mh")
    sub = ap.add_subparsers(dest="cmd", required=True)
    eg = sub.add_parser("engram", help="新库 engram.db（重构后的原语与存储）：check 检查不变量与覆盖；"
                                       "show <E|J|C编号…> 沿 Occurrence 回溯；page <来源> <会话前缀> 生成一段会话的阅读页；"
                                       "collect 采集；observe 跑一批观察；form 跑一批形成（--redo 看待重做清单）；常驻循环是 mh rhythm；"
                                       "request <给哪个过程> <被质疑的：E编号 | form_record:编号 | pack:act编号（那次整理的全部陈述）…> "
                                       "--reason … --model …：主会话写一张申请；requests [过程] 列出申请；"
                                       "reobserve --since … --until …（或 --session …） --reason … --model …：写一张再观察的范围申请；"
                                       "reobserve --reread --session … --reason …：补读（当时判了不读、按现在的规则要读的，程序重算、粗读）；"
                                       "correct [--limit N]：按给 observe 的点名申请纠正那几条 E（C1b）；"
                                       "skeleton --workspace … --root … --file … --section … --placements …：按文件申请骨架（--dry-run 预览）；"
                                       "feedback T<入口> --name …|--topic … --reason … --model …：主会话直写的反馈（会话入口）")
    eg.add_argument("action", choices=["check", "show", "page", "collect", "observe", "form", "request", "requests", "reobserve",
                                       "correct", "feedback", "skeleton"])
    eg.add_argument("--workspace", help="skeleton：骨架所属的工作区")
    eg.add_argument("--root", type=Path, help="skeleton：工作区根目录")
    eg.add_argument("--file", help="skeleton：相对工作区的架构文件")
    eg.add_argument("--section", help="skeleton：包含显式文本树的章节")
    eg.add_argument("--overview", help="skeleton：模块导览表的章节（可选）")
    eg.add_argument("--placements", type=Path, help="skeleton：已有线到节点路径的 JSON 对象")
    eg.add_argument("--name", help="feedback：改好的名字")
    eg.add_argument("--topic", help="feedback：改好的主题")
    eg.add_argument("--holder", default="我", help="feedback：谁说的（默认“我”）")
    eg.add_argument("--since", help="reobserve：会话开始时间的下界（ISO，UTC）")
    eg.add_argument("--until", help="reobserve：会话开始时间的上界（不含）")
    eg.add_argument("--dry-run", action="store_true", help="reobserve：只列出会挑中哪些会话，不写申请")
    eg.add_argument("--reread", action="store_true", help="reobserve：补读，不重新预览（2026-10-20）")
    eg.add_argument("--reason", help="request：理由")
    eg.add_argument("--model", help="request：当时主会话的模型（记进主会话的 Operator）")
    eg.add_argument("--limit", type=int, default=10, help="observe：这一批最多预览几个窗口")
    eg.add_argument("--workers", type=int, default=8, help="observe：并行的模型调用数")
    eg.add_argument("--session", help="observe：只看这个会话（ID 前缀）")
    eg.add_argument("--redo", action="store_true", help="observe、form：只列待重做清单（失败满次数的）")
    eg.add_argument("--brief", action="store_true", help="check：只比对快照、不改写，不列分布（项目现状用）")
    eg.add_argument("args", nargs="*")
    eg.add_argument("--engram-db", type=Path, default=None, help="新库路径（默认数据目录下的 engram.db）")
    eg.add_argument("--focus", help="page：开头回溯哪一条（如 J17398）")
    eg.add_argument("--title", help="page：页面标题（默认用会话标题）")
    eg.add_argument("--note", default="", help="page：导语后面补一句为什么选这一片")
    eg.add_argument("--out", type=Path, help="page：写入文件；默认打印")
    rh = sub.add_parser("rhythm", help="节律：常驻的观察循环（采集 → observe 跑一批 → 休眠），memory-harness-observe.service 跑它；"
                                       "--form 是形成循环（form 跑一批 → 休眠），memory-harness-form.service 跑它")
    rh.add_argument("--form", action="store_true", help="跑形成循环，不跑观察循环")
    rh.add_argument("--tidy", action="store_true", help="跑整理循环（维护开场推送的项目现状、检查……），memory-harness-tidy.service 跑它")
    rh.add_argument("--once", action="store_true", help="只跑一轮就退出")
    rh.add_argument("--engram-db", type=Path, default=None, help="新库路径（默认数据目录下的 engram.db）")
    sub.add_parser("inject", help="Claude Code SessionStart hook：从标准输入读宿主 JSON，输出开场推送")
    se = sub.add_parser("evoke", help="按词找记忆（同 MCP 工具 evoke）")
    se.add_argument("words", nargs="*")
    se.add_argument("--grain", default=None, choices=["L", "T", "S"], help="最小给到哪一层：L 线和骨架、T 话题、S 陈述（默认）")
    gt = sub.add_parser("relive", help="按句柄读记忆（同 MCP 工具 relive）")
    gt.add_argument("ks", nargs="*", metavar="句柄")
    gt.add_argument("--source", action="store_true", help="来源：陈述依据的 E，J 的旧会话记录和同一段会话现在的话题")
    gt.add_argument("--said", action="store_true", help="原话：陈述、E 沿出处走到原话")
    gt.add_argument("--recall", nargs="?", const="all", default=None, metavar="N|all",
                    help="历史：all 或数字 N")
    for x in (se, gt):
        x.add_argument("--session", default="", help="当前会话编号（开场推送会写进命令）")
        x.add_argument("--purpose", default="interactive", choices=list(synapse.PURPOSES))
        x.add_argument("--note", default="", help="一句话意见：好用、别扭、找不到……")
    bl = sub.add_parser("standing", help="常态记忆：看某个工作区开场实际推送的文本（design/synapse/常态记忆.md + 记忆工具与常用入口 + 项目现状）")
    bl.add_argument("--cwd", default=os.getcwd())
    bl.add_argument("--host", default="claude", help="按哪个宿主看（宿主自己给了的节不给）")
    mp = sub.add_parser("roam", help="走记忆的地图（同 MCP 工具 roam）")
    mp.add_argument("node", nargs="?")
    mp.add_argument("--session", default="")
    mp.add_argument("--purpose", default="interactive", choices=list(synapse.PURPOSES))
    mp.add_argument("--note", default="")
    for x in (se, gt, mp):
        x.add_argument("--limit", type=int, default=None, help="每页几条（默认 8，最多 30）")
        x.add_argument("--page", type=int, default=None, help="第几页，从 1 起")
    sub.add_parser("backfill", help="补足迹：form 内部的拉取（2026-10-20 以前没记的）补成 use，回复里的〖〗认出引用了哪条记忆（调 Jev）；可以重复跑")
    qa = sub.add_parser("quota", help="三家订阅的额度（codex / claude / ark），只读，不发模型请求")
    qa.add_argument("names", nargs="*", help="只看这几家")
    qa.add_argument("--json", action="store_true")
    qa.add_argument("--wait", action="store_true", help="只看一家：用完了就打印要等到的 unix 秒，没用完打印 0，读不到打印 -1")
    args = ap.parse_args()
    if args.cmd == "engram":
        from .engram import store as estore
        from .engram import check as echeck, show
        path = args.engram_db or estore.DEFAULT_DB
        conn = estore.connect(path, readonly=args.action == "skeleton" and args.dry_run)
        if args.action == "check":
            print(echeck.check(conn, path, brief=args.brief))
        elif args.action == "skeleton":
            from .engram.form import skeleton
            if not all((args.workspace, args.root, args.file, args.section)):
                raise SystemExit("给 --workspace、--root、--file、--section")
            source = skeleton.read_file(args.workspace, args.root, args.file, args.section, args.overview)
            assignments = json.loads(args.placements.read_text()) if args.placements else {}
            print(skeleton.preview(conn, source, assignments))
            if not args.dry_run:
                if not args.model or not args.reason:
                    raise SystemExit("给 --model 和 --reason；预览用 --dry-run")
                with conn:
                    rid = skeleton.request_file(conn, source, assignments, args.reason, estore.main_operator(conn, args.model))
                    act = skeleton.adopt(conn, rid, assignments)
                print(f"骨架申请 {rid} → 采用 {act}：{args.workspace}，{len(source['nodes'])} 个节点、{len(assignments)} 条线")
        elif args.action == "request":
            to, targets = args.args[0], args.args[1:]
            inputs = {}
            for t in targets:
                if t[:1] in estore.HANDLES and t[1:].isdigit():
                    inputs.setdefault(estore.HANDLES[t[0]], []).append(int(t[1:]))
                elif t.startswith("pack:"):
                    inputs.setdefault("form_record", []).extend(r[0] for r in conn.execute(
                        "SELECT id FROM form_record WHERE born=? ORDER BY idx", (int(t[5:]),)))
                elif ":" in t:
                    kind, i = t.split(":", 1)
                    inputs.setdefault(kind, []).append(int(i))
                else:
                    raise SystemExit(f"认不出：{t}")
            if not args.model:
                raise SystemExit("--model：当时主会话的模型")
            rid = estore.request(conn, estore.MAIN, to, inputs, args.reason or "", estore.main_operator(conn, args.model), cause="manual")
            conn.commit()
            print(f"申请 {rid}：{estore.MAIN} → {to}，" + "，".join(f"{k} {len(v)} 条" for k, v in inputs.items()))
        elif args.action == "reobserve":
            from .engram import observe as eobserve
            if args.session:
                keys = [tuple(r) for r in conn.execute("SELECT source, session FROM session_detail WHERE session LIKE ?",
                                                       (args.session + "%",))]
            elif args.since and args.until:
                keys = eobserve.reobserve_candidates(conn, args.since, args.until)
            else:
                raise SystemExit("给 --since、--until，或者 --session")
            if args.reread:
                pids = [p for k in keys for p in eobserve.previews_of(conn, *k)]
                if args.dry_run:
                    for p in pids:
                        for g in eobserve.reread_groups(conn, p):
                            print(f"  预览 {p} 第 {g['from']}–{g['to']} 轮，{g['chars']} 字")
                    raise SystemExit(0)
                for p, act, n in eobserve.reread(conn, pids, args.reason or ""):
                    print(f"  预览 {p} → 补读 {act}：{n} 组")
                conn.commit()
                raise SystemExit(0)
            for k in keys:
                print(f"  {k[0]} {k[1]}")
            if args.dry_run or not keys:
                print(f"会挑中 {len(keys)} 段会话（没写申请）")
            else:
                if not args.model:
                    raise SystemExit("--model：当时主会话的模型")
                how = f"--session {args.session}" if args.session else f"--since {args.since} --until {args.until}"
                rid = eobserve.request_reobserve(conn, keys, args.reason or "", estore.main_operator(conn, args.model), how=how)
                conn.commit()
                print(f"再观察的申请 {rid}：{len(keys)} 段会话，排进队列")
        elif args.action == "feedback":
            from .engram import form as eform
            target = eform.handle_of(args.args[0])
            if not target.startswith("T") or not target[1:].isdigit() or not args.model:
                raise SystemExit("现在只接会话入口（T 编号），要给 --model")
            new = eform.feedback_entry(conn, estore.main_operator(conn, args.model), int(target[1:]), args.reason or "",
                                       name=args.name, topic=args.topic, holder=args.holder)
            print(f"反馈：{target} → T{new}")
        elif args.action == "requests":
            for to in (args.args or estore.LAYERS):
                for r in estore.requests(conn, to, "all"):
                    print(f"申请 {r['id']}（{r['state']}）{r['from']} → {r['to']}：{r['reason']}  "
                          + "，".join(f"{k} {len(v)} 条" for k, v in r["inputs"].items()) + (f"  认领：{r['acts']}" if r["acts"] else ""))
        elif args.action == "collect":
            from .engram.observe import collect as ecollect
            print(json.dumps(ecollect.run(conn), ensure_ascii=False, indent=1))
        elif args.action == "correct":
            from .engram import observe as eobserve
            done, env = eobserve.correct(conn, limit=args.limit)
            print(f"纠正 {done} 组" + (f"，环境错误 {env} 次" if env else ""))
        elif args.action == "observe":
            from .engram import observe as eobserve
            if args.redo:
                rows = eobserve.redo(conn)
                print(f"待重做 {len(rows)} 件")
                for kind, key, c in rows:
                    what = f"预览（从原文 {key[0]} 开始的窗口）" if kind == "preview" else f"观察（轮次 {list(key[0])}）"
                    print(f"  {what}：" + "、".join(f"{k} ×{n}" for k, n in c.items()))
            else:
                print(eobserve.run(conn, limit=args.limit, workers=args.workers, session=args.session))
        elif args.action == "form":
            from .engram import form as eform
            if args.redo:
                rows = eform.redo(conn)
                print(f"待重做 {len(rows)} 件")
                for step, unit, c in rows:
                    print(f"  {step} {unit[1][:12]}（第一条 E{unit[2]}）：" + "、".join(f"{k} ×{n}" for k, n in c.items()))
            else:
                print(eform.run(conn, limit=args.limit, workers=args.workers, session=args.session))
        elif args.action == "show":
            print("\n\n".join(show.trace(conn, h) for h in args.args))
        else:
            text = show.page(conn, *args.args[:2], focus=args.focus, title=args.title, note=args.note)
            args.out.write_text(text) if args.out else print(text)
    elif args.cmd == "inject":
        from .synapse import push
        print(json.dumps(push.hook(synapse.library(), json.load(sys.stdin)), ensure_ascii=False))
    elif args.cmd in ("evoke", "relive", "roam"):
        from .synapse import pull
        kw = dict(session=args.session, purpose=args.purpose, cwd=os.getcwd(), note=args.note, limit=args.limit, page=args.page)
        conn = synapse.library()
        if args.cmd == "evoke":
            print(pull.evoke(conn, args.words, grain=args.grain, **kw))
        elif args.cmd == "relive":
            hs, recall = list(args.ks), args.recall
            if recall is not None and recall != "all" and not recall.isdigit():
                hs, recall = [recall] + hs, "all"          # --recall 后面紧跟的是句柄，不是窗口
            print(pull.relive(conn, hs, said=args.said, source=args.source, recall=recall, **kw))
        else:
            print(pull.roam(conn, args.node, **kw))
    elif args.cmd == "standing":
        from .synapse import pull, push
        from .synapse.push import status
        now = status.read(push.workspace(args.cwd), host=args.host)
        print(push.standing(args.cwd, push.entry(pull.layers(synapse.library()), args.cwd)) + ("\n\n" + now if now else ""))
    elif args.cmd == "backfill":
        from .engram import store as estore
        from .synapse import track
        from .synapse.track import cite
        conn = estore.connect(estore.DEFAULT_DB)
        print(f"补成 use：form 内部的拉取 {track.backfill_internal(conn)} 条；〖〗回填 {'，出错 '.join(map(str, cite.backfill(conn)))} 轮")
    elif args.cmd == "quota":
        from .rhythm import quota
        qs = quota.read(args.names or None)
        if args.wait:
            w = quota.wait_until(qs[0])
            print(-1 if w is None else w)
        else:
            print(json.dumps(qs, ensure_ascii=False, indent=1) if args.json else quota.text(qs))
    elif args.cmd == "rhythm":
        from . import rhythm
        if args.tidy:
            got = rhythm.tidy_loop(args.engram_db, once=args.once)
            if args.once:
                print(f"整理：{'、'.join(got or []) or '没有到周期的任务'}")
        elif args.form:
            got = rhythm.form_loop(args.engram_db, once=args.once)
            if args.once and got:
                print(f"形成 处理 {got[0]}，环境错误 {got[1]}")
        else:
            got = rhythm.observe_loop(args.engram_db, once=args.once)
            if args.once and got:
                print(f"新轮次 {got[0]}，处理 {got[1]}，环境错误 {got[2]}")

main()
