---
name: python
description: How to answer Python questions and write/debug Python code reliably
---

# Python

A local model can sound completely confident about Python and still be
wrong -- inventing a function that doesn't exist, misremembering a
signature, or describing what code "would" print instead of what it
actually does. Treat confidence and correctness as separate things.

1. **Never claim what code outputs without running it.** For anything more
   than a one-line, obviously-correct expression, use `run_shell_command`
   to actually execute it (`python -c "..."` or a temp script) and report
   the real output -- same principle as the math skill's "never compute by
   hand," applied to code instead of arithmetic.
2. **Don't guess at APIs.** If unsure whether a stdlib function, method, or
   third-party library call exists (or its exact signature/argument
   names), check rather than inventing something plausible-sounding --
   `python -c "help(obj.method)"`, `pip show <package>`, or reading the
   installed package's source are all fair game via `run_shell_command`.
   A wrong but confident-sounding API name is worse than saying "let me
   check."
3. **Use the right interpreter for the context.** Inside a project that has
   its own `.venv` (like this repo), use that venv's `python.exe`, not
   whatever's on PATH -- a global install may be missing the packages the
   project actually needs, or be a different Python version entirely.
   Check `requirements.txt` / `pyproject.toml` before assuming a package
   is available.
4. **Debugging: read before guessing.** Look at the actual traceback and
   the actual file content before proposing a fix. A symptom ("it crashes
   on line 40") can have several plausible causes -- don't pattern-match
   to the first one that comes to mind without confirming it against what
   the code and error actually say.
5. **Give complete, runnable code**, not fragments that assume undefined
   variables or skip imports -- someone should be able to copy-paste it
   and have it work, or clearly see what to fill in if it's a template.
6. **Package installs need a venv in mind.** `pip install` outside any
   virtual environment modifies the global/user Python install -- ask or
   note this before running it if it's not obviously already inside a
   project's venv.
7. **Pitch explanations to the question.** A syntax question gets a short,
   direct answer; "why does Python do X" gets the actual reasoning, not a
   textbook chapter. Don't over-explain basics to someone who's clearly
   already past them (judge from how the question is phrased).
8. If a tool call (running code, checking a package) fails or errors,
   report that plainly -- don't fall back to describing what the code
   would probably do instead.
