// node --test orchestra/agent_hub/test_hub.mjs   (fake agents, temp vault; no model calls)
import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const vault = mkdtempSync(join(tmpdir(), "vault-"));
process.env.AGENT_HUB_VAULT = vault;
process.env.AGENT_HUB_FAKE = "1";
process.env.AGENT_HUB_CONTEXT = "0";
const hub = await import("./server.mjs");
const HERE = dirname(fileURLToPath(import.meta.url));

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
  for (const n of ["ask_codex", "ask_gemini", "ask_copilot", "route_task", "split_tasks", "ask_all", "remember", "memory_today"])
    assert.ok(names.includes(n), n);
});
