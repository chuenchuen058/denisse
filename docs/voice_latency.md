# Voice latency: end of speech to first audio

Measured 2026-11-02 on the RTX 5070 Laptop (8 GB), with Ollama's
`llama3.1:8b` resident on the same GPU. This covers voice_mode.py
(push-to-talk, Whisper + Kokoro).

## Where the time goes

A spoken turn passes through these stages. Each one is timestamped and logged
per turn to `logs/voice_latency.jsonl` (local, git-ignored, never the vault):

| stage | meaning |
|---|---|
| `speech_last_voice` | last loud mic chunk, roughly when he stopped talking (t = 0) |
| `speech_end` | endpointer decided he's done (`DENISSE_END_SILENCE_S` later) |
| `stt_done` | Whisper transcript ready |
| `first_token` | reply text handed to the voice (see "The model" below) |
| `first_sentence` | first complete sentence split off |
| `tts_first_chunk` | Kokoro audio for that sentence ready |
| `playback_start` | first block written to the open output stream |

`.venv\Scripts\python.exe scripts\eval_local_voice.py --breakdown` prints
p50/p95 for each stage from real turns, plus the longest pause *inside* his
utterances. If that pause creeps towards the endpoint threshold, the threshold
is cutting him off.

## Before / after (fixed audio set, model excluded)

`scripts\eval_local_voice.py --latency --repeat 3`: the 6 eval sentences,
spoken by Kokoro over a quiet noise floor and replayed through the real
`Endpointer`. Each one gets a canned 2–3 sentence reply. 18 samples, in seconds:

| stage | before | after | p50 before | p50 after | p95 before | p95 after |
|---|---|---|---|---|---|---|
| endpoint wait | 0.9 s silence | 0.6 s silence | 0.961 | 0.641 | 0.994 | 0.674 |
| Whisper small.en (GPU) | same | same | 0.453 | 0.428 | 0.560 | 0.577 |
| Kokoro (GPU) | whole reply | first sentence | 0.559 | 0.292 | 0.755 | 0.542 |
| start playback | wav + MCI open + play | open stream + first block | 0.228 | 0.204 | 0.313 | 0.280 |
| **total, excl. model** | | | **2.20** | **1.57** | | |

Whisper WER on the set: 8.8% at every threshold from 0.9 down to 0.4. The
errors are the same at each one ("25" for "twenty five", "infusion" for "in
Fusion", "course you work"), so they are Whisper's mishearings, not
truncation. At **0.3 s** WER jumps to 31.9%: the endpointer fires inside a
sentence and he gets cut off.

So 0.6 s is the new default (`DENISSE_END_SILENCE_S`). It is faster with no
extra transcription errors. The longest pause inside a synthetic sentence was
0.38 s, and real speech pauses longer than Kokoro does ("um", thinking
mid-sentence), so 0.6 s keeps some margin rather than taking 0.4 s.
Check `--breakdown`'s longest-pause line after some real use before going lower.

The TTS saving grows with reply length. Whole-reply synthesis scales with the
reply, but first-sentence synthesis doesn't.

### Streaming, checked on the real output device

`StreamingSpeaker` with real Kokoro and the real PyAudio stream, with samples
zeroed so nothing was audible. A 4-sentence reply (8.51 s of audio) took
9.09–9.10 s wall time when warm. That's 0.55 s over the audio, of which
0.47 s is the first sentence's synthesis + stream open. So the three
sentence joins add up to under 0.1 s. Synthesis runs ~10x faster than
real time, so the next sentence is always ready before the current one ends.
A stop (stop button or barge-in) at 1.0 s returned at 1.13 s, and the stream
is closed, which discards its buffer.

### Warm-up

Kokoro's first call in a process: 1.1–3.4 s. The second call: 0.07–0.16 s.
Loading the model takes 1.4–2.0 s more. Whisper's load is 6.4–9.6 s.
voice_mode.py already preloaded Whisper in the background, and now warms
Kokoro in the background too. The tray already did it via
`voice_output.warm_up_audio()` in a thread. Neither blocks startup.

## The model: the stage this doesn't fix

`brain.respond()` is not streamed. It runs a tool loop, then several reply
guards that rewrite the finished text (`_unleak_json`, fake-tool-call
sniffing, retries). Speaking tokens as they arrive would speak text the
guards later change. So `first_token` is currently the moment the whole
reply comes back. `StreamingSpeaker` already takes any iterator of text
chunks, so a streamed brain could feed it directly. Doing that safely needs
the guards to work on a prefix, which is a separate job.

Model timings from `--latency --model` (plain streamed `ollama.chat`, no
tools) were **not usable**: the first run measured 17–121 s to first token,
because the GPU was at 100% from other work during the run (it stayed
at ~100% with the eval stopped). The second run left `--model` out. Re-run
`--latency --model --repeat 3` on an idle machine to fill this in.

## Is "under one second" reachable?

Not with this stack, honestly. Even with a zero-time model, the floor is
endpoint 0.64 + Whisper 0.43 + first sentence 0.29 + playback 0.20 ≈ 1.6 s.
From here, the remaining levers are:

- endpoint lower, once real-turn logs show his pauses allow it (each 0.1 s
  is 0.1 s);
- a smaller Whisper (`DENISSE_WHISPER_MODEL=base.en`): faster, but WER has
  to be re-measured first;
- streaming the brain (above), the biggest one for long replies.

## Barge-in

`DENISSE_BARGE_IN=1` turns on a mic monitor while she speaks
(`voice_pipeline.MicBargeIn`). It needs 0.3 s of continuous sound at 4x the
level heard while she was already talking (`DENISSE_BARGE_IN_MIN_S`,
`DENISSE_BARGE_IN_FACTOR`). It's off by default: the existing VAD is an
energy gate with no echo cancellation, so on open laptop speakers a loud
reply can still interrupt itself. On headphones it's fine. Not yet tried
live with a real voice.

## Settings

| env var | default | what |
|---|---|---|
| `DENISSE_END_SILENCE_S` | 0.6 (was 0.9) | quiet after speech that ends the clip |
| `DENISSE_MIN_SPEECH_RMS` | 300 | minimum loudness that counts as speech |
| `DENISSE_BARGE_IN` | 0 | interrupt her by talking |
| `DENISSE_TTS_LEAD_IN_S` | 0.03 | silence before the first word, so the device can't clip it |
| `DENISSE_VOICE_LATENCY_LOG` | `logs/voice_latency.jsonl` | per-turn timing log |

## Not covered

- tray.py and wake_word.py still synthesize the whole reply and play it
  through MCI. Only voice_mode.py streams.
- All audio here is synthetic (Kokoro speaking to Whisper). A real room and
  real hesitations will be slower to transcribe and pause longer.
  `--breakdown` on real turns is the number that counts.
