# Local agents learn from Claude's corrections

Chuen, 10 Nov 2026: "I want to continue making qwen, all the local agents
better." This is the first step: no weights change. The local model is shown
Claude's earlier accepted answers to similar requests, and a weekly report
says which checks it keeps failing so the prompts can be fixed.

Code: `tutor/learn.py`. Settings: `tutor/tutor.json` → `learn` (every limit,
sentence and file name). Tests: `tutor/test_learn.py` (fake models only).

## What is captured

Don already falls back to Claude when a local answer fails a check. At each
of those points the pair is kept:

| Job | Where | Failure kept as `why` | Accepted output |
|---|---|---|---|
| `teach` | `chat.explain` → `teach.learn_from` | the self-check that failed (`teach.check_failure`: `no_summary`, `few_steps`, `no_question`, `not_confident`) | Claude's lesson (JSON) |
| `consult` | `consult.consult` | `engine_failed`, `empty`, `not_confident` | Claude's opinion (JSON) |
| `cards` | `cards.check` (with `paths`) | the checker's reason | the corrected front/back when the verdict is *fixed*; none when *wrong* |

Hard topics (the local model is never asked) and scope rejections ("other
module") are not corrections and are not kept. When Claude fails too, the
record is kept with `accepted: null`: it counts in the report, never as a
few-shot example.

One record per line in `corrections.jsonl` in Don's data folder
(`<vault>/Atlas/Academic/Don/data/`, `conf.Paths.data`): private, outside
the public repo, beside Don's other sidecars. Fields: `when, job, model,
engine, module, input, local, why, accepted` (and `vec`, `vec_model` when
embeddings are on). Inputs and outputs are clipped (`input_chars`,
`output_chars`); the newest `keep_per_job` records per job are kept. The
store is not committed into the vault's git by the tutor.

## Few-shot before a local call

Before the local teach, consult or card-writing call, `learn.shots()` picks up
to `shots.max` (3) accepted records of the same job whose input is like this
request and appends them to the **system** prompt, within
`shots.budget_chars`. The block says to take facts only from the material
given now.

Similarity: cosine over Ollama embeddings when `learn.embed.model` names a
model and it answers (score ≥ `embed.min_score`); otherwise word overlap
(Jaccard over the tutor's content words, ≥ `shots.min_score`). The default is
**no embed model**, so similarity costs no GPU. To try embeddings, Chuen
would approve:

```
ollama pull nomic-embed-text
```

then set `"embed": {"model": "nomic-embed-text", ...}`. Records captured
before that have no vector and keep using word overlap.

## Weekly report

```
.venv\Scripts\python.exe -m tutor learned            # last learn.report.days (7) days
.venv\Scripts\python.exe -m tutor learned --days 30
```

Per job and local model: how many answers failed, how many had an accepted
replacement, and the top `report.top` reasons. Read-only, no model call. It is
not scheduled yet; it can be added to the nightly or Orchestra digest when
Chuen wants it pushed rather than pulled.

## Does it help? The bench

```
.venv\Scripts\python.exe scripts\eval_local_learning.py                    # dry: shot blocks only
.venv\Scripts\python.exe scripts\eval_local_learning.py --real --models llama3.1:8b,qwen3.5:9b
.venv\Scripts\python.exe scripts\eval_local_learning.py --real --store     # shots from the real store
```

Cases and seed corrections: `tutor/learning_bench.json` (synthetic textbook
material; public file). Each case is asked with and without the few-shot
block; it passes by the job's own check (teach self-check; consult non-empty
and confident). `--real` refuses on battery and takes the shared GPU lease.
The unit test runs the same code with a fake model; the real numbers are not
measured yet (this task ran unattended and must not use the GPU).

Three cases is a smoke bench, not evidence. Once the store has a few weeks of
real corrections, `--store` on the real failures is the meaningful run.

## A later LoRA fine-tune: what it would need, and is it worth it

Fine-tuning is deliberately not done (`roadmap.md` in the vault). If the
few-shot numbers plateau, this is what it would take:

- **Data volume.** A LoRA on an 8B model for a narrow output format needs
  roughly 500–2,000 good examples per job to beat few-shot reliably; under a
  few hundred, few-shot retrieval does as well with no training. At the
  current fallback rate the store will hold tens of corrections per month,
  not thousands. Synthetic expansion (asking Claude for more lessons on the
  same material) would be needed and would cost Claude usage.
- **Format.** Chat-format JSONL, one example per line:
  `{"messages": [{"role": "system", ...}, {"role": "user", ...}, {"role": "assistant", "content": <accepted JSON>}]}`.
  The store keeps `input` and `accepted` but not the full prompt (material is
  long and private); a fine-tune export would rebuild the prompt from the
  vault at export time, which `teach.prompt` / `consult.prompt` already do.
  Records with `accepted: null` are dropped; `local` can serve as the
  rejected side for preference tuning (DPO/ORPO) later.
- **Tools.** Training would not fit next to inference on the ~8 GB GPU at
  full precision: it needs QLoRA (4-bit base) with Unsloth or Hugging Face
  PEFT + TRL, a few hours on the laptop GPU for 1–2k examples, or a rented
  GPU (money: Chuen's decision). The adapter is merged and converted to GGUF
  (llama.cpp `convert` + `quantize`) and loaded into Ollama with a
  `Modelfile` (`FROM` the base, `ADAPTER` the LoRA). Each new adapter must
  pass `eval/run_eval.py`, the canary exams and this bench before it
  replaces the base model.
- **Worth it?** Not now. Structured JSON output (8 Nov) fixed most bad
  output; the remaining failures are mostly depth and confidence, which
  few-shot and prompt fixes from the weekly report address at no cost.
  Revisit when (a) the store holds ~500+ accepted corrections for one job,
  and (b) the bench with `--store` shows few-shot no longer improving the
  pass rate. Until then a fine-tune would overfit a small, private set and
  add a model artefact to maintain for every Ollama base update.

## Other local agents

This covers Don (teach, consult, cards). Denisse's brain already learns
accepted corrections into routing (`docs/adaptive_brain.md`), and Orchestra's
local team and Atrium digests have no Claude fallback with an accepted answer
to capture yet. `learn.capture` / `learn.block` take any job name, so they can
be wired in where such a fallback exists.
