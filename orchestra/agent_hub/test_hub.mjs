// node --test orchestra/agent_hub/test_hub.mjs   (fake agents, temp vault; no model calls)
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync, writeFileSync, mkdirSync, statSync, linkSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const vault = mkdtempSync(join(tmpdir(), "vault-"));
const jobsDir = mkdtempSync(join(tmpdir(), "hub-jobs-"));
process.env.AGENT_HUB_VAULT = vault;
process.env.AGENT_HUB_JOBS_DIR = jobsDir;
process.env.AGENT_HUB_FAKE = "1";
process.env.AGENT_HUB_CONTEXT = "0";
// Real exam scores in the checkout must not steer these tests; the scored tests write their own.
process.env.AGENT_HUB_EXAM_SCORES = join(vault, "no-scores.json");
const hub = await import("./server.mjs");
const HERE = dirname(fileURLToPath(import.meta.url));
const FAKE_CMD = join(HERE, "fake_agent.mjs");
const REPO_PY = join(HERE, "..", "..", ".venv", process.platform === "win32" ? "Scripts\\python.exe" : "bin/python");
// setup-python supplies Python on PATH in CI; only local checkouts have a .venv.
const TEST_PY = process.env.AGENT_HUB_PYTHON || (existsSync(REPO_PY) ? REPO_PY : "python");

test("exam scores: route_task picks the best working agent, and falls back to the rules without scores", async () => {
  const at = new Date().toISOString();
  const row = (ok, seconds) => ({ at, exam: "x", ok, seconds, answered: true });
  const scores = { agents: {
    codex: { writing: [row(true, 40), row(true, 50), row(true, 45)] },
    copilot: { writing: [row(true, 10), row(true, 12), row(false, 9)] },
    gemini: { writing: [row(false, 5), row(false, 5), row(false, 5)] },  // Not working
  } };
  const file = join(vault, "scores.json");
  writeFileSync(file, JSON.stringify(scores), "utf8");
  const oldPy = process.env.AGENT_HUB_PYTHON;
  process.env.AGENT_HUB_PYTHON = TEST_PY;
  try {
    assert.equal(hub.pickRoute("summarise this", "summarise").agent, "gemini", "no scores file: today's rules");
    process.env.AGENT_HUB_EXAM_SCORES = file;
    const r = hub.pickRoute("summarise the lecture notes", "summarise");
    assert.equal(r.agent, "codex");  // 100% beats copilot's 67%; gemini is not working
    assert.equal(r.scored, true);
    assert.match(r.why, /writing: 3\/3 passed/);
    assert.equal(r.write, false, "the kind's write flag is kept");
    // a kind with no exam category keeps the fixed rule
    assert.equal(hub.pickRoute("list my PRs", "github").agent, "copilot");
    const out = await hub.callTool("route_task", { prompt: "summarise the scored notes", kind: "summarise" });
    assert.match(out, /### codex — summarise: best exam score/);
    assert.match(readFileSync(hub.handoffPath(), "utf8"), /routed summarise \(scored\) → codex/);
    // python missing: today's rules, no crash
    process.env.AGENT_HUB_PYTHON = join(vault, "no-python.exe");
    assert.equal(hub.pickRoute("summarise", "summarise").agent, "gemini");
  } finally {
    process.env.AGENT_HUB_EXAM_SCORES = join(vault, "no-scores.json");
    if (oldPy === undefined) delete process.env.AGENT_HUB_PYTHON; else process.env.AGENT_HUB_PYTHON = oldPy;
  }
});

test("routing picks the agent from the kind, or guesses from the words", () => {
  assert.equal(hub.chooseRoute("anything", "long_read").agent, "gemini");
  assert.equal(hub.chooseRoute("fix the tray restart bug in tray.py").agent, "codex");
  assert.equal(hub.chooseRoute("fix the tray restart bug in tray.py").write, true);
  assert.equal(hub.chooseRoute("summarise this 40 page pdf").agent, "gemini");
  assert.equal(hub.chooseRoute("list my open PRs on github").agent, "copilot");
  assert.equal(hub.chooseRoute("hello").guessed, true);
});

// ---------------------------------------------------------------- the study tutor (ask_don)

const OPINION = JSON.stringify({ question: "q", answer: "State the region of convergence first.", confidence: "high",
  sources: ["[[A/Week 5 tutor|Week 5 tutor]]"], caveats: ["No two-sided example in the notes."], who: "local",
  module: "Signals and Systems" });

test("study-category work comes from the exams' routing data, not the hub's own words", () => {
  assert.equal(hub.categoryOf("revise the Fourier series before the exam"), "study");
  assert.equal(hub.categoryOf("anything", "study"), "study");
  assert.equal(hub.categoryOf("anything", "maths"), "study");
  assert.equal(hub.categoryOf("fix the tray restart bug"), "code-edit", "the exams' first match wins, as lane_for.py");
  assert.equal(hub.categoryOf("list my PRs", "github"), null);
  assert.equal(hub.chooseRoute("revise the Fourier series before the exam").kind, "study");
  assert.equal(hub.chooseRoute("revise the Fourier series before the exam").agent, "codex");
  assert.equal(hub.chooseRoute("fix the tutor quiz in tutor/exam.py").agent, "codex", "the hub's own rules still go first");
  assert.equal(hub.chooseRoute("hello").kind, "quick_code");
  assert.ok(Object.keys(hub.ROUTES).includes("study"));
});

test("ask_don returns the tutor's opinion as the tutor's, with confidence, sources and caveats", async () => {
  process.env.AGENT_HUB_FAKE_ANSWERS = JSON.stringify({ don: OPINION });
  try {
    const out = await hub.callTool("ask_don", { prompt: "what do my notes say about the ROC?", module: "signals" });
    assert.match(out, /^\[don\]\n/);
    assert.match(out, /says \(high confidence, answered by local, Signals and Systems\): State the region of convergence first\./);
    assert.match(out, /Sources: \[\[A\/Week 5 tutor\|Week 5 tutor\]\]/);
    assert.match(out, /Caveats: No two-sided example/);
    assert.match(out, /advisory, never an override/);
    assert.match(readFileSync(hub.handoffPath(), "utf8"), /\[don\] ask_don: what do my notes say about the ROC\? → done/);
  } finally { delete process.env.AGENT_HUB_FAKE_ANSWERS; }
  assert.equal(hub.renderOpinion("plain words, no JSON"), "plain words, no JSON");
});

test("route_task puts study work to the tutor first, and the agent gets the opinion with the task", async () => {
  process.env.AGENT_HUB_FAKE_CMD = FAKE_CMD;
  try {
    const out = await hub.callTool("route_task", { prompt: "explain the Fourier transform lecture ECHO TAG=st1" });
    const don = out.indexOf("### don — study (consulted first)");
    const agent = out.indexOf("### codex — study (guessed)");
    assert.ok(don >= 0, out);
    assert.ok(agent > don, "the tutor's section comes first, the agent's after");
    assert.match(out, /fake don answer st1/);
    assert.match(out, /prompt: TASK: .*study tutor, was asked first.*fake don answer st1.*never an override.*TASK: explain the Fourier/);
    const note = readFileSync(hub.handoffPath(), "utf8");
    assert.match(note, /\[don\] consult \(study\): explain the Fourier transform lecture/);
    assert.match(note, /\[codex\] routed study \(guessed\) → codex: explain the Fourier transform lecture/, "the log keeps the task as asked");
    // A kind the hub's own rules route still gets the consult when the words say study.
    const summ = await hub.callTool("route_task", { prompt: "summarise the lecture notes ECHO TAG=st5" });
    assert.match(summ, /### don — study \(consulted first\)[\s\S]*### gemini — summarise/);
    // Not study: nobody is consulted.
    const plain = await hub.callTool("route_task", { prompt: "list my open PRs on github ECHO TAG=st2" });
    assert.doesNotMatch(plain, /consulted first/);
    assert.match(plain, /prompt: TASK: list my open PRs/);
    // split_tasks: the same, per sub-task.
    const split = await hub.callTool("split_tasks", { tasks: [
      { prompt: "revise Laplace for the exam ECHO TAG=st3" }, { prompt: "b TAG=st4", kind: "research" }] });
    assert.match(split, /### don — study \(consulted first\)[\s\S]*### codex — study[\s\S]*fake codex answer st3/);
    assert.equal((split.match(/consulted first/g) || []).length, 1);
  } finally { delete process.env.AGENT_HUB_FAKE_CMD; }
});

test("a tutor that can't answer never stops study work, and nobody stands in for it", async () => {
  process.env.AGENT_HUB_FAKE_FAIL = "don";
  try {
    const out = await hub.callTool("route_task", { prompt: "revise the Fourier series before the exam" });
    assert.match(out, /### don — study \(consulted first\)\n\[don, exit 1\]/);
    assert.doesNotMatch(out, /did it instead/);
    assert.match(out, /### codex — study[\s\S]*fake codex answer/);
    const alone = await hub.callTool("ask_don", { prompt: "the ROC?" });
    assert.match(alone, /FAILED[\s\S]*\[don, exit 1\]/, "on its own, a failed consult is a failed job like any ask_*");
    assert.doesNotMatch(alone, /did it instead/);
  } finally { delete process.env.AGENT_HUB_FAKE_FAIL; }
});

test("FOR MEMORY lines are pulled out, 'none' is ignored", () => {
  assert.deepEqual(hub.memoryLines("answer\nFOR MEMORY:\n- a\n- b\n"), ["a", "b"]);
  assert.deepEqual(hub.memoryLines("answer\nFOR MEMORY: none"), []);
  assert.deepEqual(hub.memoryLines("no section"), []);
});

test("every hand-off and its memory lines land in today's handoff note, tagged", async () => {
  const out = await hub.callTool("route_task", { prompt: "summarise the lecture notes" });
  assert.match(out, /gemini — summarise/);
  const note = readFileSync(hub.handoffPath(), "utf8");
  assert.match(note, /^---\nname: Handoff-\d{4}-\d{2}-\d{2}\ndescription: /);
  assert.match(note, /\[gemini\] routed summarise \(guessed\) → gemini: summarise the lecture notes → done/);
  assert.match(note, /\[gemini\] fake fact from gemini/);
});

test("split_tasks runs each sub-task on its own agent", async () => {
  const out = await hub.callTool("split_tasks", { tasks: [
    { prompt: "a", kind: "code_change" }, { prompt: "b", kind: "research" }, { prompt: "c", kind: "github" }] });
  for (const a of ["codex", "gemini", "copilot"]) assert.match(out, new RegExp(`### ${a}`));
});

test("ask_all asks all three; remember files [claude] lines", async () => {
  const out = await hub.callTool("ask_all", { prompt: "second opinion" });
  assert.equal((out.match(/^\[(codex|gemini|copilot)\]/gm) || []).length, 3);
  await hub.callTool("remember", { lines: ["Chuen chose Atlas format for the vault"] });
  assert.match(readFileSync(hub.handoffPath(), "utf8"), /\[claude\] Chuen chose Atlas format/);
});

test("no vault folder: nothing is written and nothing crashes", async () => {
  // remember() returns false when the vault doesn't exist
  process.env.AGENT_HUB_VAULT = join(vault, "missing");
  const fresh = await import("./server.mjs?missing");
  assert.equal(fresh.remember("x", ["y"]), false);
  assert.equal(existsSync(join(vault, "missing")), false);
  process.env.AGENT_HUB_VAULT = vault;
});

test("context pack is skipped cleanly when turned off or python is missing", () => {
  assert.equal(hub.contextPack("codex"), "");
  process.env.AGENT_HUB_CONTEXT = "1"; process.env.AGENT_HUB_PYTHON = join(vault, "no-python.exe");
  assert.equal(hub.contextPack("codex"), "");
  process.env.AGENT_HUB_CONTEXT = "0";
});

test("an agent that can't take work falls back to Codex, and the log says so", async () => {
  process.env.AGENT_HUB_FAKE_FAIL = "gemini";
  const out = await hub.callTool("ask_gemini", { prompt: "summarise the reading list" });
  delete process.env.AGENT_HUB_FAKE_FAIL;
  assert.match(out, /gemini unavailable: IneligibleTier/);
  assert.match(out, /\[codex\]/);
  assert.match(readFileSync(hub.handoffPath(), "utf8"), /\[claude\] gemini couldn't take "summarise the reading list" \(IneligibleTier/);
});

test("speaks MCP over stdio: initialize + tools/list", async () => {
  const child = spawn(process.execPath, [join(HERE, "server.mjs")], { env: process.env });
  const lines = [];
  child.stdout.on("data", (d) => lines.push(...String(d).split("\n").filter(Boolean)));
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} }) + "\n");
  child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list" }) + "\n");
  await new Promise((r) => setTimeout(r, 800));
  child.kill();
  const msgs = lines.map((l) => JSON.parse(l));
  assert.equal(msgs[0].result.serverInfo.name, "agent-hub");
  const names = msgs[1].result.tools.map((t) => t.name);
  for (const n of ["ask_codex", "ask_gemini", "ask_copilot", "ask_don", "route_task", "split_tasks", "ask_all", "remember",
    "memory_today", "job_status", "job_result", "jobs"])
    assert.ok(names.includes(n), n);
  const don = msgs[1].result.tools.find((t) => t.name === "ask_don");
  assert.match(don.description, /advisory, never an override/);
  assert.ok(don.inputSchema.properties.module);
  assert.ok(msgs[1].result.tools.find((t) => t.name === "route_task").inputSchema.properties.kind.enum.includes("study"));
  const codex = msgs[1].result.tools.find((t) => t.name === "ask_codex");
  assert.match(codex.description, /job_status/);
  assert.ok(codex.inputSchema.properties.wait_s);
});

// ---------------------------------------------------------------- background jobs
// These run the fake agent as a real child process (fake_agent.mjs), so waiting,
// live output, timeouts and kill-tree are exercised for real.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const status = async (h, id) => JSON.parse(await h.callTool("job_status", { job_id: id }));
async function until(h, id, ms = 15000) {
  const end = Date.now() + ms;
  for (;;) {
    const s = await status(h, id);
    if (s.status !== "running" || Date.now() > end) return s;
    await sleep(100);
  }
}
const count = (re) => (readFileSync(hub.handoffPath(), "utf8").match(re) || []).length;
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; } };

test("jobs: a fast agent still answers inline", async () => {
  process.env.AGENT_HUB_FAKE_CMD = FAKE_CMD;
  const out = await hub.callTool("ask_codex", { prompt: "quick one TAG=fast1" });
  assert.match(out, /^\[codex\]\n/);
  assert.match(out, /fake codex answer fast1/);
  assert.equal(count(/fake fact from codex fast1/g), 1);
});

let slowId;
test("jobs: a slow agent returns a job, then status shows progress, then the result", async () => {
  const first = JSON.parse(await hub.callTool("ask_codex", { prompt: "big job SLEEP=1500 TAG=slow1", wait_s: 0.2 }));
  assert.equal(first.status, "running");
  assert.equal(first.agent, "codex");
  assert.ok(first.job_id && first.started);
  slowId = first.job_id;
  assert.match(await hub.callTool("job_result", { job_id: slowId }), /still running/);
  await sleep(500);
  const mid = await status(hub, slowId);
  assert.equal(mid.status, "running");
  assert.match(mid.output_tail, /working \d\/5/);
  const end = await until(hub, slowId);
  assert.equal(end.status, "done");
  assert.ok(end.elapsed_s >= 1);
  const res = await hub.callTool("job_result", { job_id: slowId });
  assert.match(res, /\[codex\]\n[\s\S]*fake codex answer slow1/);
});

test("jobs: FOR MEMORY lines are filed exactly once per job, however often the result is fetched", async () => {
  await hub.callTool("job_result", { job_id: slowId });
  await hub.callTool("job_result", { job_id: slowId });
  assert.equal(count(/\[codex\] fake fact from codex slow1/g), 1);
  assert.equal(count(/\[codex\] ask_codex: big job SLEEP=1500 TAG=slow1 → done/g), 1);
});

test("jobs: a hub restart keeps finished results and fails jobs that died with the old hub", async () => {
  writeFileSync(join(jobsDir, "20261102-dead0001.json"), JSON.stringify({ id: "20261102-dead0001", tool: "ask_codex",
    agent: "codex", status: "running", started: new Date().toISOString(), prompt: "x", timeout_s: 900,
    hub_id: "old-hub", hub_pid: process.pid }), "utf8");
  const restarted = await import("./server.mjs?restart");
  assert.match(await restarted.callTool("job_result", { job_id: slowId }), /fake codex answer slow1/);
  const dead = await status(restarted, "20261102-dead0001");
  assert.equal(dead.status, "failed");
  assert.match(dead.error, /hub stopped/);
  assert.match(await restarted.callTool("job_result", { job_id: "20261102-dead0001" }), /FAILED: the hub stopped/);
  const list = JSON.parse(await restarted.callTool("jobs"));
  assert.ok(list.some((j) => j.job_id === slowId && j.status === "done"));
  assert.ok(list.some((j) => j.job_id === "20261102-dead0001" && j.status === "failed"));
  await assert.rejects(restarted.callTool("job_status", { job_id: "../../etc" }), /No job/);
});

test("jobs: at most 4 run at once", async () => {
  const ids = [];
  for (let i = 0; i < 4; i++)
    ids.push(JSON.parse(await hub.callTool("ask_copilot", { prompt: `cap SLEEP=1200 TAG=cap${i}`, wait_s: 0 })).job_id);
  await assert.rejects(hub.callTool("ask_codex", { prompt: "one too many", wait_s: 0 }), /4 agent jobs are already running/);
  for (const id of ids) assert.equal((await until(hub, id)).status, "done");
  // a slot is free again
  assert.match(await hub.callTool("ask_codex", { prompt: "after TAG=after1" }), /fake codex answer after1/);
});

test("jobs: timeout kills the whole process tree, fails the job, and does not fall back", async () => {
  const out = await hub.callTool("ask_gemini", { prompt: "forever SLEEP=30000 GRANDCHILD TAG=to1", wait_s: 10, timeout_s: 1 });
  assert.match(out, /FAILED: timed out after 1s; agent process tree killed/);
  assert.doesNotMatch(out, /codex did it instead/);
  const pid = Number(out.match(/grandchild pid (\d+)/)?.[1]);
  assert.ok(pid, "fake agent printed its grandchild's pid");
  await sleep(300);
  assert.equal(alive(pid), false, "grandchild was killed with the tree");
  const list = JSON.parse(await hub.callTool("jobs"));
  assert.equal(list.find((j) => j.prompt.includes("TAG=to1")).status, "failed");
});

test("jobs: route_task and ask_all run as jobs too, and the gemini→codex fallback still works", async () => {
  process.env.AGENT_HUB_FAKE_FAIL = "gemini";
  const r = JSON.parse(await hub.callTool("route_task", { prompt: "summarise SLEEP=800 TAG=rt1", wait_s: 0 }));
  assert.equal(r.agent, "gemini");
  assert.equal((await until(hub, r.job_id)).status, "done");
  const res = await hub.callTool("job_result", { job_id: r.job_id });
  delete process.env.AGENT_HUB_FAKE_FAIL;
  assert.match(res, /gemini unavailable: IneligibleTier[\s\S]*\[codex\][\s\S]*fake codex answer rt1/);
  const all = await hub.callTool("ask_all", { prompt: "opinions TAG=all1" });
  assert.equal((all.match(/^\[(codex|gemini|copilot)\]/gm) || []).length, 3);
  delete process.env.AGENT_HUB_FAKE_CMD;
});

test("jobs: split_tasks returns a job at once, and its result has every sub-task's output", async () => {
  process.env.AGENT_HUB_FAKE_CMD = FAKE_CMD;
  try {
    const t0 = Date.now();
    const first = JSON.parse(await hub.callTool("split_tasks", { wait_s: 0, tasks: [
      { prompt: "fix it SLEEP=900 TAG=sp1", kind: "code_change" },
      { prompt: "read it SLEEP=900 TAG=sp2", kind: "research" },
      { prompt: "list PRs SLEEP=900 TAG=sp3", kind: "github" }] }));
    assert.ok(Date.now() - t0 < 600, "handed back before the sub-tasks finished");
    assert.equal(first.status, "running");
    assert.ok(first.job_id);
    assert.deepEqual(first.agent.split(",").sort(), ["codex", "copilot", "gemini"]);
    const end = await until(hub, first.job_id);
    assert.equal(end.status, "done");
    assert.equal(end.tool, "split_tasks");
    const res = await hub.callTool("job_result", { job_id: first.job_id });
    for (const [a, tag] of [["codex", "sp1"], ["gemini", "sp2"], ["copilot", "sp3"]]) {
      assert.match(res, new RegExp(`### ${a}`));
      assert.match(res, new RegExp(`fake ${a} answer ${tag}`));
    }
  } finally { delete process.env.AGENT_HUB_FAKE_CMD; }
});

test("jobs: split_tasks shows a failed sub-task in the text, and fails only if all of them failed", async () => {
  try {
    process.env.AGENT_HUB_FAKE_FAIL = "codex";  // codex has no fallback, gemini still answers
    const some = await hub.callTool("split_tasks", { tasks: [
      { prompt: "fix", kind: "code_change" }, { prompt: "read", kind: "research" }] });
    assert.match(some, /\[codex, exit 1\]/);
    assert.match(some, /\[gemini\]\nfake gemini answer/);
    assert.doesNotMatch(some, /FAILED:/);
    process.env.AGENT_HUB_FAKE_FAIL = "codex,gemini,copilot";  // gemini's fallback (codex) fails too
    const all = await hub.callTool("split_tasks", { tasks: [
      { prompt: "fix TAG=allfail", kind: "code_change" }, { prompt: "read TAG=allfail", kind: "research" }] });
    assert.match(all, /FAILED: the agent exited with an error/);
  } finally { delete process.env.AGENT_HUB_FAKE_FAIL; }
  await assert.rejects(hub.callTool("split_tasks", { tasks: [] }), /at least one task/);
});

test("jobs: split_tasks counts against the 4-job cap", async () => {
  process.env.AGENT_HUB_FAKE_CMD = FAKE_CMD;
  try {
    const ids = [];
    for (let i = 0; i < 4; i++)
      ids.push(JSON.parse(await hub.callTool("ask_copilot", { prompt: `cap SLEEP=800 TAG=spcap${i}`, wait_s: 0 })).job_id);
    await assert.rejects(hub.callTool("split_tasks", { wait_s: 0, tasks: [{ prompt: "x", kind: "github" }] }),
      /4 agent jobs are already running/);
    for (const id of ids) assert.equal((await until(hub, id)).status, "done");
  } finally { delete process.env.AGENT_HUB_FAKE_CMD; }
});

test("job files: old finished jobs are pruned at hub start; running and recent ones are kept", async () => {
  const dir = mkdtempSync(join(tmpdir(), "hub-prune-"));
  const day = 86400 * 1000;
  const ago = (d) => new Date(Date.now() - d * day).toISOString();
  const put = (id, rec) => {
    writeFileSync(join(dir, `${id}.json`), JSON.stringify({ id, tool: "ask_codex", agent: "codex", prompt: "x",
      timeout_s: 900, hub_id: "other", ...rec }), "utf8");
    writeFileSync(join(dir, `${id}.out`), "output\n", "utf8");
  };
  put("old-done", { status: "done", started: ago(10), finished: ago(10), result: "r" });
  put("old-failed", { status: "failed", started: ago(9), finished: ago(9), error: "e" });
  put("recent-done", { status: "done", started: ago(1), finished: ago(1), result: "r" });
  // still running in another live hub (our parent process stands in for it): never deleted
  put("old-running", { status: "running", started: ago(30), hub_pid: process.ppid });
  // A missing hub is not a recorded finish time; pruning must leave this untouched.
  put("old-orphan", { status: "running", started: ago(30), hub_pid: 999999 });
  const oldDir = process.env.AGENT_HUB_JOBS_DIR;
  process.env.AGENT_HUB_JOBS_DIR = dir;
  let h;
  try { h = await import("./server.mjs?prune"); } finally { process.env.AGENT_HUB_JOBS_DIR = oldDir; }
  const has = (id, ext = "json") => existsSync(join(dir, `${id}.${ext}`));
  for (const id of ["old-done", "old-failed"]) {
    assert.equal(has(id), false, `${id}.json pruned`);
    assert.equal(has(id, "out"), false, `${id}.out pruned`);
  }
  for (const id of ["recent-done", "old-running", "old-orphan"]) assert.ok(has(id) && has(id, "out"), `${id} kept`);
  assert.equal(JSON.parse(readFileSync(join(dir, "old-orphan.json"))).status, "running");
  // AGENT_HUB_JOB_KEEP_DAYS overrides the 7 days
  process.env.AGENT_HUB_JOB_KEEP_DAYS = "0.5";
  try { assert.deepEqual(h.pruneJobs(), ["recent-done"]); } finally { delete process.env.AGENT_HUB_JOB_KEEP_DAYS; }
  assert.ok(has("old-running"), "running job survives any keep-days");
  assert.ok(has("old-orphan"), "orphan survives until a finish time is recorded and ages out");
});

test("job files: retention uses finish time, skips uncertain records, and schedules hourly pruning", async () => {
  const dir = mkdtempSync(join(tmpdir(), "hub-retention-"));
  const now = Date.now();
  const day = 86400 * 1000;
  const ago = (d) => new Date(now - d * day).toISOString();
  const put = (id, rec) => writeFileSync(join(dir, `${id}.json`), JSON.stringify({ id,
    status: "done", started: ago(30), ...rec }));
  put("boundary", { finished: ago(7) });
  put("recent-finish", { finished: ago(1) });
  put("no-finish", {});
  put("bad-finish", { finished: "not a date" });
  put("unknown-status", { status: "pending", finished: ago(30) });
  put("running-with-finish", { status: "running", finished: ago(30) });
  writeFileSync(join(dir, "null.json"), "null");
  writeFileSync(join(dir, "broken.json"), "{");
  // Use an eight-day retention at startup, then check the seven-day boundary precisely.
  const oldDir = process.env.AGENT_HUB_JOBS_DIR;
  const oldKeep = process.env.AGENT_HUB_JOB_KEEP_DAYS;
  const originalInterval = globalThis.setInterval;
  let tick, period, unrefed = false, h;
  process.env.AGENT_HUB_JOBS_DIR = dir;
  process.env.AGENT_HUB_JOB_KEEP_DAYS = "8";
  globalThis.setInterval = (fn, ms) => {
    tick = fn; period = ms;
    return { unref() { unrefed = true; } };
  };
  try { h = await import("./server.mjs?retention"); } finally {
    globalThis.setInterval = originalInterval;
    process.env.AGENT_HUB_JOBS_DIR = oldDir;
    delete process.env.AGENT_HUB_JOB_KEEP_DAYS;
  }
  try {
    assert.equal(period, 3600 * 1000);
    assert.equal(unrefed, true, "maintenance must not keep the process alive");
    assert.deepEqual(h.pruneJobs(now), [], "exactly seven days is retained");
    assert.deepEqual(h.pruneJobs(now + 1), ["boundary"]);
    for (const invalid of ["-1", "NaN", "Infinity"]) {
      process.env.AGENT_HUB_JOB_KEEP_DAYS = invalid;
      assert.deepEqual(h.pruneJobs(now), [], "invalid retention uses the seven-day default");
    }
    delete process.env.AGENT_HUB_JOB_KEEP_DAYS;
    put("after-start", { finished: ago(8) });
    tick();
    assert.equal(existsSync(join(dir, "after-start.json")), false, "hourly callback prunes new eligible files");
    for (const id of ["recent-finish", "no-finish", "bad-finish", "unknown-status", "running-with-finish", "null", "broken"])
      assert.ok(existsSync(join(dir, `${id}.json`)), `${id} kept`);
  } finally {
    if (oldKeep === undefined) delete process.env.AGENT_HUB_JOB_KEEP_DAYS;
    else process.env.AGENT_HUB_JOB_KEEP_DAYS = oldKeep;
  }
});

test("job files: pruning never follows record IDs or directory links outside the jobs directory", async () => {
  const root = mkdtempSync(join(tmpdir(), "hub-boundary-"));
  const dir = join(root, "jobs");
  mkdirSync(dir);
  const finished = new Date(Date.now() - 30 * 86400 * 1000).toISOString();
  const outside = JSON.stringify({ id: "outside", status: "done", started: finished, finished });
  writeFileSync(join(root, "outside.json"), outside);
  writeFileSync(join(root, "outside.out"), "outside output");
  writeFileSync(join(dir, "forged.json"), JSON.stringify({ id: "../outside", status: "running",
    started: finished, hub_id: "dead", hub_pid: 999999 }));
  writeFileSync(join(dir, "forged-finished.json"), JSON.stringify({ id: "../outside", status: "done", finished }));
  const nested = join(root, "nested");
  mkdirSync(nested);
  writeFileSync(join(nested, "keep.txt"), "outside nested file");
  symlinkSync(nested, join(dir, "linked.json"), process.platform === "win32" ? "junction" : "dir");
  symlinkSync(nested, join(dir, "old.out"), process.platform === "win32" ? "junction" : "dir");
  writeFileSync(join(dir, "old.json"), JSON.stringify({ id: "old", status: "done", finished }));
  const oldDir = process.env.AGENT_HUB_JOBS_DIR;
  process.env.AGENT_HUB_JOBS_DIR = dir;
  try { await import("./server.mjs?boundary"); } finally { process.env.AGENT_HUB_JOBS_DIR = oldDir; }
  assert.equal(readFileSync(join(root, "outside.json"), "utf8"), outside);
  assert.equal(readFileSync(join(root, "outside.out"), "utf8"), "outside output");
  assert.equal(readFileSync(join(nested, "keep.txt"), "utf8"), "outside nested file");
  assert.ok(existsSync(join(dir, "linked.json")), "JSON directory link is not read or removed");
  assert.ok(existsSync(join(dir, "forged-finished.json")), "mismatched record ID is retained");
  assert.equal(JSON.parse(readFileSync(join(dir, "forged.json"))).status, "running");
});

test("job files: finished output keeps its last 2 MiB with metadata; running output stays intact", async () => {
  const max = 2 * 1024 * 1024;
  const originalCmd = process.env.AGENT_HUB_FAKE_CMD;
  const script = join(vault, "large-output.mjs");
  const payload = Buffer.concat([Buffer.alloc(257, "a"), Buffer.alloc(max - 5, "b"), Buffer.from("tail\n")]);
  try {
    for (const [label, code, delay, timeout] of [["done", 0, 1200, 10], ["failed", 1, 1200, 10], ["timeout", 0, 30000, 2]]) {
      writeFileSync(script, `process.stdin.resume();
        process.stdout.write(Buffer.concat([Buffer.alloc(257, "a"), Buffer.alloc(${max - 5}, "b"), Buffer.from("tail\\n")]),
          () => setTimeout(() => process.exit(${code}), ${delay}));`);
      process.env.AGENT_HUB_FAKE_CMD = script;
      const first = JSON.parse(await hub.callTool("ask_codex", { prompt: label, wait_s: 0, timeout_s: timeout }));
      const output = join(jobsDir, `${first.job_id}.out`);
      const json = join(jobsDir, `${first.job_id}.json`);
      const deadline = Date.now() + 1500;
      while (statSync(output).size < payload.length && Date.now() < deadline) await sleep(20);
      assert.equal(JSON.parse(readFileSync(json)).status, "running");
      assert.deepEqual(readFileSync(output), payload, "running output is not truncated");
      await until(hub, first.job_id);
      // Timeout publishes failed before the killed child's stdout has closed.
      const end = Date.now() + 3000;
      let rec;
      do {
        rec = JSON.parse(readFileSync(json));
        if (rec.memory_filed) break;
        await sleep(20);
      } while (Date.now() < end);
      assert.equal(rec.status, label === "done" ? "done" : "failed");
      assert.equal(rec.memory_filed, true, "finish completed");
      assert.deepEqual(readFileSync(output), payload.subarray(-max), `${label}: exact byte tail retained`);
      assert.equal(rec.output_truncated, true);
      assert.equal(rec.output_original_bytes, payload.length);
      assert.equal(rec.output_retained_bytes, max);
      assert.equal(rec.output_truncation_error, undefined);
    }
  } finally {
    if (originalCmd === undefined) delete process.env.AGENT_HUB_FAKE_CMD;
    else process.env.AGENT_HUB_FAKE_CMD = originalCmd;
  }
});

test("job files: output at or below the limit is unchanged and a linked outside log is not overwritten", async () => {
  const max = 2 * 1024 * 1024;
  const originalCmd = process.env.AGENT_HUB_FAKE_CMD;
  const script = join(vault, "boundary-output.mjs");
  try {
    for (const size of [max - 1, max, max + 1]) {
      writeFileSync(script, `process.stdin.resume();
        process.stdout.write(Buffer.alloc(${size}, "x"), () => setTimeout(() => process.exit(0), 1200));`);
      process.env.AGENT_HUB_FAKE_CMD = script;
      const first = JSON.parse(await hub.callTool("ask_codex", { prompt: "output boundary", wait_s: 0 }));
      const file = join(jobsDir, `${first.job_id}.out`);
      const data = Buffer.alloc(size, "x");
      const deadline = Date.now() + 1500;
      while (statSync(file).size < size && Date.now() < deadline) await sleep(20);
      assert.equal((await status(hub, first.job_id)).status, "running");
      const outside = join(vault, `linked-${size}.out`);
      linkSync(file, outside);
      const s = await until(hub, first.job_id);
      assert.equal(s.status, "done");
      assert.deepEqual(readFileSync(file), data.subarray(-max));
      assert.deepEqual(readFileSync(outside), data);
      const rec = JSON.parse(readFileSync(join(jobsDir, `${first.job_id}.json`)));
      assert.equal(rec.output_truncated, size > max ? true : undefined);
      assert.equal(rec.output_truncation_error, undefined);
    }
  } finally {
    if (originalCmd === undefined) delete process.env.AGENT_HUB_FAKE_CMD;
    else process.env.AGENT_HUB_FAKE_CMD = originalCmd;
  }
});
