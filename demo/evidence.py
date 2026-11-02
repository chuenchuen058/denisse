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
    return {"summary": s, "cases": cases}


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
