# Graphify trial: does a code graph cut how much our agents re-read?

**Verdict: no.** Don't add Graphify to the agents or the Orchestra runner.

I measured three real Claude-lane runs from 9 Nov 2026. Even in a best case
that can't happen (the graph replaces *every* code-exploration read and
costs only one query), the most it could save is **9–21 %** of a run's
cache-read tokens. A more realistic estimate, scaled by how often the
graph's answer named the files the agent actually changed, is **−0.4 % to
+4.5 %**: about zero. The agents hardly re-read files at all. Across the
three runs there was **one** repeated file read. Most cache-read comes from
the fixed start-of-session prefix and the agent's own growing conversation
being re-sent on every API call, and a code graph touches neither.

This finishes `2026-11-09T1230-trial-graphify` (PARTIAL). That run checked
Graphify's identity and risk and measured three one-shot questions (graph
path 9–14k chars against 5–7k for grep plus one slice). This run measures
real tasks.

## Method

Every Claude-lane run leaves a stream-json log at
`.review/reports/<task>.lane.log`. `scripts/graphify_trial.py` reads one
and, for each tool result, records the characters it returned and the
number of later API calls in the same thread. Each later call re-reads the
whole prefix from cache, so `chars / 4 × later calls` is that result's
share of the run's cache-read tokens. The script then groups those shares
by category. Its classification lists live in `scripts/graphify_trial.json`.

```
.venv\Scripts\python.exe scripts\graphify_trial.py <lane.log> [...] [--json]
```

- **Without** = measured: the run as it actually happened. The cache-read
  totals come from the API's own per-call usage (they match the run's
  final `result` record exactly). The splits by category are estimates at
  4 chars per token.
- **With** = modelled. I built Graphify 0.9.97's code graph (`graphify
  update`, local tree-sitter, no LLM or network) on a `git archive` of each
  task's base commit in a temp dir. Then I ran `graphify query "<task
  title>"`, which is the first move an agent with the graph would make, and
  recorded (a) the size of the output and (b) which of the files the task
  really changed it named.
  - Ceiling (A): the graph removes *all* graph-addressable reads (first
    reads of code, re-reads, Grep/Glob, read-only shell on code files), and
    its query output stays in context for the rest of the run.
  - Realistic (B): the same, but only the fraction of those reads matching
    the query's recall of edited files. This is still generous, because a
    named file still has to be read before it can be edited.
- All three runs are Claude lane, full length, not limit-interrupted, and
  merged. No Codex run is mixed in (Codex logs `tokens n/a`).

## Runs

| Task | Base | API calls | Cache-read | Cost |
|---|---|---|---|---|
| `2026-11-09T2246` agent cursors on the live Atrium page | `577c98e` | 32 | 2,517,716 | $1.89 |
| `2026-11-09T2124` hermetic temp-repo test fixtures | `af2bb68` | 58 | 5,652,322 | $3.06 |
| `2026-11-09T1420` fix orchestra gate: pre-push + 600 s | `5e85715` | 57 | 4,313,101 | $2.32 |

## Where the cache-read goes (without the graph)

| | cursors | hermetic | prepush |
|---|---|---|---|
| Start prefix (system prompt, tools, CLAUDE.md files) × every call | 958k (**38 %**) | 1,763k (**31 %**) | 1,565k (**36 %**) |
| All tool results | 705k (28 %) | 1,473k (26 %) | 1,031k (24 %) |
| …of which graph-addressable (code exploration) | 575k (23 %) | 912k (16 %) | 469k (11 %) |
| Rest (agent's own messages, tool inputs, estimation error) | 855k (34 %) | 2,416k (43 %) | 1,717k (40 %) |
| Code files read (approx., incl. globs) | 14 | 33 | 14 |
| Repeat reads of the same file | 0 | 1 | 0 |

The start prefix alone is 28–31k tokens, re-sent on every call.

## With the graph

| | cursors | hermetic | prepush |
|---|---|---|---|
| Graph build (cold) | 27 s, 25 MB | 25 s, 25 MB | 24 s, 25 MB |
| Query output for the task title | 6,658 chars | 6,715 chars | 6,742 chars |
| Distinct files the query names | 74 | 46 | 51 |
| Changed code files it names (recall) | 2 / 7 | 3 / 18 | 1 / 6 |
| Query kept in context for the run | ≈52k tok | ≈96k tok | ≈94k tok |
| **Ceiling A saving** | 523k (20.8 %) | 816k (14.4 %) | 375k (8.7 %) |
| **Realistic B saving** | 113k (4.5 %) | 56k (1.0 %) | −16k (−0.4 %) |

Files read with the graph: no fewer in practice. In each run the query
missed most of the files the agent changed: `atrium/cursors.py` and the
config, `testing_git.py` and most of the test fixtures, `run_checks.py`
and the soak/local-team tests. Its top hits were broad hubs such as
`atrium/conf.py`, `tutor/conf.py` and `settings.py`. The agent would still
have had to grep its way to the right files. Recall is matched by
basename, so it errs high.

## Why the answer is no

1. **The premise is off.** "Agents keep re-reading the same files" is not
   what these logs show: one repeated read in 147 API calls. The ~95 %
   cache-read share is the *conversation* being re-sent each call, and a
   code graph doesn't shrink it.
2. **The ceiling is small, and unreachable.** A graph can only replace
   code exploration, at most 11–23 % here, and it can't remove the reads
   of files being edited.
3. **What it really retrieves is poor for our tasks.** Task titles describe
   behaviour ("hermetic", "overlap warning"), and the graph's BFS lands on
   high-degree config hubs. The earlier trial found the same on one-shot
   questions.

## What would actually move the number (not done here)

- **Shrink the start prefix** (31–38 %): fewer always-loaded tools and MCP
  servers in lane sessions, and shorter always-loaded instruction files.
  Every 1k tokens cut saves about 1k × API calls per run.
- **Fewer, fuller API calls**: batch independent reads into one turn.
  Each call re-reads the prefix plus everything added so far, so
  cache-read grows faster than the number of calls.
- **Narrower tool output**: `grep -n` plus line slices, not whole files
  (already mostly the habit).

## Not covered

- No live A/B re-run: the "with" side is modelled from real graph output,
  not from an agent actually using the graph. One pair of non-deterministic
  runs per task would not have been more conclusive than the ceiling.
- Graphify's semantic/LLM mode and MCP server were not tried. Semantic
  mode sends file contents to an LLM backend.
- Subagent threads: none of the three runs used them.
- Token splits use 4 chars per token. The totals are exact.
- The graph is still installed at user level (from 28 Oct, see the 1230
  report). Whether to remove it is Chuen's call, and this trial didn't
  change it.
