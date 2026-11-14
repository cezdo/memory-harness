---
description: 临时提权（内核态）：这一轮可以改内核（core.md）（memory-harness）
argument-hint: <要求>
disable-model-invocation: true
---
用户用 /mhcore 临时提权，这一轮是内核态。要求：$ARGUMENTS

照要求改内核（core.md）（路径在 memory-harness 钩子补的上下文里）：只改要求涉及的句子，改完告诉用户改了哪句（改前、改后）。这一轮结束就回到用户态，改动由程序记进内核日志。
