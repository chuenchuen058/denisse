# Denisse as study tutor — part 1: ingest, syllabus and methods

Chuen asked on 5 Nov 2026 for Denisse to be his study tutor: hold everything
from his KEATS modules, extract each lecture's syllabus and methods, teach him
what he doesn't know, give him questions, track his progress. This is the
first of four tasks: the `tutor/` package that reads the KEATS material and
turns it into **tutor notes** in the vault and a **topic map** per module.
The other three plug into those (see the end).

His choices, which every part keeps to:

- part of Denisse, not a separate app;
- KEATS material arrives in a folder he downloads into — nothing here signs
  in to KEATS or scrapes it;
- the local model (Ollama, the `chat` entry in `config.json`) does the daily
  reading; Claude through `delegate.py` is for hard explanations and marking
  (task 2+), never an API key;
- question review happens in Anki (task 3).

## How it works

```
python -m tutor ingest [--dry]
python -m tutor topics <module>
```

`ingest` does five things, in `tutor/ingest.py`:

1. **Scan.** Every file under each module's inbox folder (`KEATS inbox`
   inside `Atlas/Academic/SEM 1 Modules/<code> <module>/`) and under the
   shared inbox folders (`paths.inboxes.shared`, `~/Downloads/KEATS` by
   default; skipped when absent). Modules are the folder names under
   `paths.modules` — there is no module list anywhere.
2. **Match** each file to a module and a week: from the folder it sits in
   (a module's inbox, a `Week N` subfolder), then its file name (`Lecture 5
   ...`, `W4 ...`, `week_4`), then the first page of its text (a module code
   like `5CCYB020`, the module's name, `Week 6`). Nothing is asked. A file
   that can't be placed is recorded as *unmatched* with the reason and left
   alone; it is tried again next run (a rename or a better file name fixes
   it).
3. **Dedupe** by content hash. A file already read is not read again. A
   week whose set of files is unchanged is not re-extracted, so a nightly
   run on an unchanged inbox makes no model call at all.
4. **Extract.** Text comes out of PDF (PyMuPDF, falling back to pypdf), PPTX
   and DOCX (their XML, read straight out of the zip — no python-pptx or
   python-docx, no pip), TXT and MD. Audio is off unless
   `extract.audio.enabled` is true (then the installed faster-whisper, the
   `whisper` model entry). The local model reads the week's text in chunks
   sized to the brain's context (`brain.json` `limits.num_ctx`, `DENISSE_NUM_CTX`
   winning, less `model.reserve_tokens`), answering each chunk with one JSON
   object (`prompt.schema`): learning outcomes, methods with LaTeX
   equations, worked examples, check-yourself questions, topics with
   prerequisites. A reply that isn't JSON gets one retry; a chunk that never
   gives JSON fails the week, and **no note is written from a half-read
   lecture** — it is tried again next run.
5. **Write.** The tutor note goes into the module's `Week N` folder as
   `<Module> Week N tutor.md`; the module's topic map to
   `memory/tutor/topics-<slug>.json`; and a `<Module> tutor.md` index in the
   module folder linking every tutor note and listing the topic map.

Files stay in the inbox: the ingest never moves, renames or deletes his
downloads. (Filing them into the Week folders is `scripts/nightly.py`'s
existing job and is unchanged.)

### The tutor note

Every line comes from a template in `tutor.json` (`note`). Frontmatter
carries `name`, `description`, `updated`, `tags`, `module_code`, `module`,
`week` and `generated_by: denisse-tutor`, so the vault contract
(`scripts/vault_contract.py`) passes. The `name` slug is
`<module-slug>-week-<n>-tutor`, unique across the vault. Links are only
made to things that exist: the week note, the module hub, the tutor index,
and each source file (path-qualified when it is inside the vault, a plain
file name when it came from a shared inbox outside it).

Sections, in order: Sources · Learning outcomes · Key methods and
equations · Worked examples · Check yourself (a tick list) · Topics (each
with what it assumes). Caps on each section are `limits`.

### Never over his work

The state remembers the hash of every note Denisse wrote. A note on disk
whose hash differs is his — written or edited by him — and is never
touched: the new version goes beside it as `<Module> Week N tutor (tutor
update).md` with its own `name` slug, and the module index says so. The
same rule covers the index itself. A note is only regenerated when its
sources changed; an unchanged week is skipped before any model call.

### The topic map

`memory/tutor/topics-<slug>.json`:

```json
{"module": "Signals and Systems", "code": "5CCYB020", "updated": "2026-11-05",
 "topics": [{"name": "Laplace transform", "lectures": [5, 6],
             "prerequisites": ["Fourier transform"], "coursework": ["Q2 table"]}]}
```

A topic named in two weeks is one topic covering both. `coursework` links
come from the module's Coursework note read by `study_brief.read_coursework`
(the same reader the study brief uses): a question whose topic names every
word of the topic. `python -m tutor topics signals` prints the map (module
by name, code or a word of its title).

## Config: `tutor/tutor.json`

Rule 0: nothing in `tutor/*.py` names a path, a module, a model, a prompt
or a limit; `tutor/test_tutor.py` checks the string constants for that.

| Key | What it holds |
|---|---|
| `paths.modules`, `paths.week_dir`, `paths.week_note`, `paths.hub_note` | The vault layout (same as `scripts/next_lecture.json`). |
| `paths.inboxes.module`, `paths.inboxes.shared` | The inbox folder name inside each module, and extra folders (`~`, `{vault}` allowed). |
| `paths.coursework_globs` | Where a module's coursework note(s) are, for the topic map. |
| `paths.state`, `paths.state_env` | Private state folder (repo-relative, git-ignored) and its env override `DENISSE_TUTOR_STATE`. |
| `paths.context_file`, `paths.context_key`, `paths.context_env` | Where the brain's `num_ctx` is read from. |
| `modules.*` | How a folder name becomes a code and a title; connectors ignored when matching names. |
| `match.week`, `match.week_dir`, `match.code`, `match.content_chars` | The regexes that place a file, and how much of its text is looked at. |
| `match.unmatched_reasons` | What an unmatched file is recorded with. |
| `extract.*` | Extensions per reader, the Office XML parts and tags, audio on/off, the minimum text length. |
| `model.*` | Which `config.json` model entry, temperature, keep-alive, chunk sizing, JSON retries. |
| `prompt.*` | The system prompt, the per-chunk user prompt, the JSON schema, the retry nudge. |
| `limits.*` | Caps per note section and per item. |
| `note.*` | Every file name, frontmatter line and section template of the tutor note and the index. |
| `topics.*` | Word rules for linking topics to coursework. |
| `say.*` | Every line `ingest` prints. |

State: `memory/tutor/ingest.json` (`files`: hash → where it was placed;
`lectures`: module → week → signature, note, topics; `written`: note path →
hash of what she wrote). Private, git-ignored; delete it to re-read
everything (notes she wrote are then treated as his and copied beside, not
overwritten — safe, just untidy).

## Tests

```
.venv\Scripts\python.exe -m unittest discover -s tutor -p "test_*.py" -v
```

`tutor/test_tutor.py` builds a fixture vault and a fixture inbox (a PDF
through PyMuPDF, a PPTX and a DOCX as hand-made zips, a TXT, an MP3, a
shared inbox outside the vault) and runs the ingest with a fake model that
answers from the chunk it is given. It checks the end-to-end run, that a
second run makes no model call, that a changed file regenerates only its
week, that his edited note is never overwritten, the dry run, model
failure, chunking and merging, the CLI, and that
`scripts/vault_contract.py` passes on the result (its `VAULT` constant is
swapped in the AST and every check run, the way `atrium/test_import_chats.py`
and `scripts/vault_index_fix.py` do it — the checker still carries the real
path as a literal, which is its own Rule 0 job). It never touches the real
vault, `memory/`, Ollama or the network, and runs under
`scripts/run_checks.py`'s guard.

## What is not here yet (tasks 2–4)

- **Task 2 — teaching.** Reads the topic map and the tutor notes; asks the
  learner model (`learner/`) how he learns; explains a topic with the local
  model, and hands the hard ones to Claude through `delegate.py`. Hook:
  `topics.load(paths, module)` and the `## Topics` / `## Key methods` sections
  of each note.
- **Task 3 — questions to Anki.** Turns each note's `## Check yourself` list
  and the methods into cards; the Anki export format and deck naming go in
  `tutor.json`. Hook: the note's check list and `result["check"]` from
  `model.extract_lecture`.
- **Task 4 — progress and tutoring.** Tracks what he has reviewed, where he
  struggles, and marks his answers (Claude via `delegate.py` for marking).
  State joins `memory/tutor/`; the per-topic `lectures` and `coursework`
  fields say what to revise before which question. `study_brief.py` can
  then cite the tutor note for each unwritten week.
