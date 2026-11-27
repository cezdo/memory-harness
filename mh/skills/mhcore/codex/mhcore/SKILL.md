---
name: "mhcore"
description: "只在用户的消息以 $mhcore（Codex）或 /mhcore（agy）开头时使用：用户临时提权（内核态），这一轮可以改 memory-harness 的内核（core.md）。不要自行调用。"
---

用户用 $mhcore（Codex）或 /mhcore（agy）临时提权，这一轮是内核态，要求写在命令后面。

照要求改内核（core.md）（路径在 memory-harness 钩子补的上下文里）：只改要求涉及的句子，改完告诉用户改了哪句（改前、改后）。这一轮结束就回到用户态，改动由程序记进内核日志。
