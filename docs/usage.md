# 安装、配置与接入

[English](usage.en.md) · [返回介绍](../README.md)

## 安装与首次检查

需要 Python 3.11 或更新版本。核心存储、读取和测试使用 Python 标准库；安装使用 setuptools。不同宿主、模型调用和中文分词需要另行配置。

```sh
python3 -m pip install --no-cache-dir --no-build-isolation .
mh --help
```

先创建独立的测试实例；以下命令只在临时目录创建空库和运行完整性检查：

```sh
demo_dir=$(mktemp -d)
MH_HOME="$demo_dir/data" MH_INSTANCE="$demo_dir/instance.toml" \
  mh engram check --engram-db "$demo_dir/data/engram.db" --brief
```

源码直接运行可用 `python3 -m mh` 替代 `mh`。首次检查通过表示存储接口能工作；模型和宿主接入需要下面的额外配置。

## 实例配置

配置默认从 `~/.config/memory-harness/instance.toml` 读取。可以从包内的 `mh/engram/store/instance.example.toml` 复制并修改，也可以用 `MH_INSTANCE` 指定另一份文件。

```toml
projects = "~/projects"

[user]
name = "用户"
aliases = []

[resources]
standing = "~/.config/memory-harness/standing.md"
glossary = "~/.config/memory-harness/glossary.md"
workspace_status = "~/.config/memory-harness/workspace-status"
```

这三类文本由使用者自行维护，可省略：常态记忆用于开场推送；词典用于术语别名，词头形如 `**完整名字 / alias**`；项目现状目录放 `<工作区>.md`。发行包不包含个人 profile、真实对话和开发设计文档。

项目设计、接口、协作规则和公开介绍用“用户”描述角色；直接给本人阅读的回复、报告和文档用“你”，需要称呼时按私有偏好使用名字。模型自称“我”。人称按读者、用途和叙述视角选择，模型读取文档时保留该文档的视角。运行时记录和提示词使用 `[user].name`（默认“用户”），识别其他称呼使用 `[user].aliases`。个人名字和称呼偏好放在仓库外的实例配置或 profile；原始对话与已有记忆保留原来的称呼。

写入提示词与记忆读取共用 `mh.express` 的表达规范。程序保留正文，附上作者、受话人和读者的视角提示；当次称呼及规则版本随输入和送达记录保存。个人别名也会作为检索候选和单独的称呼线索供给模型，同名对象按上下文判断。记忆返回末尾的 `use` 编号可用于呈现反馈：`feedback(scope="presentation", use=<编号>, reason="...")`；记录内容有误时使用默认的记录反馈。呈现反馈不改源记录、不计作一次新的记忆曝光。私有实例继续使用配置中的名字，本功能不提供输入匿名化保证。

| 环境变量 | 用途 |
|---|---|
| `MH_HOME` | 运行数据目录，默认 `~/.local/share/memory-harness` |
| `MH_INSTANCE` | 实例配置文件 |
| `MH_DB` | Synapse 读取和记访问足迹的数据库；Engram 命令使用 `--engram-db` |
| `MH_LTP_PYTHON`、`MH_LTP_MODEL` | 安装 LTP 的 Python 和本地模型目录 |
| `MH_LEXICON` | 可选词库目录，读取 `rime-ice/cn_dicts/` 和 `thuocl/data/` |
| `MH_TYPESAFE_PYTHON` | 安装 TypeSafe SDK 的 Python，用于引用回填 |
| `MH_COUNT` | 精读允许调用的 `mh-count` 字数工具路径 |

可选工具的缺省路径位于 `/usr/local/mh-tools/`：`ltp/venv/bin/python`、`ltp/model`、`lexicon/`、`typesafe/venv/bin/python`、`mh-count/mh-count`。工具、模型权重与词库需自行准备，发行包不附带。LTP 和 TypeSafe 的辅助 Python 进程直接执行包内脚本。

## 使用与接入

```sh
mh engram collect                # 采集本机宿主的会话
mh engram check --brief          # 检查来源、引用与覆盖
mh evoke 关键词
mh roam
mh relive L3:T5 --said
```

`collect` 会读取本机宿主会话并将原文保存到实例中；执行前确认数据目录。原文可能含凭据等敏感内容，数据库和输出阅读页应私有保存。`evoke` 按词找，`relive` 按句柄展开并回到原话，`roam` 浏览工作区、话题线和话题。

需要 MCP 的宿主可将服务命令配置为：

```sh
python3 -m mh.synapse.pull.mcp
```

该进程使用标准输入输出。配置宿主时给出正确的 Python、代码路径及实例环境变量。Codex 钩子安装器可先通过 `python3 -m mh.synapse.hosts.codex.install --help` 查看；它会修改宿主配置，需显式执行安装。

Claude Code 的开场钩子入口是 `python3 -m mh inject`；逐轮钩子入口是 `python3 -m mh.synapse.push.associate`。dsh 插件和观察开关适配随源码放在 `mh/synapse/hosts/dsh/` 和 `mh/engram/observe/switch/`，需按各自宿主的插件配置加载。

观察与形成默认调用外部 CLI 或 dsh 测试实例：预览、形成使用已登录的 Codex / Claude CLI；精读与粗读使用已配置模型提供方的 dsh 实例。通过实例配置的 `[tools].dsh_test` 指定该实例根目录。调用凭据沿用相应宿主配置；模型选择和隔离逻辑在 `mh/engram/invoke/` 及各过程模块中。

完成调用环境配置后，可手动执行 `mh engram observe`、`mh engram form`，或分别运行 `mh rhythm`、`mh rhythm --form`、`mh rhythm --tidy`。自动安装常驻服务暂未提供。引用回填 `mh backfill` 还需要 TypeSafe SDK 和 `~/.config/memory-harness/typesafe.env` 中的 `TYPESAFE_API_KEY`，可选 `TYPESAFE_MODEL`。
