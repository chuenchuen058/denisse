# .review — Orchestra task queue

Copied from Thomas van Pul's Atrium core loop (31 Oct 2026), rebuilt for Windows.

- `queue/` one Markdown file per job, named `YYYY-MM-DDTHHMM-slug.md`. Copy
  `TEMPLATE.md`, or run `python orchestra\new_task.py "title"`.
- `orchestra/runner.py` picks the top runnable task (urgent first, his work
  before maintenance), makes a worktree at `..\denisse-wt\<slug>` on branch
  `review/<slug>`, runs the lane (Claude Code or Codex) with
  `orchestra/review_prompt.md`, and reads the report. Copy that file to
  `.claude/commands/review.md` if you want `/review` in interactive sessions too.
- PASS -> merged (only if the main checkout is clean) and moved to
  `archive/<date>/`. HUMAN line set -> `parked/` as waiting on Chuen.
  Otherwise retried with backoff, max 3 attempts, then `parked/`.
- `STATE_NOW.md` is regenerated after every tick.

Everything here except this README and TEMPLATE.md is git-ignored: tasks
and reports can hold private details and this repo is public.

Not copied on purpose: tasks generated from log errors, a model council,
anything that sends as Chuen without a one-tap approval.
