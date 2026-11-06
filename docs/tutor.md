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
   `topics-<slug>.json` in the Don area (part 2); and a `<Module> tutor.md` index in the
   module folder linking every tutor note and listing the topic map.

Files stay in the inbox: the ingest never moves, renames or deletes his
downloads. (Filing them into the Week folders is `scripts/nightly.py`'s
existing job and is unchanged.)

### Scanned PDF pages

`python -m tutor ingest` also reads image-only and mixed PDFs through local
Windows OCR (`Windows.Media.Ocr`). PyMuPDF renders each page whose stripped
native text is shorter than `extract.ocr.min_text_chars`. The installed
Windows PowerShell WinRT bridge supplies OCR; no pip package, installer,
cloud request or language download is used. Windows' installed OCR
languages determine availability.

All options are in `tutor/tutor.json`, under `extract.ocr`: `enabled`,
`engine` (`windows`), `language` (empty selects the user profile language),
`min_text_chars` (20), `dpi` (200), and `page_cap` (100 sparse pages per PDF).
Native-text pages do not consume the cap. Large pages render at a lower DPI
when the engine's runtime image limit requires it. The bridge timeout and
launch command are under `windows`; each call has a bounded lifetime and
no visible console window. Set `enabled` to false for native text only.

The model input marks OCR text as potentially noisy. Tutor notes warn which
source pages used OCR and which remain unread because of failure or the
cap; native text is retained. Equations, handwriting, diagrams and reading
order still need checking against the scan. The engine is ordinary text
recognition, not an equation parser.

Successful OCR pages (including empty results) are saved atomically under
`<Don area>/data/ocr/`, addressed by the PDF SHA-256 and OCR settings. Each
JSON sidecar contains page text, native text, engine, actual language, DPI
and render/recognition time. A rename, repeat ingest or failed model pass
reuses that cache. Changing the file or OCR settings invalidates it.
Interrupted OCR resumes from saved pages. Sidecars contain source material
and belong with private tutor data, never in the public code repository.

Old skipped scans and mixed PDFs are reconsidered when OCR settings change.
Recognition failure logs one line per ingest, preserves native text, and
is retried next time. Raising the cap reconsiders capped pages. Notes are
regenerated when extracted content or coverage changes, with the existing
protection for user edits. `python -m tutor ingest --dry` can perform OCR
and read existing caches but writes neither cache, state nor notes.

The offline regression suite uses temporary generated scans and fake OCR
engines (`tutor/test_ocr.py`); it does not measure recognition quality. The
bridge uses Microsoft's [OcrEngine](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine)
and [BitmapDecoder](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.imaging.bitmapdecoder)
APIs.

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

`<Don area>/data/topics-<slug>.json`:

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

State: `ingest.json` (`files`: hash → where it was placed; `lectures`:
module → week → signature, note, topics; `written`: note path → hash of
what the tutor wrote). Since part 2 it and the topic maps live in the
vault's Don area (`<Don area>/data/`), not `memory/tutor/` — see below.
Delete it to re-read everything (notes it wrote are then treated as his and
copied beside, not overwritten — safe, just untidy).

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

# Part 2: Don in the vault, cards and Anki

Chuen named the tutor **Don** (5 Nov 2026). The name is `tutor.json`
`"name"`; nothing in `tutor/*.py` says it (`tutor/test_don.py` checks).
Decks are `<name>::<module>`, the notes say Don wrote them, and commit
messages start with the name. (Since part 3 the chat tools introduce
themselves with it too.)

```
python -m tutor cards <module>       cards from every vault note of a module
python -m tutor anki-sync [module]   TSV per module; with AnkiConnect, add/update + review stats
```

### Don lives in the vault

Chuen: "Don should also have access to Obsidian, collect the files from there
and add my progress and memory back into it."

- **Memory moved into the vault.** What the foundation kept under
  `memory/tutor/` now lives in the Don area (`don.folder`, default
  `Atlas/Academic/Don/`): JSON sidecars in `data/` (`ingest.json`,
  `topics-<slug>.json`, `cards-<slug>.json`, `anki-<slug>.json`) and
  readable notes beside them: `Don.md` (the hub), `Don - <module>
  progress.md` (material read, cards, the Anki table, weak topics linked to
  the week's tutor note) and `Don - <module> cards.md` (every card under a
  link to its source). Every command except `topics` and `ingest --dry`
  first copies anything still only in `memory/tutor/` into `data/`
  (`don.migrate`: an existing vault copy is never overwritten, and the old
  copy is left in place). Until a file has been moved, it is read from the
  old place (`Paths.readable`).
- **Sources are the whole vault** (`tutor/sources.py`): every note in the
  module's folder at any depth (his own notes, Reference, Coursework, Week N,
  study briefs, the tutor notes), plus any vault note whose frontmatter
  (`sources.frontmatter_keys`) names the module's code, slug or title, or
  which links to its hub, title or folder (`sources.link_names`). Skipped:
  `sources.exclude_dirs` (`.obsidian`, Templates, Archive...), the Don area,
  the tutor index, `(tutor update)` copies, and notes under
  `sources.min_chars`. Each card cites its source as a vault link.
- **Notes follow the contract**: `name`/`description`/`updated` plus `type`,
  `status`, `tags`, `protection: open`, `generated_by`. They link only to
  notes that exist, and are never written over a note he edited (the same
  rule as tutor notes). The module tutor index links the hub, and the hub
  links each module's progress, cards and index. `scripts/vault_contract.py`
  passes on the fixture vault. The hub is not listed in
  `Atlas/Academic/Academic-Index.md`, because that is his note: add the line
  by hand.
- **Every vault write is committed** through `scripts/vault_git.commit_owned`:
  only the files the command wrote, never `.obsidian/` (`git.never_dirs`),
  with a message from `git.messages`, and nothing when the vault root has no
  `.git`. His own staged or unstaged changes are left alone.
- **The learner model** (`learner/`) is fed through its API, never by
  editing his notes. A weak topic becomes a `measured` belief in
  `learner.area` (default `struggles`), with the Anki numbers as evidence. A
  topic that recovers supersedes it (`learner.ok_claim`). The store and
  `Learner-Model.md` are committed with the rest.

### Cards (`tutor/cards.py`)

For each source note whose text changed since the last run, the model is
asked (`cards.system` / `cards.user`) for the kinds in `cards.kinds`, with a
count for each:

- `recall`: definitions and equations;
- `method`: which method to use, and why;
- `problem`: short worked problems.

Each kind names an engine:

- `local`: Ollama (`model.config_entry`);
- `claude`: `delegate.run(..., engines=["claude"])`, his Claude plan through
  the claude CLI, never an API key.

Every kind is local by default. For the harder problems, set
`cards.kinds.problem.engine` to `claude`. A reply that isn't JSON gets one
retry; a note that never gives JSON keeps its old cards.

Each card holds `id`, `kind`, `front`, `back`, `topic`, `source` (its vault
path), `week` and `engine`. The id is a hash of (module, source note, kind,
position), so a regenerated note updates its cards in Anki instead of adding
new ones. A question that two notes both produce is sent once.

### The check (`cards.check`)

Added 5 Nov 2026 after the first real run on Electromagnetism wrote wrong
answers, e.g. `∮B·dA = μ0 I / L` for the flux density and a garbled force
as "the field at a point charge". Chuen drills these cards, so a wrong one
teaches him a wrong fact. Every new or changed card is now checked against
its source note before it can reach Anki. The check asks three things: is
the physics or maths right, does the back actually answer the front, and
does it agree with the note.

- **Who checks**: each kind has a `check` engine. Recall cards go to `local`;
  method and problem cards go to `claude`, through `delegate.run`, his plan,
  never an API key. Set `cards.check.all_with` to `"claude"` to send every
  kind to Claude. Cards are checked in one batch per note and engine
  (`check.system` / `check.user` / `check.item` / `check.schema`).
- **Verdicts** (`check.verdicts`):
  - `right`: kept as written.
  - `fixed`: the checker said wrong, gave a corrected back, and quoted the
    note sentence behind it. The quote has to really be in the note: at
    least `evidence_min_words` words, with `evidence_min_overlap` of them
    found in the note in order. The corrected back replaces the old one,
    which is kept as `written_back`.
  - `wrong`: no correction, or one the note doesn't back. The card is held
    back.
  - `unchecked`: the checker didn't answer or gave no verdict for the card.
    The card is held back too, and checked again on the next run.
- **Held-back cards** go in the sidecar's `held` list and under a
  `## Held back` heading in the cards note, with the reason. They never go to
  the TSV or AnkiConnect: `anki-sync` sends `cards.checked_cards(deck)`, so
  a sidecar card with no passing check (including any written before the
  check existed) is never sent.
- **Per card**, `check` records `by`, `verdict`, `when`, `reason` and `key`
  (a hash of the card as written). On a re-run a card is checked again only
  if it is new or changed, if its checker engine changed, or if it was
  `unchecked`. An unchanged note makes no call at all.

### Anki (`tutor/anki.py`)

- **TSV** (`anki.tsv.enabled`): `<Don area>/Anki/Don - <module>.tsv`, with
  Anki's header lines (`#notetype`, `#deck`, `#guid column:1`, `#tags
  column`). File > Import puts it in the right deck, and a re-import updates
  by GUID. The Source column is an `obsidian://open` link.
- **AnkiConnect** (`anki.connect`, default `http://127.0.0.1:8765`). If it
  answers `version`:
  1. The deck and the note type (`Front/Back/Source/Topic/Id`, with
     templates and CSS from config) are created if missing.
  2. Notes are found with `"note:<type>" "deck:<deck>"` and matched on the
     `Id` field. A note is updated when a field differs, and added otherwise
     (`allowDuplicate: false`).
  3. `findCards`, `cardsInfo` and `areDue` give per-topic counts: cards,
     reviewed, reviews, lapses, mean ease and due. A topic is weak when its
     reviewed cards average `weak.lapse_rate` lapses, or its mean ease is
     under `weak.ease_below`; `weak.score_*` sets the order.
  4. The stats go to `anki-<slug>.json` and the progress note, and the weak
     topics go to the learner model.
- **No AnkiConnect** (switched off, or not answering): the TSV is written
  and nothing is recorded, because no stats does not mean no lapses.

`Id` is a fifth field, beside the four the task asked for. AnkiConnect
search needs it to update a card instead of duplicating it.

### Tests

`tutor/test_don.py` sets up:

- a fixture vault, with notes of his outside the module folder (tagged and
  linked), an unrelated note and an `.obsidian` file;
- a fake card writer for each engine;
- a fake AnkiConnect, served in-process on a free loopback port.

It checks:

- which notes count as sources;
- card kinds, ids and dedupe, the `claude` switch, and that `claude` goes
  through `delegate.run`;
- the check, with fake checkers (any engine a test doesn't fake raises, so
  Ollama and the claude CLI are never reached). It covers: the three bad
  cards from the first real run caught (one corrected from the note, two
  held back and kept out of the TSV); recall checked locally and
  method/problem by Claude; the `all_with` switch; a re-run checking only
  new or changed cards; a checker that's down (cards held, then checked
  next run); pre-check sidecar cards checked before Anki; and evidence
  that isn't in the note being refused;
- the TSV-only fallback, which records nothing;
- add, then unchanged, then update, with no duplicates;
- weak topics in the sidecar, the progress note and the learner model, and
  the belief being superseded when the topic recovers;
- migration from the old place;
- that the vault commit holds only Don's files;
- the CLI and the vault contract;
- that the tutor's name is not in the code.

# Part 3: Don teaches, quizzes, sets exam questions and marks -- in chat

Part 3 (5 Nov 2026) is Don as something Chuen talks to. Five tools in
`tools.py` (`tutor_explain`, `tutor_quiz`, `tutor_weak_topics`,
`tutor_exam_question`, `tutor_mark_answer`), described in `brain.json`,
policed and routed by `tool_policy.json`, implemented in `tutor/chat.py` over
`tutor/teach.py` (explaining) and `tutor/exam.py` (quizzes, exam problems,
marking). The same functions run from the command line:

```
python -m tutor explain <module> <topic>    "explain convolution"
python -m tutor quiz <module> [topic]       "quiz me on signals"
python -m tutor weak <module>               "what's weak in EM?"
python -m tutor exam <module> [topic]       "give me an exam question"
python -m tutor mark <module> <answer...>   his answer to the open question
```

Every result opens with the tutor introducing itself (`chat.intro`,
`"{tutor} here."`), so the model's reply can carry the name; when the reply
leaves it out, `tool_policy.json`'s `reply_must_mention` / `fallback_reply`
put that first sentence in front. For the quiz and the exam question the
reply is **replaced** by the result instead (`must_mention_replaces`): on the
first eval run the 8B model worked the problem itself rather than setting it,
and a reply that isn't in the tutor's voice is a reply that didn't relay the
question. What Chuen then sees is the questions verbatim -- the model-facing
instructions in those two results sit after ` Tell him`, the brain's result
cut marker, so they are cut off. The policy values are the name too
(`tutor/test_teach.py` checks they agree with `tutor.json`); nothing in
`tutor/*.py`, `tools.py` or `brain.json` says it.

### Explain (`tutor/teach.py`)

`teach.material` gathers what the vault holds on the topic: the topic map
entry (which weeks lectured it), those weeks' tutor notes in full, then every
other source note (`sources.collect`: tutor notes, then his own, capped at
`teach.own_notes_max`) whose text names the topic -- all cut to
`teach.material_share` of the model's chunk. No material means no model
call: the tool says there is nothing in the vault about it, rather than
teaching from general knowledge.

The prompt (`teach.system` / `teach.user`) also carries how he learns: the
learner model's current beliefs in `teach.learner_areas` (format, feedback,
struggles), read through `learner/`'s API and never written here. The model
answers JSON (`teach.schema`): a one-line summary, ordered steps, one check
question with its model answer, and `confident`.

Engines are `cards.engines` (local = Ollama, claude = `delegate.run`, his
Claude plan through the CLI). The local model goes first unless the topic is
**hard** (`teach.hard_topics`: Chuen's own list of topic words -- every word
of an entry in the topic, or the reverse). A local answer must pass
`teach.self_check` (enough steps of real length, a question, the model
confident); one that fails goes to the fallback engine. Claude runs **in the
background** (`chat.BACKGROUND`): the tool returns at once saying so, with
the local take meanwhile, and the finished lesson is handed to
`delegate.on_done` -- the tray's hook for a finished Claude job -- so it pops
up like any other hand-off. With `delegate.DRY_RUN` (the eval) nothing is
handed off and the tool says so.

Each lesson is recorded in the Don area: `data/lessons-<slug>.json` and `Don -
<module> lessons.md` (newest first, each with its sources as vault links and
the check question), the progress note gets a *Lessons and practice* section,
and every write is committed through `vault_git.commit_owned` (`git.messages
.teach`). Never over a note he edited (the same rule as every Don note).

### Quiz, weak topics (`tutor/exam.py`, `chat.py`)

- **`tutor_quiz`**: `quiz.questions` cards (5, also the end-of-round check),
  weak topics first by the Anki stats, then due, then the rest
  (`quiz.order`), narrowed to a topic when he named one. The questions go to
  the model; the answers stay in the practice sidecar as the **open item**.
  No cards yet: the tool says how to make them.
- **End of a study block**: `study_mode.set_round_end(fn)` (tray wires it to
  `tools.tutor_round_offer`). When a block ends and the rest starts, the
  clock's state line -- what the model is told and what the status tool says
  -- carries `quiz.offer`: round done, Don can give a 5-question check. Only
  when some module has cards; a failing hook never touches the clock. The
  bubble is unchanged.
- **`tutor_weak_topics`**: `anki-<slug>.json`'s weak topics with lapses,
  reviewed count and the weeks to revise, plus the learner model's
  `struggles` beliefs naming the module. No stats yet: said plainly, with
  the command that gets them; nothing is guessed.

### Exam practice and marking

`tutor_exam_question` reads the module's coursework and past papers
(`exam.paper_globs`, relative to the module folder; notes as text, PDFs and
Office files through `extract.py`) as the **style** to copy, and the topic's
material (or the latest tutor note when no topic was named) as the content.
The engine (`exam.engine`, local by default) answers JSON: title, the problem
with parts, total marks, and a mark scheme step by step. The scheme is kept
in the open item; the problem goes to the model and into `Don - <module> exam
practice.md` as *Open*.

`tutor_mark_answer` marks the open item (the module named, else the one set
last). A **quiz** is marked by `quiz.mark_engine` (local) against the card
backs, question by question, unanswered ones listed. An **exam** answer goes
to `exam.marking_engine` (Claude) with the mark scheme, in the background like
a hard explanation: marks per step with a comment he can act on, and
feedback on what to revise, popping up when done. Either way the result goes
into the practice history, the practice note (the question, each step's
marks, the feedback; quizzes as a score line) and the progress note.

### Modules, short forms

`conf.find_module` now takes the forms he says: a title's initials (`CAB`,
`SS`) or, for a one-word title, letters picked in order from its start
(`EM` for Electromagnetism) -- at least `modules.alias_min_chars` long, and
no match when two modules fit. With no module named, a topic that only one
module's topic map or cards know picks that module; one module with any data
is taken; otherwise the tool asks which.

### Config

| Key | What it holds |
|---|---|
| `teach.*` | Hard topics, learner areas, material caps, the prompts and schema, the self-check, the lessons note templates. |
| `quiz.*` | Count, order, the practice sidecar, the marking prompt, the end-of-round offer. |
| `exam.*` | Paper globs, style caps, engines, the setting and marking prompts and schemas, the practice note templates. |
| `chat.*` | Every sentence the tools return, `intro` first. |
| `don.progress_practice_*`, `don.lessons_link`, `don.practice_link` | The progress note's practice section and the links between Don's notes. |
| `git.messages.teach`, `git.messages.practice` | The vault commit messages. |

### Tests and eval

`tutor/test_teach.py` (on `test_don`'s fixture: ingest, cards, a fake
delegate module, the learner store in the fixture vault) checks the material
gathering, the local lesson and its records, the hard-topic and failed-check
hand-offs (and that they go through `delegate.run` when no call is injected),
the dry run, the honest no-material and no-JSON cases, module resolution and
short forms, the quiz order and marking, the round-end offer reaching the
clock, weak topics with and without stats, exam questions in coursework style
and their marking (inline and through the fake delegate), the registry,
policy, router and the name net in the brain, and the CLI. The vault contract
passes on the result.

`eval/cases.py` has five `tutor:*` cases (`run_eval.py --group tutor`): a
quiz, an explanation, weak topics with and without stats, an exam question.
The fixture vault carries a Week 4 tutor note, a topic map, six cards and
Anki stats for Signals (convolution weak), nothing for EM; a case gets a
scratch copy of it, so the tutor's writes never touch the checked-in fixture.

# Part 4: progress, per module

Part 4 (5 Nov 2026) is where Chuen stands, module by module, in one place:

```
python -m tutor progress [module]
```

`tutor/progress.py` reads only what the other parts left behind -- no model
call -- and for every module folder works out:

- **Lectures: read vs on KEATS.** The weeks the inbox holds: every file the
  last ingest placed (`ingest.json`), plus files downloaded since, placed by
  their folder and file name only (nothing is extracted here). Against them,
  the weeks that have a tutor note. A week on KEATS without one, or a new
  file, makes "run the ingest" the first thing to do.
- **Topics and mastery.** The topic map, plus any topic only the cards know,
  each with its Anki numbers from part 2's stats (`anki-<slug>.json`).
  Mastery is the share of the topic's cards reviewed times how well they are
  kept: `1 - (lapses per reviewed card) / progress.mastery.lapse_rate_zero`,
  floored at 0. None until something is reviewed; the module's figure is
  card-weighted.
- **Weak topics**: the stats' weak flags, worst score first, each with the
  tutor notes to revise from and the coursework items the topic map links.
- **Coursework**: every note matching `paths.coursework_globs` in the module
  folder, its due date from `planner.coursework_deadlines` (none = undated,
  never guessed) and its unticked items from `study_brief.read_coursework`.
- **What to study next**, in `progress.next.order`: lectures not read yet; a
  weak topic a coursework item due soonest needs; coursework due within
  `horizon_days`; other weak topics; cards due in Anki; topics never
  reviewed; topics with no cards. At most `next.max`.

It writes:

| Where | What |
|---|---|
| `Atlas/Academic/Study Progress.md` (`progress.note`) | The overview: a table of every module, then one section each. Only the block between `progress.block_start` and `block_end` is regenerated; everything he writes outside it is kept (only the frontmatter's `updated:` changes). A note of his without the markers gets the block appended. The Don hub links it. |
| `<Don area>/data/progress-<slug>.json` | One module in full -- what `study_brief.py` reads. |
| `<Don area>/data/study.json` (`progress.dataset_file`) | One flat row per module. `atrium/snapshot.py` copies it into the Atrium hub as `study.json` (the eleventh table); nothing run yet = an empty table. |

Weak topics are also filed in the learner model (`don.feed_learner`, the same
measured `struggles` beliefs `anki-sync` files; running it twice adds
nothing), the Don notes are refreshed, and every write is committed through
`vault_git.commit_owned` (`git.messages.progress`). With a module named, only
that module is recomputed; the others keep their last rows.

### The study brief puts weak topics first

`study_brief.py` reads the module's `progress-<slug>.json` through
`progress.focus` (step `tutor` of the brief job). With weak topics:

- each becomes a plan step at the front (`tutor.revise_step`, at most
  `tutor.revise_max`, weighted `tutor.revise_weight`);
- coursework items those topics feed (named in the topic map, or revised
  from one of their weeks) are planned before the others;
- the brief gets a *Weak topics first* section linking the tutor notes to
  revise from, and *Where you are* cites the tutor note for each week not
  written up.

No progress run, nothing weak, or anything unreadable: the brief is byte for
byte what it was (the golden briefs in `scripts/test_study_brief_learner.py`
still hold). Templates are `study_brief.json` `tutor`.

### Tests

`tutor/test_progress.py`, on `test_don`'s fixture (ingest, cards, a fake
AnkiConnect with Laplace lapsing and Fourier kept, a new lecture PDF dropped
in the inbox afterwards) checks: lectures read vs on KEATS, mastery, the weak
topic with its tutor notes and coursework, the coursework due date and items,
the next-step order (and that it follows the config), the sidecar, the hub
rows, the learner model fed once, the note (his edits outside the block kept,
inside replaced; a note without markers appended to), that the note is not a
card source, the vault contract, a one-module run keeping the others, the
CLI, and the study brief's weak-topic section, plan steps and order.

# Part 5: Don as the specialist every agent consults

Chuen (5 Nov 2026): "integrate it with the other AI models and treat Don like
a professional opinion on task". Whenever an agent works on something about
his modules, coursework, revision or learning, it asks Don first and weighs
the answer the way it would a subject expert's: informed and grounded in his
own lecture notes, advisory, never an override.

## The consult API (`tutor/consult.py`)

```
python -m tutor consult "<question>" [--module X] [--json]
```

One structured opinion, from `consult.consult(paths, question, module)`:

| Field | What it is |
|---|---|
| `answer` | What the notes say that bears on the question, from the model. |
| `confidence` | `low` / `medium` / `high`, from how well the notes cover the question (below). |
| `sources` | The vault notes the answer was given from, as vault links. Chosen by code and handed to the model; the model never names a source. |
| `caveats` | What the notes don't cover (the model's), plus anything that went wrong (no notes, a model that wasn't confident, a hand-off that was off or failed). |
| `who` | The engine that answered: `local` (Ollama) or `claude` (`delegate.run`, his plan, never an API key), or `none`. |
| `module`, `code`, `notes`, `coverage`, `question`, `date` | The module it answered for, how many notes it used, the coverage, and what was asked. |

**Which notes.** Every source note of the module (`sources.collect`: the
tutor notes, his own, anything tagged or linked to the module) is scored by
how many of the question's content words it holds (`topics.stop_words`,
`match_min_word_chars`); the tutor notes of the weeks whose topic-map entry
names those words get `consult.week_boost`. The top `max_notes` with at
least `min_overlap_words` go to the model, tutor notes before his own.
**Coverage** = question words found in the chosen notes / question words.
No module named: the question is matched to a module by name or code, else
every module is tried and the best coverage wins.

**Confidence** (`consult.confidence`): no notes → `low` and no model call
(the answer says so). Otherwise `high` needs `high_share` coverage and
`high_min_notes` notes, `medium` needs `medium_share`, else `low`; a model
that answers `confident: false` drops one level.

**Engines** (`consult.engines`, names from `cards.engines`): the local model
first; a question on a hard topic (`teach.hard_topics`) goes to Claude at
once; a local answer that is empty or not confident goes to Claude as well,
*waited for* (a consult is synchronous: the caller is another agent in the
middle of a task). With `delegate.DRY_RUN`, or when Claude fails, the local
answer stands and a caveat says so. When nothing answers, `answer` is empty,
`who` is `none`, and the CLI exits 1 ("no grounded opinion to act on").

**Recorded in the vault**: `<Don area>/data/consults.json` (last
`consult.keep`) and `Don - consults.md` (newest first: module, confidence,
who answered, the answer, caveats, sources as links), linked from the Don
hub, committed through `vault_git.commit_owned` (`git.messages.consult`),
never over a note he edited. A consult that gave no opinion is not recorded.

## Who asks

- **Denisse** (`tutor_consult` in `tools.py`, described in `brain.json`,
  policed by `tool_policy.json`): "what do my notes say about the ROC?",
  "does the lecture cover this?", "what should I prioritise?". The result
  reads `Don says (<confidence> confidence, the local model): ...` with the
  sources and caveats, and the policy keeps the tutor's name in her reply so
  Chuen can tell Don's view from Denisse's. `tutor_explain` results now
  carry the same line (`chat.says`: "Don says this with high confidence,
  from 3 note(s) of his").
- **The agent hub** (`orchestra/agent_hub/server.mjs`): `ask_don` beside
  `ask_codex` / `ask_copilot` / `ask_gemini`, calling the consult CLI and
  rendering the opinion as Don's. `route_task` and `split_tasks` classify
  the work with the exams' routing data (`orchestra/exams/rules.json`
  `kind_to_category` and `title_routes`, first match wins, exactly as
  `lane_for.py`): study-category work is put to Don first, his opinion goes
  in front of the agent's prompt ("weigh it as a subject expert's opinion:
  informed, advisory, never an override") and in the answer. A Don that
  can't answer is shown as such and the agent still runs. A new `study`
  route kind sits before the quick-snippet default; the hub's own rules
  (code, review, debug...) still go first.
- **Orchestra** (`orchestra/consult.py`, data in `orchestra/consult.json`):
  a task whose header says `Consult: don`, or whose text names
  `study_brief`, `study_mode`, `tutor` or `learner` as a file or folder, has
  the consult run on its prompt before the lane starts. The opinion is
  written into the worktree as `.review/don.md`, the lane prompt gets a
  paragraph naming it, and the reviewer prompt shows it (`{{DON}}`,
  `review_diff_prompt.md` v2, marked advisory). A tutor that can't answer,
  times out or isn't installed is one `CONSULT` line in `runner.log` and the
  lane runs on the unchanged prompt. Nothing here parks a task or blocks a
  merge. `enabled`, the patterns, the command, the timeout and every line of
  wording are in `consult.json`; `orchestra/config.json` is untouched.
- **Exams** (`orchestra/exams`): a `don` agent runs the consult CLI; an exam
  that ships a `vault/` folder is answered from it (and a scratch `state/`),
  so the canary never reads the real vault. `bank/study-don-1.json` (Don's
  canary) and `study-don-2.json` carry the fixture vault's Week 4 and Week 5
  tutor notes and are graded on the facts those notes hold. `core.applies`:
  an exam with `"agents": ["don"]` is Don's alone, and Don sits only those
  (`rules.json agents.own_exams_only`). `python -m orchestra.exams canary
  --agent don`; to put him in the daily run, add `don` to `exams_agents` in
  `orchestra/config.json`.

## Tests

`tutor/test_consult.py` (on the part-3 fixture: a fake engine per prompt, a
fake delegate): the opinion grounded in the notes with sources and
confidence, no notes → low and no model call, the module worked out from the
question or said unknown, hard topics and unconfident local answers going to
Claude and being waited for, hand-offs off, Claude through `delegate.run`,
every engine failing giving no answer rather than an invented one, the
grading table, the vault record (hub link, newest first, his edited note kept,
the contract), the chat tool and `tutor_explain` saying the confidence, the
CLI's text, JSON and exit codes. `orchestra/test_consult.py` and three runner
tests: who is consulted, the note and prompt line, every way the tutor can
fail leaving the lane untouched. `orchestra/agent_hub/test_hub.mjs`: the
category data, `ask_don`, study work consulted first with the opinion in
the agent's prompt, a failing Don never stopping the work.
`orchestra/exams/test_exams.py`: Don-only exams, the consult argv on the
exam's vault, exit 1 as a scored fail, a tree of exam files.
