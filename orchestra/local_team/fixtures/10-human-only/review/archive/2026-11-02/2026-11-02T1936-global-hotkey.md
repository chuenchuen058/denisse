# Global hotkey: summon Denisse from any application
Queue: auto
Priority: normal
Weight: heavy
Kind: work
Lane: claude
Written: 2026-11-02T19:36

Roadmap section 4 (vault: Denisse/roadmap.md, "Deeper laptop integration"): today Denisse can only be reached from the tray, the orb or the wake word. Add a system-wide hotkey on Windows that opens her chat input from whatever app is in front.

- Use the Win32 `RegisterHotKey` API through `ctypes` (no new pip dependency), on its own thread with a message loop, posting back to the Tk main thread safely (the tray/orb code shows how UI work is scheduled).
- Default `Ctrl+Alt+D`; configurable with env `DENISSE_HOTKEY` (e.g. `ctrl+alt+space`); `DENISSE_HOTKEY=off` disables it. If registration fails (another app owns the combo), log one clear line and carry on; never crash startup.
- Pressing it shows and focuses the existing chat window (or orb input — whichever is the main text entry today), with the caret in the input box. Pressing again while focused hides it.
- Unregister on exit.
- Put the parsing of the hotkey string into a pure function and unit-test it (modifiers, keys, bad strings, `off`). Test the thread/registration logic with the Win32 calls mocked.
- Document it in README (one short section) and the tray menu tooltip if there is one.

Don't start the real tray app as part of the task beyond an import smoke test.

Put new test modules in scripts/ next to the existing ones (the gate discovers them there).

## Gate
```
.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_hotkey.py" -v
.venv\Scripts\python.exe -c "import hotkey"
```
