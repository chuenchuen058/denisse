// node --test orchestra/agent_hub/test_hub.mjs   (fake agents, temp vault; no model calls)
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync, writeFileSync } from "node:fs";
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
const hub = await import("./server.mjs");
const HERE = dirname(fileURLToPath(import.meta.url));
const FAKE_CMD = join(HERE, "fake_agent.mjs");

test("routing picks the agent from the kind, or guesses from the words", () => {
  assert.equal(hub.chooseRoute("anything", "long_read").agent, "gemini");
  assert.equal(hub.chooseRoute("fix the tray restart bug in tray.py").agent, "codex");
  assert.equal(hub.chooseRoute("fix the tray restart bug in tray.py").write, true);
  assert.equal(hub.chooseRoute("summarise this 40 page pdf").agent, "gemini");
  assert.equal(hub.chooseRoute("list my open PRs on github").agent, "copilot");
  assert.equal(hub.chooseRoute("hello").guessed, true);
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
  for (const n of ["ask_codex", "ask_gemini", "ask_copilot", "route_task", "split_tasks", "ask_all", "remember", "memory_today",
    "job_status", "job_result", "jobs"])
    assert.ok(names.includes(n), n);
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
