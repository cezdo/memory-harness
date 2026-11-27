---
name: "no-observe"
description: "只在用户的消息正好是 $no-observe（Codex）或 /no-observe（agy）时使用：这个会话标成不被 memory-harness 观察。不要自行调用。"
---

用户输入了 $no-observe（Codex）或 /no-observe（agy）：这个会话标成不观察，memory-harness 不会把它写进记忆（输入 $do-observe（Codex）或 /do-observe（agy）可以恢复）。只原样回一句“memory-harness: 已关闭对本会话的观察，本会话的信息将不再进入记忆。可使用 $do-observe 恢复。” 在 agy 中把恢复命令写作 /do-observe，其余照原句回复。
