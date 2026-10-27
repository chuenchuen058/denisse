# Denisse

<p align="center"><img src="assets/orb.svg" width="160" alt="Denisse's orb, breathing"/></p>

A personal AI assistant that runs 100% locally on this machine, no cloud
API, no API key — a v1 in the direction of Jarvis/IRIS: talk to it, and it
can check the time, read/write files, run commands, open applications, and
remember things about you, not just answer questions.

## Why

Most "AI assistant" demos are a chat box wrapped around an LLM. The
interesting part is the tool-use loop underneath: Denisse decides *when* a
task needs a real action on the machine versus just an answer, executes it,
and folds the result back into the conversation. Running the model itself
locally (via [Ollama](https://ollama.com)) means everything stays private
and there's nothing to pay for per message.

## How it works

- `main.py` — text CLI: read input, get a reply, print it, persist history.
- `voice_mode.py` — push-to-talk voice CLI: press Enter, speak, Denisse
  transcribes it, replies, and speaks back. Same brain and memory as the
  text CLI.
- `wake_word.py` — hands-free "hey Denisse" voice mode: no button, just
  say the wake word (see "Hands-free" below). Same brain/memory again.
- `brain.py` — talks to a model running locally via Ollama
  (`localhost:11434`), defines the tool schema, and runs the tool-use loop
  (model calls a tool → local function executes → result goes back to the
  model → repeat until it has a final answer).
- `tools.py` — the actual local functions the model can call: run a shell
  command, open an app, list/read/write files, get the time, and
  remember/recall/list facts about you.
- `memory_store.py` — persists conversation history to
  `memory/conversation.json` so context survives a restart.
- `memory/facts.json` — the growing set of things Denisse has learned about
  you via the `remember` tool. This is Denisse's personalization today —
  see Roadmap for where this goes next.
- `skills/` — a small skills library: markdown how-to guides (with a name +
  description in frontmatter) for specific tasks, e.g. taking notes,
  organizing files, giving a daily briefing. Denisse calls `list_skills` /
  `read_skill` to consult one before attempting a task it covers, instead
  of improvising.
- `mission_control.py` — a local web dashboard (see below).
- `tray.py` — a system tray icon with an in-popup chat (see below).
- `voice_output.py` — shared text-to-speech helper (see "Talking to
  Denisse" below) used by both `voice_mode.py` and the tray popup.
- `auto_organize.py` — background Downloads-folder sorter (see "Background
  maintenance" below).
- `optimize.py` — background temp-file cleanup + resource monitoring, plus
  on-demand startup-program management (see "Background maintenance").
- `obsidian_sync.py` — best-effort mirror of Denisse's memory into an
  Obsidian vault, if one is configured on this machine (see its own
  docstring); a no-op if there isn't one.
- `safety.py` — a confirmation gate in front of tools that touch the real
  machine. Currently only `open_application` and `disable_startup_program`
  prompt; `run_shell_command` and `write_file` were opened up on request
  (see the file's own comments for the current exact set and why).

## Setup

Requires [Ollama](https://ollama.com) installed and running (it starts
automatically in the background after install — look for its icon in the
system tray), with a model pulled:

```
ollama pull llama3.1:8b
```

Then:

```
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
python main.py
```

No API key, no `.env` required (unless you want to override the model —
see `.env.example`).

## Talking to Denisse (voice mode)

```
python voice_mode.py
```

Press Enter, then speak — it records until you pause, transcribes with
Google's free speech API (no key needed, but does need internet for this
one step), sends the text through the same local brain as the text CLI, and
speaks the reply back.

Replies are spoken with [Inworld](https://inworld.ai)'s neural voice,
**Ashley**, by default — this is the one piece of Denisse that isn't
free: it's a paid cloud API (a ~40-minute free trial, then billed per
character) and needs your own `INWORLD_API_KEY` (see `.env.example`, key
from [platform.inworld.ai](https://platform.inworld.ai)). It's entirely
opt-in — with no key set, voice mode just uses your system's local built-in
voice instead (fully offline, free, less natural-sounding), same as if
Inworld's API fails for any other reason. Override the voice with
`DENISSE_INWORLD_VOICE` (any Inworld voice name).

### Hands-free: say her name (`wake_word.py`)

```
python wake_word.py
```

Or toggle **'Listen for "hey Denisse"'** from the tray icon's right-click
menu — off by default. Any mention of her name wakes her up — "hey
Denisse, what time is it", "Denisse, what time is it", or just "Denisse"
alone (then wait for "Yes?" and say your command) — no chat popup, no
pressing Enter.

**Real tradeoff worth knowing**: unlike push-to-talk, where nothing is
sent anywhere until you press Enter, this listens continuously and sends
every detected phrase to Google's free speech API to check whether it was
the wake word — not just the ones meant for Denisse. A true offline wake
word would need a dedicated local engine (and, for a custom phrase like
"hey Denisse," another third-party account to train it), so this is the
pragmatic version: same free API `voice_mode.py` already uses, just
always listening instead of push-to-talk.

### Desktop shortcut

A shortcut is already on your Desktop: **Denisse.lnk** — double-click it to
launch voice mode directly, no terminal needed.

To pin it to the taskbar (Windows 11 no longer offers "Pin to taskbar" from
a plain right-click on a shortcut):
1. Double-click **Denisse.lnk** so Denisse starts and its window/icon shows
   up in the taskbar.
2. Right-click that taskbar icon and choose **Pin to taskbar**.

(Alternatively, drag the Desktop icon down onto the taskbar.)

## Mission Control (dashboard)

```
python mission_control.py
```

Opens a local web dashboard at `http://localhost:8765` (nothing leaves
your machine — it just reads local files and your local Ollama server).
It shows:

- **Status** — whether Ollama is reachable and your model is pulled.
- **Quick Launch** — a sidebar of one-click shortcuts (→ Talk to Denisse,
  → Outlook, → File Explorer, → project/skills folders by default). Click
  **+ Add shortcut** to pin your own — an app to open, a folder to open,
  or another "Talk to Denisse" launcher — the same idea as pinning a
  shortcut anywhere else, just inside Denisse's own page. Shortcuts are
  saved to `memory/shortcuts.json`.
- **Overview / Memory / Skills / Tools tabs** — recent conversation, every
  fact Denisse has remembered about you, the installed skills, and the
  full tool list, all live.

A **Denisse Mission Control** shortcut is also on your Desktop.

## Tray icon

```
python tray.py
```

Puts a **D** icon in the system tray (bottom-right, next to the clock).
Left-click / double-click it to open a small chat popup right above the
tray — type and get replies right there (spoken aloud too, if voice is
set up), no console window involved. Right-click for the full menu:
chat, a console-based voice mode for anyone who wants that instead,
Mission Control, the auto-organize/auto-optimize toggles (see below), the
project folder, or Quit.

## Background maintenance

Two things run continuously in the background whenever the tray icon is
up (which, with autostart below, is whenever you're logged in) — both
deterministic and rule-based, not LLM-driven, since nobody's watching a
confirmation prompt for something running unattended:

- **Auto-organize** (`auto_organize.py`) — sorts loose files dropped in
  Downloads into type folders (Documents, Images, Videos, Audio, Archives,
  Installers, Other) every 15s. Never touches subfolders, never deletes or
  overwrites (same-named collisions get a " (2)" suffix), and leaves a
  file alone until it's sat untouched for 20s so it doesn't grab something
  mid-download. Every move is logged to `memory/organize_log.json`.
- **Auto-optimize** (`optimize.py`) — cleans temp files (anything in your
  Temp folder untouched for 24h+) once an hour, logged to
  `memory/cleanup_log.json`; checks disk space and memory every 5 minutes
  and shows a tray notification if either looks concerning.

Both can be paused/resumed from the tray's right-click menu, and Denisse
can also be asked directly to check system status, list what launches at
login, or disable/re-enable a startup program (the last one asks for
confirmation first — see `safety.py`).

Windows puts new tray icons in the hidden/overflow area behind the **^**
arrow by default — drag it out onto the main tray, or go to **Settings →
Personalization → Taskbar → Other system tray icons** and turn it on, if
you want it always visible.

### Autostart

A shortcut is already in your Startup folder (`shell:startup` →
**Denisse Tray.lnk**), so the tray icon launches automatically every time
you log in — nothing to run by hand. To stop that, delete the shortcut
from `shell:startup`; to launch it manually instead, run `python tray.py`
or double-click the shortcut yourself.

## Status

v1: fully local text chat + voice mode + local system/task tools +
persistent memory + growing personal fact store + a skills library +
a Mission Control dashboard + a system tray icon with its own in-popup
chat, background file-sorting and system optimization, with desktop
shortcuts and autostart for quick launch, plus a confirmation gate in
front of tools that touch the real machine. Verified end-to-end: tool
calls (time, weather, system status, remember/recall, shell commands)
actually execute and persist to disk, the confirm/decline path was
checked live, and the dashboard's API and tray icon were both checked
live against a running Ollama instance.

## Adaptive brain and editable preferences

Denisse can learn phrase-specific tool-routing lessons from corrections you
positively confirm. Candidate lessons need two independent accepted fixes;
negative feedback can demote them. This updates routing, not model weights
or source code. Private settings live in `memory/brain_rules.json`; the
public template is `brain_rules.example.json`.

See [Adaptive brain](docs/adaptive_brain.md) for `/good` and `/bad`, the
settings, current-project vault overview, off-switches and tests.

## Roadmap

- **Model-weight fine-tuning** is a separate future experiment, requiring
  curated examples and held-out evaluation. Raw history and facts are not
  automatically training data. The adaptive brain currently learns routing
  from confirmed corrections without changing model weights.
- Wake word ("hey Denisse") instead of push-to-talk
- Fully offline speech-to-text (swap Google's API for local
  faster-whisper) so voice mode needs zero internet
- Calendar and email integration
- Background/daemon mode instead of foreground CLI (the tray icon still
  launches a full CLI process per action rather than running Denisse as
  a persistent background service)
