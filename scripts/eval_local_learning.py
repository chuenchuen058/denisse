"""Does Claude's earlier correction help the local model? Pass rate with vs
without the few-shot block (tutor/learn.py) on the bench cases
(tutor.json learn.bench.file).

    .venv\\Scripts\\python.exe scripts\\eval_local_learning.py              dry: the shot blocks only
    .venv\\Scripts\\python.exe scripts\\eval_local_learning.py --real       the local model, both ways
        [--models llama3.1:8b,qwen3.5:9b]   each in turn (the chat model's env override)
        [--store]                           shots from the real private store, not the bench seed
        [--json]                            the result as JSON

--real runs on AC only (battery rule, 9 Nov) and under the shared GPU lease
(.review/gpu.lease), waiting learn.bench.wait_gpu_s for it. A case passes
by the job's own check: teach's self-check, consult's non-empty confident
answer. Tests call run() with a fake model; nothing here pulls a model.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tutor import cards, consult, learn, model, teach  # noqa: E402
from tutor.conf import CONFIG, Module  # noqa: E402

_B = CONFIG["learn"]["bench"]
MODES = ("without", "with")


class _NoLearner:
    """The learner store is not part of the bench: every case sees 'none'."""

    @staticmethod
    def load():
        return {}


def load_bench(path: str | None = None) -> dict:
    with open(path or os.path.join(REPO, _B["file"]), encoding="utf-8-sig") as f:
        return json.load(f)


def _prompt(module: Module, case: dict) -> tuple:
    found = {"text": case["material"], "weeks": [], "links": []}
    if case["job"] == teach.JOB:
        return teach.prompt(module, case["query"], found, core=_NoLearner)
    return consult.prompt(module, case["query"], found)


def _passes(job: str, call, system: str, user: str) -> bool:
    schema = CONFIG["teach" if job == teach.JOB else "consult"]["schema"]
    try:
        data = cards._ask(call, system, user, schema)
    except model.model_errors():
        return False
    if job == teach.JOB:
        return teach.self_check(teach.normalise(data))
    answer = consult.normalise(data)
    return bool(answer["answer"]) and answer["confident"]


def run(call, bench: dict, records: list | None = None, embed=None, name: str = "") -> dict:
    """{name, modes: {mode: {passed, cases}}, cases: [{job, query, shots, without, with}]}."""
    module = Module(folder="", code=bench["module"]["code"], title=bench["module"]["title"])
    records = bench["seed"] if records is None else records
    out = {"name": name, "modes": {m: {"passed": 0, "cases": 0} for m in MODES}, "cases": []}
    for case in bench["cases"]:
        system, user = _prompt(module, case)
        shots = learn.block(records, case["job"], case["query"], embed)
        row = {"job": case["job"], "query": case["query"], "shots": len(learn.similar(records, case["job"],
                                                                                  case["query"], embed))}
        for mode in MODES:
            ok = _passes(case["job"], call, system + (shots if mode == "with" else ""), user)
            row[mode] = ok
            out["modes"][mode]["cases"] += 1
            out["modes"][mode]["passed"] += int(ok)
        out["cases"].append(row)
    return out


def render(result: dict) -> str:
    lines = []
    for row in result["cases"]:
        for mode in MODES:
            lines.append(_B["say_case"].format(model=result["name"], mode=mode, job=row["job"], query=row["query"],
                                               result=_B["pass"] if row[mode] else _B["fail"]))
    for mode in MODES:
        m = result["modes"][mode]
        lines.append(_B["say_total"].format(model=result["name"], mode=mode, rate=m["passed"] / max(1, m["cases"]),
                                            **m))
    return "\n".join(lines)


def dry(bench: dict, records: list) -> str:
    lines = [_B["say_dry"].format(n=len(bench["cases"]))]
    for case in bench["cases"]:
        block = learn.block(records, case["job"], case["query"])
        lines.append(_B["say_dry_case"].format(job=case["job"], query=case["query"], chars=len(block),
                                               shots=len(learn.similar(records, case["job"], case["query"]))))
    return "\n".join(lines)


def _on_battery() -> bool:
    try:
        from orchestra import power
        return power.on_battery(power.power_status())
    except Exception:  # noqa: BLE001 -- unknown power counts as AC, as orchestra.power does
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--models", default="")
    ap.add_argument("--store", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--bench", default=None)
    a = ap.parse_args(argv)
    bench = load_bench(a.bench)
    records = None
    if a.store:
        from tutor import conf
        records = learn.load(conf.paths())
    if not a.real:
        print(dry(bench, bench["seed"] if records is None else records))
        return 0
    if _on_battery():
        print(_B["say_battery"])
        return 2
    from orchestra.local_team.lease import GpuLease
    lease = GpuLease.for_repo(REPO, _B["lease_holder"], ttl_s=_B["lease_ttl_s"])
    if not lease.acquire(wait_s=_B["wait_gpu_s"], poll_s=5):
        print(_B["say_busy"].format(holder=(lease.info() or {}).get("holder", "?"), wait=_B["wait_gpu_s"]))
        return 3
    try:
        import model_config
        env = model_config.entry(CONFIG["model"]["config_entry"]).get("env")
        names = [n for n in a.models.split(",") if n.strip()] or [learn.local_model()]
        results = []
        for name in names:
            if env:
                os.environ[env] = name.strip()
            results.append(run(model.ollama_call, bench, records, name=name.strip()))
    finally:
        lease.release()
    print(json.dumps(results, indent=2) if a.json else "\n\n".join(render(r) for r in results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
