# Denisse

A personal AI assistant that runs locally and can act on this machine — a v1 in the direction of Jarvis/IRIS: talk to it, and it can check the time, read/write files, run commands, and open applications, not just answer questions.

## Why

Most "AI assistant" demos are a chat box wrapped around an LLM. The interesting part is the tool-use loop underneath: Denisse decides *when* a task needs a real action on the machine versus just an answer, executes it, and folds the result back into the conversation.

## How it works

- `main.py` — text CLI: read input, get a reply, print it, persist history.
- `voice_mode.py` — push-to-talk voice CLI: press Enter, speak, Denisse transcribes it, replies, and speaks back. Same brain and memory as the text CLI.
- `brain.py` — wraps the Anthropic API, defines the tool schema, and runs the tool-use loop (model calls a tool → local function executes → result goes back to the model → repeat until it has a final answer).
- `tools.py` — the actual local functions the model can call: run a shell command, open an app, list a directory, read/write a file, get the time.
- `memory_store.py` — persists conversation history to `memory/conversation.json` so context survives a restart.

## Setup

```
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
copy .env.example .env   # then edit .env and add your ANTHROPIC_API_KEY
python main.py
```

## Talking to Denisse (voice mode)

```
python voice_mode.py
```

Press Enter, then speak — it records until you pause, transcribes with
Google's free speech API (no key needed, but does need internet), sends the
text through the same brain as the text CLI, and speaks the reply back with
your system's built-in voice.

### Desktop shortcut

A shortcut is already on your Desktop: **Denisse.lnk** — double-click it to
launch voice mode directly, no terminal needed.

To pin it to the taskbar (Windows 11 no longer offers "Pin to taskbar" from
a plain right-click on a shortcut):
1. Double-click **Denisse.lnk** so Denisse starts and its window/icon shows
   up in the taskbar.
2. Right-click that taskbar icon and choose **Pin to taskbar**.

(Alternatively, drag the Desktop icon down onto the taskbar.)

## Status

v1: text chat + voice mode + local system/task tools + persistent memory,
with a desktop shortcut for quick launch.

## Roadmap

- Wake word ("hey Denisse") instead of push-to-talk
- Calendar and email integration
- Background/daemon mode instead of foreground CLI
- Safer, scoped permissions for shell/file tools
- Custom taskbar icon
