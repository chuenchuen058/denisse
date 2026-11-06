// Fake agent CLI for test_hub.mjs (AGENT_HUB_FAKE_CMD): node fake_agent.mjs <exe> ...args.
// Reads the prompt from args + stdin. Knobs in the prompt: SLEEP=<ms>, TAG=<word>,
// GRANDCHILD (spawn a long-lived child and print its pid, to test kill-tree), EXIT=<code>,
// ECHO (print 800 chars of the prompt it got, from its first "TASK:" marker when there is one,
// so a test can see what was put in front of the task).
import { spawn } from "node:child_process";

const exe = process.argv[2];
let input = process.argv.slice(3).join(" ");
process.stdin.on("data", (d) => { input += d; });
process.stdin.on("end", async () => {
  const sleep = Number(input.match(/SLEEP=(\d+)/)?.[1] || 0);
  const tag = input.match(/TAG=(\w+)/)?.[1] || "";
  if (/GRANDCHILD/.test(input)) {
    // detached: outside libuv's kill-on-close job object, like a native CLI's own children,
    // so only a real tree kill (taskkill /T) takes it down
    const g = spawn(process.execPath, ["-e", "setTimeout(() => {}, 60000)"], { stdio: "ignore", detached: true });
    g.unref();
    console.log(`grandchild pid ${g.pid}`);
  }
  if (/ECHO/.test(input)) {
    const at = Math.max(0, input.indexOf("TASK:"));
    console.log(`prompt: ${input.slice(at, at + 800).replace(/\s+/g, " ")}`);
  }
  const steps = 5;
  for (let i = 1; i <= steps; i++) {
    console.log(`working ${i}/${steps}`);
    await new Promise((r) => setTimeout(r, sleep / steps));
  }
  console.log(`fake ${exe} answer ${tag}\nFOR MEMORY:\n- fake fact from ${exe} ${tag}`);
  process.exitCode = Number(input.match(/EXIT=(\d+)/)?.[1] || 0);
});
