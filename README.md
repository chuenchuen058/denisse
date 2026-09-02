# Denisse

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
speaks the reply back with your system's built-in voice.

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

## Status

v1: fully local text chat + voice mode + local system/task tools +
persistent memory + growing personal fact store + a skills library +
a Mission Control dashboard, with desktop shortcuts for quick launch.
Verified end-to-end: tool calls (time, remember/recall) actually execute
and persist to disk, and the dashboard's API was checked live against a
running Ollama instance.

## Roadmap

- **Real fine-tuning**: once `memory/conversation.json` and
  `memory/facts.json` have real history in them, that becomes training data
  for actually fine-tuning a local model on your own conversations/style
  (e.g. via Ollama's `Modelfile` + LoRA, or tools like Unsloth/Axolotl).
  This is a bigger, separate project — worth doing once there's enough
  real data to train on, not from day one.
- Wake word ("hey Denisse") instead of push-to-talk
- Fully offline speech-to-text (swap Google's API for local
  faster-whisper) so voice mode needs zero internet
- Calendar and email integration
- Background/daemon mode instead of foreground CLI
- Safer, scoped permissions for shell/file tools
- Custom taskbar icon
