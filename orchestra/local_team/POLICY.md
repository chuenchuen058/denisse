# Local-team policy

`rules.json` holds runtime defaults, GPU and lease timing, evidence bounds and
signals, planner priorities, librarian filters, baseline run settings and role
system prompts. Role metadata remains in `roles/*.md`; output constraints remain
in `roles/*.schema.json`. The librarian's output count comes from its schema.

`load_config()` applies `rules.json` defaults, then the existing `config.json`,
then `models.json`; explicit `Ctx.cfg` values win last. The last two files retain
their existing repo-local lookup and checkout fallback. Changing a default that
is already overridden requires changing that override too. New contexts read
the current defaults; other operations read policy when called. Explicit CLI and
function arguments override baseline defaults.

Counts and character bounds must be positive integers, durations positive
numbers, patterns valid regular expressions and templates must keep their named
placeholders. Keep role prompts consistent with their output schemas when editing
constraints. Policy is required: malformed or missing data is not silently
replaced. Fixed-format parsers, protocol identifiers, indices and OS constants
remain code.
