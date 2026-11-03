"""Orchestra soak test: the whole runner under stress, with invariants checked at the end.

    .venv\\Scripts\\python.exe -m orchestra.soak --minutes 5 --seed 1

Builds a throwaway repo with a bare "origin", queues ~40 tasks on three fake script lanes
(sleep, edit, commit, PASS/PARTIAL/FAIL/no report, rate-limit lines, a shared "hot" file for
conflicts, grandchild processes), drives `runner.py` from several concurrent tick processes
while killing some mid-run and flipping the fake power state, then checks the invariants
listed in `harness.INVARIANTS`. Stdlib only; nothing real (vault, GitHub, Ollama) is touched.
See docs/orchestra_soak.md for the bugs it found and the seeds that reproduce them.
"""
