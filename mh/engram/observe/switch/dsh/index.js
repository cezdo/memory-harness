// memory-harness observe 开关（mh/engram/observe/switch/）：注册 /no-observe、/do-observe。
// 命令不经过模型：dsh 把每次执行记成会话日志里的 command/run，mh 的采集从那里读出开关（mh/collect/dsh.py），
// 这里只给用户一行确认。
export const name = "mh-observe-switch";
export const inject = ["commands"];

const COMMANDS = [
  { name: "no-observe", description: "这个会话不让记忆观察（memory-harness）", text: "memory-harness: 已关闭对本会话的观察，本会话的信息将不再进入记忆。可使用 /do-observe 恢复。" },
  { name: "do-observe", description: "恢复记忆观察这个会话（memory-harness）", text: "memory-harness: 已恢复对本会话的观察。" },
];

export function apply(ctx) {
  ctx.effect(function* () {
    for (const c of COMMANDS) {
      yield ctx.commands.register({
        name: c.name,
        description: c.description,
        handler: ({ rawInput }) => rawInput.trim()
          ? { kind: "error", text: `用法：/${c.name}（不带参数）` }
          : { kind: "success", text: c.text },
      });
    }
  }, "mh-observe-switch commands");
}
