"""python -m orchestra.local_team <role> [options]

  scout                         scan the delta since the last successful scan -> candidates
  planner [--candidates F] [--index N] [--admit]
                                one candidate -> a task packet; --admit hands it to admission
  librarian [REPORT]            one finished report -> <=3 FOR MEMORY lines (data only);
                                default: the oldest archived report not yet read
  produce                       the scheduled pass: librarian, scout, planner + admission
  status                        is Ollama up, the model installed, the GPU lease free?

Exit 0 for done or skipped (Ollama down, GPU busy, lease held: logged in
.review/local_team/producer.log), 1 for a bad model answer, 2 for bad usage.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import core, roles
from .core import BadOutput, Ctx, Skip
from .lease import GpuLease


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m orchestra.local_team")
    ap.add_argument("role", choices=["scout", "planner", "librarian", "produce", "status"])
    ap.add_argument("report", nargs="?", help="librarian: the report to read")
    ap.add_argument("--candidates", help="planner: candidates file (default: the newest)")
    ap.add_argument("--index", type=int, default=0, help="planner: which candidate in the file")
    ap.add_argument("--admit", action="store_true", help="planner: hand the packet to admission")
    ap.add_argument("--lane", help="lane for admitted tasks (default: orchestra/exams router, else claude)")
    ap.add_argument("--repo", default=os.environ.get("ORCHESTRA_REPO", str(core.DEFAULT_REPO)))
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    ctx = Ctx(Path(a.repo))
    try:
        if a.role == "status":
            lease = GpuLease.for_repo(ctx.repo, "status")
            print(json.dumps({"ollama": ctx.ollama, "model": ctx.cfg["local_team_model"],
                              "lease": str(lease.path), "lease_holder": lease.info(),
                              "gpu_busy_pct": core.gpu_utilisation()}, indent=2))
            print("ready: " + core.ready(ctx))
        elif a.role == "scout":
            roles.scout(ctx)
        elif a.role == "planner":
            path = Path(a.candidates) if a.candidates else roles.latest_candidates(ctx)
            if not path or not path.exists():
                ctx.log("planner: no candidates file; run the scout first")
                return 0
            cands = json.loads(path.read_text(encoding="utf-8")).get("candidates", [])
            if not 0 <= a.index < len(cands):
                ctx.log(f"planner: {path.name} has {len(cands)} candidates; nothing at index {a.index}")
                return 0
            res = roles.plan(ctx, cands[a.index], admit_it=a.admit, lane=a.lane)
            print(json.dumps(res["admission"] or res["packet"], indent=2, ensure_ascii=False))
        elif a.role == "librarian":
            rep = Path(a.report).resolve() if a.report else next(iter(roles.new_reports(ctx)), None)
            if rep is None:
                ctx.log("librarian: no unread archived report")
                return 0
            out = roles.librarian(ctx, rep)
            if not a.report:
                roles.mark_librarian_done(ctx, rep)
            print("\n".join(l["text"] for l in out["lines"]) or "(nothing worth keeping)")
        else:
            print(json.dumps(roles.produce(ctx, lane=a.lane), indent=2, ensure_ascii=False))
    except Skip as e:
        ctx.log(f"{a.role}: skipped: {e}")
        return 0
    except BadOutput as e:
        ctx.log(f"{a.role}: bad model answer: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
