"""写入与读取共用的表达上下文；不改写源正文，不推断身份。"""
import hashlib

VERSION = "0.1"
RULES = """人称按作者、受话人、当前读者和用途理解。原话与引语中的“我/你”保留原表达的视角；任务提示词中的“你”指执行任务的模型。
写记录时保留说话人、来源、名字、句柄和数字，按实例记录名标明当前用户；同名的人、机器、目录和作品角色按上下文区分。
直接给本人阅读的回复用“你”，需要称呼时采用私有偏好；代拟邮件等成文内容中的“我”按署名人解释。项目角色描述用“用户”。
称呼线索供检索和理解使用，不凭名字相同合并主体。源记录错误在所属层纠正，呈现错误引用送达记录反馈。"""


# 给模型的记忆整段包在 <memory-harness> 里（2026-10-24：原来每次在开头加一句“〔记忆视角〕……”、末尾加“〔送达 use N〕”）；
# 下面三句的意思写在内核（core.md）的图例里，只说一次；这里留着进规范的指纹。
TAG = "memory-harness"
READ_NOTE = "<memory-harness> 里的记录保留原记录的叙述视角。"
RAW_NOTE = "带 source=\"原话\" 的保留来源原文和原说话人的视角。"
VIEW_NOTE = "引语、自述和代拟文中的“我/你”按其作者与受话人理解；给本人的回复按私有称呼偏好措辞。"


def signature():
    return {"version": VERSION, "rules_hash": hashlib.sha256("\n".join((RULES, READ_NOTE, RAW_NOTE, VIEW_NOTE)).encode()).hexdigest()}


def context(user_name, aliases=(), *, purpose, author="source", addressee="source", reader="model"):
    return {**signature(), "purpose": purpose, "author": author, "addressee": addressee, "reader": reader,
            "user_name": user_name, "aliases": list(dict.fromkeys([user_name, *aliases]))}


def prompt(template, user_name):
    """只填程序定义的名字槽位；不对原始材料作全文替换。"""
    return template.strip().replace("{{用户}}", user_name) + "\n\n## 表达规范\n" + RULES


def present(text, ctx, *, raw=False, use=None):
    """给模型读的记忆包进 <memory-harness>（use：这次送达的编号；raw：原话），正文逐字保留；给内部过程、给人看的原样返回。"""
    if not text or ctx["reader"] != "model" or ctx["purpose"] not in ("memory", "standing"):
        return text
    attrs = (f' use="{use}"' if use is not None else "") + (' source="原话"' if raw else "")
    return f"<{TAG}{attrs}>\n{text}\n</{TAG}>"


def evidence(ctx, text):
    return {**ctx, "text_hash": hashlib.sha256(text.encode()).hexdigest()}
