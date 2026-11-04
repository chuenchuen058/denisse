# Exam policy

`rules.json` holds score windows, failure/silence thresholds, category aliases,
ordered title routes, fallback preferences, canary counts, timeouts, agent prompt
templates and paths. The defaults preserve the previous Python values. Exam
questions and their grading data remain in `bank/*.json`.

Title routes are tried in order only when no explicit kind is supplied. Unknown
explicit kinds keep the fallback lane. Scoring fallback preferences are separate
from daily agents that require the existing opt-in flag.

Policy is read when operations run. Compatibility constants and the exam Ollama
endpoint are loaded at import, so restart the process after changing those.
Runner `config.json` overrides such as `exams_agents` and `exams_cap_s` still win;
an absent daily schedule still disables scheduled exams. Explicit timeout
arguments and per-exam timeouts retain their existing precedence.

Keep counts and character limits positive integers, durations positive numbers,
routes valid regular expressions, and template placeholders unchanged. Missing,
malformed or incomplete policy data raises an error instead of silently restoring
Python defaults. Fixed-format parsers, protocol names, indices, formatting widths
and OS constants remain code.
