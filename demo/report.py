"""Render docs/denisse_evidence.md from the latest demo/results/*.json.

    .venv\\Scripts\\python.exe demo\\report.py [results.json]

Every number in the page is a link to the results file it came from, and
the field it was read from is named next to it. Parts that did not run are
listed as not measured, with the recorded reason. If an older results file
sits next to this one, a "Since last run" table compares the two.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "demo"))
import evidence  # noqa: E402

RESULTS = ROOT / "demo" / "results"
DOC = ROOT / "docs" / "denisse_evidence.md"

AREAS = [
    ("suite", "Prompt suite (eval/cases.py)",
     "Each case is one prompt through the real brain on the local model, with an empty "
     "history. Checks are deterministic: which tools ran, required/forbidden reply text, "
     "invented file paths, leaked tool names. Tools that change the machine are vetoed; "
     "task and plan stores are scratch copies, and remembered facts are a synthetic fixture "
     "(`DEMO_FACTS` in demo/suite_rounds.py), so no personal data is needed or read."),
    ("dialogue", "Dialogue reliability (scripts/eval_dialogue_reliability.py)",
     "Short multi-turn scenarios (retry, correction, feedback, greeting) on the real model. "
     "Every tool is replaced by a fixture, so this measures the model's decisions, not the tools."),
    ("actions", "Unattended action loop (scripts/eval_standing_instructions.py)",
     "The bounded plan/observe/finish loop used by standing instructions, on synthetic task "
     "and Git observations. Pass = the loop completed, called exactly the required observation, "
     "and (where set) chose the urgent task."),
    ("learning", "Feedback learning on vs off (scripts/eval_adaptive_brain.py)",
     "Synthetic corrected episodes build a lesson in a scratch vault; the same prompts are then "
     "asked with learning off and on. The script fixes 3 trials. Failures with learning off "
     "are the control arm: they show what the lesson changes, and only learning-on is the "
     "script's own pass condition."),
    ("voice", "Local voice stack (scripts/eval_local_voice.py)",
     "Kokoro speaks fixed sentences to a WAV; Whisper and Vosk transcribe them. No microphone. "
     "Times are per sentence for synthesis and for transcription, not end-to-end reply latency."),
    ("voice_stages", "Voice turn stages (demo/voice_stages.py)",
     "The `scripts/eval_local_voice.py --breakdown` numbers: p50/p95 seconds from the end of "
     "speech to each stage of real spoken turns, read from the voice turn log, one table per "
     "path (voice_mode, wake_word, tray_chat). Runs only where the voice stack and a turn log exist."),
]

STAGE_LABELS = {"speech_last_voice": "last loud chunk", "speech_end": "end of speech detected",
                "stt_done": "transcript ready", "first_token": "reply text ready",
                "first_sentence": "first sentence", "tts_first_chunk": "first TTS audio",
                "playback_start": "playback starts"}


def previous(results_path: Path):
    """The results file just before this one in the same folder, or None."""
    here = Path(results_path)
    older = sorted(p for p in here.parent.glob("*.json") if p.name < here.name)
    return older[-1] if older else None


def latest() -> Path:
    files = sorted(RESULTS.glob("*.json"))
    if not files:
        raise SystemExit("no demo/results/*.json yet; run demo/run_demo.py first")
    return files[-1]


def _pct(x):
    return f"{100 * x:.0f}%"


def render(data: dict, results_path: Path, doc_path: Path = DOC) -> str:
    rel = os.path.relpath(results_path, doc_path.parent).replace(os.sep, "/")
    name = results_path.name

    def num(value, field):
        return f"[{value}]({rel}) <sub>`{field}`</sub>"

    def ln(value):
        return f"[{value}]({rel})"

    parts = data.get("parts", {})
    m, g = data.get("machine", {}), data.get("git", {})
    ran = [k for k, _, _ in AREAS if parts.get(k, {}).get("status") == "ran"]
    not_ran = [k for k, _, _ in AREAS if k not in ran]

    # ---- summary paragraph, built only from recorded numbers -------------
    bits = []
    for key, label in (("suite", "prompt-suite cases"), ("dialogue", "dialogue scenarios"),
                       ("actions", "unattended-loop cases")):
        s = parts.get(key, {}).get("summary")
        if s and s.get("cases"):
            bits.append(f"{ln(s['cases_all_pass'])} of {ln(s['cases'])} {label} passed all {ln(s['k'])} "
                        f"attempts (pass@1 {ln(_pct(s['pass_at_1']))})")
    lr = parts.get("learning", {}).get("summary", {})
    if lr.get("learning_on", {}).get("attempts") and lr.get("learning_off", {}).get("attempts"):
        bits.append(f"with feedback learning on, {ln(lr['learning_on']['passes'])}/{ln(lr['learning_on']['attempts'])} "
                    f"learning-eval attempts passed against {ln(lr['learning_off']['passes'])}/"
                    f"{ln(lr['learning_off']['attempts'])} with it off")
    v = parts.get("voice", {}).get("summary")
    if v and "whisper_mean_wer" in v:
        bits.append(f"Whisper transcribed synthetic speech at {ln(_pct(v['whisper_mean_wer']))} mean word error rate")
    sa = parts.get("suite", {}).get("areas") or {}
    area_bits = [f"{label.split(' (')[0]} {ln(sa[key]['cases_all_pass'])}/{ln(sa[key]['cases'])}"
                 for key, label, _ in evidence.SUITE_AREAS if key in sa]
    if area_bits:
        bits.append("by suite area, cases passing every attempt: " + ", ".join(area_bits))
    para = (f"Measured on {data.get('generated', '?')[:10]} at commit `{(g.get('commit') or '?')[:7]}` "
            f"with {m.get('model', 'unknown model')} on {m.get('gpu') or 'an unrecorded GPU'}. ")
    para += ("; ".join(bits) + ". ") if bits else "No model-backed part ran. "
    if not_ran:
        para += "Not measured in this run: " + ", ".join(
            f"{k} ({parts.get(k, {}).get('reason', 'not recorded')})" for k in not_ran) + ". "
    para += ("These are small, fixed, local test sets on one laptop. They detect regressions "
             "and show repeatability; they are not a general accuracy score.")

    L = ["# Denisse: measured evidence", "",
         f"_Generated by `demo/report.py` from [`demo/results/{name}`]({rel}). "
         "Do not edit by hand; re-run the commands at the end._", "",
         "## Summary", "", para, "",
         "## Machine and build", "",
         "| Field | Value |", "|---|---|"]
    md = m.get("model_details") or {}
    for label, value in (("Commit", g.get("commit")), ("Branch", g.get("branch")),
                         ("Uncommitted tracked changes", g.get("dirty_tracked_files")),
                         ("Model", m.get("model")), ("Quantization", md.get("quantization_level")),
                         ("Context tokens", m.get("num_ctx")), ("Ollama", m.get("ollama_version")),
                         ("GPU", m.get("gpu")), ("CPU", m.get("cpu")), ("RAM (GB)", m.get("ram_gb")),
                         ("OS", m.get("os")), ("Python", m.get("python")),
                         ("Started / finished", f"{data.get('generated')} / {data.get('finished')}"),
                         ("Command", f"`{data.get('command')}`")):
        shown = "_not recorded_" if value in (None, "") else (ln(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value)
        L.append(f"| {label} | {shown} |")
    L.append("")

    failures = []
    for key, title, what in AREAS:
        p = parts.get(key, {"status": "skipped", "reason": "not in results file"})
        L += [f"## {title}", "", what, ""]
        if p.get("status") != "ran":
            L += [f"**Not measured: {p.get('status')}** — {p.get('reason', 'no reason recorded')}", ""]
            continue
        s = p["summary"]
        f = f"parts.{key}.summary"
        if key == "voice":
            L += ["| Measure | Value |", "|---|---|"]
            for k, label in (("sentences", "Sentences"), ("whisper_model", "Whisper model"),
                             ("whisper_device", "Whisper device"), ("tts_mean_s", "Kokoro synthesis, mean s/sentence"),
                             ("whisper_mean_s", "Whisper transcription, mean s/sentence"),
                             ("whisper_mean_wer", "Whisper word error rate, mean"),
                             ("vosk_mean_s", "Vosk transcription, mean s/sentence"),
                             ("vosk_mean_wer", "Vosk word error rate, mean"),
                             ("kokoro_load_s", "Kokoro load s"), ("whisper_load_s", "Whisper load s"),
                             ("vosk_load_s", "Vosk load s")):
                if k in s:
                    L.append(f"| {label} | {num(s[k], f + '.' + k)} |")
            if s.get("vosk_error"):
                L.append(f"| Vosk | not measured: {s['vosk_error']} |")
            L.append("")
            continue
        if key == "voice_stages":
            L += [f"Timed turns: {num(s.get('turns'), f + '.turns')}.", ""]
            for source, stages in (p.get("sources") or {}).items():
                L += [f"`{source}`:", "", "| Stage | Turns | p50 s | p95 s |", "|---|---|---|---|"]
                for st, v in stages.items():
                    sf = f"parts.{key}.sources.{source}.{st}"
                    L.append(f"| {STAGE_LABELS.get(st, st)} (`{st}`) | {num(v.get('n'), sf + '.n')} | "
                             f"{num(v.get('p50'), sf + '.p50')} | {num(v.get('p95'), sf + '.p95')} |")
                L.append("")
            continue
        L += ["| Measure | Value |", "|---|---|",
              f"| Cases | {num(s['cases'], f + '.cases')} |",
              f"| Attempts per case (k) | {num(s['k'], f + '.k')} |",
              f"| Passed attempts | {num(s['passes'], f + '.passes')} of {num(s['attempts'], f + '.attempts')} |",
              f"| pass@1 (mean share of attempts passed) | {num(s['pass_at_1'], f + '.pass_at_1')} |",
              f"| pass^k (mean chance all k attempts pass) | {num(s['pass_hat_k'], f + '.pass_hat_k')} |",
              f"| Cases passing all k | {num(s['cases_all_pass'], f + '.cases_all_pass')} |"]
        if "median_seconds" in s:
            L += [f"| Median seconds per turn | {num(s['median_seconds'], f + '.median_seconds')} |",
                  f"| p90 seconds per turn | {num(s['p90_seconds'], f + '.p90_seconds')} |",
                  f"| Replies flagged robotic (counted, not failed) | {num(s['robotic_replies'], f + '.robotic_replies')} |"]
        if key == "learning":
            for side in ("learning_off", "learning_on"):
                if s.get(side, {}).get("attempts"):
                    q = s[side]
                    L.append(f"| {side.replace('_', ' ')}: passed | {num(q['passes'], f + '.' + side + '.passes')} "
                             f"of {num(q['attempts'], f + '.' + side + '.attempts')} |")
        L += [f"| Wall time, s | {num(p.get('seconds'), f'parts.{key}.seconds')} |", ""]
        if p.get("areas"):
            L += ["By area (cases added since the first pack, scored on their own):", "",
                  "| Area | Cases | Passed attempts | pass^k | Cases passing all k |", "|---|---|---|---|---|"]
            for akey, alabel, _ in evidence.SUITE_AREAS:
                a = p["areas"].get(akey)
                if not a:
                    continue
                af = f"parts.{key}.areas.{akey}"
                L.append(f"| {alabel} | {num(a['cases'], af + '.cases')} | "
                         f"{num(a['passes'], af + '.passes')} of {num(a['attempts'], af + '.attempts')} | "
                         f"{num(a['pass_hat_k'], af + '.pass_hat_k')} | "
                         f"{num(a['cases_all_pass'], af + '.cases_all_pass')} |")
            L.append("")
        bad = [c for c in p["cases"] if c["passes"] < c["attempts"]]
        show = p["cases"] if len(p["cases"]) <= 25 else bad
        if show:
            if show is bad and len(p["cases"]) > 25:
                L += [f"Cases below k/k ({len(bad)}; the other {len(p['cases']) - len(bad)} passed every attempt):", ""]
            L += ["| Case | Passed | pass^k |", "|---|---|---|"]
            for c in show:
                idx = p["cases"].index(c)
                score = f"{c['passes']}/{c['attempts']}"
                L.append(f"| `{c['id']}` | {num(score, f'parts.{key}.cases[{idx}]')} | {ln(c['pass_hat_k'])} |")
            L.append("")
        failures += [(title, c) for c in bad]

    L += ["## Known failures", ""]
    if failures:
        L += ["Every case that failed at least one attempt in this run, with the recorded reasons:", ""]
        for title, c in failures:
            L.append(f"- **{title.split(' (')[0]}** `{c['id']}` — {c['passes']}/{c['attempts']}: "
                     + "; ".join(c["failures"]))
    else:
        L.append("None recorded in the parts that ran.")
    L.append("")

    prev_path = previous(results_path)
    L += ["## Since last run", ""]
    if prev_path is None:
        L += ["No earlier results file to compare with.", ""]
    else:
        prev = json.loads(prev_path.read_text(encoding="utf-8"))
        prel = os.path.relpath(prev_path, doc_path.parent).replace(os.sep, "/")
        pg = (prev.get("git") or {}).get("commit") or "?"
        L += [f"Against [`demo/results/{prev_path.name}`]({prel}) (commit `{pg[:7]}`). "
              "A dash means that side did not measure it. The prompt suite grew between "
              "the runs, so its totals cover different case sets; old cases and new are "
              "not separated in the totals.", "",
              "| Eval | Field | Then | Now | Change |", "|---|---|---|---|---|"]
        for label, field, ov, nv in evidence.diff_rows(prev, data):
            then = f"[{ov}]({prel})" if ov is not None else "\u2014"
            now = ln(nv) if nv is not None else "\u2014"
            change = ""
            if isinstance(ov, (int, float)) and isinstance(nv, (int, float)):
                d = round(nv - ov, 3)
                change = "same" if d == 0 else f"{d:+g}"
            L.append(f"| `{label}` | `{field}` | {then} | {now} | {change} |")
        L.append("")

    L += ["## Measured vs claimed", "",
          "Measured here (numbers above):", "",
          "- Tool choice and reply checks on fixed prompts, repeated k times, on this laptop's local model.",
          "- Whether the unattended loop completes on synthetic observations and stays in scope.",
          "- Whether a learned routing lesson changes behaviour against the same prompts with learning off.",
          "- By suite area: multi-step requests through the planner, `look_at_this` called when "
          "pointed at text and left alone otherwise, fixture-vault search and study mode.",
          "- Speech synthesis and transcription time and word error rate on synthetic speech." if "voice" in ran
          else "- (Voice was not measured in this run.)",
          "",
          "Not measured here, so not claimed:", "",
          "- Accuracy on open-ended real use. The suites are small and hand-written; a pass means "
          "these checks held, not that every reply is good.",
          "- Real-room speech recognition. Synthetic speech is cleaner than a microphone.",
          "- End-to-end voice latency (wake word to spoken reply), except as far as the voice "
          "turn stages section above has numbers from real logged turns.",
          "- The `live` eval condition (replayed real history) is excluded because it uses private "
          "conversation data; it runs nightly outside this pack.",
          "- Reply quality beyond the deterministic checks: no LLM judge is used.",
          "- Desktop UI behaviour; that is `scripts/smoke_pet_chat.py`, which needs an interactive desktop.",
          "",
          "Reply text is deliberately not stored: the results file is public, and some suite "
          "cases answer from personal facts. Failure reasons are kept with file paths redacted.", "",
          "## Reproduce", "",
          "Windows, Python 3.12, [Ollama](https://ollama.com) running with the model pulled:", "",
          "```powershell",
          "ollama pull llama3.1:8b",
          ".venv\\Scripts\\python.exe demo\\run_demo.py --rounds 3",
          "# fewer rounds, or without parts:  --rounds 1 --skip voice learning",
          "# voice models elsewhere:          --models-dir C:\\path\\to\\models",
          ".venv\\Scripts\\python.exe demo\\report.py            # re-render this page from the latest results",
          ".venv\\Scripts\\python.exe scripts\\run_checks.py     # offline regression gate, no model needed",
          "```", "",
          "Each part can also be run alone: `demo/suite_rounds.py`, "
          "`scripts/eval_dialogue_reliability.py`, `scripts/eval_standing_instructions.py`, "
          "`scripts/eval_adaptive_brain.py`, `scripts/eval_local_voice.py`, `demo/voice_stages.py`.", ""]
    return "\n".join(L)


def write(results_path: Path, doc_path: Path = DOC) -> Path:
    data = json.loads(Path(results_path).read_text(encoding="utf-8"))
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(render(data, Path(results_path), doc_path), encoding="utf-8")
    return doc_path


if __name__ == "__main__":
    print(write(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else latest()))
