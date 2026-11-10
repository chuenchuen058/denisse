"""One adapter per local-model job: run the job's own code on the fixtures, score each case.

Nothing here re-implements a job. Don's jobs call tutor/ with an injected `call` (how its own
tests run without Ollama); Denisse's brain is eval/run_eval.py; the local team is
orchestra/local_team/baseline.py; Atrium's is autopilot.model_classify. The model comes from
model_routing's override env, which the runner sets for the cell, so `Env` only carries the
seams a test replaces with a fake model:

    Env(model=..., tutor_call=fake(system, user, **kw) -> str,
        classify=fake(candidate, cfg) -> dict, eval_run=fake(model, spec) -> dict,
        local_team_run=fake(model, spec) -> dict)

Each adapter returns {"cases": [...], "metrics": {...}}; metrics a job cannot see (tokens/s from
a subprocess) come back from the job itself or stay None.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import score

ROOT = Path(__file__).resolve().parent.parent


class Skipped(Exception):
    """The job could not run now (GPU lease held, GPU busy): not a result; the cell is retried."""


@dataclasses.dataclass
class Env:
    model: str
    tutor_call: Callable | None = None
    classify: Callable | None = None
    eval_run: Callable | None = None
    local_team_run: Callable | None = None


def fixtures(spec: dict, root: Path = ROOT) -> list:
    return json.loads((root / spec["fixtures"]).read_text(encoding="utf-8"))["cases"]


def _module(case: dict, folder: str):
    from tutor.conf import Module
    return Module(folder=folder, code=case.get("code", ""), title=case["module"])


def _source(case: dict, folder: str):
    from tutor.conf import CONFIG
    from tutor.sources import Source
    stem = case["id"]
    return Source(path=os.path.join(folder, stem + ".md"), rel=f"{stem}.md", kind=CONFIG["sources"]["tutor"],
                  week=case.get("week"), topic=case.get("topic", ""), text=case["text"])


class _NoLearner:
    """teach.prompt's learner model, empty: the bench must not read Chuen's."""

    @staticmethod
    def load():
        return {"beliefs": []}


def _timed(fn, policy):
    t0 = time.monotonic()
    try:
        return fn(), None, time.monotonic() - t0
    except Exception as exc:  # noqa: BLE001 -- a job that fails a case is a scored failure, not a crash
        return None, f"{type(exc).__name__}: {exc}"[:policy["error_chars"]], time.monotonic() - t0


# ---------------------------------------------------------------- Don

def don_extract(spec, cases, env, policy):
    from tutor import model
    out = []
    for c in cases:
        failed = []
        res, err, secs = _timed(lambda: model.extract_lecture(c["module"], c.get("code", ""), c["week"],
                                                              c["sources"], c["text"], call=env.tutor_call,
                                                              failed=failed), policy)
        data = res[0] if res else {}
        text = score.flatten(data)
        checks = {"json": res is not None and not failed}
        for field in spec["required"]:
            checks[f"has_{field}"] = len(data.get(field) or []) >= spec["min_items"]
        checks["terms"] = score.has_terms(text, c["expect_terms"])
        checks["grounded"] = bool(text) and score.grounded(text, c["text"], policy)
        out.append(score.case(c["id"], checks, secs, err, chunks_failed=len(failed)))
    return out


def don_cards(spec, cases, env, policy):
    from tutor import cards
    kinds = {k for k in cards.active_kinds() if cards._C["kinds"][k]["engine"] == spec["engine"]}
    out = []
    with tempfile.TemporaryDirectory(prefix="bench-cards-") as folder:
        for c in cases:
            dropped = []
            res, err, secs = _timed(lambda: cards.generate(_module(c, folder), _source(c, folder),
                                                           calls={spec["engine"]: env.tutor_call},
                                                           only=kinds, dropped=dropped), policy)
            made = res or []
            backs = "\n".join(card["back"] for card in made)
            checks = {"json": res is not None,
                      "enough": len(made) >= spec["min_cards"],
                      "fields": all(card["front"] and card["back"] for card in made) and bool(made),
                      "terms": score.has_terms(score.flatten([[x["front"], x["back"]] for x in made]),
                                               c["expect_terms"]),
                      "grounded": bool(backs) and score.grounded(backs, c["text"], policy)}
            out.append(score.case(c["id"], checks, secs, err, cards=len(made), dropped=len(dropped)))
    return out


def don_card_check(spec, cases, env, policy):
    from tutor import cards
    v = cards._V
    out = []
    with tempfile.TemporaryDirectory(prefix="bench-check-") as folder:
        for c in cases:
            mod, src = _module(c, folder), _source(c, folder)
            # Only cards this engine checks today: another engine's would go to it (Claude).
            mine = [x for x in c["cards"] if cards.check_engine(x["kind"]) == spec["engine"]]
            written = [{"id": f"{c['id']}-{n}", "source": src.rel, "week": src.week,
                        **{k: x[k] for k in ("kind", "front", "back", "topic")}} for n, x in enumerate(mine, 1)]
            res, err, secs = _timed(lambda: cards.check(mod, src, written, {}, checks={spec["engine"]: env.tutor_call}), policy)
            verdicts = {}
            if res:
                for card in res[0] + res[1]:
                    verdicts[card["id"]] = card["check"]["verdict"]
            checks = {"answered": res is not None and bool(verdicts)
                      and all(x != v["unchecked"] for x in verdicts.values())}
            for x, w in zip(mine, written):
                got = verdicts.get(w["id"])
                want = (v["right"],) if x["truth"] == "right" else (v["wrong"], v["fixed"])
                checks[f"agree_{w['id']}"] = got in want
            out.append(score.case(c["id"], checks, secs, err, verdicts=verdicts,
                                  not_local=len(c["cards"]) - len(mine)))
    return out


def don_teach(spec, cases, env, policy):
    from tutor import teach
    out = []
    with tempfile.TemporaryDirectory(prefix="bench-teach-") as folder:
        for c in cases:
            found = {"text": c["text"], "weeks": c.get("weeks", []), "links": [], "entry": None}
            system, user = teach.prompt(_module(c, folder), c["topic"], found, core=_NoLearner)
            engine = teach._T["engines"][spec["engine"]]
            res, err, secs = _timed(lambda: teach.ask(engine, system, user, calls={engine: env.tutor_call}), policy)
            lesson = res or {}
            text = score.flatten([lesson.get("summary"), lesson.get("steps"), lesson.get("question"),
                                  lesson.get("answer")])
            checks = {"json": res is not None, "self_check": bool(res) and teach.self_check(lesson),
                      "terms": score.has_terms(text, c["expect_terms"]),
                      "grounded": bool(text) and score.grounded(text, c["text"], policy)}
            out.append(score.case(c["id"], checks, secs, err, steps=len(lesson.get("steps") or [])))
    return out


def don_consult(spec, cases, env, policy):
    from tutor import consult
    out = []
    with tempfile.TemporaryDirectory(prefix="bench-consult-") as folder:
        for c in cases:
            system, user = consult.prompt(_module(c, folder), c["question"], {"text": c["text"]})
            engine = consult._K["engines"][spec["engine"]]
            res, err, secs = _timed(lambda: consult.ask(engine, system, user, calls={engine: env.tutor_call}), policy)
            answer = (res or {}).get("answer", "")
            checks = {"json": res is not None, "answered": bool(answer),
                      "terms": score.has_terms(answer, c["expect_terms"]),
                      "grounded": bool(answer) and score.grounded(answer, c["text"], policy)}
            out.append(score.case(c["id"], checks, secs, err, confident=(res or {}).get("confident")))
    return out


# ---------------------------------------------------------------- Atrium

def orchestra_cfg(root: Path = ROOT) -> dict:
    from orchestra.local_team import core
    return core.load_config(root)


def atrium_autopilot(spec, cases, env, policy):
    from atrium.autopilot import Candidate
    cfg = orchestra_cfg()
    example = cfg["autopilot_classify_example"]
    out = []
    for c in cases:
        cand = Candidate(source=c["source"], origin=c["origin"], text=c["text"], context=c.get("context", ""))
        res, err, secs = _timed(lambda: env.classify(cand, cfg), policy)
        got = res or {}
        checks = {"schema": res is not None and all(isinstance(got.get(k), type(v)) for k, v in example.items())}
        for k, want in c["expect"].items():
            checks[k] = got.get(k) == want
        out.append(score.case(c["id"], checks, secs, err))
    return out


# ---------------------------------------------------------------- Denisse (eval/run_eval.py)

def eval_command(model: str, spec: dict, json_path: Path, out_dir: Path, wait_s: int) -> list:
    cmd = [sys.executable, str(ROOT / "eval" / "run_eval.py"), "--quiet", "--no-dataset",
           "--json", str(json_path), "--out", str(out_dir), "--wait-gpu", str(wait_s)]
    cmd += ["--group", spec["group"]] if spec.get("group") else ["--only", ",".join(spec["only"])]
    return cmd


def run_eval_subprocess(model: str, spec: dict, wait_s: int, env_name: str, error_chars: int) -> dict:
    """run_eval.py with the model set; its own lease, its own scoring. Returns its --json."""
    with tempfile.TemporaryDirectory(prefix="bench-eval-") as tmp:
        jp = Path(tmp) / "eval.json"
        env = {**os.environ, env_name: model}
        p = subprocess.run(eval_command(model, spec, jp, Path(tmp), wait_s), cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=spec["timeout_s"])
        if p.returncode == 3:
            raise Skipped("eval/run_eval.py: GPU lease busy")
        if not jp.exists():
            raise RuntimeError(f"eval/run_eval.py exit {p.returncode}: {(p.stdout + p.stderr)[-error_chars:]}")
        return json.loads(jp.read_text(encoding="utf-8"))


def eval_cases(results: dict) -> tuple:
    """run_eval's --json -> (cases, metrics). Only pass/fail and timings: replies stay out of the bench file."""
    cases = []
    for cond in ("clean", "live"):
        for r in results.get(cond) or []:
            checks = {"passed": bool(r.get("passed")), "not_robotic": not r.get("robotic")}
            cases.append(score.case(f"{cond}:{r['id']}", checks, float(r.get("seconds") or 0),
                                    None, failures=len(r.get("failures") or []), tools=r.get("tools") or []))
    t = results.get("timing") or {}
    gen_s = t.get("gen_s") or 0
    metrics = {"calls": t.get("calls"), "gen_tokens": t.get("gen_tokens"), "prompt_tokens": t.get("prompt_tokens"),
               "model_s": t.get("model_s"), "gen_s": gen_s,
               "tokens_per_s": round(t["gen_tokens"] / gen_s, 1) if gen_s and t.get("gen_tokens") else None}
    return cases, metrics


def denisse_eval(spec, cases, env, policy):
    found, metrics = eval_cases(env.eval_run(env.model, spec))
    return {"cases": found, "metrics": metrics}


# ---------------------------------------------------------------- Orchestra local team

def run_local_team(model: str, spec: dict, wait_s: float) -> dict:
    """baseline.run on the configured fixtures; model_routing's override (set by the runner)
    makes core.ready() ask for `model`. core.ask takes the GPU lease for every call."""
    from orchestra.local_team import baseline, core
    try:
        return baseline.run(rounds=spec["rounds"], only=spec["only"], wait_gpu_s=wait_s, out_dir=None,
                            log=lambda *_a, **_k: None)
    except core.Skip as exc:
        raise Skipped(f"local team: {exc}")


def local_team_cases(result: dict) -> list:
    from orchestra.local_team import baseline
    expected = {f["name"]: f["expected"] for f in baseline.load_fixtures(only=None)}
    cases = []
    for r in result.get("runs") or []:
        exp = expected.get(r["fixture"], {})
        checks = {"scout_schema": "scout_bad_output" not in r,
                  "candidate_right": bool(r.get("candidates")) == bool(exp.get("candidate")),
                  "plans_schema": all("bad_output" not in p for p in r.get("plans") or [])}
        kinds = [p.get("admission", {}).get("kind") for p in r.get("plans") or [] if "admission" in p]
        cases.append(score.case(f"{r['fixture']}#r{r.get('round', 1)}", checks, float(r.get("seconds") or 0),
                                None, admission=kinds))
    return cases


def local_team(spec, cases, env, policy):
    res = env.local_team_run(env.model, spec)
    return {"cases": local_team_cases(res), "extra": {"baseline_score": {
        k: (res.get("score") or {}).get(k) for k in ("fixture_accuracy_pct", "candidate_precision_pct",
                                                    "admission_pass_pct", "false_admissions")}}}


KINDS = {"don_extract": don_extract, "don_cards": don_cards, "don_card_check": don_card_check,
         "don_teach": don_teach, "don_consult": don_consult, "atrium_autopilot": atrium_autopilot,
         "denisse_eval": denisse_eval, "local_team": local_team}


def run_job(spec: dict, env: Env, policy: dict, root: Path = ROOT) -> dict:
    """{"cases", "metrics"?, "extra"?} for one job on one model."""
    fn = KINDS[spec["kind"]]
    cases = fixtures(spec, root) if spec.get("fixtures") else []
    got = fn(spec, cases, env, policy)
    return got if isinstance(got, dict) else {"cases": got}
