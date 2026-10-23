"""从 event 的正文由程序读出说话人、行为（mode）、限定（2026-10-17：行为交给程序，不要求严格精确）。

event 的写法是“说话人 + 动词 + 内容”（旧版本 v3a2 和 event-v3 都是）：开头认出说话人，跳过几个副词，接着的动词按候选表对到行为；
限定按词表从正文里找。认不出的记为未知，不猜。

这是可以随时重算的读法，不写进 event：event 的 mode、限定、说话人只存模型自己写的（没让模型写的记为没做）；
读的时候调 read()，旧版本的 E 也一样能读出来。候选表来自 tmp/live/mode候选-2026-10-17.md。
"""
import re

from ..store import UNKNOWN
from ..store.instance import USER_NAME, USER_ALIASES

VERSION = "modes-v1"
WINDOW = 14             # 状语之后找动词的范围（字）     # 候选表或规则改了就升，读出来的结果标上版本

# 行为 → 正文里会出现的说法（按言语行为归类，见 design/engram/observe/设计.md）
MODES = {
    "告知": "告知 告诉 说明 表示 报告 汇报 解释 描述 展示 列出 给出 补充 强调 举例 总结 介绍 回答 答复 回顾 追加 写明 称 说",
    "指出": "指出 提到 提及 点出 注意到",
    "转述": "转述 贴出 贴了 发来 引用 复述 转来",
    "认为": "认为 主张 坚持 觉得 相信",
    "判断": "判断 判定 区分 断定 论断 得出结论 下结论 结论",
    "确认": "确认 核实 查明 验证 查证 核对",
    "发现": "发现 查到 观察到 看到 找到",
    "推测": "推测 怀疑 设想 估计 猜测 猜",
    "认识到": "认识到 理解 意识到 明白",
    "要求": f"要求 请求 让我 让{USER_NAME} 请我 请{USER_NAME} 规定 指定 禁止 吩咐 命令",
    "询问": f"询问 追问 问我 问{USER_NAME} 问 请教 好奇",
    "提醒": "提醒 提示 警告",
    "提出": "提出 抛出",
    "建议": "建议 提议 推荐",
    "计划": "计划 安排 打算 准备 预计",
    "同意": "同意 认可 接受 肯定 赞同 支持 表态支持 答应 应下",
    "选择": "选择 选定 采用 选了",
    "反对": "反对 否决 拒绝 不同意 质疑",
    "纠正": "纠正 修正 澄清 更正 反驳",
    "撤回": "撤回 放弃 收回",
    "承认": "承认 坦承 道歉",
    "评价": "评价 回应 称赞 夸 批评 吐槽",
    "决定": "决定 拍板 定下 敲定",
    "定义": "定义 命名 起名",
    "宣布": "宣布 宣告",
    "执行": "执行 实现 检查 查看 查了 运行 跑了 读了 读取 修改 改了 写了 写入 提交 创建 删除 安装 部署 重启 测试 搜索 调用 生成 整理 更新 打开 启动 记录 验证了",
    "希望": "希望 想要 想 期待 偏好",
    "担心": "担心 顾虑 害怕",
}
WORDS = sorted(((w, m) for m, ws in MODES.items() for w in ws.split()), key=lambda x: -len(x[0]))
SPEAKERS = ("转来的文本", "之前的我", "宿主", *USER_ALIASES, "我")
ADVERBS = ("随后", "接着", "然后", "首先", "最后", "进一步", "再次", "随即", "当即", "立即", "马上", "直接", "明确", "主动", "又", "再",
           "也", "还", "先", "已经", "已", "开始", "着手", "特别", "转折", "便", "就", "则", "都", "同时", "继续", "顺带", "顺便", "最终", "正式", "当场", "向我", f"向{USER_NAME}",
           "对我", f"对{USER_NAME}", "给我", f"给{USER_NAME}", "跟我", "和我", "一起", "并")
QUALIFIERS = re.compile(r"可能|也许|或许|似乎|好像|大概|应该是|不一定|未必|暂时|暂定|临时|先[^，。；]{0,12}看看|以后再说|再说|"
                        r"如果[^，。；]{0,20}|假如[^，。；]{0,20}|除非[^，。；]{0,20}|等[^，。；]{1,12}之?后|应当|最好|倾向于?")


def read(text):
    """正文 → {"speaker", "mode", "qualifier", "version"}；认不出的项是 UNKNOWN，正文里没有限定是空列表。"""
    t = (text or "").strip().lstrip("「“")
    speaker, rest = UNKNOWN, t
    for s in SPEAKERS:
        if t.startswith(s):
            speaker, rest = (USER_NAME if s in USER_ALIASES else s), t[len(s):]
            break
    mode = UNKNOWN
    if speaker != UNKNOWN:
        rest = rest.lstrip("，, ")
        changed = True
        while changed:
            changed = False
            for a in ADVERBS:
                if rest.startswith(a):
                    rest, changed = rest[len(a):], True
        mode = next((m for w, m in WORDS if rest.startswith(w)), None)
        if mode is None:         # “在最终回答中说明”“以表格展示”：动词前有一段状语，在前 WINDOW 个字里找最早出现的（单字词不找）
            hits = [(rest.find(w), -len(w), m) for w, m in WORDS if len(w) > 1 and 0 <= rest.find(w) < WINDOW]
            mode = min(hits)[2] if hits else UNKNOWN
    return {"speaker": speaker, "mode": mode, "qualifier": QUALIFIERS.findall(t), "version": VERSION}
