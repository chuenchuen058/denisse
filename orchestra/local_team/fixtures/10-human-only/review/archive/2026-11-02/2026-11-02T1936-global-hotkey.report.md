RESULT: PASS
GATE: `.venv\Scripts\python.exe -m unittest discover -s scripts -p "test_hotkey.py" -v` (15 tests, OK) and `.venv\Scripts\python.exe -c "import hotkey"` (exit 0) -> pass; does not cover: the tray wiring in tray.py (`_on_global_hotkey` toggle, the forced focus/caret on a new popup, menu label and tooltip, unregister in `_quit`), whether Windows lets the popup take the foreground from another app, and any real key press. I checked separately that `import tray` works and that a real RegisterHotKey/stop/re-register cycle on ctrl+alt+shift+f23 succeeds, but I never pressed a key and never started the tray.
HUMAN: none
PREMISE: right. Before this, the chat opened only from the tray menu, a click on the orb, or the wake word. Nothing registered a hotkey. The main text entry is ChatPopup's entry box in tray.py.
DONE:
- hotkey.py: pure `parse_hotkey()` (modifiers ctrl/alt/shift/win and their aliases; a-z, 0-9, F1-F24, named keys, punctuation; returns None for `off`; raises ValueError for bad strings), `GlobalHotkey` (RegisterHotKey with MOD_NOREPEAT on a daemon thread with a GetMessageW loop; each press goes to `schedule`; stop posts WM_QUIT and the same thread then unregisters), `start_from_env()`, `describe()` and `is_foreground()`.
- tray.py: registers at startup with `root.after(0, fn)` as the scheduler, wrapped so a failure can never crash startup. A press opens or focuses the chat and forces focus into the entry box. Pressing again while the chat is the foreground window closes it. Unregisters on Quit. The menu item reads "Talk to Denisse (Ctrl+Alt+D)" and the tooltip reads "Denisse -- Ctrl+Alt+D to chat" while the hotkey is active.
- scripts/test_hotkey.py: tests for parsing and for describe(), plus tests of the thread with a fake user32/kernel32: it registers on its own thread, a press runs the callback, a foreign hotkey id is ignored, stop unregisters from the registering thread, a registration failure logs exactly one line, off and bad specs register nothing, and Win32 or schedule exceptions don't escape. One more test covers is_foreground.
- README: a short "Global hotkey" section under Desktop and voice.
NOT DONE:
- Not tested by hand in the running tray (the task said not to start it). the owner should press Ctrl+Alt+D with another app in front and check that the caret lands in the input box.
ODD:
- "Hides it" is done with ChatPopup.close(), because the popup has no hide/withdraw state. Chat history lives in conversation.py, so reopening loses nothing visible.
- parse_hotkey rejects a combo with no modifier, so a bare system-wide key can't swallow that key in every other app.
- The hotkey thread calls AllowSetForegroundWindow(ASFW_ANY) on each press, and a root.after(50) focus() runs after _open_chat. Without that, a new popup's focus_set() does nothing while another app is in front.
- The registration-failure log line goes to stdout via print. Under pythonw nothing captures it; I found no logging framework in tray.py to use instead.
COMMITS: 30a00e3
