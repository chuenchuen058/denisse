"""The bench runner: every (model, job) cell once, saved as it goes, resumable; and `pick`.

    run(models, jobs)  -> out_dir/<date>.json + <date>.md; exit code (see EXIT)
    pick()             -> per job, the model the latest results favour; prints, never writes

A cell is retried on a later run until its status is "done". Before every cell: the battery
(orchestra/power.py: on battery, stop) and, for in-process jobs, the shared GPU lease
(orchestra/local_team/lease.py: still held after lease.wait_s, stop). Jobs that take the lease
themselves (lease "own") run with the bench's hold released. Models must be installed: the
bench never pulls one. Everything tunable is in bench.json.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
from pathlib import Path

from . import jobs, meter, score

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = Path(__file__).with_name("bench.json")
EXIT = {"done": 0, "incomplete": 1, "usage": 2, "lease": 3, "battery": 4}


def load_config(path: Path | None = None) -> dict:
    return json.loads(Path(path or os.environ.get("BENCH_CONFIG") or CONFIG_FILE).read_text(encoding="utf-8"))


def job_names(cfg: dict, wanted: str) -> list:
    names = [k for k in cfg["jobs"] if not k.startswith("_")]
    if wanted in ("", "all"):
        return names
    asked = [w.strip() for w in wanted.split(",") if w.strip()]
    unknown = [a for a in asked if a not in names]
    if unknown:
        raise ValueError(f"unknown job(s): {', '.join(unknown)} (have: {', '.join(names)})")
    return asked


# ---------------------------------------------------------------- the results file

def out_dir(cfg: dict, root: Path = ROOT) -> Path:
    return root / cfg["out_dir"]


def results_path(cfg: dict, date: str, root: Path = ROOT) -> Path:
    return out_dir(cfg, root) / f"{date}.json"


def load_results(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    path.with_suffix(".md").write_text(markdown(data), encoding="utf-8")


def cell_key(model: str, job: str) -> str:
    return f"{model} | {job}"


def _fmt(v, suffix=""):
    return "-" if v is None else f"{v}{suffix}"


def markdown(data: dict) -> str:
    models, names, cells = data.get("models", []), data.get("jobs", []), data.get("cells", {})
    lines = [f"# Local model bench {data.get('date', '')}", "",
             "Score = mean share of checks passed per case (1.0 = every check); ok = cases with every check; "
             "s = median seconds per case; tok/s = generated tokens per second; VRAM from Ollama's /api/ps.", "",
             "| job | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    for job in names:
        row = []
        for m in models:
            c = cells.get(cell_key(m, job)) or {}
            if c.get("status") != "done":
                row.append(c.get("status", "not run") + (f": {c['why']}" if c.get("why") else ""))
                continue
            s, mt = c["summary"], c.get("metrics") or {}
            row.append(f"**{_fmt(s['score'])}** ({s['ok']}/{s['cases']} ok) · {_fmt(s['median_s'], ' s')} · "
                       f"{_fmt(mt.get('tokens_per_s'), ' tok/s')} · {_fmt(mt.get('vram_mib'), ' MiB')}")
        lines.append(f"| {job} | " + " | ".join(row) + " |")
    if data.get("stopped"):
        lines += ["", f"Stopped: {data['stopped']}. Run the same command again to resume."]
    if data.get("suggest"):
        lines += ["", "Worth trying (not pulled; Chuen approves):", ""]
        lines += [f"- {s.get('why', '')}: `{s['pull']}`" for s in data["suggest"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- real seams

def real_env(model: str, cfg: dict, routing_env: str) -> jobs.Env:
    from tutor import model as tutor_model
    from atrium import autopilot

    def classify(c, ocfg):
        return autopilot.model_classify(c, ocfg)
    return jobs.Env(model=model, tutor_call=tutor_model.ollama_call, classify=classify,
                    eval_run=lambda m, spec: jobs.run_eval_subprocess(m, spec, cfg["lease"]["wait_s"], routing_env,
                                                                    cfg["score"]["error_chars"]),
                    local_team_run=lambda m, spec: jobs.run_local_team(m, spec, cfg["lease"]["wait_s"]))


def real_lease(cfg: dict, root: Path):
    from orchestra.local_team.lease import GpuLease
    return GpuLease.for_repo(root, cfg["lease"]["holder"], cfg["lease"]["ttl_s"])


def real_power() -> dict:
    from orchestra.power import power_status
    return power_status()


def _tags_url(cfg: dict) -> str:
    return cfg["ollama"]["ps_url"].rsplit("/", 1)[0] + "/" + cfg["ollama"]["tags_path"]


@contextlib.contextmanager
def _env_var(name: str, value: str):
    old = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


# ---------------------------------------------------------------- run

def run(models: list, names: list, *, cfg: dict | None = None, date: str | None = None, fresh: bool = False,
        root: Path = ROOT, env_factory=None, lease=None, power=None, installed=None, vram=None,
        log=print) -> int:
    import model_routing
    cfg = cfg or load_config()
    date = date or dt.date.today().isoformat()
    routing_env = model_routing.override_env()
    env_factory = env_factory or (lambda m: real_env(m, cfg, routing_env))
    lease = lease if lease is not None else real_lease(cfg, root)
    power = power or real_power
    o = cfg["ollama"]
    installed = installed or (lambda: meter.installed(_tags_url(cfg), o["timeout_s"]))
    vram = vram or (lambda m: meter.vram(o["ps_url"], m, o["timeout_s"]))
    path = results_path(cfg, date, root)
    data = {} if fresh else load_results(path)
    data.update(date=date, models=list(dict.fromkeys(data.get("models", []) + models)),
                jobs=list(dict.fromkeys(data.get("jobs", []) + names)), suggest=cfg.get("suggest", []))
    data.setdefault("cells", {})
    data.pop("stopped", None)
    have = installed()
    code = EXIT["done"]
    try:
        for m in models:
            if have is not None and m not in have:
                for j in names:
                    data["cells"][cell_key(m, j)] = {"status": "skipped", "why": "model not installed (not pulling it)"}
                save(path, data)
                log(f"{m}: not installed; skipped (the bench never pulls)")
                code = EXIT["incomplete"]
                continue
            for j in names:
                key = cell_key(m, j)
                if (data["cells"].get(key) or {}).get("status") == "done":
                    log(f"{key}: done earlier; kept")
                    continue
                if cfg["battery"]["refuse_on_battery"]:
                    from orchestra.power import on_battery
                    if on_battery(power()):
                        data["stopped"] = "on battery (no local batch jobs on battery)"
                        save(path, data)
                        log(f"stopped before {key}: on battery")
                        return EXIT["battery"]
                spec = cfg["jobs"][j]
                own = spec.get("lease") == "own"
                if own:
                    lease.release()
                elif not lease.held and not lease.acquire(wait_s=cfg["lease"]["wait_s"], poll_s=cfg["lease"]["poll_s"]):
                    held = lease.info() or {}
                    data["stopped"] = f"GPU lease held by {held.get('holder', '?')}"
                    save(path, data)
                    log(f"stopped before {key}: {data['stopped']}")
                    return EXIT["lease"]
                data["cells"][key] = cell = run_cell(m, j, spec, cfg, env_factory, routing_env, vram, root)
                save(path, data)
                s = cell.get("summary") or {}
                log(f"{key}: {cell['status']}" + (f" score {s.get('score')} ({s.get('ok')}/{s.get('cases')} ok)"
                                                  if cell["status"] == "done" else f" ({cell.get('why')})"))
                if cell["status"] != "done":
                    code = EXIT["incomplete"]
    finally:
        lease.release()
    return code


def run_cell(model: str, job: str, spec: dict, cfg: dict, env_factory, routing_env: str, vram, root: Path) -> dict:
    started = dt.datetime.now().isoformat(timespec="seconds")
    try:
        with _env_var(routing_env, model), meter.metered(cfg["ollama"]["chat_path"]) as prof:
            got = jobs.run_job(spec, env_factory(model), cfg["score"], root)
    except jobs.Skipped as exc:
        return {"status": "skipped", "why": str(exc), "started": started}
    except Exception as exc:  # noqa: BLE001 -- one broken job must not cost the others
        return {"status": "error", "why": f"{type(exc).__name__}: {exc}"[:cfg["score"]["error_chars"]], "started": started}
    metrics = got.get("metrics") or meter.figures(prof)
    metrics.update(vram(model))
    out = {"status": "done", "started": started, "finished": dt.datetime.now().isoformat(timespec="seconds"),
           "summary": score.summarise(got["cases"]), "metrics": metrics, "cases": got["cases"]}
    if got.get("extra"):
        out["extra"] = got["extra"]
    return out


# ---------------------------------------------------------------- pick

def latest(cfg: dict, root: Path = ROOT) -> Path | None:
    files = sorted(p for p in out_dir(cfg, root).glob("*.json") if not p.name.endswith(".tmp"))
    return files[-1] if files else None


def today_model(job: str, cfg: dict, root: Path = ROOT) -> str | None:
    """What the job uses now: model_routing.json if it pins one, else where the job always read it."""
    import model_config
    import model_routing
    t = cfg["jobs"][job].get("today") or {}
    if "entry" in t:
        own = model_config.entry(t["entry"]).get("env")
        if own and os.environ.get(own):  # an env override still beats routing, as in model_for
            return os.environ[own]
        default = model_config.entry(t["entry"])["value"]
    else:
        default = jobs.orchestra_cfg(root).get(t["orchestra"]) if "orchestra" in t else None
    return model_routing.routed(job) or default


def pick(data: dict, cfg: dict, root: Path = ROOT) -> list:
    """[{job, today, best, why, change}] from one results file."""
    p = cfg["pick"]
    out = []
    for job in data.get("jobs", []):
        if job not in cfg["jobs"]:
            continue
        scored = []
        for m in data.get("models", []):
            c = data["cells"].get(cell_key(m, job)) or {}
            s = c.get("summary") or {}
            if c.get("status") == "done" and s.get("cases", 0) >= p["min_cases"] and s.get("score") is not None \
                    and not s.get("errors"):
                scored.append((m, s))
        now = today_model(job, cfg, root)
        if not scored:
            out.append({"job": job, "today": now, "best": None, "why": "no complete results", "change": False})
            continue
        top = max(s["score"] for _m, s in scored)
        tied = [(m, s) for m, s in scored if s["score"] >= top - p["quality_margin"]]
        best, s = min(tied, key=lambda ms: (ms[1]["median_s"] if ms[1]["median_s"] is not None else float("inf"),
                                            -ms[1]["score"]))
        why = f"score {s['score']} ({s['ok']}/{s['cases']} ok), median {s['median_s']} s"
        if len(tied) > 1:
            why += f"; within {p['quality_margin']} of the best, so the fastest"
        out.append({"job": job, "today": now, "best": best, "why": why, "change": bool(now) and best != now})
    return out


def pick_lines(rows: list, cfg: dict, source: Path) -> list:
    lines = [f"From {source}. Proposals only: nothing switches until you edit {cfg['routing_file']}.", ""]
    for r in rows:
        if r["best"] is None:
            lines.append(f"  {r['job']:22} keep {r['today']} ({r['why']})")
        elif r["change"]:
            lines.append(f"  {r['job']:22} {r['today']} -> {r['best']}  ({r['why']})")
        else:
            lines.append(f"  {r['job']:22} keep {r['best']}  ({r['why']})")
    edits = {r["job"]: r["best"] for r in rows if r["change"]}
    if edits:
        lines += ["", f"To accept, set these \"model\" fields in {cfg['routing_file']} jobs:",
                  json.dumps(edits, indent=2)]
    return lines
