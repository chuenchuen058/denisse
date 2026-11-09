# KEATS pull

From the checkout that will remain on the laptop, install the Python dependency
and sign in once:

```powershell
.venv\Scripts\python.exe -m pip install playwright
.venv\Scripts\python.exe -m keats login
.venv\Scripts\python.exe -m keats pull --dry
.venv\Scripts\python.exe -m keats pull
```

Use the visible Chrome window to sign in yourself. No password is typed by the
program, read, or stored by it. Chrome persists its own session in the separate
`%LOCALAPPDATA%/Denisse/keats/chrome` profile. It does not reuse or interfere with
your everyday Chrome profile. No `playwright install` or browser download is
needed: the configured channel uses installed Chrome. The first interactive
`pull` also opens that sign-in window. Later pulls run headless. An expired session
exits nonzero with `run python -m keats login`; scheduled runs never wait for login.

The manifest supplies course IDs, module folders and original captured filenames.
Every visible Week section is scanned, including older weeks with late additions
and linked single-section pages. HTML is parsed under the main content region;
theme/setcheck links are excluded. Resource redirects are resolved without
reading binary bodies. Folder resources and direct pluginfile links are supported.
Downloads go through Chrome directly into private staging, then into the vault
using exclusive file creation. Existing files and week notes are never replaced.
Duplicate target names receive a suffix. MATLAB filenames are preserved.

`--dry` resolves names and reports the candidate files and elapsed time, but does
not save downloads, change the manifest, create week notes, notify, or ingest.
Like every pull attempt, it appends one handoff line and commits that line.
Normal pulls commit their explicit file list using the existing vault checkpoint
helper; unrelated staged files are preserved. Nothing is pushed. A failed scan
does not advance `last_sync`; successfully downloaded files before a later
download failure are recorded and checkpointed. A concurrent pull using the same
profile is refused by an OS lock. There is no cloud model or Moodle API/token.

Configuration is in `keats/config.json`; `KEATS_CONFIG` selects a complete local
replacement. Vault discovery uses the existing settings helper (`DENISSE_VAULT`
wins). Selectors, week matching, naming rules, note template, excluded file types,
profile location, messages and schedule time are data. Videos are excluded by
default, consistent with the manifest policy. Non-file activities and video links
are not added to existing notes. A deliberately withheld filename can be placed
in `scan.skip_original_names` in the local config. Course details stay in the
private manifest, not in this public repository.

To request background tutor ingestion after a successful pull:

```powershell
.venv\Scripts\python.exe -m keats pull --ingest
```

The worker runs `python -m tutor ingest` in this checkout and writes `ingest.log`
under the profile. `ingest.enabled` enables it for scheduled pulls. `ingest.cards`
and `ingest.anki_sync` are separate opt-ins, both false by default; without them
ingestion never invokes card generation or Anki sync. Background failures appear
in that log; the pull reports launch, not completion, of ingestion.

Preview the daily Windows task; register only when ready:

```powershell
.venv\Scripts\python.exe -m keats.install
.venv\Scripts\python.exe -m keats.install --register
```

It uses the configured time (shipped: 18:00), runs as the logged-in user without
admin rights or a stored password, requires AC power and a network connection,
and suppresses overlapping runs. Run registration from the permanent checkout
after merge, not a disposable worktree. If using `KEATS_CONFIG`, set it persistently
for that user so scheduled processes inherit it. `scheduled.log` under the profile
contains output when `pythonw` has no console. Windows network availability does
not prove KEATS is reachable; HTTP failures are logged and exit nonzero.

Offline verification (no network, real browser, or real vault):

```powershell
.venv\Scripts\python.exe -m unittest discover -s keats -p "test_*.py" -v
.venv\Scripts\python.exe scripts\run_checks.py --fast
.venv\Scripts\python.exe -m keats.test_pull --benchmark
```

The benchmark times a synthetic dry pull only. Real timing depends on Chrome
startup, number of resources, and KEATS latency; each real run prints its own
elapsed time. Live SSO, live Moodle markup, delivery of Windows toasts, and the
registered schedule need a laptop smoke test after the human signs in.

Toasts use the installed PowerShell host's notification identity, discovered at
runtime, with Denisse/KEATS in the displayed text. Delivery failures are reported
in the handoff and exit nonzero; they do not undo files already saved.

Playwright APIs: [persistent Chrome context](https://playwright.dev/python/docs/api/class-browsertype#browser-type-launch-persistent-context),
[download events](https://playwright.dev/python/docs/downloads).
Windows requirement: [registered notification identity](https://learn.microsoft.com/en-us/windows/win32/shell/enable-desktop-toast-with-appusermodelid).
