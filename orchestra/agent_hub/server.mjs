// Agent hub: one MCP server (stdio) that lets the Claude chat hand work to Codex, Copilot
// and Gemini on Chuen's laptop, and keeps one shared memory for all of them in the
// Obsidian vault (Atlas format).
//
// Memory rule (same idea as IRIS / Atrium: "one memory for every model"):
//   * every agent is told to read the vault's hot.md and AI-RULES.md before working
//   * agents do NOT write to the vault themselves; they end their answer with a
//     FOR MEMORY: section, and the hub files those lines, tagged with the agent, into
//     today's handoff note: <vault>/Calendar/Handoff-YYYY-MM-DD.md
//   * every hand-off (who, what, outcome) is logged there too, so the next chat and
//     every other agent can see what was done
//
// Extends ~/scripts/codex-bridge (same Codex call), no npm dependencies.
// Env: AGENT_HUB_VAULT (default: OneDrive KCL\Denisse), AGENT_HUB_FAKE=1 for tests.
import { spawn, execFileSync } from "node:child_process";
import { readFileSync, rmSync, mkdtempSync, existsSync, mkdirSync, appendFileSync, writeFileSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

const NPM = join(process.env.APPDATA || join(homedir(), ".npm-global"), "npm", "node_modules");
const VAULT = process.env.AGENT_HUB_VAULT || join(homedir(), "OneDrive - King's College London", "Denisse");
const DEFAULT_CWD = existsSync(join(homedir(), "GitHub")) ? join(homedir(), "GitHub") : homedir();
const TIMEOUT_MS = Number(process.env.AGENT_HUB_TIMEOUT_MS || 20 * 60 * 1000);
const FAKE = process.env.AGENT_HUB_FAKE === "1";

// ------------------------------------------------------------------ helpers

function binOf(pkg, name) {
  // Run the CLI's own JS entry with node, so long multi-line prompts never pass through cmd.exe.
  try {
    const dir = join(NPM, ...pkg.split("/"));
    const meta = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
    const rel = typeof meta.bin === "string" ? meta.bin : meta.bin?.[name];
    if (rel) return join(dir, rel);
  } catch {}
  return null;
}

function today() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return { date: `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`, time: `${p(d.getHours())}:${p(d.getMinutes())}` };
}

function oneLine(s, n = 140) {
  const t = String(s || "").replace(/\s+/g, " ").trim();
  return t.length > n ? t.slice(0, n - 1) + "…" : t;
}

export function handoffPath(date = today().date) {
  return join(VAULT, "Calendar", `Handoff-${date}.md`);
}

export function remember(agent, lines) {
  // Append-only, one line each, tagged with who wrote it. Never edits other notes.
  if (!lines.length || !existsSync(VAULT)) return false;
  const { date, time } = today();
  const file = handoffPath(date);
  mkdirSync(dirname(file), { recursive: true });
  if (!existsSync(file)) {
    writeFileSync(file, `---\nname: Handoff-${date}\ndescription: What every agent did on ${date}, one line each, tagged with who.\nupdated: ${date}\ntype: dated\nstatus: active\ntags: [handoff]\nprotection: append\n---\n\n` +
      `# Handoff ${date}\n\nOne line per decision, correction, hand-off or thing waiting on Chuen, tagged with who wrote it.\n\n## Log\n`, "utf8");
  }
  appendFileSync(file, lines.map((l) => `- ${time} [${agent}] ${oneLine(l, 400)}\n`).join(""), "utf8");
  return true;
}

export function memoryLines(answer) {
  // Lines under a "FOR MEMORY:" heading at the end of an agent's answer.
  const m = String(answer).match(/FOR MEMORY:?\s*\n([\s\S]*)$/i);
  if (!m) return [];
  return m[1].split("\n").map((l) => l.replace(/^[-*\s]+/, "").trim())
    .filter((l) => l && !/^none\.?$/i.test(l)).slice(0, 10);
}

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const pythonPath = () => process.env.AGENT_HUB_PYTHON
  || join(REPO, ".venv", process.platform === "win32" ? "Scripts\\python.exe" : "bin/python");

export function contextPack(lane) {
  // The shared brain: live state from the vault (python -m atrium context), so an agent
  // that can't or won't read the vault still starts from the same facts as everyone else.
  if (process.env.AGENT_HUB_CONTEXT === "0") return "";
  try {
    return execFileSync(pythonPath(), ["-m", "atrium", "context", "--lane", lane],
      { cwd: REPO, timeout: 10000, encoding: "utf8", windowsHide: true, env: { ...process.env, PYTHONIOENCODING: "utf-8" } });
  } catch {
    return "";
  }
}

function preamble(agent) {
  const lane = agent.toLowerCase().includes("copilot") ? "copilot" : agent.toLowerCase();
  const ctx = contextPack(lane);
  return [
    ...(ctx ? [ctx, "---", ""] : []),
    `You are ${agent}, one of Chuen's AI agents. Claude (the main chat) handed you this task; you cannot see that chat.`,
    `Shared memory: Chuen's Obsidian vault at ${VAULT}. If you can read files, read hot.md and AI-RULES.md there first (they may not exist yet).`,
    `Do not write to the vault yourself. If you learn something durable (a decision, a fact about Chuen's projects, a correction),`,
    `end your answer with a section "FOR MEMORY:" listing it, one line each, or "FOR MEMORY: none".`,
    `Never send messages, emails or posts, never spend money, never push to git.`,
    ``, `TASK:`, ``,
  ].join("\n");
}

function run(exe, args, { cwd, stdin, env }) {
  return new Promise((resolve) => {
    if (FAKE) {
      if ((process.env.AGENT_HUB_FAKE_FAIL || "").split(",").includes(exe))
        return resolve({ code: 1, out: "IneligibleTierError: This client is no longer supported", ms: 1 });
      const fake = JSON.parse(process.env.AGENT_HUB_FAKE_ANSWERS || "{}");
      return resolve({ code: 0, out: fake[exe] ?? `fake ${exe} answer\nFOR MEMORY:\n- fake fact from ${exe}`, ms: 1 });
    }
    const start = Date.now();
    const child = spawn(process.execPath, [exe, ...args], { cwd, windowsHide: true, env: { ...process.env, ...env } });
    let out = "";
    child.stdout.on("data", (d) => { out += d; });
    child.stderr.on("data", (d) => { out += d; });
    const timer = setTimeout(() => child.kill(), TIMEOUT_MS);
    child.on("error", (e) => { clearTimeout(timer); resolve({ code: -1, out: String(e), ms: Date.now() - start }); });
    child.on("close", (code) => { clearTimeout(timer); resolve({ code, out, ms: Date.now() - start }); });
    if (stdin !== undefined) child.stdin.end(stdin); else child.stdin.end();
  });
}

function workdir(cwd) {
  return cwd && existsSync(cwd) ? cwd : DEFAULT_CWD;
}

// ------------------------------------------------------------------ agents

const AGENTS = {
  async codex({ prompt, cwd, write = false, model }) {
    const exe = FAKE ? "codex" : binOf("@openai/codex", "codex");
    if (!exe) return { code: -1, out: "Codex CLI not found (npm i -g @openai/codex)." };
    const dir = mkdtempSync(join(tmpdir(), "agent-hub-"));
    const last = join(dir, "last.txt");
    const wd = workdir(cwd);
    const args = ["exec", "--skip-git-repo-check", "--color", "never", "-s", write ? "workspace-write" : "read-only",
      "-C", wd, "-o", last];
    if (model) args.push("-m", model);
    args.push("-");
    const r = await run(exe, args, { cwd: wd, stdin: preamble("Codex") + prompt });
    try { const a = readFileSync(last, "utf8").trim(); if (a) r.out = a; } catch {}
    try { rmSync(dir, { recursive: true, force: true }); } catch {}
    return r;
  },
  async gemini({ prompt, cwd, write = false, model }) {
    const exe = FAKE ? "gemini" : binOf("@google/gemini-cli", "gemini");
    if (!exe) return { code: -1, out: "Gemini CLI not found (npm i -g @google/gemini-cli)." };
    const wd = workdir(cwd);
    // plan = read-only; auto_edit = may edit files, no shell
    const args = ["--approval-mode", write ? "auto_edit" : "plan", "-o", "text", "-p", "Do the task given above."];
    if (existsSync(VAULT)) args.push("--include-directories", VAULT);
    if (model) args.push("-m", model);
    return run(exe, args, { cwd: wd, stdin: preamble("Gemini") + prompt });
  },
  async copilot({ prompt, cwd, write = false, model }) {
    const exe = FAKE ? "copilot" : binOf("@github/copilot", "copilot");
    if (!exe) return { code: -1, out: "Copilot CLI not found (npm i -g @github/copilot)." };
    const wd = workdir(cwd);
    const args = ["-p", preamble("GitHub Copilot") + prompt, "-s"];
    if (existsSync(VAULT)) args.push("--add-dir", VAULT);
    if (write) args.push("--allow-all-tools");  // without it, tools needing approval are refused
    if (model) args.push("--model", model);
    return run(exe, args, { cwd: wd });
  },
};

// Starting rules, by kind of work. Thomas's lesson: route by measured results, not by
// model names. Every routing is logged with its outcome, so these can be replaced by
// numbers once there are enough runs.
export const ROUTES = {
  code_change: { agent: "codex", write: true, why: "edits code in a repo and runs its tests" },
  debug: { agent: "codex", write: false, why: "reads code and finds the cause" },
  code_review: { agent: "codex", write: false, why: "reviews a diff or a file" },
  long_read: { agent: "gemini", write: false, why: "large context: long docs, many files, PDFs" },
  research: { agent: "gemini", write: false, why: "reads widely and summarises" },
  summarise: { agent: "gemini", write: false, why: "large context summaries" },
  github: { agent: "copilot", write: false, why: "issues, PRs, repo questions" },
  shell_help: { agent: "copilot", write: false, why: "commands and quick how-tos" },
  quick_code: { agent: "copilot", write: false, why: "a small snippet or explanation" },
};

export function chooseRoute(task, kind) {
  if (kind && ROUTES[kind]) return { kind, ...ROUTES[kind] };
  const t = String(task).toLowerCase();
  const rules = [
    [/\b(fix|implement|refactor|add (a )?test|rename|edit|change the code|write the code)\b/, "code_change"],
    [/\b(review|diff|pull request review)\b/, "code_review"],
    [/\b(bug|error|traceback|crash|why does|failing)\b/, "debug"],
    [/\b(pr|issue|github|repo settings|actions workflow)\b/, "github"],
    [/\b(command|powershell|terminal|shell|how do i run)\b/, "shell_help"],
    [/\b(summari[sz]e|tl;?dr)\b/, "summarise"],
    [/\b(pdf|paper|document|long|read all|compare these)\b/, "long_read"],
    [/\b(research|find out|look into|options for)\b/, "research"],
  ];
  for (const [re, k] of rules) if (re.test(t)) return { kind: k, ...ROUTES[k], guessed: true };
  return { kind: "quick_code", ...ROUTES.quick_code, guessed: true };
}

// When an agent can't take work (signed out, quota, service refused), its job goes to
// the next one here instead of failing. 2 Nov 2026: Google stopped serving the free
// "Gemini Code Assist for individuals" tier to Gemini CLI, so Gemini falls back to Codex.
const FALLBACK = { gemini: "codex", copilot: "codex" };

async function dispatch(agent, args, label) {
  const fn = AGENTS[agent];
  if (!fn) return `Unknown agent: ${agent}`;
  const r = await fn(args);
  const fb = FALLBACK[agent];
  if (r.code !== 0 && fb && !args._fellBack) {
    const why = oneLine(String(r.out).match(/(IneligibleTier|not (signed|logged) in|login|quota|rate limit|unauthori[sz]ed)[^\n]*/i)?.[0] || `exit ${r.code}`, 140);
    remember("claude", [`${agent} couldn't take "${oneLine(args.prompt, 80)}" (${why}); sent to ${fb} instead`]);
    const out = await dispatch(fb, { ...args, _fellBack: true }, `${label || "task"} (fallback from ${agent})`);
    return `(${agent} unavailable: ${why}. ${fb} did it instead.)\n${out}`;
  }
  const ok = r.code === 0;
  const answer = String(r.out || "").trim() || `${agent} returned nothing (exit ${r.code}).`;
  const mem = ok ? memoryLines(answer) : [];
  remember(agent, [
    `${label || "task"}: ${oneLine(args.prompt, 120)} → ${ok ? "done" : `FAILED (exit ${r.code})`}, ${Math.round((r.ms || 0) / 1000)}s`,
    ...mem,
  ]);
  return `[${agent}${ok ? "" : `, exit ${r.code}`}]\n${answer}`;
}

// ------------------------------------------------------------------ MCP

const taskProps = {
  prompt: { type: "string", description: "Full, self-contained instructions. The agent cannot see this chat." },
  cwd: { type: "string", description: "Absolute Windows folder to work in. Defaults to C:\\Users\\Xichu\\GitHub." },
  write: { type: "boolean", description: "Allow the agent to edit files in cwd. Default false (read-only)." },
  model: { type: "string", description: "Optional model override." },
};

export const TOOLS = [
  { name: "ask_codex", description: "Hand a task to OpenAI Codex. Best for changing code in a repo, debugging, code review.",
    inputSchema: { type: "object", properties: taskProps, required: ["prompt"] } },
  { name: "ask_gemini", description: "Hand a task to Google Gemini. Best for long documents, many files at once, research and summaries.",
    inputSchema: { type: "object", properties: taskProps, required: ["prompt"] } },
  { name: "ask_copilot", description: "Hand a task to GitHub Copilot. Best for GitHub questions (issues, PRs), shell commands and quick code snippets.",
    inputSchema: { type: "object", properties: taskProps, required: ["prompt"] } },
  { name: "route_task", description: "Let the hub pick the agent for a task (codex, gemini or copilot) from its kind, run it, and say who did it and why. Use this by default when Chuen asks for work that another agent can do.",
    inputSchema: { type: "object", properties: { ...taskProps,
      kind: { type: "string", enum: Object.keys(ROUTES), description: "What sort of work this is. Leave out to let the hub guess from the prompt." } },
      required: ["prompt"] } },
  { name: "split_tasks", description: "Run several sub-tasks at once, each routed to the best agent (max 5). Give each its own cwd, or keep them read-only, so they don't edit the same files.",
    inputSchema: { type: "object", properties: { tasks: { type: "array", maxItems: 5, items: { type: "object",
      properties: { ...taskProps, kind: { type: "string", enum: Object.keys(ROUTES) } }, required: ["prompt"] } } }, required: ["tasks"] } },
  { name: "ask_all", description: "Ask Codex, Gemini and Copilot the same question at once, read-only, for second opinions.",
    inputSchema: { type: "object", properties: { prompt: taskProps.prompt, cwd: taskProps.cwd }, required: ["prompt"] } },
  { name: "remember", description: "File durable facts or decisions into the shared vault's handoff note for today, tagged [claude]. One line each.",
    inputSchema: { type: "object", properties: { lines: { type: "array", items: { type: "string" } } }, required: ["lines"] } },
  { name: "memory_today", description: "Read the vault's hot.md, STATE NOW and today's handoff note, so the chat starts from shared live state.",
    inputSchema: { type: "object", properties: {} } },
];

export async function callTool(name, a = {}) {
  if (name === "ask_codex") return dispatch("codex", a, "ask_codex");
  if (name === "ask_gemini") return dispatch("gemini", a, "ask_gemini");
  if (name === "ask_copilot") return dispatch("copilot", a, "ask_copilot");
  if (name === "route_task" || name === "split_tasks") {
    const tasks = name === "route_task" ? [a] : (a.tasks || []).slice(0, 5);
    const outs = await Promise.all(tasks.map((t) => {
      const r = chooseRoute(t.prompt, t.kind);
      const label = `routed ${r.kind}${r.guessed ? " (guessed)" : ""} → ${r.agent}`;
      return dispatch(r.agent, { ...t, write: t.write ?? r.write }, label)
        .then((out) => `### ${r.agent} — ${r.kind}${r.guessed ? " (guessed)" : ""}: ${r.why}\n${out}`);
    }));
    return outs.join("\n\n");
  }
  if (name === "ask_all") {
    const outs = await Promise.all(["codex", "gemini", "copilot"].map((ag) =>
      dispatch(ag, { prompt: a.prompt, cwd: a.cwd, write: false }, "ask_all")));
    return outs.join("\n\n");
  }
  if (name === "remember") return remember("claude", a.lines || []) ? "Filed." : `Vault not found at ${VAULT}.`;
  if (name === "memory_today") {
    const ctx = contextPack("claude");
    if (ctx) return ctx;
    const read = (p, n = 6000) => { try { return readFileSync(p, "utf8").slice(0, n); } catch { return "(missing)"; } };
    return [`## hot.md\n${read(join(VAULT, "hot.md"))}`, `## STATE NOW\n${read(join(VAULT, "STATE NOW.md"))}`,
      `## Today's handoff\n${read(handoffPath(), 8000)}`].join("\n\n");
  }
  throw new Error(`Unknown tool: ${name}`);
}

const send = (msg) => process.stdout.write(JSON.stringify(msg) + "\n");

if (process.argv[1] && import.meta.url.endsWith(process.argv[1].replace(/\\/g, "/").split("/").pop())) {
  createInterface({ input: process.stdin }).on("line", async (line) => {
    let msg;
    try { msg = JSON.parse(line); } catch { return; }
    const { id, method, params } = msg;
    if (id === undefined) return;
    try {
      if (method === "initialize") {
        send({ jsonrpc: "2.0", id, result: { protocolVersion: params?.protocolVersion || "2025-06-18",
          capabilities: { tools: {} }, serverInfo: { name: "agent-hub", version: "1.0.0" } } });
      } else if (method === "tools/list") {
        send({ jsonrpc: "2.0", id, result: { tools: TOOLS } });
      } else if (method === "tools/call") {
        const text = await callTool(params.name, params.arguments);
        send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text }] } });
      } else if (method === "ping") {
        send({ jsonrpc: "2.0", id, result: {} });
      } else {
        send({ jsonrpc: "2.0", id, error: { code: -32601, message: `Method not found: ${method}` } });
      }
    } catch (e) {
      send({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: String(e?.message || e) }], isError: true } });
    }
  });
}
