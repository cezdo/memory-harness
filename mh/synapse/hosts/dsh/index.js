// memory-harness Synapse 接入 dsh（web profile 的 bundle；2026-10-15：把 Claude 那套推送换进 dsh，前后对照）。
//
// 三件事，Python 侧与 Claude 共用同一套入口，只在 JSON 里多带 host: "dsh"：
// - 开场推送：agent/created → `python3 -m mh inject`（开场映像：内核 + 工具 + 项目现状 + 环境），作为一条 user 消息注入；
// - 联想：agent/pre-step → `python3 -m mh.synapse.push.associate`。只把 source.kind 为 user 的话送去联想；
//   goal、子代理通知、别的 agent 发来的消息记成宿主事件（host_kind），工作区说明、运行时快照这类每步都有的不送；
// - 拉取：自己连 `python3 -m mh.synapse.pull.mcp`（stdio，一行一条 JSON-RPC），工具名照 Claude 的 mcp__mh__search/get/map，
//   定义取自服务的 tools/list；每次调用把会话编号放进参数 session、会话目录放进 cwd。
//   不用 dsh-mcp-client：一个进程里的会话共用一个服务进程、环境被清洗，服务拿不到是哪段会话在拉取。
//
// 不走 dsh-hooks-claude-code 桥接：桥接把一步里所有待处理消息不分来源拼成 prompt（工作区说明会触发联想），
// SessionStart 也是脱离运行、可能赶不上第一轮。这里 agent/created 被等待，开场映像一定在第一轮之前。
//
// 只用 node 内置模块：本包经 link: 指向仓库，import 按真实路径解析，找不到 profile 的 node_modules。
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

export const name = "mh-synapse";
export const inject = ["tools"];

// 仓库根：本包在仓库的 mh/synapse/hosts/dsh/ 下，从模块目录往上找含 mh/__init__.py 的那一层。
// 不写死层数：E2 把插件从 mh/synapse/dsh/ 搬到 hosts/dsh/ 后，三层上变成了 mh/，
// 三个 python 入口全部 No module named mh，开场推送、联想、拉取一起静默失效（2026-10-22）。
function repoRoot(start) {
  for (let d = start; ; d = dirname(d)) {
    if (existsSync(join(d, "mh", "__init__.py"))) return d;
    if (dirname(d) === d) return start;
  }
}

const DEFAULTS = {
  repo: repoRoot(fileURLToPath(new URL(".", import.meta.url))).replace(/\/$/, ""),   // 本包所在的仓库（link: 指到哪个检出就用哪个）
  python: "/usr/bin/python3",
  injectTimeoutMs: 30000,
  associateTimeoutMs: 30000,
  toolTimeoutMs: 60000,
  resume: false,
  subagents: false,
};
// 每一步都可能带上的宿主内容，不是事件：不联想，也不记宿主事件
const QUIET = new Set(["agent-instructions", "runtime-context", "ptc-mode", "hooks-claude-code", "mh-synapse",
  "memory-souhatsu"]);

function childEnv(cfg) {
  const env = { ...process.env, PYTHONPATH: cfg.repo };
  delete env.CLAUDE_CODE_SESSION_ID;   // dsh web 若从 Claude Code 里启动，会继承那段 Claude 会话的编号
  return env;
}

function runPy(cfg, args, input, timeoutMs, signal) {
  return new Promise((resolve) => {
    const p = spawn(cfg.python, args, { cwd: cfg.repo, env: childEnv(cfg), stdio: ["pipe", "pipe", "pipe"] });
    let out = "", err = "", done = false;
    const finish = (r) => { if (!done) { done = true; clearTimeout(timer); signal?.removeEventListener("abort", onAbort); resolve(r); } };
    const timer = setTimeout(() => { p.kill("SIGKILL"); finish({ ok: false, out, err: err + `\n超时 ${timeoutMs}ms` }); }, timeoutMs);
    const onAbort = () => { p.kill("SIGKILL"); finish({ ok: false, out, err: "取消" }); };
    signal?.addEventListener("abort", onAbort, { once: true });
    p.stdout.on("data", (d) => { out += d; });
    p.stderr.on("data", (d) => { err += d; });
    p.on("error", (e) => finish({ ok: false, out, err: String(e) }));
    p.on("close", (code) => finish({ ok: code === 0, out, err }));
    p.stdin.end(JSON.stringify(input));
  });
}

function contextOf(r) {
  if (!r.out.trim()) return "";
  return JSON.parse(r.out)?.hookSpecificOutput?.additionalContext || "";
}

// 和 Claude Code 给模型看的样子一致（hook 上下文进 <system-reminder>，带事件名），两边对照时包装不是变量
function message(event, form, text) {
  return {
    id: randomUUID(), role: "user",
    content: [{ type: "text", text: `<system-reminder>\n${event} hook additional context: ${text}\n</system-reminder>` }],
    source: { kind: "mh-synapse", form },
  };
}

const textOf = (m) => (m?.content || []).filter((b) => b?.type === "text").map((b) => b.text).join("\n").trim();
const isSubagent = (h) => h?.origin === "subagent" || (h?.delegationDepth ?? 0) > 0;

// 拉取服务：一个常驻子进程，退出了下次调用再拉起
function mcpClient(cfg, logger) {
  let proc = null, buf = "", nextId = 1;
  const pending = new Map();
  function start() {
    proc = spawn(cfg.python, ["-m", "mh.synapse.pull.mcp"], { cwd: cfg.repo, env: childEnv(cfg), stdio: ["pipe", "pipe", "pipe"] });
    buf = "";
    proc.stdout.setEncoding("utf8");
    proc.stdout.on("data", (d) => {
      buf += d;
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (!line) continue;
        let msg;
        try { msg = JSON.parse(line); } catch { continue; }
        const w = pending.get(msg.id);
        if (w) { pending.delete(msg.id); w(msg); }
      }
    });
    proc.stderr.on("data", (d) => logger.warn(`mh-synapse(mcp): ${String(d).trim().slice(0, 500)}`));
    const dead = proc;
    proc.on("exit", () => {
      if (proc === dead) proc = null;
      for (const [id, w] of pending) { pending.delete(id); w({ error: { message: "拉取服务退出了" } }); }
    });
  }
  function request(method, params, timeoutMs, signal) {
    if (!proc) start();
    const id = nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`拉取服务 ${method} 超时 ${timeoutMs}ms`)); }, timeoutMs);
      const onAbort = () => { clearTimeout(timer); pending.delete(id); reject(new Error("取消")); };
      signal?.addEventListener("abort", onAbort, { once: true });
      pending.set(id, (msg) => {
        clearTimeout(timer);
        signal?.removeEventListener("abort", onAbort);
        msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
      });
      proc.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n");
    });
  }
  return {
    request,
    stop() { proc?.kill(); proc = null; },
  };
}

// 旧联想器的影子。照用旧模块自己的词表、别名与匹配（ensureTables、collectHits），条数也照旧（max(3, ⌈候选/3⌉)）；
export async function apply(ctx, config) {
  const cfg = { ...DEFAULTS, ...(config || {}) };
  const log = ctx.logger;
  // 仓库根认错时三个入口会一起失败，只剩日志里的一行 warn；启动时先说清楚（2026-10-22 的 E2 就是这样漏过去的）
  if (!existsSync(join(cfg.repo, "mh", "__init__.py"))) log.warn(`mh-synapse: 仓库根不对（${cfg.repo} 下没有 mh/__init__.py），python 入口会失败`);

  // 开场推送
  ctx.on("agent/created", async ({ agent, source }) => {
    try {
      const h = agent.session.header;
      if ((!cfg.subagents && isSubagent(h)) || (!cfg.resume && source === "resume")) return;
      const r = await runPy(cfg, ["-m", "mh", "inject"], {
        session_id: h.id, cwd: h.cwd ?? "", source, host: "dsh", hook_event_name: "SessionStart", transcript_path: "",
      }, cfg.injectTimeoutMs);
      if (!r.ok) log.warn(`mh-synapse: 开场推送失败：${r.err.slice(-500)}`);
      const text = contextOf(r);
      if (text) agent.inject(message("SessionStart", "assist", text));
    } catch (e) {
      log.warn(`mh-synapse: 开场推送出错：${e?.message || e}`);
    }
  });

  // 联想
  ctx.on("agent/pre-step", async (payload, next) => {
    const decision = await next();
    try {
      if (decision.kind !== "enter") return decision;
      const h = payload.agent?.session?.header;
      if (!cfg.subagents && isSubagent(h)) return decision;
      const user = [], events = [];
      for (const m of payload.messages || []) {
        const kind = m?.source?.kind, text = textOf(m);
        if (!text) continue;
        if (kind === "user") user.push(text);
        else if (!QUIET.has(kind)) events.push([kind, text]);
      }
      let body;
      if (user.length) body = { prompt: user.join("\n") };
      else if (events.length) body = { prompt: events.map((e) => e[1]).join("\n"), host_kind: "dsh:" + events.map((e) => e[0]).join("+") };
      else return decision;
      const r = await runPy(cfg, ["-m", "mh.synapse.push.associate"], {
        ...body, session_id: h?.id ?? "", cwd: h?.cwd ?? "", host: "dsh", hook_event_name: "UserPromptSubmit", transcript_path: "",
      }, cfg.associateTimeoutMs, payload.signal);
      const text = contextOf(r);
      if (!text) return decision;
      return { ...decision, messages: [...decision.messages, message("UserPromptSubmit", "associate", text)] };
    } catch (e) {
      log.warn(`mh-synapse: 联想出错：${e?.message || e}`);
      return decision;
    }
  });

  // 拉取工具
  const mcp = mcpClient(cfg, log);
  ctx.effect(() => () => mcp.stop(), "mh-synapse: stop pull server");
  let tools = [];
  try {
    await mcp.request("initialize", { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "dsh-mh-synapse", version: "0.6.0" } }, cfg.toolTimeoutMs);
    tools = (await mcp.request("tools/list", {}, cfg.toolTimeoutMs)).tools || [];
  } catch (e) {
    log.warn(`mh-synapse: 拉取服务没起来，不注册拉取工具：${e?.message || e}`);
  }
  for (const t of tools) {
    ctx.tools.register({
      name: `mcp__mh__${t.name}`,
      description: t.description || "",
      parameters: t.inputSchema,
      output: {
        schema: { type: "object", additionalProperties: false, properties: { text: { type: "string" } }, required: ["text"] },
        render: (_args, value) => [{ type: "text", text: value.text }],
      },
      async execute(args, exec) {
        const h = exec?.agent?.session?.header;
        const res = await mcp.request("tools/call", {
          name: t.name, arguments: { ...args, session: h?.id ?? "", cwd: h?.cwd ?? "" },
        }, cfg.toolTimeoutMs, exec?.signal);
        const text = (res?.content || []).filter((b) => b?.type === "text").map((b) => b.text).join("\n");
        if (res?.isError) throw new Error(text || "拉取出错");
        return { text };
      },
    });
  }
}
