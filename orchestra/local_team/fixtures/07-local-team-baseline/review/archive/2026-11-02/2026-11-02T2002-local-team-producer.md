# Local agent team on Ollama + a task producer that keeps the queue supplied without a chat
Queue: auto
Priority: normal
Weight: heavy
Kind: work
Lane: claude
Written: 2026-11-02T20:02

Master prompt sections 7, 8 and 9 (read them: `<private path>`). Today new Orchestra tasks come only from a Claude chat (the owner's, or a scheduled check-in). Build the part that runs on its own, on the laptop's local model.

1. `orchestra/local_team/` with small versioned role files (`roles/*.md` + an output JSON schema each) and one runner `python -m orchestra.local_team <role>`:
   - **scout**: reads a bounded delta since its last run (`.review/runner.log`, `.review/parked/*.why.txt`, archived reports' NOT DONE / ODD sections, failing checks from `scripts/run_checks.py --fast` output if present) and emits candidate problems with evidence paths.
   - **planner**: turns one candidate into a draft task packet (title, objective, evidence, scope paths, acceptance, gate chosen from the registry below).
   - **librarian**: turns a finished report into ≤3 concise FOR MEMORY lines with provenance (data only; it never writes the vault).
   Use Ollama at http://127.0.0.1:11434 with the installed model (check `ollama list`; `llama3.1:8b` is expected). Never pull/download a model. One local inference at a time via a file lease (`.review/gpu.lease`) that Denisse-heavy jobs also respect; skip and log if Denisse is using the GPU heavily or Ollama isn't up.
2. **Admission layer** (deterministic, `orchestra/admission.py`) owns the queue write: validates the packet schema, scope paths inside the repo, no vault/home writes, gate commands only from `orchestra/gates.json` (a registry of approved commands per area, e.g. `unittest <module>`, `node --test <file>`, `scripts/run_checks.py --fast`), dedupes against queued/running/archived/parked tasks (normalised goal + affected files), caps the queue at 20 and new admissions at 4 per hour. Admitted local-model tasks go in as `Queue: draft` unless their area is on an allowlist in config (`auto_admit_areas`, default empty) — Claude chats/check-ins can promote them.
3. Schedule: `orchestra/install_task.ps1` gains an optional "Denisse Orchestra Producer" every 30 minutes (don't register it yourself).
4. Tests with a fake Ollama (local HTTP stub) and temp repo: scout on fixture logs, planner output validation, admission rejecting a bad path / unregistered gate / duplicate / flood, the GPU lease.
5. Docs in `orchestra/README.md`: what each role does, what it can't do, how to turn auto-admit on per area.

## Gate
```
.venv\Scripts\python.exe -m unittest discover -s orchestra/local_team -p "test_*.py" -v
.venv\Scripts\python.exe -m unittest orchestra.test_admission -v
```
