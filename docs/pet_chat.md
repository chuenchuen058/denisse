# Chat with the desktop pet

Click the purple orb or its hover-only chat icon. The conversation opens
beside Denisse and follows when you drag her. It flips sides and stays
inside the primary display's usable work area, including near the taskbar.
Escape or the close button folds it away; your saved conversation remains.

Typed and voice replies share the open chat. A separate duplicate reply
bubble is suppressed while chat is open. When folded away, replies use the
orb's small speech bubble, with **Continue chat**, **Stop voice** and
**Dismiss** actions. Continue chat reopens shared history with the full
answer, including text shortened in the small bubble. Timer/study notices
remain visible without being written as fake conversation turns.

The purple orb, pet movement, voice listening and hover-only Chat / Voice /
Reminders controls are preserved. Reminders open their own card. The chat
uses the existing local model and conversation store.

## Stop speech

- Click **Stop voice** in the chat header or reply bubble.
- While a typed answer is being prepared, its send arrow becomes a stop
  square. Clicking it stops speech without erasing a follow-up draft.
- Type **shush**, **stop talking**, or **you are still talking**. These work
  while the chat is busy and do not require the model to be reachable.
- With wake-word listening enabled, the same spoken controls work without
  saying Denisse's name first. Recognition quality still depends on Vosk.

Stop speech is silent: no model call, calculation, spoken acknowledgement
or synthetic "silence" reply. It invalidates pending speech too, so a reply
which finishes synthesizing afterward cannot start playing. Current text
can still finish appearing. This does not undo actions already performed.
Speech cancellation is shared inside the tray process; an independently
launched console is a separate process.

Windows MCI playback polls cancellation on its owner thread, preserving
the device's thread-affinity requirements. The wake-word local SAPI
fallback also polls cancellation instead of blocking in `runAndWait()`.

## Conversation audit

The recent conversation showed repeated spoken acknowledgements after
requests to stop, plus a file follow-up with invented Windows paths after
reading a skill. Speech controls now bypass the model; file follow-ups
such as "why can't you?" retain file context, and replies with unverified
paths are replaced with actual lookup evidence or an explicit unverified
status. An unsuccessful lookup is not proof that a file does not exist.

These changes do not make all local-model decisions reliable. The same
isolated seven-scenario model suite scored **16/21** before and after this
change. Both versions had greeting failures; file-opening and task-list
choices varied. Do not describe the UI and speech fixes as a general model
quality improvement.

## Verification

```powershell
.venv\Scripts\python.exe scripts\test_pet_chat.py
.venv\Scripts\python.exe scripts\smoke_pet_chat.py
.venv\Scripts\python.exe scripts\test_reply_bubble.py
.venv\Scripts\python.exe eval\test_voice_confirm.py
```

The Tk smoke test uses synthetic history, no model calls, no TTS requests
and no real history writes. It checks movement, screen edges, 100/125/150/200%
scales, reply actions, transient notices, input visibility and closed-window
callbacks. Fixture screenshots stay under ignored `logs/`.
