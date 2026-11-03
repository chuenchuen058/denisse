"""Pure helpers for the demo pack: scoring, redaction, summaries.

No model, network or file access here, so scripts/test_demo_pack.py can test
all of it offline.
"""
import re
import statistics

# A Windows or POSIX-home path. Results are committed to a public repo, and a
# failure reason such as "invented path(s) [...]" or an exception message can
# quote a real path from this laptop.
_PATH = re.compile(r"[A-Za-z]:\\[^\s\"'<>|*?`\]\[,]+|/(?:home|Users)/[^\s\"'\]\[,]+")


def redact(text) -> str:
    return _PATH.sub("<path>", str(text))


def pass_hat_k(passes: int, attempts: int, k: int) -> float:
    """pass^k: probability that k attempts drawn from these n all pass,
    C(c, k) / C(n, k). With k == n it is 1.0 only if every attempt passed.
    This is the reliability number; pass@1 (c / n) is the average one."""
    if attempts <= 0 or k <= 0 or k > attempts:
        return 0.0
    num = den = 1.0
    for i in range(k):
        num *= passes - i
        den *= attempts - i
    return max(0.0, num / den)


def per_case(records, id_key="id", ok_key="passed", fail_key="failures"):
    """Group attempt records by case id, preserving first-seen order."""
    cases = {}
    for r in records:
        c = cases.setdefault(r[id_key], {"id": r[id_key], "attempts": 0, "passes": 0, "failures": []})
        c["attempts"] += 1
        if r.get(ok_key):
            c["passes"] += 1
        else:
            for f in r.get(fail_key) or ["failed"]:
                f = redact(f)
                if f not in c["failures"]:
                    c["failures"].append(f)
    out = []
    for c in cases.values():
        n = c["attempts"]
        c["pass_at_1"] = round(c["passes"] / n, 3)
        c["pass_hat_k"] = round(pass_hat_k(c["passes"], n, n), 3)
        c["k"] = n
        out.append(c)
    return out


def summarize(cases) -> dict:
    """Area totals from per_case() output."""
    if not cases:
        return {"cases": 0, "attempts": 0, "passes": 0}
    attempts = sum(c["attempts"] for c in cases)
    passes = sum(c["passes"] for c in cases)
    return {
        "cases": len(cases),
        "attempts": attempts,
        "passes": passes,
        "pass_at_1": round(passes / attempts, 3),
        # Share of cases that passed every one of their attempts.
        "cases_all_pass": sum(c["passes"] == c["attempts"] for c in cases),
        "pass_hat_k": round(statistics.mean(c["pass_hat_k"] for c in cases), 3),
        "k": min(c["k"] for c in cases),
    }


# ---- per-area shaping: keep scores, drop free text ------------------------

def shape_suite(records):
    cases = per_case(records)
    secs = [r["seconds"] for r in records if isinstance(r.get("seconds"), (int, float))]
    s = summarize(cases)
    if secs:
        s["median_seconds"] = round(statistics.median(secs), 1)
        s["p90_seconds"] = round(sorted(secs)[int(0.9 * (len(secs) - 1))], 1)
    s["robotic_replies"] = sum(bool(r.get("robotic")) for r in records)
    return {"summary": s, "areas": suite_areas(cases), "cases": cases}


def shape_dialogue(records):
    rows = [{"id": r["id"], "trial": r["trial"], "passed": r["passed"],
             "tools": r.get("tools", []), "failures": [redact(f) for f in r.get("failures", [])]}
            for r in records]
    cases = per_case(rows)
    return {"summary": summarize(cases), "cases": cases}


def shape_actions(records):
    rows = []
    for r in records:
        fails = [] if r["passed"] else [f"status={r.get('status')}" + (f": {r['error']}" if r.get("error") else "")]
        rows.append({"id": r["id"], "passed": r["passed"], "failures": fails})
    cases = per_case(rows)
    return {"summary": summarize(cases), "cases": cases}


def shape_adaptive(records):
    rows = [{"id": f"{r['prompt']} [learning {'on' if r['learning'] else 'off'}]",
             "passed": r["passed"],
             "failures": [] if r["passed"] else ["check failed; tools called: " + (", ".join(r.get("tools") or []) or "no tools")]}
            for r in records]
    cases = per_case(rows)
    on = [c for c in cases if c["id"].endswith("[learning on]")]
    off = [c for c in cases if c["id"].endswith("[learning off]")]
    return {"summary": {**summarize(cases), "learning_on": summarize(on), "learning_off": summarize(off)},
            "cases": cases}


VOICE_KEYS = ("whisper_model", "whisper_device", "kokoro_load_s", "whisper_load_s", "vosk_load_s",
              "tts_mean_s", "whisper_mean_wer", "whisper_mean_s", "vosk_mean_wer", "vosk_mean_s", "vosk_error")


def shape_voice(out):
    rows = []
    for r in out.get("rows", []):
        rows.append({k: r[k] for k in ("text", "audio_s", "tts_s", "whisper_s", "whisper_wer",
                                       "vosk_s", "vosk_wer") if k in r})
    summary = {k: (redact(out[k]) if k == "vosk_error" else out[k]) for k in VOICE_KEYS if k in out}
    summary["sentences"] = len(rows)
    return {"summary": summary, "rows": rows}


# ---- suite areas: named slices of the prompt suite ------------------------
# Explicit ids, not prefixes: "vault_deadline" and "vault_not_for_general" are
# older cases that read the real vault, not the fixture vault the study/vault
# set uses. Ids missing from a run are simply not counted (an older results
# file has none of them).

SUITE_AREAS = (
    ("multistep", "Multi-step requests (planner, file-reading follow-up)",
     ("multi_signals_revision", "multi_lecture_pdfs", "multi_weather_umbrella",
      "multi_tasks_timer", "multi_tool_error", "multi_news_plate")),
    ("look_right", "look_at_this, right use (should read the selection)",
     ("look_rewrite_this", "look_translate_selected", "look_summarise_copied",
      "look_what_does_that_mean", "explain_this_error", "explain_this_text")),
    ("look_wrong", "look_at_this, wrong use (must not read it)",
     ("look_not_thats_great", "look_not_this_is_fine", "look_not_do_that_again", "look_not_here")),
    ("vault", "Vault search on the fixture vault",
     ("vault_week_topic", "vault_tutorial_pdf", "vault_which_week", "vault_lecturer",
      "vault_miss_honest", "vault_no_leak")),
    ("study", "Study mode (headless pomodoro and study brief)",
     ("study_start", "study_timer_cycles", "study_stop_clean", "study_off_not_swallowed",
      "study_keeps_module", "study_no_invented_lecture")),
)


def suite_areas(cases) -> dict:
    """{area: summarize(...) + "ids"} over the suite's per-case output."""
    by_id = {c["id"]: c for c in cases}
    out = {}
    for key, _, ids in SUITE_AREAS:
        got = [by_id[i] for i in ids if i in by_id]
        if got:
            out[key] = {**summarize(got), "ids": [c["id"] for c in got]}
    return out


def shape_voice_stages(out):
    """Worker output of demo/voice_stages.py: per-source stage p50/p95 only.
    The turn log holds timings, never text, but keep only known fields anyway."""
    sources = {}
    for source, stages in (out.get("sources") or {}).items():
        sources[str(source)] = {
            st: {k: v[k] for k in ("n", "p50", "p95") if k in v}
            for st, v in stages.items() if isinstance(v, dict)}
    turns = int(out.get("turns", 0))
    return {"summary": {"turns": turns, "sources": sorted(sources)}, "sources": sources}


# ---- since last run -------------------------------------------------------

DIFF_FIELDS = ("cases", "passes", "attempts", "pass_at_1", "pass_hat_k", "cases_all_pass")


def diff_rows(old: dict, new: dict) -> list:
    """(row label, field, old value, new value) for every scored summary either
    run has. A part that ran in only one of them is listed with None on the
    other side, so a part that stopped running cannot quietly vanish."""
    def scored(data):
        rows = {}
        for name, part in (data.get("parts") or {}).items():
            s = part.get("summary") if part.get("status") == "ran" else None
            if s and "attempts" in s:
                rows[name] = s
                for side in ("learning_off", "learning_on"):
                    if isinstance(s.get(side), dict) and s[side].get("attempts"):
                        rows[f"{name}.{side}"] = s[side]
            for area, a in ((part.get("areas") or {}) if part.get("status") == "ran" else {}).items():
                rows[f"{name}.areas.{area}"] = a
        return rows

    a, b = scored(old), scored(new)
    out = []
    for label in list(a) + [k for k in b if k not in a]:
        for f in DIFF_FIELDS:
            ov, nv = a.get(label, {}).get(f), b.get(label, {}).get(f)
            if ov is None and nv is None:
                continue
            out.append((label, f, ov, nv))
    return out
