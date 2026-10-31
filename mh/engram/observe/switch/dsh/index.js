// memory-harness observe 开关（mh/engram/observe/switch/）：注册 /no-observe、/do-observe。
// 命令不经过模型：dsh 把每次执行记成会话日志里的 command/run，mh 的采集从那里读出开关（mh/engram/observe/sources/dsh.py），
// 这里只给用户一行确认。
// 叫醒（observe 设计“叫醒”）：一轮结束（agent/turn-stopping）和 /do-observe 碰一下数据目录的 observe.wake，休眠的观察循环提前醒。
import { closeSync, mkdirSync, openSync, utimesSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export const name = "mh-observe-switch";
export const inject = ["commands"];

const COMMANDS = [
  { name: "no-observe", description: "这个会话不让记忆观察（memory-harness）", text: "memory-harness: 已关闭对本会话的观察，本会话的信息将不再进入记忆。可使用 /do-observe 恢复。" },
  { name: "do-observe", description: "恢复记忆观察这个会话（memory-harness）", text: "memory-harness: 已恢复对本会话的观察。" },
];

// 同 store.HOME；循环没在跑、目录不在都不报错
const HOME = process.env.MH_HOME || join(homedir(), ".local/share/memory-harness");

function wake() {
  try {
    mkdirSync(HOME, { recursive: true });
    const f = join(HOME, "observe.wake");
    closeSync(openSync(f, "a"));
    const now = new Date();
    utimesSync(f, now, now);
  } catch {}
}

export function apply(ctx) {
  ctx.effect(function* () {
    for (const c of COMMANDS) {
      yield ctx.commands.register({
        name: c.name,
        description: c.description,
        handler: ({ rawInput }) => {
          if (rawInput.trim()) return { kind: "error", text: `用法：/${c.name}（不带参数）` };
          if (c.name === "do-observe") wake();            // 把尾巴现在读掉
          return { kind: "success", text: c.text };
        },
      });
    }
  }, "mh-observe-switch commands");
  ctx.on("agent/turn-stopping", () => { wake(); });
}
