# .review — Orchestra task queue

Copied from Thomas van Pul's Atrium core loop (31 Oct 2026), rebuilt for Windows.

- `queue/` one Markdown file per job, named `YYYY-MM-DDTHHMM-slug.md`, from
  `TEMPLATE.md`. `parked/` holds tasks that need a person, with a `.why.txt`;
  `archive/<date>/` holds finished ones; `reports/` the lane logs.
- `orchestra/runner.py` makes a worktree at `..\denisse-wt\<slug>` on branch
  `review/<slug>`, runs the lane with `orchestra/review_prompt.md`, and merges
  a PASS only after re-running its gate. Copy that prompt to
  `.claude/commands/review.md` if you want `/review` in interactive sessions too.
- `STATE_NOW.md` is regenerated after every tick; `runner.log` has one line
  per event.

**How to run it day to day** (writing a task, status, requeue, pause, what
each log line means): `docs/operator_guide.md`. Every command:
`orchestra/README.md`. Limits: `orchestra/config.json`.

Everything here except this README and TEMPLATE.md is git-ignored: tasks
and reports can hold private details and this repo is public.

Not copied on purpose: tasks generated from log errors, a model council,
anything that sends as Chuen without a one-tap approval.
