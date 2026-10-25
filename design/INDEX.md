# Memory Harness 文档索引

Memory Harness 保存助手与用户共同经历中形成的认识，让它们能够被引用、重新组织，并参与以后的思考。新的理解可以改变现在的看法，旧的认识和形成来路仍然可寻。

design 根目录只放本索引；全局设计、词典与安装配置在 `global/`，各模块在自己的目录里，与 `mh/` 的架构对应。做事的规则见 [AGENTS.md](../AGENTS.md)。

## 阅读入口

| 阅读目的 | 文档 | 实现 |
|---|---|---|
| 目标、不变量、分工、回放与评估 | [总体设计](global/设计.md) | [`mh/`](../mh) |
| 术语与句柄 | [词典](global/词典.md) | 各模块共用 |
| 三原语、存储与来源 | [Engram](engram/设计.md) | [`mh/engram/`](../mh/engram) |
| 原话如何进入系统 | [采集](engram/observe/采集.md) | [`observe/sources/`](../mh/engram/observe/sources)、[`collect.py`](../mh/engram/observe/collect.py) |
| 原话如何变成事件 | [observe](engram/observe/设计.md) | [`mh/engram/observe/`](../mh/engram/observe) |
| 会话记录、话题、挂线与骨架 | [form](engram/form/设计.md) | [`mh/engram/form/`](../mh/engram/form) |
| 跨会话的认知与版本引用 | [cognize](engram/cognize/设计.md) | [`mh/engram/cognize/`](../mh/engram/cognize) |
| 申请、纠正与反馈 | [修正机制](engram/申请.md) | Engram 各过程 |
| 推送、拉取、足迹与宿主 | [Synapse](synapse/设计.md) | [`mh/synapse/`](../mh/synapse) |
| 循环、并发与额度 | [rhythm](rhythm/设计.md) | [`mh/rhythm/`](../mh/rhythm) |
| 人称、来源视角与实际送达 | [表达](express/设计.md) | [`mh/express/`](../mh/express) |
| 安装、实例配置与可选依赖 | [使用说明](global/使用.md)、[English](global/使用.en.md) | 各模块用法链接到对应设计 |
| 公开材料与打包边界 | [公开发布](release/设计.md) | 安装元数据与发布清单 |

安装与命令用法见[使用说明](global/使用.md)，协作要求见 [AGENTS.zh-CN.md](../AGENTS.zh-CN.md)。

## 实现边界

| 部分 | 当前状态 |
|---|---|
| 采集、预览、精读与粗读 | 已实现；模型和宿主需另行配置 |
| 会话记录、入口、挂线、文件骨架 | 已实现；部分反馈只有程序接口 |
| 点名纠正、范围再观察、按需修订 | 已实现；各入口的支持范围见[修正机制](engram/申请.md) |
| 手记、MCP 拉取、访问足迹、呈现反馈 | 已实现 |
| 逐轮联想 | 只记录候选，暂停送达；“没印象”提示仍可送达 |
| cognition | 已有存储、引用与确认接口，自动形成尚未接入 |
| Atlas、睡眠整理 | 设计方向，尚未接入 |
| 任意深度分层索引 | 已有子线与反馈基础；自动插层、逐层挂线和完整句柄迁移尚未实现 |
| 统一节律控制器、记忆时刻评估 | 尚未完成 |

“已实现”指仓库提供相应代码，不表示已为使用者安装服务、接通模型或验证生产运行。

## 文档维护

本目录由原项目同路径的设计文件整理而来，安装与接入内容由原 `docs/usage.md`、`docs/usage.en.md` 合并。保留设计含义与来源关系，私人材料、真实案例、运行记录和私有提交历史留在原处；本仓库的修订通过 Git 保留。

各模块的 `设计.md` 说明职责、当前做法与实现边界；采集与申请等专门内容单独成文。通用安装配置只维护一份，具体操作跟随模块。草稿与旧做法按需放进模块的 `shelved/`，不作为当前行为；没有公开材料时不创建占位档案。

源码运行时可从本目录构造项目骨架，并在未显式配置时读取通用词典；个人内核、项目现状、称呼与运行数据仍放在仓库外。
