// Agent hub: one MCP server (stdio) that lets the Claude chat hand work to Codex, Copilot
// and Gemini on Chuen's laptop, and keeps one shared memory for all of them in the
// Obsidian vault (Atlas format). Since 5 Nov 2026 it also asks the study tutor (ask_don,
// python -m tutor consult): study-category work is put to the tutor first, and the agent
// that does the work gets the tutor's opinion as a subject expert's -- advisory, never an
// override (routing data: orchestra/exams/rules.json).
//
// Memory rule (same idea as IRIS / Atrium: "one memory for every model"):
//   * every agent is told to read the vault's hot.md and AI-RULES.md before working
//   * agents do NOT write to the vault themselves; they end their answer with a
//     FOR MEMORY: section, and the hub files those lines, tagged with the agent, into
//     today's handoff note: <vault>/Calendar/Handoff-YYYY-MM-DD.md
//   * every hand-off (who, what, outcome) is logged there too, so the next chat and
//     every other agent can see what was done
//
// Background jobs: the desktop bridge cancels any tool call that runs past 60 s, and a real
// Codex run takes minutes. So ask_* / route_task / split_tasks / ask_all wait up to wait_s (45 s) and,
// if the agent isn't done, hand back a job_id while the agent keeps running. Claude then
// polls job_status and fetches job_result. Jobs persist to .review/hub/jobs/<id>.json +
// <id>.out (live output), so a hub restart keeps finished results; finished jobs older than
// AGENT_HUB_JOB_KEEP_DAYS (default 7) are pruned at start and hourly. Finished output
// logs retain at most their last 2 MiB; truncation is recorded in the job JSON.
//
// Extends ~/scripts/codex-bridge (same Codex call), no npm dependencies.
// Env: AGENT_HUB_VAULT (default: OneDrive KCL\Denisse), AGENT_HUB_FAKE=1 for tests
// (AGENT_HUB_FAKE_CMD=<node script> makes the fakes real child processes),
// AGENT_HUB_JOBS_DIR, AGENT_HUB_JOB_TIMEOUT_MS (default 15 min).
import { spawn, execFileSync } from "node:child_process";
import { readFileSync, rmSync, mkdtempSync, existsSync, mkdirSync, appendFileSync, writeFileSync, readdirSync,
  lstatSync, openSync, readSync, closeSync, renameSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";
import { AsyncLocalStorage } from "node:async_hooks";
import { randomBytes } from "node:crypto";

const CONFIG = join(dirname(fileURLToPath(import.meta.url)), "..", "config.json");
const VAULT = process.env.AGENT_HUB_VAULT || join(homedir(), "OneDrive - King's College London", "Denisse");
const DEFAULT_CWD = existsSync(join(homedir(), "GitHub")) ? join(homedir(), "GitHub") : homedir();
const TIMEOUT_MS = Number(process.env.AGENT_HUB_TIMEOUT_MS || 20 * 60 * 1000);
const FAKE = process.env.AGENT_HUB_FAKE === "1";

// ------------------------------------------------------------------ helpers

export function npmDirs(patterns) {
  // The same list the Orchestra runner searches (config.json "npm_dirs", runner.npm_dirs):
  // %VAR% and ~ expanded, * matched within one path segment, an entry with an unset variable
  // skipped. npm installs made from the Claude desktop app live in its packaged AppData, which
  // the APPDATA npm folder alone never finds (3 Nov 2026).
  if (!patterns) {
    try { patterns = JSON.parse(readFileSync(CONFIG, "utf8")).npm_dirs; } catch {}
    if (!Array.isArray(patterns)) patterns = [];
  }
  const out = [];
  for (const pat of patterns) {
    let unset = false;
    let s = String(pat).replace(/%([^%]+)%/g, (_, v) => process.env[v] ?? (unset = true, ""));
    if (unset) continue;
    if (s === "~" || s.startsWith("~/") || s.startsWith("~\\")) s = join(homedir(), s.slice(1));
    const parts = s.split(/[\\/]+/);
    let paths = [parts[0] === "" ? "/" : parts[0] + (parts[0].endsWith(":") ? "\\" : "")];
    for (const part of parts.slice(1)) {
      if (!part) continue;
      if (!part.includes("*")) { paths = paths.map((p) => join(p, part)); continue; }
      const re = new RegExp("^" + part.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*") + "$", "i");
      paths = paths.flatMap((p) => { try { return readdirSync(p).filter((n) => re.test(n)).sort().map((n) => join(p, n)); } catch { return []; } });
    }
    for (const p of paths) { try { if (lstatSync(p).isDirectory() && !out.includes(p)) out.push(p); } catch {} }
  }
  return out;
}

export function binOf(pkg, name, dirs = npmDirs()) {
  // Run the CLI's own JS entry with node, so long multi-line prompts never pass through cmd.exe.
  for (const npm of dirs) {
    try {
      const dir = join(npm, "node_modules", ...pkg.split("/"));
      const meta = JSON.parse(readFileSync(join(dir, "package.json"), "utf8"));
      const rel = typeof meta.bin === "string" ? meta.bin : meta.bin?.[name];
      if (rel) return join(dir, rel);
    } catch {}
  }
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

// The study tutor (tutor/, study tutor 5/5; Chuen, 5 Nov 2026: "treat Don like a professional
// opinion on task"). Its name is tutor.json's, read once; the agent key stays "don" (ask_don).
const TUTOR_KEY = "don";
const TUTOR = (() => {
  try { return String(JSON.parse(readFileSync(join(REPO, "tutor", "tutor.json"), "utf8")).name || TUTOR_KEY); }
  catch { return TUTOR_KEY; }
})();

export function renderOpinion(out) {
  // python -m tutor consult --json prints one JSON object (the last line); anything else is shown as is.
  let data;
  for (const line of String(out || "").trim().split(/\r?\n/).reverse()) {
    const t = line.trim();
    if (!t.startsWith("{")) continue;
    try { const d = JSON.parse(t); if (d && typeof d === "object" && "answer" in d) { data = d; break; } } catch {}
  }
  if (!data) return String(out || "").trim();
  const list = (xs, none) => (Array.isArray(xs) && xs.length ? xs.join(" · ") : none);
  return [`${TUTOR} says (${data.confidence} confidence, answered by ${data.who}${data.module ? `, ${data.module}` : ""}): ${data.answer}`,
    `Sources: ${list(data.sources, "none")}`, `Caveats: ${list(data.caveats, "none")}`,
    `(${TUTOR}'s view from Chuen's own notes: weigh it as a subject expert's opinion, advisory, never an override.)`].join("\n");
}

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

export function killTree(pid) {
  // child.kill() on Windows leaves the CLI's own children running; taskkill /T takes the tree.
  if (!pid) return;
  try {
    if (process.platform === "win32") execFileSync("taskkill", ["/T", "/F", "/PID", String(pid)], { stdio: "ignore", windowsHide: true });
    else process.kill(pid, "SIGKILL");
  } catch {}
}

function run(exe, args, { cwd, stdin, env }) {
  const job = jobCtx.getStore();  // set when this run belongs to a background job
  return new Promise((resolve) => {
    if (FAKE) {
      if ((process.env.AGENT_HUB_FAKE_FAIL || "").split(",").includes(exe))
        return resolve({ code: 1, out: "IneligibleTierError: This client is no longer supported", ms: 1 });
      if (!process.env.AGENT_HUB_FAKE_CMD) {
        const fake = JSON.parse(process.env.AGENT_HUB_FAKE_ANSWERS || "{}");
        return resolve({ code: 0, out: fake[exe] ?? `fake ${exe} answer\nFOR MEMORY:\n- fake fact from ${exe}`, ms: 1 });
      }
    }
    if (job?.killed) return resolve({ code: -1, out: `job ${job.rec.id} was stopped: ${job.rec.error}`, ms: 0 });
    const start = Date.now();
    const argv = FAKE ? [process.env.AGENT_HUB_FAKE_CMD, exe, ...args] : [exe, ...args];
    const child = spawn(process.execPath, argv, { cwd, windowsHide: true, env: { ...process.env, ...env } });
    job?.children.add(child);
    let out = "";
    const onData = (d) => { out += d; job?.log(d); };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.stdin.on("error", () => {});  // EPIPE if the child is killed before reading its prompt
    // A job has its own timeout (and kills the tree itself); a bare call keeps the old one.
    const timer = job ? null : setTimeout(() => killTree(child.pid), TIMEOUT_MS);
    const done = (r) => { clearTimeout(timer); job?.children.delete(child); resolve(r); };
    child.on("error", (e) => done({ code: -1, out: String(e), ms: Date.now() - start }));
    child.on("close", (code) => done({ code, out, ms: Date.now() - start }));
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
  async don({ prompt, module }) {
    // The study tutor's opinion: python -m tutor consult, grounded in the vault, synchronous. No
    // preamble and no FOR MEMORY protocol: the consult records itself in the vault's Don area.
    const exe = FAKE ? TUTOR_KEY : pythonPath();
    if (!FAKE && !existsSync(exe)) return { code: -1, out: `${TUTOR} not available: ${exe} not found.` };
    const args = ["-m", "tutor", "consult", String(prompt || ""), "--json"];
    if (module) args.push("--module", String(module));
    const r = await run(exe, args, { cwd: REPO, env: { PYTHONIOENCODING: "utf-8" } });
    r.out = renderOpinion(r.out);
    return r;
  },
};

// Study-category work (the exams' routing data, orchestra/exams/rules.json: kind_to_category and
// title_routes, first match wins -- the same rules lane_for.py applies) goes to the tutor first.
const RULES = process.env.AGENT_HUB_RULES || join(REPO, "orchestra", "exams", "rules.json");
let rulesCache;
function rules() {
  if (rulesCache === undefined) {
    try { rulesCache = JSON.parse(readFileSync(RULES, "utf8")).routing || null; } catch { rulesCache = null; }
  }
  return rulesCache;
}

export function categoryOf(task, kind) {
  const r = rules();
  if (!r) return null;
  if (kind) return r.kind_to_category?.[String(kind).toLowerCase().replace(/-/g, "_")] ?? null;
  const t = String(task || "").toLowerCase();
  for (const [pat, cat] of r.title_routes || []) {
    try { if (new RegExp(pat, "i").test(t)) return cat; } catch {}
  }
  return null;
}

export const STUDY = "study";
export const isStudy = (task, kind) => categoryOf(task, kind) === STUDY;

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
  study: { agent: "codex", write: false, why: "about Chuen's modules or revision: the study tutor is asked first, then the work is done with that opinion in hand" },
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
  // Study work (data: the exams' title routes) before the quick-snippet default, so a question
  // about his modules or revision is never mistaken for a code snippet. Whatever the kind, study-
  // category work is put to the tutor first (routed()).
  if (isStudy(task)) return { kind: STUDY, ...ROUTES[STUDY], guessed: true };
  return { kind: "quick_code", ...ROUTES.quick_code, guessed: true };
}

// Measured routing (orchestra/exams, after Thomas's Atrium): when .review/exams/scores.json
// exists, the best working agent for the kind's exam category takes the job
// (python -m orchestra.exams best <kind> --among codex,gemini,copilot). No scores, no
// category for the kind, or nobody working has passed one: the fixed rules above decide.
const HUB_AGENTS = ["codex", "gemini", "copilot"];
export const scoresPath = () => process.env.AGENT_HUB_EXAM_SCORES || join(REPO, ".review", "exams", "scores.json");

export function scoredRoute(r) {
  const file = scoresPath();
  if (!existsSync(file)) return r;
  let b;
  try {
    const out = execFileSync(pythonPath(), ["-m", "orchestra.exams", "--scores", file, "best", r.kind,
      "--among", HUB_AGENTS.join(","), "--json"],
    { cwd: REPO, timeout: 10000, encoding: "utf8", windowsHide: true, env: { ...process.env, PYTHONIOENCODING: "utf-8" } });
    b = JSON.parse(out.trim().split("\n").pop());
  } catch {
    return r;  // exit 1 = nobody measured; python missing = today's rules
  }
  if (!b?.agent || !AGENTS[b.agent]) return r;
  return { ...r, agent: b.agent, scored: true,
    why: `best exam score for ${b.category}: ${b.passed}/${b.n} passed, median ${Math.round(b.median_s)}s` };
}

export function pickRoute(task, kind) {
  return scoredRoute(chooseRoute(task, kind));
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
  const job = jobCtx.getStore();
  if (r.code !== 0 && fb && !args._fellBack && !job?.killed) {
    const why = oneLine(String(r.out).match(/(IneligibleTier|not (signed|logged) in|login|quota|rate limit|unauthori[sz]ed)[^\n]*/i)?.[0] || `exit ${r.code}`, 140);
    remember("claude", [`${agent} couldn't take "${oneLine(args.prompt, 80)}" (${why}); sent to ${fb} instead`]);
    const out = await dispatch(fb, { ...args, _fellBack: true }, `${label || "task"} (fallback from ${agent})`);
    return `(${agent} unavailable: ${why}. ${fb} did it instead.)\n${out}`;
  }
  const ok = r.code === 0;
  if (job) job[ok ? "ok" : "failed"] += 1;
  const answer = String(r.out || "").trim() || `${agent} returned nothing (exit ${r.code}).`;
  const mem = ok ? memoryLines(answer) : [];
  remember(agent, [
    `${label || "task"}: ${oneLine(args._logPrompt ?? args.prompt, 120)} → ${ok ? "done" : `FAILED (exit ${r.code})`}, ${Math.round((r.ms || 0) / 1000)}s`,
    ...mem,
  ]);
  return `[${agent}${ok ? "" : `, exit ${r.code}`}]\n${answer}`;
}

// ------------------------------------------------------------------ background jobs

const JOBS_DIR = process.env.AGENT_HUB_JOBS_DIR || join(REPO, ".review", "hub", "jobs");
const JOB_TIMEOUT_MS = Number(process.env.AGENT_HUB_JOB_TIMEOUT_MS || 15 * 60 * 1000);
const JOB_RETENTION_DAYS = 7;  // overridden by AGENT_HUB_JOB_KEEP_DAYS
const MAX_OUTPUT_BYTES = 2 * 1024 * 1024;
const MAX_JOBS = 4;
const DEFAULT_WAIT_S = 45;  // under the bridge's 60 s cut-off
const HUB_ID = randomBytes(6).toString("hex");
const jobs = new Map();  // id -> live job started by this hub process
const jobCtx = new AsyncLocalStorage();

const jobFile = (id, ext) => join(JOBS_DIR, `${id}.${ext}`);
const nowIso = () => new Date().toISOString();

function writeRec(rec) {
  mkdirSync(JOBS_DIR, { recursive: true });
  writeFileSync(jobFile(rec.id, "json"), JSON.stringify(rec, null, 2), "utf8");
}

function truncateOutput(rec) {
  const file = jobFile(rec.id, "out");
  let fd, temp;
  try {
    // Do not follow links or load an arbitrarily large log into memory.
    const info = lstatSync(file);
    if (!info.isFile() || info.size <= MAX_OUTPUT_BYTES) return;
    fd = openSync(file, "r");
    const buf = Buffer.alloc(MAX_OUTPUT_BYTES);
    let read = 0;
    while (read < buf.length) {
      const n = readSync(fd, buf, read, buf.length - read, info.size - buf.length + read);
      if (!n) throw new Error("output changed while truncating");
      read += n;
    }
    closeSync(fd);
    fd = undefined;
    // Replace the directory entry, so even a hard link cannot overwrite an outside file.
    temp = jobFile(rec.id, `out-${randomBytes(6).toString("hex")}.tmp`);
    writeFileSync(temp, buf, { flag: "wx" });
    renameSync(temp, file);
    Object.assign(rec, { output_truncated: true, output_original_bytes: info.size,
      output_retained_bytes: MAX_OUTPUT_BYTES });
  } catch (e) {
    if (e.code !== "ENOENT") rec.output_truncation_error = String(e.message || e);
  } finally {
    if (fd !== undefined) closeSync(fd);
    if (temp) { try { rmSync(temp, { force: true }); } catch {} }
  }
}

function pidAlive(pid) {
  try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; }
}

function readRec(id) {
  // A job on disk that says "running" but whose hub is gone died with that hub.
  if (!/^[\w-]+$/.test(String(id))) return null;
  let rec;
  try { rec = JSON.parse(readFileSync(jobFile(id, "json"), "utf8")); } catch { return null; }
  const otherLiveHub = rec.hub_pid !== process.pid && pidAlive(rec.hub_pid);
  if (rec.status === "running" && rec.hub_id !== HUB_ID && !otherLiveHub) {
    Object.assign(rec, { status: "failed", finished: nowIso(),
      error: "the hub stopped while this job was running, and the agent's process died with it; ask again if still needed" });
    writeRec(rec);
  }
  return rec;
}

const getRec = (id) => jobs.get(id)?.rec || readRec(id);

function tail(id, n = 20) {
  try {
    return readFileSync(jobFile(id, "out"), "utf8").split(/\r?\n/).filter((l) => l.trim()).slice(-n).join("\n");
  } catch { return ""; }
}

const elapsed = (rec) => Math.round(((rec.finished ? Date.parse(rec.finished) : Date.now()) - Date.parse(rec.started)) / 1000);

function resultText(rec) {
  if (rec.status === "done") return rec.result;
  if (rec.status === "running") return `Job ${rec.id} (${rec.agent}) is still running, ${elapsed(rec)}s so far. Poll job_status("${rec.id}"); don't ask again, that starts a second run.`;
  const out = rec.result || tail(rec.id) || "(no output)";
  return `Job ${rec.id} (${rec.agent}) FAILED: ${rec.error}\n\n${out}`;
}

function startJob(tool, agent, a, work) {
  const running = [...jobs.values()].filter((j) => j.rec.status === "running");
  if (running.length >= MAX_JOBS)
    throw new Error(`${MAX_JOBS} agent jobs are already running (${running.map((j) => j.rec.id).join(", ")}). ` +
      `Wait for one to finish (job_status / job_result) before starting another.`);
  const id = `${new Date().toISOString().slice(0, 10).replace(/-/g, "")}-${randomBytes(4).toString("hex")}`;
  const timeoutMs = Number(a.timeout_s) > 0 ? Number(a.timeout_s) * 1000 : JOB_TIMEOUT_MS;
  const rec = { id, tool, agent, status: "running", started: nowIso(), prompt: oneLine(a.prompt, 200),
    timeout_s: timeoutMs / 1000, hub_id: HUB_ID, hub_pid: process.pid };
  const job = { rec, children: new Set(), killed: false, ok: 0, failed: 0,
    log: (d) => { try { appendFileSync(jobFile(id, "out"), d); } catch {} } };
  jobs.set(id, job);
  writeRec(rec);
  writeFileSync(jobFile(id, "out"), "", "utf8");
  const timer = setTimeout(() => {
    job.killed = true;
    Object.assign(rec, { status: "failed", finished: nowIso(), error: `timed out after ${rec.timeout_s}s; agent process tree killed` });
    writeRec(rec);
    for (const c of job.children) killTree(c.pid);
  }, timeoutMs);
  const finish = (result, error) => {
    clearTimeout(timer);
    rec.result = result;
    // dispatch() has filed the FOR MEMORY lines by now, once, whether or not anyone fetches the result.
    rec.memory_filed = true;
    if (rec.status === "running") {
      if (error || (job.failed && !job.ok)) Object.assign(rec, { status: "failed", error: error || "the agent exited with an error" });
      else rec.status = "done";
      rec.finished = nowIso();
    }
    // work has settled and all child output has drained, including after a timeout.
    truncateOutput(rec);
    writeRec(rec);
    return rec;
  };
  job.promise = jobCtx.run(job, work).then((r) => finish(r), (e) => finish("", String(e?.message || e)));
  return job;
}

async function asJob(tool, agent, a, work) {
  // Answer inline if the agent is done within wait_s, otherwise hand back the job.
  const job = startJob(tool, agent, a, work);
  const waitS = a.wait_s === undefined ? DEFAULT_WAIT_S : Math.max(0, Number(a.wait_s) || 0);
  let t;
  const rec = await Promise.race([job.promise, new Promise((r) => { t = setTimeout(r, waitS * 1000, null); })]);
  clearTimeout(t);
  if (rec) return resultText(rec);
  const { id, status, started } = job.rec;
  return JSON.stringify({ job_id: id, status, agent, started,
    next: `Still working. Poll job_status("${id}") every 30-60 s, then job_result("${id}"). Don't ask again: that starts a second run.` });
}

function listJobs() {
  let names = [];
  try { names = readdirSync(JOBS_DIR).filter((f) => f.endsWith(".json")); } catch {}
  const since = Date.now() - 24 * 3600 * 1000;
  const recs = names.map((f) => getRec(f.slice(0, -5))).filter((r) => r && Date.parse(r.started) >= since)
    .sort((x, y) => y.started.localeCompare(x.started));
  if (!recs.length) return "No agent jobs in the last 24 h.";
  return JSON.stringify(recs.map((r) => ({ job_id: r.id, status: r.status, agent: r.agent, tool: r.tool,
    started: r.started, elapsed_s: elapsed(r), prompt: r.prompt, ...(r.error ? { error: r.error } : {}) })), null, 2);
}

// Job files would pile up forever otherwise. Pruned when the hub starts, then hourly.
const PRUNE_EVERY_MS = 3600 * 1000;

function keepMs() {
  const d = Number(process.env.AGENT_HUB_JOB_KEEP_DAYS);
  return (process.env.AGENT_HUB_JOB_KEEP_DAYS !== undefined && Number.isFinite(d) && d >= 0 ? d : JOB_RETENTION_DAYS) * 86400 * 1000;
}

export function pruneJobs(now = Date.now()) {
  // Use only recorded finish times. Do not call readRec(): pruning must not turn a
  // running job into a failed one, or write to a path taken from persisted JSON.
  const cutoff = now - keepMs();
  let names = [];
  try { names = readdirSync(JOBS_DIR).filter((f) => f.endsWith(".json")); } catch { return []; }
  const removed = [];
  for (const f of names) {
    const id = f.slice(0, -5);
    if (!/^[\w-]+$/.test(id)) continue;
    let raw;
    try {
      if (!lstatSync(jobFile(id, "json")).isFile()) continue;
      raw = JSON.parse(readFileSync(jobFile(id, "json"), "utf8"));
    } catch { continue; }
    if (!raw || raw.id !== id || !["done", "failed"].includes(raw.status)) continue;
    const at = Date.parse(raw.finished);
    if (!Number.isFinite(at) || at >= cutoff) continue;
    const live = jobs.get(id);
    if (live && (live.rec.status === "running" || live.children.size)) continue;
    try {
      // Output first: keep the JSON for a later retry if either removal fails.
      // No recursion, and paths are built only from validated directory entry names.
      rmSync(jobFile(id, "out"), { force: true });
      rmSync(jobFile(id, "json"), { force: true });
    } catch { continue; }
    jobs.delete(id);
    removed.push(id);
  }
  return removed;
}

try { pruneJobs(); } catch {}
setInterval(() => { try { pruneJobs(); } catch {} }, PRUNE_EVERY_MS).unref();

// ------------------------------------------------------------------ MCP

const taskProps = {
  prompt: { type: "string", description: "Full, self-contained instructions. The agent cannot see this chat." },
  cwd: { type: "string", description: "Absolute Windows folder to work in. Defaults to C:\\Users\\Xichu\\GitHub." },
  write: { type: "boolean", description: "Allow the agent to edit files in cwd. Default false (read-only)." },
  model: { type: "string", description: "Optional model override." },
};

const jobProps = {
  wait_s: { type: "number", description: `Seconds to wait for the answer before handing back a job_id instead. Default ${DEFAULT_WAIT_S}; keep it under 60.` },
  timeout_s: { type: "number", description: "Kill the agent (and its child processes) after this many seconds. Default 900 (15 min)." },
};

const JOB_NOTE = ` Answers inline if done within wait_s (default ${DEFAULT_WAIT_S} s); otherwise returns {job_id, status: "running"} ` +
  `and the agent keeps working. Then poll job_status(job_id) every 30-60 s and call job_result(job_id) once it is done. ` +
  `Never re-ask the same task because it is slow: that starts a second run.`;

export const TOOLS = [
  { name: "ask_codex", description: "Hand a task to OpenAI Codex. Best for changing code in a repo, debugging, code review." + JOB_NOTE,
    inputSchema: { type: "object", properties: { ...taskProps, ...jobProps }, required: ["prompt"] } },
  { name: "ask_gemini", description: "Hand a task to Google Gemini. Best for long documents, many files at once, research and summaries." + JOB_NOTE,
    inputSchema: { type: "object", properties: { ...taskProps, ...jobProps }, required: ["prompt"] } },
  { name: "ask_copilot", description: "Hand a task to GitHub Copilot. Best for GitHub questions (issues, PRs), shell commands and quick code snippets." + JOB_NOTE,
    inputSchema: { type: "object", properties: { ...taskProps, ...jobProps }, required: ["prompt"] } },
  { name: "ask_don", description: `Ask ${TUTOR}, Chuen's study tutor, for a professional opinion on anything about his university modules, coursework, revision or learning, before working on it. ${TUTOR} answers only from Chuen's own lecture notes and notes in the vault, and gives confidence (low/medium/high), the notes used and what they don't cover. Weigh it as a subject expert's opinion: informed, advisory, never an override. Runs the local model; a hard topic goes to Claude and takes longer.` + JOB_NOTE,
    inputSchema: { type: "object", properties: { prompt: { type: "string", description: "The question, in full. The tutor cannot see this chat." },
      module: { type: "string", description: "The module if known (name, code or short form like 'signals', 'EM'); otherwise the tutor works it out from the question." }, ...jobProps },
      required: ["prompt"] } },
  { name: "route_task", description: `Let the hub pick the agent for a task (codex, gemini or copilot) from its kind, run it, and say who did it and why. Study work (his modules, coursework, revision) is put to ${TUTOR} first and the agent gets the tutor's opinion with the task. Use this by default when Chuen asks for work that another agent can do.` + JOB_NOTE,
    inputSchema: { type: "object", properties: { ...taskProps, ...jobProps,
      kind: { type: "string", enum: Object.keys(ROUTES), description: "What sort of work this is. Leave out to let the hub guess from the prompt." } },
      required: ["prompt"] } },
  { name: "split_tasks", description: "Run several sub-tasks at once, each routed to the best agent (max 5). Give each its own cwd, or keep them read-only, so they don't edit the same files. Runs as one job." + JOB_NOTE,
    inputSchema: { type: "object", properties: { tasks: { type: "array", maxItems: 5, items: { type: "object",
      properties: { ...taskProps, kind: { type: "string", enum: Object.keys(ROUTES) } }, required: ["prompt"] } }, ...jobProps },
      required: ["tasks"] } },
  { name: "ask_all", description: "Ask Codex, Gemini and Copilot the same question at once, read-only, for second opinions. Runs as one job." + JOB_NOTE,
    inputSchema: { type: "object", properties: { prompt: taskProps.prompt, cwd: taskProps.cwd, ...jobProps }, required: ["prompt"] } },
  { name: "queue_task", description: "Hand a piece of work to Orchestra, Chuen's task runner, instead of running it here: it is written to the queue (orchestra/new_task.py), run by a lane in its own git worktree, consulted with the study tutor first, checked by its gate and merged only on a pass, and it shows in runner.log, STATE NOW, the hub and the 'Orchestra live' window. Use it for real work on a repo (code changes, fixes, write-ups) that can wait for the runner; use ask_* for a question you need answered in this chat. The body must stand alone: the lane cannot see this chat.",
    inputSchema: { type: "object", properties: {
      title: { type: "string", description: "Short title: what will be true when it is done." },
      body: { type: "string", description: "The facts the session needs: which files, what is wrong, what you want. Real paths, relative to the repo root." },
      gate: { type: "array", items: { type: "string" }, description: "Commands that must all exit 0 before the work is merged. Default: the repo's fast checks." },
      lane: { type: "string", description: "claude, codex, copilot or gemini. Default: the best-scoring lane for the work (exam scores)." },
      work: { type: "string", description: "Kind of work for the lane choice when no lane is given: code-edit, debug, writing, study, extract." },
      priority: { type: "string", enum: ["urgent", "normal"] },
      weight: { type: "string", enum: ["light", "heavy"] } },
      required: ["title", "body"] } },
  { name: "job_status", description: "Check a background agent job: running / done / failed, seconds elapsed, and the last 20 lines of its output. Use this to follow a slow ask_* call instead of asking again.",
    inputSchema: { type: "object", properties: { job_id: { type: "string" } }, required: ["job_id"] } },
  { name: "job_result", description: "Get a finished job's full answer (its FOR MEMORY lines are already filed to the vault, once). If it is still running, says so: keep polling job_status.",
    inputSchema: { type: "object", properties: { job_id: { type: "string" } }, required: ["job_id"] } },
  { name: "jobs", description: "List agent jobs from the last 24 h (newest first) with their status. Use it to find a job_id you lost.",
    inputSchema: { type: "object", properties: {} } },
  { name: "remember", description: "File durable facts or decisions into the shared vault's handoff note for today, tagged [claude]. One line each.",
    inputSchema: { type: "object", properties: { lines: { type: "array", items: { type: "string" } } }, required: ["lines"] } },
  { name: "memory_today", description: "Read the vault's hot.md, STATE NOW and today's handoff note, so the chat starts from shared live state.",
    inputSchema: { type: "object", properties: {} } },
];

const CONSULT_LINE = (opinion) =>
  `${TUTOR}, Chuen's study tutor, was asked first. ${TUTOR}'s opinion, grounded in Chuen's own lecture notes:\n${opinion}\n` +
  `Weigh it as a subject expert's opinion: informed, advisory, never an override. Say where you go against it.\n\nTASK:\n\n`;

async function routed(t, r = pickRoute(t.prompt, t.kind)) {
  const label = `routed ${r.kind}${r.guessed ? " (guessed)" : ""}${r.scored ? " (scored)" : ""} → ${r.agent}`;
  let task = t, head = "";
  if ((r.kind === STUDY || isStudy(t.prompt, t.kind)) && r.agent !== TUTOR_KEY) {
    // Study-category work: the tutor first; its opinion goes in front of the agent's prompt and
    // in the answer. A tutor that can't answer is shown as such and the agent still runs.
    const opinion = await dispatch(TUTOR_KEY, { prompt: t.prompt, module: t.module }, `consult (${r.kind})`);
    head = `### ${TUTOR_KEY} — ${STUDY} (consulted first)\n${opinion}\n\n`;
    task = { ...t, prompt: CONSULT_LINE(opinion) + t.prompt, _logPrompt: t.prompt };  // the log keeps the task as asked
  }
  const out = await dispatch(r.agent, { ...task, write: t.write ?? r.write }, label);
  return `${head}### ${r.agent} — ${r.kind}${r.guessed ? " (guessed)" : ""}: ${r.why}\n${out}`;
}

const ASK = { ask_codex: "codex", ask_gemini: "gemini", ask_copilot: "copilot", ask_don: TUTOR_KEY };

// Work handed to Orchestra rather than run here (Chuen, 9 Nov 2026: every agent's work goes
// through the queue, so it shows in runner.log, STATE NOW and the hub snapshot). new_task.py
// writes the task; the runner does the rest. The default gate and the From: name are
// orchestra/config.json (hub_queue_default_gate, hub_queue_source).
export function queueTask(a = {}) {
  const title = oneLine(String(a.title || ""), 120).trim();
  if (!title) throw new Error("queue_task needs a title.");
  if (!String(a.body || "").trim()) throw new Error("queue_task needs a body: the lane cannot see this chat.");
  let cfg = {};
  try { cfg = JSON.parse(readFileSync(CONFIG, "utf8")); } catch {}
  const gate = Array.isArray(a.gate) && a.gate.length ? a.gate.map(String) : (cfg.hub_queue_default_gate || []);
  const dir = mkdtempSync(join(tmpdir(), "hub-task-"));
  try {
    const bodyFile = join(dir, "body.md");
    writeFileSync(bodyFile, String(a.body), "utf8");
    const args = [join(REPO, "orchestra", "new_task.py"), title, "--auto", `--body-file=${bodyFile}`,
      `--from=${cfg.hub_queue_source || "agent hub"}`, ...gate.map((g) => `--gate=${g}`)];
    for (const k of ["priority", "weight", "lane", "work"]) if (a[k]) args.push(`--${k}=${a[k]}`);
    let out;
    try {
      out = execFileSync(pythonPath(), args, { cwd: REPO, timeout: 30000, encoding: "utf8", windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: "utf-8" } });
    } catch (e) {
      throw new Error(`queue_task: new_task.py failed: ${String(e.stderr || e.stdout || e.message).trim()}`);
    }
    const lines = out.trim().split(/\r?\n/);
    const path = lines.pop();
    remember("hub", [`queued for Orchestra: ${title} (${path})`]);
    return [`Queued for Orchestra: ${path}`, ...lines,
      "The runner picks it up on its next tick: follow it in runner.log, STATE NOW or the 'Orchestra live' window."].join("\n");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

export async function callTool(name, a = {}) {
  if (name === "queue_task") return queueTask(a);
  if (ASK[name]) return asJob(name, ASK[name], a, () => dispatch(ASK[name], a, name));
  if (name === "route_task") {
    const r = pickRoute(a.prompt, a.kind);
    return asJob(name, r.agent, a, () => routed(a, r));
  }
  if (name === "split_tasks") {
    // One job for the lot; a sub-task that fails shows up in the text, and the job only
    // fails if every sub-task did (finish(): job.failed && !job.ok), same as ask_all.
    const tasks = (Array.isArray(a.tasks) ? a.tasks : []).slice(0, 5);
    if (!tasks.length) throw new Error("split_tasks needs at least one task.");
    const routes = tasks.map((t) => pickRoute(t.prompt, t.kind));
    const agents = [...new Set(routes.map((r) => r.agent))].join(",");
    const summary = { ...a, prompt: tasks.map((t) => oneLine(t.prompt, 60)).join(" | ") };
    return asJob(name, agents, summary, () => Promise.all(tasks.map((t, i) => routed(t, routes[i]).catch((e) => {
      const job = jobCtx.getStore();
      if (job) job.failed += 1;
      return `### ${routes[i].agent} — ${routes[i].kind}: FAILED\n${String(e?.message || e)}`;
    }))).then((outs) => outs.join("\n\n")));
  }
  if (name === "ask_all") {
    return asJob(name, "codex,gemini,copilot", a, () => Promise.all(["codex", "gemini", "copilot"].map((ag) =>
      dispatch(ag, { prompt: a.prompt, cwd: a.cwd, write: false }, "ask_all"))).then((outs) => outs.join("\n\n")));
  }
  if (name === "job_status" || name === "job_result") {
    const rec = getRec(a.job_id);
    if (!rec) throw new Error(`No job ${a.job_id}. Use jobs() to list the last 24 h.`);
    if (name === "job_result") return resultText(rec);
    return JSON.stringify({ job_id: rec.id, status: rec.status, agent: rec.agent, tool: rec.tool, started: rec.started,
      elapsed_s: elapsed(rec), ...(rec.error ? { error: rec.error } : {}), output_tail: tail(rec.id) }, null, 2);
  }
  if (name === "jobs") return listJobs();
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
