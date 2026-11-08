// dsh 插件的测试：node mh/synapse/hosts/dsh/test.mjs（不依赖 dsh，事件照 session.v4 的形状造）
import assert from "node:assert/strict";
import { unseen } from "./index.js";

const s = (evs) => ({ ownEvents: () => evs });
const ins = { type: "agent/inbox/spliced", data: { target: "next-step", start: 0, inserted: [{ source: { kind: "mh-synapse", form: "assist" } }] } };
const cancel = { type: "agent/inbox/spliced", data: { target: "next-step", start: 0, removedCount: 1, inserted: [], outcome: "canceled" } };
const turn = { type: "turn/start", data: { turn: 1 } };

// 恢复时还没开始过一轮、手记也没挂在待发里才补推（2026-11-08：dsh web 重启撤掉待发的手记，恢复不推，整段没有手记）
assert.equal(unseen(s([])), true);
assert.equal(unseen(s([ins])), false);              // 手记还挂着，不重复推
assert.equal(unseen(s([ins, cancel])), true);       // 被撤了：补推
assert.equal(unseen(s([ins, cancel, turn])), false); // 已经开始过一轮
assert.equal(unseen(undefined), true);
console.log("dsh 插件：ok");
