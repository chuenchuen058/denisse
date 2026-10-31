"""
The local brain: talks to a model running on this machine via Ollama
(http://localhost:11434), defines the tool schema, and runs the tool-use
loop that turns a user message into a final text reply (after any tool
calls).

No API key, no internet required after the model is downloaded once.
"""

import datetime
import inspect
import json
import logging
import ntpath
import os
import platform
import re

import ollama
from dotenv import load_dotenv

import learning
import brain_rules
import speech_control
import tool_router
import tools
# obsidian_sync is intentionally not imported -- its read_claude_memory()
# is currently disabled below (measurably broke reply quality); re-add
# this import if that's ever re-enabled.

load_dotenv()

MODEL = os.environ.get("DENISSE_MODEL", "llama3.1:8b")

# Ollama defaults this model to a 4096-token context, which is small enough
# that what goes into the prompt is a real constraint (see conversation.py
# and the 2026-09-04 history-poisoning bug). Raised to 8192 on 2026-09-05
# after measuring it on this machine with eval/run_eval.py:
#
#   4096  clean 8/8, live 7/8 (twice)   median 2.1s
#   8192  clean 8/8, live 8/8           median 2.0s
#
# The extra KV cache costs ~0.5GB: 5.8GB of the 8151MB laptop GPU, still
# 100% on GPU with ~2GB free, no CPU spill and no measured slowdown. The
# remaining live failure at 4096 ("hey" answered with a get_current_time
# call) stopped reproducing at 8192.
#
# Don't raise this further without re-running that eval -- the headroom is
# small, and if it ever spills to CPU every reply gets dramatically slower.
NUM_CTX = int(os.environ.get("DENISSE_NUM_CTX", "8192"))

# How many times one turn may go round the tool-use loop before Denisse has
# to answer in words. The loop used to be `while True` -- a model that keeps
# asking for tools never terminated, and every round appends the call and
# its result to the request, so the prompt grows until it fills NUM_CTX and
# Ollama starts truncating it.
#
# That is not hypothetical: the 2026-09-12 eval run started and never
# finished, and the 2026-09-19 run was still going after ten minutes, with
# Ollama logging ~8200-token requests at ~6s each (vs ~2s when the prompt
# was small). Bounded at 6 -- deep enough for a genuine multi-step answer,
# shallow enough that a stuck turn costs seconds rather than forever. On
# the final round tools are withheld entirely, which forces a text reply.
MAX_TOOL_ROUNDS = int(os.environ.get("DENISSE_MAX_TOOL_ROUNDS", "6"))

# Without this the model guesses POSIX paths for file tools on a Windows
# box -- observed calling list_directory("/home/chuen") and read_file(
# "/home/user/setup.txt") in reply to "what do you think of my setup?".
# Stated as fact rather than left to inference, and derived at runtime so
# it can't drift from the machine it's actually running on.
_MACHINE_FACTS = (
    f"You are running on {platform.system()} {platform.release()} as user "
    f"'{os.environ.get('USERNAME') or os.environ.get('USER') or 'unknown'}', "
    f"whose home folder is {os.path.expanduser('~')} and whose project "
    f"folder is {os.path.dirname(os.path.abspath(__file__))}. Write paths "
    f"the way this OS does (separator '{os.sep}')"
    + (
        ", e.g. C:\\Users\\Name\\Documents -- never invent a POSIX path "
        "like /home/... "
        if os.name == "nt"
        else ". "
    )
    + "If you need a path you weren't given, build it from those, or use "
    "find_files to locate the real one first. "
)

SYSTEM_PROMPT = (
    _MACHINE_FACTS +
    "You are Denisse, a personal AI assistant built from scratch by Chuen "
    "and running locally on his own computer, in the spirit of Jarvis "
    "from Iron Man. You know exactly what you are: a local model "
    f"(currently '{MODEL}', served through Ollama) with a set of tools "
    "bolted on -- not a cloud service, not sentient, and not cagey about "
    "your own nature. If asked what model or AI you're built on, answer "
    "plainly and accurately instead of dodging or guessing. The person "
    "you're talking to is named Chuen -- that's a known fact, not "
    "something to ask about or hedge on. Address him by name naturally "
    "sometimes, not in every reply, the way someone who actually knows "
    "him would. Personality: polite and easygoing, a little casual -- more "
    "like a friend hanging around than a formal assistant. Keep things "
    "light and low-key rather than serious or stiff; it's fine to be warm "
    "and a bit playful, but don't force jokes or overdo enthusiasm -- no "
    "gushing, no exclamation-point-per-sentence energy. Personality is "
    "seasoning, not the meal: stay concise, and never let tone get in the "
    "way of being clear, honest, or useful. You can hold a conversation "
    "and, when it genuinely helps, use your tools to check the time, do "
    "exact math (see calculate -- never compute arithmetic yourself, even "
    "something that looks trivial), run shell commands, open applications, "
    "read/write files, find/locate files by name when the user doesn't "
    "have an exact path, control "
    "Spotify playback (play/pause/skip/check what's playing) when it's "
    "set up, and remember/recall facts about the user. Be direct. Only use a tool "
    "when it's actually needed to answer or act -- don't narrate your "
    "intentions, just do it and report the result. "
    # A "RELAYING TOOL RESULTS" paragraph lived here on 2026-09-23, telling
    # her to name the items in a list and never answer with JSON. It fixed
    # those, and broke other things: clean eval went 10/10 -> 8/10, with
    # greetings picking up a spurious recall() call and "what is 17 times
    # 23?" answering "I called the 'calculate' tool" instead of 391.
    # A/B'd twice each way -- 8/10 with it, 10/10 without.
    #
    # The lesson is about budget, not wording. This prompt is already ~4000
    # characters and an 8B model spreads its attention across all of it, so
    # anything added here is paid for by everything already in it. Guidance
    # that only matters for one tool belongs in that tool's description,
    # where it is in context; the JSON ban lives in _unleak_json, where it
    # costs no attention at all. Think hard before adding to this prompt.
    #
    # Also tried 2026-10-21, and reverted: pairing the greeting examples
    # ("'hey, what's up?' to a greeting, 'anytime' to thanks, an opinion to
    # small talk") and narrowing the garbled clause below to "doesn't make
    # sense at all". Aim: stop "thanks" -> "doing alright, you?" and "do you
    # like rainy days?" -> "I didn't catch that". Clean went 25,25 -> 24,24
    # over two runs each, and greetings started calling list_skills and
    # read_skill. Small talk still answered "didn't catch that".
    "IMPORTANT: for a "
    "greeting, small talk, or a short acknowledgment like 'thanks' or "
    "'ok', respond the way a person actually would -- 'doing alright, "
    "you?', 'anytime', 'no worries' -- without calling any tool first "
    "(not recall, not read_file, not list_directory, not anything). Do "
    "not describe your reply or explain that no tool is needed -- just "
    "say the thing a person would say. The same applies to a message that "
    "doesn't read like a real request at all -- a fragment, a stray word, "
    "or something that looks garbled (voice input sometimes mishears "
    "words) -- ask what he meant or say you didn't catch that, instead of "
    "guessing a tool call for it (e.g. calling get_news with nonsense as "
    "the 'topic'). Only reach for a tool once the message actually reads "
    "like a coherent request for something. Every tool call costs real time "
    "the person is sitting there waiting "
    "through, so skip it whenever a direct answer is possible. You also have a "
    "skills library -- specific how-to guides for particular kinds of "
    "tasks. Call 'list_skills' to see what's available, and 'read_skill' "
    "to load one BEFORE attempting a task it covers (e.g. organizing "
    "files, giving a daily briefing, taking notes) -- follow its "
    "instructions rather than improvising. A tool result is ground truth: "
    "report exactly what it says, including errors and 'User declined' "
    "messages. Never invent or guess at a command's output, a file's "
    "contents, or any other tool result -- if a tool failed or was "
    "declined, say so plainly instead of describing what it would have "
    "returned. If a tool call errors because you got the arguments wrong "
    "(e.g. a function that takes no arguments got one anyway), just call "
    "it again correctly -- do not present your own guess at what it "
    "would have said as if it were the real result."
)

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "description": "Run a shell command on the local machine and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string", "description": "The shell command to run."}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": "Open an installed app by name, or open an existing document using its full path. For a file, CV or resume, first use find_files to get the real full path; then pass that path here. Finding or reading a file does not open it on screen.",
            "parameters": {
                "type": "object",
                "properties": {"app_name": {"type": "string", "description": "Name or path of the application to open."}},
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and folders at a given path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path to list. Defaults to current directory."}},
                "required": [],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find/locate a file by name when the user doesn't know or give an exact path -- searches recursively through their common folders (Desktop, Documents, Downloads, Pictures, Videos, Music, GitHub, OneDrive) and returns matching full paths, newest first. Use this instead of list_directory when the user is asking you to find or locate something rather than browse a folder they already named.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Filename or part of a filename to search for, e.g. 'invoice' or '*.pdf'."},
                    "location": {"type": "string", "description": "Optional: narrow the search to one folder, e.g. 'downloads' or 'desktop'. Leave empty to search everywhere."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Path to the file to read."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file, overwriting it if it already exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to write."},
                    "content": {"type": "string", "description": "Text content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current date and time -- this machine's own local time, or, given a timezone, the current time somewhere else instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone name for elsewhere, e.g. 'Asia/Tokyo', 'Asia/Hong_Kong', 'America/New_York'. Omit for this machine's own local time.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a math expression exactly -- always use this for arithmetic instead of computing it yourself, even for something that looks simple, since that's exactly where a mental slip happens. Supports + - * / // % ** parentheses, unary minus, constants pi/e, and sqrt/abs/round/sin/cos/tan/log/log10/log2/exp/floor/ceil/factorial/pow/min/max. Translate a word problem or percentage into a plain expression first, e.g. '15% of 240' -> '240 * 0.15'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "A plain math expression, e.g. '(17 + 3) * 4' or '240 * 0.15' or 'sqrt(144)'."}
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get a short current-weather summary for a place.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City or place name, e.g. 'Malaysia', 'Tokyo'."}
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "Get the latest real headlines from CNA (Channel NewsAsia). Use this for any news/current-events question -- never guess or make up headlines. It always returns real headlines, even with no topic given -- an empty topic is normal and valid, not an error, and just means general top headlines. Your reply MUST actually name at least 2-3 of the specific real headlines/topics it returned, in your own words -- a reply like 'I've got the news for you' with no actual content is wrong and useless, exactly as wrong as saying you couldn't find any.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Optional topic/category to filter by, e.g. 'business', 'singapore', 'world'. Leave empty for general top headlines -- do not ask the user for a topic first, just call it."}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_song",
            "description": "Search Spotify for a track and start playing it. Needs Spotify set up (SPOTIFY_CLIENT_ID/SECRET) and the Spotify app already open on this machine -- if the result says no active device, tell the user to open Spotify first (or call open_application('Spotify') yourself, then retry). Not configured at all yet is a normal, expected state to report plainly, not an error to apologize for.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Song name, optionally with artist, e.g. 'Heart Shaker Twice' or 'Blinding Lights The Weeknd'."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pause_music",
            "description": "Pause whatever is currently playing on Spotify.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_music",
            "description": "Resume Spotify playback after a pause.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skip_song",
            "description": "Skip to the next track on Spotify.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "previous_song",
            "description": "Go back to the previous track on Spotify.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "current_track",
            "description": "Check what's currently playing on Spotify (song, artist, playing/paused) -- call this instead of guessing if asked what's playing.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_voice_settings",
            "description": "Check what voice is actually speaking your replies out loud right now. You have no built-in knowledge of this -- call this instead of guessing if asked about your own voice.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_brain_location",
            "description": "Check your actual code/memory paths, editable brain rules and current learning status. Use when asked about your brain, rules or whether you are learning; never guess these settings.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": "Check the laptop's current disk space, memory, and CPU usage.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clean_temp_files",
            "description": "Delete old (24h+ untouched) temp files to free disk space. This already runs automatically every hour in the background -- use this to trigger it on demand.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_startup_programs",
            "description": "List programs configured to launch automatically at login.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "disable_startup_program",
            "description": "Stop a program from launching at login (reversible with restore_startup_program). See list_startup_programs for valid names.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Program name as shown by list_startup_programs."}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restore_startup_program",
            "description": "Undo a previous disable_startup_program call, restoring that program to launch at login again.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Program name previously disabled."}},
                "required": ["name"],
            },
        },
    },

    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a fact about the user for later recall, e.g. remember('favorite_coffee', 'flat white').",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short identifier for the fact, e.g. 'favorite_coffee'."},
                    "value": {"type": "string", "description": "The fact to store."},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Recall a previously remembered fact about the user by its key.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "The identifier used when the fact was saved."}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_known_facts",
            "description": "List every fact currently remembered about the user.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skills",
            "description": "List available skills (how-to guides for specific kinds of tasks).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill",
            "description": "Read the full instructions for one skill by name (see list_skills).",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Skill name, e.g. 'organize_files'."}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_task",
            "description": (
                "Add something Chuen has to do, optionally with a deadline. Use this "
                "whenever he says he needs to do something by a time -- 'remind me to "
                "email Sunway before Friday' is add_task('email Sunway', 'before Friday'). "
                "Pass his own words for the deadline; don't work out the date yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "What needs doing, e.g. 'email Sunway about the firm visit'."},
                    "due": {"type": "string", "description": "The deadline, copied from his words -- 'before Friday', 'tomorrow 5pm', 'on the 6th of October 14:00'. If the date is part of the sentence, put it here AS WELL as leaving it in the text. Omit only when he gave no time at all."},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": (
                "Read out Chuen's open tasks. Use this for ANY question about what he "
                "has to do or where his tasks are -- 'what's on my plate', 'what do I "
                "have to do', 'where is the task list', 'show me my tasks', 'anything "
                "due'. Then tell him what the result actually says: name each task and "
                "its deadline. Never answer a question about his tasks from memory, and "
                "never say the list 'is there' without reading it. If he asks WHERE the "
                "list is, answer by telling him what is on it -- do not state a file "
                "path or filename; you do not know where it is stored and will get it "
                "wrong."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": (
                "Mark a task done once Chuen says he's finished it. Describe the task in "
                "words -- it's matched on those. If more than one matches it refuses "
                "rather than guessing, so pass enough to be specific."
            ),
            "parameters": {
                "type": "object",
                "properties": {"task": {"type": "string", "description": "Words identifying the task, e.g. 'the Sunway email'."}},
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_task",
            "description": "Delete a task that was added by mistake. For tasks he has actually done, use complete_task instead so the record is kept.",
            "parameters": {
                "type": "object",
                "properties": {"task": {"type": "string", "description": "Words identifying the task to delete."}},
                "required": ["task"],
            },
        },
    },
    # Timers (timers.py), added 2026-10-20 after "start timer" was answered
    # with run_shell_command("start") and "The timer has started." All the
    # guidance is in these descriptions, not SYSTEM_PROMPT -- see the
    # prompt-budget comment above -- and tool_router only offers them when
    # the message is about a timer, so no other turn pays for them.
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": (
                "Start a countdown timer that pings Chuen from the orb when it ends. Use for "
                "'start a timer', 'set a timer for 10 minutes', 'remind me in 5 minutes to "
                "check the oven', 'time me for 30 seconds'. This is the ONLY way to start a "
                "timer -- never use run_shell_command or open_application for one. If he "
                "didn't say how long, call it with no length and it will tell you to ask him. "
                "Not for the pomodoro study timer, and not for a deadline days away (add_task)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "minutes": {"type": "number", "description": "Length in minutes, e.g. 10 or 1.5."},
                    "seconds": {"type": "number", "description": "Extra seconds, or the whole length for a short timer."},
                    "label": {"type": "string", "description": "What it's for, in a few words, e.g. 'oven'. Optional."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_timer",
            "description": (
                "Stop a running countdown timer: 'stop the timer', 'cancel the oven timer'. "
                "Report exactly what it returns -- if it says no timer was running, say that."
            ),
            "parameters": {
                "type": "object",
                "properties": {"label": {"type": "string", "description": "Which timer, if he named one. Leave out to stop the running one."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_timers",
            "description": "How long is left on running timers: 'how long is left on my timer?'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

# Second attempt at letting Denisse see the Obsidian vault. The first one
# (the disabled block in respond() below) pasted Memory/ into the system
# prompt and measured 0/4 clean against 4/4 with it off. This one is a tool
# she calls, so an unrelated question pays only for this description -- and
# ALL of its guidance lives here rather than in SYSTEM_PROMPT, for the
# reason recorded in the prompt-budget comment above.
#
# Gated so the A/B can be re-run: DENISSE_VAULT_SEARCH=0 removes the tool
# entirely, which is the control arm.
VAULT_SEARCH_DEFINITION = {
    "type": "function",
    "function": {
        "name": "search_vault",
        "description": (
            "Search Chuen's own Obsidian notes and quote them. Use this for a question "
            "about HIS material and HIS history -- his modules, lectures, tutorials and "
            "deadlines, what was decided about Denisse and why, how his machine is set "
            "up. For a current-project overview use query 'current projects'. "
            "For his societies, 'when is the CM coursework due', 'who lectures "
            "electromagnetism', 'what did we decide about the reminder card', 'what is "
            "convolution again' all belong here, because his notes have his answer "
            "rather than a general one. Do NOT use it for general knowledge, for "
            "anything about the current moment (time, weather, news), or for his task "
            "list -- list_tasks owns that. Report only what the result actually quotes; "
            "if it says nothing in the notes matches, say exactly that instead of "
            "filling the gap from your own guess."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The subject to look up, in a few words -- 'computational methods coursework deadline', 'reminder card decision'. Keywords, not a sentence.",
                }
            },
            "required": ["query"],
        },
    },
}

if os.environ.get("DENISSE_VAULT_SEARCH", "1").lower() not in ("0", "false", "off", "no"):
    TOOL_DEFINITIONS.append(VAULT_SEARCH_DEFINITION)


def _unleak_json(text: str, last_tool_result: str) -> str:
    """Replace a reply that is really a JSON status object with something a
    person would say.

    Observed 2026-09-23: asked to tick a task off, the model answered
    '{"status": "success", "message": "Task completed successfully."}'.
    The tools already return plain English, so the model invented that. The
    system prompt now forbids it, but an 8B model will do it anyway
    sometimes, so this is the net underneath.

    Prefers a human-readable field out of the blob, then the actual tool
    result, and only then a generic line -- in that order, because the tool
    result is the one thing here that is definitely true.
    """
    stripped = (text or "").strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return text
    try:
        blob = json.loads(stripped)
    except (ValueError, TypeError):
        return text
    if not isinstance(blob, dict):
        return text
    for key in ("message", "output", "result", "response", "text", "content"):
        value = blob.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return (last_tool_result or "").strip() or "Done."


# A reply that is nothing but commentary on her own tool use: "No tool call
# needed for that." Seen 2026-10-18 in the eval's live condition, answering
# "thanks" -- twice out of two runs once the router had shrunk the tool list.
# The system prompt already forbids it; like _unleak_json, this is the net
# underneath. Only a reply made ENTIRELY of such a sentence matches, so a
# real answer that happens to mention a tool is never touched.
_TOOL_NARRATION = re.compile(
    r"^\s*(?:no|none|there(?:'s| is) no|i don'?t need)\b[^.!?\n]*"
    r"\b(?:tools?|functions?|json)(?:\s+(?:calls?|responses?))?\b[^.!?\n]*[.!]?\s*$",
    re.IGNORECASE,
)
# "json" since 2026-10-22: "No JSON response is needed for this one." --
# all three greetings in one eval run once _STYLE_EXAMPLES were on.

# "I didn't catch that. Could you rephrase?" to a clear, typed question.
# Traced 2026-10-30: "do you like rainy days?" got it 4/4 against the live
# history and 0/3 clean -- the history confused her, the question was fine.
# conversation._drop_failed now keeps the worst turns out of the replay;
# this is the backstop when it still happens: one retry with the history
# left out. A message of under three words can still get "didn't catch".
_DIDNT_CATCH = re.compile(
    r"didn'?t\s+(?:quite\s+)?catch|\brephrase\b|say\s+that\s+again|ask\s+that\s+again"
    r"|^\s*what\s+do\s+you\s+mean\??\s*$",
    re.IGNORECASE,
)


def _clear_message(text: str) -> bool:
    return len(re.findall(r"[A-Za-z][A-Za-z']*", text or "")) >= 3


# "and in Penang?" after a weather question. The router carries the weather
# tool over, but live the model called nothing and answered from memory --
# 3/3 before the 2026-10-30 replay change, still 2/3 after. Only one right
# thing to do, so the code asks for the place.
_PLACE_FOLLOW_UP = re.compile(
    r"^\s*(?:and|what\s+about|how\s+about)\s+(?:in\s+|for\s+|at\s+)?"
    r"([A-Za-z][A-Za-z .'-]{1,40}?)\s*[?.!]*\s*$", re.IGNORECASE)
_NOT_A_PLACE = re.compile(r"^(?:you|me|it|that|this|them|there|here|him|her|us)$", re.IGNORECASE)


def _weather_place(text: str, request_groups) -> str | None:
    """The place in a weather follow-up, or None."""
    if "weather" not in (request_groups or ()):
        return None
    m = _PLACE_FOLLOW_UP.match(text or "")
    if not m:
        return None
    place = m.group(1).strip()
    if _NOT_A_PLACE.match(place) or _TIME_REQUEST.search(place):
        return None
    return place


# His standing asks, 2026-10-31, each said more than once in one evening:
# "you dont have to say the file path its too long and not great to hear",
# "dont show the path at all ... ask if i want the file path", "you could
# just say me instead of chuen", "keep it short and simple". The model
# agreed every time and did it again in the next reply, so the code does it
# to every reply instead. Paths become file names plus one question; the
# full paths are kept (Brain._last_paths) for a "yes".
_WIN_PATH = re.compile(
    r"(?:[A-Za-z]:|\\\\)(?:[\\/][^\\/\r\n]+)*[\\/]"
    r"(?P<name>[^\\/\r\n]*?\.[A-Za-z0-9]{2,5}(?=$|[\s,;:)\]'\"!?]|\.(?:\s|$))"
    r"|[^\\/\s,;:)\]'\"]+)")
_PATH_ASK = "Want the full path?"
# Asking for it, not mentioning it: "you dont have to say the file path"
# kept every path in the first version.
_WANTS_PATH = re.compile(
    r"\b(?:what'?s|whats|what\s+is|give|show|send|tell|copy)\b[^.?!]{0,25}\b(?:path|location)s?\b"
    r"|\bfull\s+paths?\b|\b(?:which|what)\s+folder\b", re.IGNORECASE)
_PATH_YES = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|go\s+on|show\s+(?:me|it|them)"
    r"|(?:what'?s|give\s+me|send\s+me|show\s+me)\s+(?:the\s+)?(?:full\s+)?paths?)"
    r"(?:[\s,]+(?:please|thanks?|show\s+me|the\s+path))*[.!\s]*$", re.IGNORECASE)


def _hide_paths(text: str) -> tuple[str, list]:
    """Swap every Windows path in a reply for its file name. Returns the new
    text and the paths taken out, in order, without repeats."""
    taken = []
    names = {}
    for m in _WIN_PATH.finditer(text or ""):
        names.setdefault(m.group("name").lower(), set()).add(m.group(0).rstrip(".").lower())

    def where(full, others):
        # The nearest folder that tells same-named files apart: "Resume\IUMC"
        # and "Resume - Copy\IUMC" differ one level up, not at IUMC.
        parts = re.split(r"[\\/]", full.lower())
        rest = [re.split(r"[\\/]", o) for o in others if o != full.lower()]
        for k in range(2, len(parts) + 1):
            if all(len(o) < k or o[-k] != parts[-k] for o in rest):
                return re.split(r"[\\/]", full)[-k]
        return re.split(r"[\\/]", full)[-2]

    def name_of(m):
        full = m.group(0).rstrip(".")
        if full not in taken:
            taken.append(full)
        name = m.group("name")
        same = names.get(name.lower(), ())
        if len(same) > 1:
            # Two "Passport ctc.pdf"s read as a repeat; say where each is.
            return f"{name} (in {where(full, same)})"
        return name

    out = _WIN_PATH.sub(name_of, text or "")
    out = re.sub(r"(No files matching '[^']*' found) in [^.\n]*\.(?: That only covers the folders searched\.)?",
                 r"\1 in the folders I search.", out)
    return out, taken


_YOU_VERB = {"is": "are", "has": "have", "was": "were", "does": "do"}


def _second_person(text: str) -> str:
    """Notes say "Chuen is a collaborator"; she reads that out to Chuen.
    Talking about him to him, "Chuen" is "you". "Hey Chuen" and "Thanks,
    Chuen" stay -- those are talking to him already."""
    if "Chuen" not in (text or ""):
        return text
    sentences = re.split(r"(?<=[.!?\n])(\s+)", text)
    out = []
    for s in sentences:
        if "Chuen" not in s:
            out.append(s)
            continue
        before = s
        s = re.sub(r"\bChuen'?s\b(?!\s+(?:is|has|was)\b)", "your", s)
        s = re.sub(r"\bChuen\s+(is|has|was|does)\b", lambda m: "you " + _YOU_VERB[m.group(1)], s)
        s = re.sub(r"\bChuen(?=\s+(?:\w+ed|can|could|will|would|should|did|had|also|said|told|"
                   r"wants?|needs?|asked|chose|wrote|made|built|got|went|saw|kept|left)\b)",
                   "you", s)
        s = re.sub(r"(?<!, )(?<!Hey )(?<!hey )(?<!Hi )(?<!hi )\bChuen\b(?!,)(?![.!?]?\s*$)", "you", s)
        if s != before:
            s = re.sub(r"\bhis\b", "your", s)
            s = re.sub(r"\bhimself\b", "yourself", s)
            s = re.sub(r"\bhe\s+(is|has|was|does)\b", lambda m: "you " + _YOU_VERB[m.group(1)], s)
            s = re.sub(r"\bhe\b", "you", s)
            s = re.sub(r"(^|[.!?]\s+|\n)(you|your)\b", lambda m: m.group(1) + m.group(2).capitalize(), s)
        out.append(s)
    return "".join(out)


_NOTES_HEADER = re.compile(r"^From your notes \([^)]*\):", re.MULTILINE)


def _tidy_reply(text: str, latest_user: str) -> tuple[str, list]:
    """The last step before a reply leaves: his standing style asks, done in
    code. Returns (reply, paths hidden from it)."""
    text = _NOTES_HEADER.sub("From your notes:", text or "")
    text = _second_person(text)
    if _WANTS_PATH.search(latest_user or ""):
        return text, []
    text, paths = _hide_paths(text)
    if paths and _PATH_ASK not in text:
        text = text.rstrip() + "\n\n" + _PATH_ASK
    return text, paths


# Writing a file is something he asks for. On 2026-10-31 nobody did, and she
# wrote "yes delete them" to old_cvs.txt, made Documents\CVs and Notes, and
# overwrote her own settings file with one of his sentences -- write_file is
# auto-allowed (safety.py, his choice), so nothing stopped it.
_WRITE_REQUEST = re.compile(
    r"\b(?:write|save|create|make|draft|jot|type|put|store|export|copy)\b[^.?!]{0,60}?"
    r"\b(?:files?|notes?|txt|text|docs?|documents?|lists?|csv|markdown|md|log|letter|draft"
    r"|desktop|folder|downloads?|documents)\b"
    r"|\b(?:save|write)\s+(?:it|this|that|them)\b",
    re.IGNORECASE)
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))


def _inside_denisse(path: str) -> bool:
    try:
        p = os.path.normcase(os.path.abspath(os.path.expanduser(path or "")))
        return p == os.path.normcase(_REPO_DIR) or p.startswith(os.path.normcase(_REPO_DIR) + os.sep)
    except Exception:
        return True


# A new task is something he asks for, too. "you also dont need to
# pronounce the file path ... ask if i want the file path next time" became
# the task "ask for file path next time" (2026-10-31).
_TASK_REQUEST = re.compile(
    r"\bremind|\breminders?\b|\btasks?\b|\bto-?do\b|\badd\b|\bdon'?t\s+(?:let\s+me\s+)?forget"
    r"|(?<!don't )(?<!dont )(?<!do not )\b(?:need|have|got)\s+to\b|\bmust\b|\bdeadline\b|\bdue\b"
    r"|\bschedule\b|\bput\s+(?:it|that|this)\s+(?:on|in)\b",
    re.IGNORECASE)

# How he wants her to talk, not a task: "you dont have to say the file
# path", "from now on ...", "next time you could just ...", "keep it short".
# On 2026-10-31 the model answered these with "I've moved the files to a
# temporary folder", a junk task, and three junk files. He asked for
# "alright noted"; the note is written to memory/style_notes.jsonl, so a
# person can turn it into code the way paths and "Chuen" were.
_STYLE_NOTE = re.compile(
    r"^\s*(?:(?:okay|ok|also|and|great|alright|but|btw|nice)[,.!\s]+)*(?:"
    r"you\s+(?:also\s+)?(?:don'?t|dont|do\s+not)\s+(?:have|need)\s+to\b"
    r"|(?:i\s+)?(?:don'?t|dont|do\s+not)\s+need\s+you\s+to\b"
    r"|from\s+now\s+on\b|next\s+time\b|in\s+(?:the\s+)?future\b|no\s+need\s+to\b"
    r"|keep\s+it\s+(?:short|simple|brief)\b|stop\s+(?:saying|showing|reading|repeating)\b"
    r"|you\s+(?:could|can|should)\s+just\s+(?:say|call|use|ask|add)\b)",
    re.IGNORECASE)
STYLE_NOTES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory", "style_notes.jsonl")


def _style_note(text: str) -> bool:
    t = (text or "").strip()
    return (bool(_STYLE_NOTE.match(t)) and not re.search(r"\bremind\b", t, re.IGNORECASE)
            # "..., but find my cv" carries a real request after the note.
            and not re.search(r"\b(?:but|and|then|now)\s+(?:(?:can|could)\s+)?(?:you\s+)?(?:please\s+)?"
                              r"(?:find|open|play|search|check|pull|show|get|delete|send|write|look)\b",
                              t, re.IGNORECASE))


def _write_style_note(text: str) -> None:
    try:
        with open(STYLE_NOTES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.datetime.now().isoformat(timespec="minutes"),
                                "text": text.strip()}, ensure_ascii=False) + "\n")
    except OSError:
        logging.warning("style note not saved")


# Deleting or moving his files is never a side effect of a chat. On
# 2026-10-31 "yes delete them" produced three Move-Item calls on his CVs
# (stubbed in the replay; run_shell_command runs unasked, safety.py).
_DESTRUCTIVE_SHELL = re.compile(
    r"\b(?:Remove-Item|rm|rmdir|rd|del|erase|Move-Item|mv|move|ren|Rename-Item|Clear-Content"
    r"|Set-Content|Out-File|Clear-RecycleBin|format|takeown|icacls|cipher)\b"
    r"|(?<![2\d])>(?!\s*\$?null\b)\s*[^\s&|]",
    re.IGNORECASE)

# A deadline he never said. "remind me to submit the team sign up" became
# "on the 6th of October at 2 PM" (2026-10-31). The words that pin a date
# have to be his.
_DUE_WORDS = re.compile(
    r"\d+|\b(?:today|tonight|tomorrow|tmrw|tmr|tmw|yesterday|weekend|week|month|year|noon|midnight"
    r"|morning|afternoon|evening|night|mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug"
    r"|sep|oct|nov|dec)", re.IGNORECASE)
_DUE_ALIAS = {"tmrw": "tomorrow", "tmr": "tomorrow", "tmw": "tomorrow", "tonight": "today"}


def _due_said(due: str, text: str) -> bool:
    def words(s):
        out = set()
        for w in _DUE_WORDS.findall((s or "").lower()):
            if w.isdigit():
                if int(w):
                    out.add(str(int(w)))
            else:
                out.add(_DUE_ALIAS.get(w, w))
        return out
    return words(due) <= words(text) | {"today"} if words(due) else True


# "before 2nd of november", said straight after she added a task: that's
# the task's deadline. Live she said "I'll add that to your reminders" and
# changed nothing.
_DEADLINE_ONLY = re.compile(
    r"^\s*(?:it'?s\s+|its\s+|make\s+it\s+|actually\s+|no,?\s+)?"
    r"(?:by|before|due|on|for|until|till|at)\s+[\w\s.:'-]{2,40}?\s*[.!?]?\s*$",
    re.IGNORECASE)


# A real answer wrapped in commentary on how she got it: "I called the
# get_weather tool to get the current weather in London. According to the
# tool, ..." and "The tool call returned a list of news headlines" (eval,
# 2026-10-20 -- both passed, and the second went into dataset.jsonl).
# _TOOL_NARRATION only matches a reply made entirely of such talk, so these
# got through. Any tool's own name in a reply counts too: Chuen never says
# "current_track", so she has no reason to.
_TOOL_TALK = re.compile(
    r"\b(?:i (?:called|used|ran|invoked)|called the|tool calls?|function calls?|"
    r"the tool|the function|according to the (?:tool|function))\b",
    re.IGNORECASE,
)
_TOOL_NAMES = {d["function"]["name"] for d in TOOL_DEFINITIONS
               if "_" in d["function"]["name"]}


def _talks_about_tools(text: str) -> bool:
    low = (text or "").lower()
    return bool(_TOOL_TALK.search(low)) or any(n in low for n in _TOOL_NAMES)


# Lead-ins that wrap a real answer: "According to the tool, it's raining" and
# "I called the 'current_track' tool to see what's playing, and it said
# nothing is". Cut, the rest is the answer.
_TOOL_LEAD_IN = re.compile(
    r"^(?:according to the (?:tool|function)[^,]*,\s*"
    r"|i (?:called|used|ran|checked) [^,]*?(?:tool|function)[^,]*,\s*"
    r"(?:and\s+)?(?:it|that)\s+(?:said|says|shows?|showed|returned|reports?|reported)"
    r"\s+(?:that\s+)?)",
    re.IGNORECASE,
)


# The chat template's role marker emitted as text: "assistant\n\nNo worries."
# Seen 2026-10-21 on live greetings, old code too. Traced to the
# _PLAIN_REPLY_NUDGE retry: a request ending on a system note gets the
# marker back 3 times out of 3. Lowercase only, as observed.
_ROLE_PREFIX = re.compile(r"^\s*assistant\b[:\s]+")


# Made-up exchanges in her voice, placed between the system prompt and the
# real conversation. 2026-10-22: Chuen wants her to "speak like a real
# person", and she talks in report-voice -- "The Pomodoro Timer has been
# started.", "The result of the multiplication is 391." Rules in
# SYSTEM_PROMPT have twice made her worse (see its comments), but the model
# demonstrably copies the conversation it's shown -- that is how the bad
# history poisoned her on 2026-09-04. This turns that the right way round.
# Tool results use the real formats so the examples look like her own turns.
# No prompt here may match an eval case -- the suite would then measure
# recall of the example, not her voice.
_STYLE_EXAMPLES = [
    {"role": "system", "content":
        "Here are a few made-up example exchanges showing how you sound. They "
        "never happened -- never mention them. The real conversation follows."},
    {"role": "user", "content": "yo"},
    {"role": "assistant", "content": "Hey Chuen. What's up?"},
    {"role": "user", "content": "what's 12 times 4?"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "calculate", "arguments": {"expression": "12*4"}}}]},
    {"role": "tool", "content": "12*4 = 48"},
    {"role": "assistant", "content": "48."},
    {"role": "user", "content": "remind me to email Ben friday at 5"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "add_task", "arguments": {"text": "email Ben", "due": "friday 5pm"}}}]},
    {"role": "tool", "content": "Added: email Ben (Fri 17:00)"},
    {"role": "assistant", "content": "Got it -- I'll remind you to email Ben on Friday at 5."},
    {"role": "user", "content": "cats or dogs?"},
    {"role": "assistant", "content": "Dogs, probably. Cats do have the better attitude though. You?"},
    {"role": "user", "content": "cheers"},
    {"role": "assistant", "content": "Anytime."},
]


def _style_examples() -> list:
    # Read per call, so the eval can A/B it without a restart.
    default = "1" if brain_rules.load().style_examples else "0"
    on = os.environ.get("DENISSE_STYLE_EXAMPLES", default).lower() not in ("0", "false", "off", "no")
    return [dict(m) for m in _STYLE_EXAMPLES] if on else []


# A news reply with no news in it: "I've got the latest news for you.",
# "Today's news.", "It looks like the news summary was cut off" -- all real
# (his history and the 2026-10-22 eval), while get_news had returned six
# good headlines. Its description already demands 2-3 named headlines and
# the model ignores that, so -- like _unleak_json -- the net reads them out
# from the tool result, the one thing here that is definitely true.
_NEWS_HEAD = re.compile(r"^[^:]*headlines[^:]*:\s*", re.IGNORECASE)


def _headlines(result: str) -> list:
    body = _NEWS_HEAD.sub("", (result or "").strip(), count=1)
    if body == (result or "").strip():
        return []           # not a headline list -- an error, say
    heads = [re.sub(r"\s*\[[^\]]*\]\s*\.?$", "", h).strip().rstrip(".")
             for h in body.split(";")]
    return [h for h in heads if h]


def _has_news_content(reply: str, result: str) -> bool:
    """True if the reply uses at least two distinctive words from the
    headlines -- enough to show it relayed some of them."""
    words = {w for h in _headlines(result)
             for w in re.findall(r"[a-z]{5,}", h.lower())}
    if not words:
        return True         # nothing to compare against; leave it alone
    said = set(re.findall(r"[a-z]{5,}", (reply or "").lower()))
    return len(words & said) >= 2


def _speak_headlines(result: str, n: int = 3) -> str:
    heads = _headlines(result)[:n]
    if not heads:
        return ""
    heads = [re.sub(r"^Analysis:\s*", "", h) for h in heads]
    if len(heads) == 1:
        return f"Top story on CNA: {heads[0]}."
    return ("Top stories on CNA: " + ". ".join(heads[:-1])
            + f". And {heads[-1]}.")


def _strip_role_prefix(text: str) -> str:
    out = _ROLE_PREFIX.sub("", text or "", count=1)
    return out if out.strip() else text


def _strip_tool_talk(text: str) -> str:
    """Cut the narration out of a reply that wraps a real answer in it.

    Deterministic on purpose. The first version re-asked the model with tools
    off, and on 2026-10-21 that turned a good weather answer into "assistant
    I checked the weather for you." and the news into "assistant I didn't
    catch that." -- worse than the narration. So: strip lead-ins, drop the
    sentences that are still tool talk, and if fewer than four words survive
    there was no answer inside it -- return the original untouched.
    """
    if not _talks_about_tools(text):
        return text
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        cut, n = _TOOL_LEAD_IN.subn("", sentence)
        if n and cut:
            cut = cut[0].upper() + cut[1:]
        if cut and not _talks_about_tools(cut):
            kept.append(cut)
    out = " ".join(kept)
    return out if len(out.split()) >= 4 else text

_PLAIN_REPLY_NUDGE = (
    "Your last draft talked about tools instead of answering. Reply to his "
    "last message the way a person would, in a few words, and don't mention "
    "tools or functions."
)


# A reply that says something was DONE (learning.CLAIMS_DONE, shared with
# the nightly labeller). Seen 2026-10-20: "start timer" ->
# run_shell_command("start") -> "(no output)" -> "The timer has started.",
# then "stop timer" -> no tool at all -> "Done." Neither was true; there was
# no timer. The system prompt already says a tool result is ground truth,
# and an 8B model claims success anyway, so -- like _unleak_json -- this is
# the net underneath, checked against what actually ran this turn.
_CLAIMS_DONE = learning.CLAIMS_DONE

_NO_TOOL_CLAIM_NUDGE = (
    "You just said it was done, but you didn't call any tool this turn, so "
    "nothing actually happened. If one of your tools does what he asked, "
    "call it now. If none does, tell him plainly that you can't do that yet "
    "-- don't say it's done."
)

_UNCONFIRMED_CLAIM_NUDGE = (
    "You just said it worked, but no tool this turn confirmed that anything "
    "happened -- they returned: {results}. Don't say it worked. Either call "
    "the tool that actually does what he asked, or tell him plainly what "
    "happened."
)

_WRONG_TOOL_CLAIM_NUDGE = (
    "You just said it was done, but the only tools that ran this turn were "
    "{ran}, and none of them does that. Call the tool that actually does "
    "what he asked now, or tell him plainly it isn't done."
)


# A claim about a thing that only certain tools can change. "Timer set for 5
# minutes" after calculate(300) passed the check above -- calculate DID
# succeed, it just isn't a timer (2026-10-20). So a done-claim that names one
# of these also needs one of its tools to have run this turn.
_CLAIM_NEEDS = (
    (re.compile(r"\btimers?\b", re.IGNORECASE), {"set_timer", "cancel_timer"}),
    (re.compile(r"\btasks?\b|\bto-?do\b|\breminders?\b", re.IGNORECASE),
     {"add_task", "complete_task", "remove_task", "set_timer"}),
    (re.compile(r"\b(?:playing|played|paused|resumed|skipped)\b", re.IGNORECASE),
     {"play_song", "pause_music", "resume_music", "skip_song", "previous_song"}),
    (re.compile(r"\b(?:opened|pulled up)\b|\bis (?:now )?open\b", re.IGNORECASE),
     {"open_application"}),
)
_SET_FOR = re.compile(r"\b(?:set|started|running)\s+for\b", re.IGNORECASE)
# "Deleted 280 old CVs." (2026-10-31) after three write_file calls and a
# refused delete. She has no tool that deletes his files, so a claim that
# files are gone is checked on its own, done-word or not.
_DELETE_CLAIM = re.compile(
    r"\b(?:i'?ve\s+|i\s+have\s+|i\s+)?(?:deleted|erased|trashed|got\s+rid\s+of|cleared\s+out|removed)\b"
    r"(?![^.!?\n]{0,40}\btask)", re.IGNORECASE)
_DELETE_TOOLS = {"remove_task", "clean_temp_files"}


def _claims_deleted(text: str) -> bool:
    for m in _DELETE_CLAIM.finditer(text or ""):
        before = text[max(0, m.start() - 20):m.start()].lower()
        if not re.search(r"\b(?:not|n't|never|no|nothing|haven'?t|hasn'?t)\b\W*(?:\w+\W+)?$", before):
            return True
    return False


def _claim_without_its_tool(text: str, ran: set) -> bool:
    if _claims_deleted(text) and not ran & _DELETE_TOOLS:
        return True
    if not (_CLAIMS_DONE.search(text) or _SET_FOR.search(text)):
        return False
    return any(rx.search(text) and not (ran & needs) for rx, needs in _CLAIM_NEEDS)


def _confirms_nothing(result: str) -> bool:
    """A tool result that is no evidence the thing was done: an error, a
    refusal, or no output at all (run_shell_command("start") returns
    "(no output)" and opens nothing useful)."""
    r = (result or "").strip()
    return (not r or r == "(no output)" or learning.tool_failed(r)
            or r.startswith(("User declined", "Refused", "No timer started"))
            or "nothing was stopped" in r.lower())


def _has_task_content(reply: str, result: str) -> bool:
    """Don't replace a real task list with 'you have a lot on your plate'."""
    items = re.findall(r"^\s*-\s+(.+)$", result, re.MULTILINE)
    if not items:
        return True
    words = {w for item in items for w in re.findall(r"[a-z]{4,}", item.lower())}
    words -= {"overdue", "today", "tomorrow", "monday", "tuesday", "wednesday",
              "thursday", "friday", "saturday", "sunday", "task", "tasks"}
    said = set(re.findall(r"[a-z]{4,}", reply.lower()))
    return bool(words) and len(words & said) >= min(2, len(words))


_VAULT_ACCESS_DENIAL = re.compile(
    r"\b(?:don'?t|do not|can'?t|cannot|no longer)\s+(?:have\s+)?access\b.{0,60}\bvault\b"
    r"|\bvault\b.{0,100}\b(?:inaccessible|disabled|not accessible)\b"
    r"|\bit(?:'s| has| is)\s+(?:been\s+)?disabled\b",
    re.IGNORECASE,
)

_FILE_ABSENCE = re.compile(
    r"\b(?:couldn'?t|could not|can'?t|cannot)\s+find\b"
    r"|\b(?:doesn'?t|does not|does not appear to)\s+exist\b",
    re.IGNORECASE,
)


# ---- 2026-10-29: replies that were wrong 3 runs out of 3 ------------------
# Each piece below replaces a model decision that the eval showed failing
# every time, in both clean and live (eval/cases.py, the 2026-10-26..28
# block; scripts/test_reply_guards.py). The pattern is the one Friday's A/B
# pointed at (decisions.md 2026-10-24): where there is one right answer,
# decide it in code, before or after the model, not by asking it nicely.

# "Stop telling me the news" asks for the daily briefing to be switched
# off. The word "news" routed every one of these to get_news, and
# _speak_headlines then forced the headlines into the reply even when the
# model hadn't -- so the code itself turned "stop" into a news bulletin.
# Nothing she could call turned the briefing off. Now it does.
_NEWS_OFF = re.compile(
    r"\b(?:stop|quit|no\s+more|don'?t|do\s+not|never|turn\s+off|switch\s+off"
    r"|disable|cancel|unsubscribe)\b[^.?!]{0,40}?\b(?:news|headlines?|briefings?|cna)\b",
    re.IGNORECASE,
)

# What she runs on is a fact, not something for the model to work out.
# Asked "how many parameters do you have?" she called calculate and said
# "I have 8 parameters", or list_skills and "I have 8 skills" (3/3 each).
_SELF_MODEL = re.compile(
    r"\bhow\s+many\s+parameters?\b"
    r"|\bwhat(?:'?s|\s+is|\s+are)?\s+your\s+parameters?\b"
    r"|\b(?:what|which)\s+(?:model|llm|language\s+model)\s+(?:are\s+you|do\s+you\s+(?:run|use)"
    r"|are\s+you\s+running)\b"
    r"|\bhow\s+big\s+(?:are\s+you|is\s+your\s+model)\b",
    re.IGNORECASE,
)


def _model_identity() -> str:
    name, _, tag = MODEL.partition(":")
    m = re.match(r"([a-z]+)([\d.]*)", name, re.IGNORECASE)
    family = (m.group(1).capitalize() + (" " + m.group(2) if m.group(2) else "")) if m else name
    size = re.search(r"(\d+(?:\.\d+)?)\s*b\b", tag, re.IGNORECASE)
    if size:
        n = size.group(1)
        return (f"I'm {family} {n}B -- about {n} billion parameters, running "
                "locally on your laptop through Ollama.")
    return f"I'm {MODEL}, running locally on your laptop through Ollama."


# calculate is always offered (tool_router.CORE), and on 2026-10-28 she
# called it nine times on things that were not maths: "0" for "stop
# talking", "100" for "how high is your iq", "parameter". Friday's finding
# again -- the tool reflex is conserved and lands on whatever is always on
# offer. It isn't removed here, it's blocked: a calculation needs a number
# or a maths word in what he actually said.
_MATHS_REQUEST = re.compile(
    r"\d|[+*/^=%]|\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    r"|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty"
    r"|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion"
    r"|half|quarter|double|twice|triple|plus|minus|times|multipl\w*|divid\w*"
    # Not "mean", "sum" or "root": "what do you mean?" is not maths, and
    # every real calculation using those words has a number in it anyway.
    r"|percent\w*|squared?|cubed?|sqrt|average|calculat\w*|comput\w*"
    r"|convert\w*"
    # Units: a question in these is conversion-shaped. Without them the
    # guard refused calculate('212') for "boiling point of water in
    # fahrenheit", and the refusal itself confused her into "I didn't
    # catch that" 2/5 -- a regression on a case that had always passed.
    r"|fahrenheit|celsius|kelvin|degrees?|km|kilomet\w*|miles?|metres?|meters?"
    r"|kg|kilos?|kilograms?|pounds?|lbs?|grams?|ounces?|litres?|liters?"
    r"|gallons?|inch(?:es)?|feet|foot|cm|mm|mph|kph|usd|myr|gbp|sgd|ringgit)\b",
    re.IGNORECASE,
)

# What to search for when she answered a file request without searching.
# Only from "my <thing>" or "called/named <thing>" -- guessing a query out
# of a whole sentence would search for junk.
_FILE_QUERY_STOP = {
    "my", "the", "a", "an", "latest", "newest", "recent", "most", "last",
    "file", "files", "document", "documents", "doc", "copy", "please", "for",
    "me", "it", "its", "literally", "again", "check", "that", "this", "up",
    "pull", "open", "show", "bring", "find", "get", "called", "named", "to",
    "of", "now", "then", "here", "there",
}


def _file_query(text: str) -> str | None:
    t = (text or "").lower()
    if re.search(r"[a-z]:\\", t):
        return None  # he gave a path; don't turn it into keywords
    m = re.search(r"\b(?:called|named)\s+[\"']?([^\"'?!]+)", t) or re.search(r"\bmy\s+([^?!]+)", t)
    if not m:
        return None
    words = [w for w in re.findall(r"[a-z0-9][a-z0-9._-]*", m.group(1)) if w not in _FILE_QUERY_STOP]
    return " ".join(words[:4]) or None


# list_skills ran, and the reply was "I've got a few skills that might
# come in handy" -- four times running on 2026-10-28, 3/3 in the eval.
# Same shape as the task-list restore: fetching the answer and not saying
# it is the failure.
def _skill_names(result: str) -> list:
    return re.findall(r"^([a-z][a-z0-9_]*):", result or "", re.MULTILINE)


# Words in skill names that also turn up in vague replies. The first version
# counted "take" (from take_notes), so "I can take care of a few things"
# passed as naming a skill and the list was never restored (2/3 clean runs,
# 2026-10-29).
_GENERIC_SKILL_WORDS = {"elementary", "take", "daily", "files", "help", "quick"}


def _names_a_skill(reply: str, result: str) -> bool:
    low = (reply or "").lower()
    for name in _skill_names(result):
        words = [w for w in name.split("_") if len(w) >= 4 and w not in _GENERIC_SKILL_WORDS]
        if any(w in low for w in words):
            return True
    return not _skill_names(result)


def _skills_reply(result: str) -> str:
    lines = []
    for name, desc in re.findall(r"^([a-z][a-z0-9_]*):\s*(.+)$", result or "", re.MULTILINE):
        desc = re.sub(r"^how to\s+", "", desc.strip(), flags=re.IGNORECASE)
        lines.append(f"- {name.replace('_', ' ').capitalize()}: {desc}")
    return "Here's what I can help with:\n" + "\n".join(lines) if lines else result


# ---- 2026-10-29 (afternoon): "actually do the task" ----------------------
# Chuen, after the morning's fixes: "i dont want her to reply me to satisfy
# me i want her to actually do task". Every case below sounded helpful and
# did nothing (eval/cases.py, the block of the same date; 3/3 failures).

# A complaint means "redo what I asked", not "call a tool". Live, "thats not
# what im asking" and "what?" were answered with the time, five times.
_COMPLAINT = re.compile(
    r"^\s*(?:no[,.!]?\s+)?(?:"
    r"that'?s\s+not\s+(?:it|right|what\s+i\s*(?:'?m|am)?\s*(?:asking|asked|meant|want(?:ed)?))"
    r"|(?:still\s+)?not\s+what\s+i\s*(?:'?m|am)?\s*(?:asking|asked|meant|want(?:ed)?)"
    r"|still\s+not\s+(?:it|right)|that'?s\s+wrong|wrong"
    r"|what|huh|is\s+there\s+a\s+problem"
    r"|you'?re\s+not\s+listening"
    r"|you\s+(?:didn'?t|did\s+not)\s+(?:answer|do)\s+(?:it|that|me|my\s+question|what\s+i\s+asked)"
    r")[.!?\s]*$",
    re.IGNORECASE,
)
_ACK = re.compile(
    r"^\s*(?:ok(?:ay)?|k|thanks?|thank\s+you|cool|nice|good|great|perfect|alright|sure"
    r"|yes|yeah|yep|no|nope|that'?s\s+(?:good|great|fine|nice|it)"
    r"|(?:ok(?:ay)?\s+)?(?:that'?s?\s+)?(?:good|great|fine))[.!\s]*$",
    re.IGNORECASE,
)


def _call_key(name: str, args) -> str:
    return json.dumps([name, args or {}], sort_keys=True, default=str)


# A lookup that ran but found nothing. These are the real tools' own words
# (tools.recall, vault_search) and none of them is an "error", so until
# 2026-10-30 _confirms_nothing missed them: live, a retry repeated
# recall('exams') 3/3 against the real wording while the fixtures, which
# said "Error: ...", passed.
_FOUND_NOTHING = re.compile(
    r"^(?:i don'?t have anything remembered|nothing in your notes mentions|nothing to search for"
    r"|i can'?t reach the vault|vault lookups are disabled)", re.IGNORECASE)


def _no_evidence(result: str) -> bool:
    return _confirms_nothing(result) or bool(_FOUND_NOTHING.search((result or "").strip()))


# Where else a lookup that found nothing can be checked. Read-only, and
# only tools the original request was offered. One step, not a planner.
_RECOVERY_SOURCES = {"recall": ("search_vault",), "list_known_facts": ("search_vault",),
                     "search_vault": ("recall",)}
_LOOKUP_FILLER = {"save", "saved", "about", "anything", "have", "got", "tell", "know", "note", "notes",
                  "find", "look", "check", "remembered", "thing", "things", "stuff", "info", "information",
                  "anywhere", "somewhere", "everything"}
_SAYS_NOTHING = re.compile(r"\b(?:don'?t|do not|didn'?t|can'?t|couldn'?t) (?:have|find|see)\b"
                           r"|\bnothing (?:saved|about|on)\b|\bno (?:notes|information)\b", re.IGNORECASE)


def _lookup_terms(text: str) -> str:
    """The subject of a lookup, from his own words -- never invented."""
    import instincts
    keep = instincts.keywords(text) - _LOOKUP_FILLER
    return " ".join(dict.fromkeys(w for w in re.findall(r"[a-z][a-z']+", (text or "").lower()) if w in keep))


def _permitted_reads() -> set:
    """Read-only tools that are defined and switched on."""
    rules = brain_rules.load()
    return {d["function"]["name"] for d in TOOL_DEFINITIONS
            if d["function"]["name"] not in brain_rules.MUTATING_TOOLS
            and (d["function"]["name"] != "search_vault" or rules.vault_search)}


def _blocked_reply(terms: str, checked: list) -> str:
    """Nothing found anywhere: say where she looked, then one question."""
    parts = []
    for name, res in checked:
        if name == "search_vault":
            parts.append("I couldn't reach your notes" if re.search(r"can'?t reach|disabled", res, re.I)
                         else f"no note mentions {terms}")
        elif name in ("recall", "list_known_facts"):
            parts.append(f"nothing is saved under {terms}" if name == "recall" else "none of your saved facts covers it")
    where = "; ".join(dict.fromkeys(parts)) or "I had nowhere else to look"
    return (f"I checked again: {where}. I haven't found it, and I won't guess. "
            f"What did you file it under -- a note title or a word in it?")


def _corrected_attempt(prev: str, messages: list, prev_i: int) -> list:
    """The calls the complained-about attempt made, with their results:
    from `messages` when they carry tool traffic (tests, the CLI), else
    from the stored history (the tray -- replay() is prose only)."""
    calls, results = [], []
    for m in messages[prev_i + 1:-1]:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                f = c.get("function") or {}
                if f.get("name"):
                    calls.append({"name": f["name"], "arguments": f.get("arguments") or {}})
        elif m.get("role") == "tool":
            results.append(m.get("content") or "")
    if calls:
        for call, res in zip(calls, results):
            call["result"] = res
        return calls
    try:
        import conversation
        return conversation.last_attempt(prev)
    except Exception:
        return []


def _last_real_request(messages: list) -> int | None:
    """Index of the last thing he actually asked for, skipping the current
    message and any complaints, acknowledgements or stop requests."""
    for i in range(len(messages) - 2, -1, -1):
        m = messages[i]
        if m.get("role") != "user":
            continue
        text = (m.get("content") or "").strip()
        if (not text or _COMPLAINT.fullmatch(text) or _ACK.fullmatch(text)
                or tool_router.feedback_only(text) or speech_control.is_stop_request(text)):
            continue
        return i
    return None


# The clock is always offered (tool_router.CORE), and live it answered
# "thats good", "what?", "is there a problem?" and "capital of japan".
# Same treatment as calculate: it runs when he said something about time.
_TIME_REQUEST = re.compile(
    r"\b(?:time|times|date|day|days|today|tonight|tomorrow|yesterday|clock|o'?clock"
    r"|hours?|minutes?|week|weekend|month|year|when|timezone|morning|afternoon|evening"
    r"|night|monday|tuesday|wednesday|thursday|friday|saturday|sunday|now|late|early)\b"
    r"|\d\s*(?:am|pm)\b",
    re.IGNORECASE,
)

# Weather that no weather tool returned. Live, asked for the briefing:
# "Temperature: 68°F ... Humidity: 60% ... Conditions: Partly Cloudy ...
# Forecast: High of 72°F" -- get_weather was never called. Only replies
# carrying weather *data* words trigger this, so "water boils at 212
# degrees fahrenheit" or "I like sunny days" are left alone.
_WEATHER_DATA = re.compile(
    r"humidity|forecast|chance\s+of\s+rain|\bhigh\s+of\b|\blow\s+of\b|conditions\s*:"
    r"|temperature\s*:|wind\s+speed|uv\s+index", re.IGNORECASE)
_WEATHER_LINE = re.compile(
    r"humidity|forecast|chance\s+of\s+rain|\bhigh\s+of\b|\blow\s+of\b|conditions\s*:"
    r"|temperature|wind|cloudy|overcast|sunny|showers?|drizzle|°|\bdegrees?\b", re.IGNORECASE)


# The current date or time, said with no clock run. Live (2026-10-30 probe
# of the 28 Oct "alright tell me"): "It's currently December 2023." -- her
# training cutoff, stated as today. A bare number only counts with a clock
# shape (18:48, 6 pm), so "it's 14°C" and "it is 391" are left alone. A day
# or month only counts as "now" when she says so -- "It's Tuesday 24
# November" can be a deadline read from his notes.
_CLOCK_SHAPE = r"(?:\d{1,2}:\d{2}|\d{1,2}\s*[ap]\.?m\b)"
_CALENDAR = (r"(?:(?:mon|tues|wednes|thurs|fri|satur|sun)day|january|february|march|april"
             r"|june|july|august|september|october|november|december)")
_CLOCK_CLAIM = re.compile(
    rf"\b(?:it'?s|it\s+is)\s+(?:(?:currently|now|right\s+now)\s+)?{_CLOCK_SHAPE}"
    rf"|\b(?:it'?s|it\s+is)\s+(?:currently|now|right\s+now)\s+{_CALENDAR}"
    rf"|\b(?:today\s+is|the\s+(?:current\s+)?(?:time|date)\s+is)\s+(?:{_CLOCK_SHAPE}|{_CALENDAR})",
    re.IGNORECASE)


def _correct_unfetched_clock(text: str, now: datetime.datetime | None = None) -> str:
    if not _CLOCK_CLAIM.search(text or ""):
        return text
    now = now or datetime.datetime.now()
    real = f"It's {now:%A} {now.day} {now:%B}, {now.hour % 12 or 12}:{now:%M} {now:%p}."
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept = [s for s in sentences if not _CLOCK_CLAIM.search(s)]
    return " ".join([real] + kept)


def _strip_unfetched_weather(text: str) -> str:
    if not _WEATHER_DATA.search(text or ""):
        return text
    kept_lines = []
    for line in text.splitlines():
        sentences = re.split(r"(?<=[.!?])\s+", line)
        kept = [s for s in sentences if not _WEATHER_LINE.search(s)]
        if kept or not line.strip():
            kept_lines.append(" ".join(kept))
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept_lines)).strip()
    return out or "I haven't checked the weather -- ask me and I'll look it up."


_BRIEFING = re.compile(
    r"\b(?:briefing|brief\s+me|check-?in)\b"
    r"|\bwhat'?s\s+(?:up|on)\s+(?:for\s+)?today\b|\bwhat\s+do\s+i\s+have\s+(?:on\s+)?today\b",
    re.IGNORECASE)
_PLAN_STUDY = re.compile(
    r"\b(?:plan|schedule|organi[sz]e|timetable)\b[^.?!]{0,40}?\b(?:stud(?:y|ies|ying)|revision"
    r"|revis(?:e|ing)|modules?|coursework|assignments?|semester|term|deadlines?)\b"
    r"|\b(?:study|revision)\s+(?:plan|schedule|timetable)\b",
    re.IGNORECASE)
# A study brief for one module (study_brief.py). "prep me for signals" went
# to the Claude hand-off and "brief me on electromagnetism" to the daily
# briefing; a brief for a named module is local work from his own notes.
# Without a module she asks which one -- study_brief.run does that.
_NAMES_MODULE = (r"(?=[^.?!]*\b(?:signals?|electromag\w*|em|cm|cab|s&s|methods|biomath\w*"
                 r"|5ccyb\d{3})\b)")
# invest.py, 2026-10-30: read-only, no model call. Watching only -- there is
# no trading tool, and adding/removing instruments is the CLI's job for now.
_WATCHLIST_BRIEF = re.compile(
    r"^\s*(?:(?:give|run|show)\s+(?:me\s+)?(?:my\s+|the\s+)?|brief\s+me\s+on\s+my\s+)"
    r"(?:watchlist|market)(?:\s+brief(?:ing)?)?\s*[.!?]*\s*$"
    r"|^\s*(?:my\s+)?(?:watchlist|market)\s+brief(?:ing)?\s*[.!?]*\s*$", re.I)
_WATCHLIST_SHOW = re.compile(
    r"^\s*(?:what'?s\s+on\s+|list\s+|show\s+)?my\s+watchlist\s*[.!?]*\s*$", re.I)

_STUDY_BRIEF = re.compile(
    r"\b(?:study|revision|coursework)\s+brief\b"
    rf"|\b(?:prep|prepare|get\s+me\s+ready)\s+(?:me\s+)?(?:for|on)\b{_NAMES_MODULE}"
    rf"|\bhelp\s+me\s+(?:study|revise|prepare)\b{_NAMES_MODULE}"
    rf"|\brevise\s+for\b{_NAMES_MODULE}"
    rf"|\bbrief\s+(?:me\s+)?(?:for|on)\b{_NAMES_MODULE}",
    re.IGNORECASE)


# "Go on, do that one" -- the job is named by the turn before. Live on 28-29
# Oct, after she offered the briefing, "alright tell me" got her skills
# list; after "plan my study modules", "i want you to plan it" got "use the
# list_skills tool". Re-probed 2026-10-30: the first invented "It's
# currently December 2023", the second asked which subjects he studies.
_FOLLOW_ON = re.compile(
    r"^\s*(?:(?:alright|ok(?:ay)?|yes|yeah|sure|so|then|well)[,\s]+)*"
    r"(?:tell\s+me|go\s+on|go\s+ahead|do\s+it|do\s+that|show\s+me|give\s+it\s+to\s+me"
    r"|(?:i\s+want\s+you\s+to\s+|can\s+you\s+|could\s+you\s+|just\s+)?(?:do|plan|make)\s+(?:it|that|one)"
    r"(?:\s+for\s+me)?)"
    r"(?:[,\s]+(?:please|then|now|already))*[.!?\s]*$",
    re.IGNORECASE)
_PLAN_YES = re.compile(
    r"^\s*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|go\s+ahead|do\s+it|add\s+(?:them|it|those|all"
    r"(?:\s+of\s+them)?)|please\s+do)(?:[\s,]+(?:please|do\s+it|add\s+them|thanks?|go\s+ahead))*[.!\s]*$",
    re.IGNORECASE)

# His internships, projects and trackers live in the vault. Live, "hows my
# internship tracker" was told there was no information, "the projects im
# doing" got one stale saved fact, and "plan my internship applications"
# went skill-hunting.
_SKILL_QUESTION = re.compile(
    r"\bskills?\b|what\s+can\s+you\s+do|what\s+are\s+you\s+(?:good|able)\b|capabilit|abilit",
    re.IGNORECASE)


# Greetings and acknowledgements have one right kind of answer, and the model
# kept finding wrong ones: "hey" -> "No response needed." 4/6 traced, "I
# didn't catch that." 3/3 in the eval; "thats good" -> "I didn't catch
# that" 3/3, or the clock. This is the 2026-10-24 conclusion applied at last:
# the low-content turn is answered before the model, not argued with.
_GREETING = re.compile(
    r"^\s*(?:hey|hi|hello|hiya|yo|morning|evening|good\s+(?:morning|afternoon|evening))"
    r"(?:\s+(?:denisse|there|chuen))?[.!\s]*$", re.IGNORECASE)
_THANKS = re.compile(r"^\s*(?:thanks?|thank\s+you|thx|ty|cheers)(?:\s+(?:denisse|a\s+lot|so\s+much))?[.!\s]*$",
                     re.IGNORECASE)
_PLAIN_ACK = re.compile(
    r"^\s*(?:ok(?:ay)?|alright|cool|nice|great|perfect|good|sounds\s+good"
    r"|(?:ok(?:ay)?\s+)?that'?s?\s+(?:good|great|fine|nice|perfect))[.!\s]*$", re.IGNORECASE)


def _quick_reply(text: str, last_said: str, now: datetime.datetime | None = None) -> str | None:
    """The reply to a greeting or an acknowledgement, or None if it isn't one.
    An "ok" that answers one of her own questions is a real answer -- that
    goes to the model, so "want me to open it?" -> "ok" still opens it."""
    t = (text or "").strip()
    if _GREETING.fullmatch(t):
        hour = (now or datetime.datetime.now()).hour
        part = "Morning" if hour < 12 else "Afternoon" if hour < 18 else "Evening"
        if re.search(r"good\s+(?:morning|afternoon|evening)|^\s*(?:morning|evening)", t, re.IGNORECASE):
            return f"{part}, Chuen."
        return "Hey Chuen."
    if (last_said or "").rstrip().endswith("?"):
        return None
    if _THANKS.fullmatch(t):
        return "Any time."
    if _PLAIN_ACK.fullmatch(t):
        return "Good."
    return None


# The jobs Denisse hands to Claude (delegate.py). From ~180 of his own
# non-Denisse prompts to Claude Code and Codex: CV and cover-letter writing,
# internship research, pitch prep, people lookups and coursework solutions.
# An 8B model answers these plausibly and badly. Kept explicit, so the line
# is testable: finding his CV is local, improving it is Claude's.
_DELEGATE = re.compile(
    r"\bcover\s+letter\b|\blinked?\s*in\s+post\b|\bpersonal\s+statement\b"
    r"|\b(?:change|edit|improve|update|tailor|tweak|review|rewrite|polish|fix)\b[^.?!]{0,40}"
    r"\b(?:cv|resume|r[eé]sum[eé])\b"
    r"|\b(?:cv|resume)\b[^.?!]{0,30}\b(?:change|edit|improve|tailor|tweak|review|rewrite|feedback)\b"
    r"|\bwhat\s+should\s+i\s+(?:add|change|put)\b"
    r"|\bsearch\s+(?:\w+\s+){0,2}(?:up|online)\b|\bsearch\s+the\s+web\b|\blook\s+(?:\w+\s+){0,2}up\b"
    r"|\bresearch\b"
    r"|\bfind\s+(?:me\s+)?(?:\d+\s+|some\s+|more\s+)?(?:internships?|jobs?|roles?|companies|places)\b"
    r"|\binternships?\b[^.?!]{0,40}\b(?:apply|due|deadline|open)\b|\bplaces\s+(?:each\s+)?to\s+apply\b"
    r"|\bpitch\b|\bprep(?:are)?\s+me\b"
    r"|\b(?:solve|prove)\b|\bworked\s+solutions?\b|\bsolutions?\s+(?:to|for)\b"
    r"|\bhow\s+(?:do|should|would)\s+i\s+(?:do|approach|answer|tackle)\b"
    r"|\bhow\s+to\s+(?:do|approach|answer|tackle)\b"
    r"|\b(?:ask|use|get)\s+(?:claude|codex)\b|\bhand\s+(?:it|this|that)\s+(?:to|over)\b"
    r"|\b(?:draft|write)\s+(?:me\s+)?(?:a|an|the)\s+(?:email|message|post|letter|reply|essay|summary|report)\b",
    re.IGNORECASE)
# ...but never these, which are local and checkable.
_KEEP_LOCAL = re.compile(
    r"\bremind\b|\breminder\b|\btask\b|\bto-?do\b|\btimer\b|\b(?:find|open|pull\s+up|bring\s+up"
    r"|show\s+me|where(?:'s|\s+is))\s+(?:me\s+)?(?:my|the)\b", re.IGNORECASE)


def _should_delegate(text: str) -> bool:
    return bool(_DELEGATE.search(text or "")) and not _KEEP_LOCAL.search(text or "")


def _is_known_skill(name: str) -> bool:
    """read_skill('plan_study_modules') was the opening move of three of the
    week's dead-end replies. A skill that doesn't exist gets a refusal that
    says to do the task instead. If the list can't be read, don't block."""
    try:
        listing = str(tools.TOOL_FUNCTIONS["list_skills"]())
    except Exception:
        return True
    return name in _skill_names(listing)


def _vault_query(text: str) -> str | None:
    t = (text or "").lower()
    if re.search(r"\binternships?\b|\btracker\b", t):
        return "internships"
    if re.search(r"\bprojects?\b", t) and re.search(r"\b(?:my|i'?m|i\s+am|im|doing|working)\b", t):
        return "current projects"
    return None


def _known_document_path(path: str, messages: list, outputs: dict) -> bool:
    """Only open a document path supplied by Chuen or observed in a lookup.

    A model guessing 'latest_cv.pdf' must search, not use the app launcher
    to pretend the document was found. No filesystem scan is done here.
    """
    if not isinstance(path, str) or not ntpath.isabs(path):
        return False
    wanted = ntpath.normcase(ntpath.normpath(path.strip().strip('"')))
    sources = [m.get("content", "") for m in messages if m.get("role") == "user"]
    found = outputs.get("find_files", "")
    if found.startswith("Found "):
        sources.append(found)
    pattern = re.compile(re.escape(wanted) + r"(?=$|[\s\"'\)\],;]|\.(?:\s|$))")
    return any(pattern.search(source.replace("/", "\\").lower()) for source in sources if source)


_OPEN_DOCUMENT_REQUEST = re.compile(
    r"^\s*(?:(?:could|can|would|will)\s+you\s+|please\s+|i (?:want|need) you to\s+)?"
    r"(?:open|pull up|bring up|show(?!\s+(?:me\s+)?(?:how|why|whether)\b)(?:\s+me)?)\b"
    r"(?!\s+(?:how|why|whether)\b)",
    re.IGNORECASE,
)


def _single_document_path(result: str) -> str:
    if not re.match(r"^Found\s+1\s+match", result):
        return ""
    match = re.search(r"(?:[A-Za-z]:[\\/]|\\\\)[^\r\n]+", result)
    return match.group(0).strip().strip('"') if match else ""


def _get(obj, key, default=None):
    """Works whether ollama hands back a dict or an attribute-style object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _tool_call_to_dict(call) -> dict:
    """ollama hands back ToolCall objects, not plain dicts -- flatten one to
    a plain dict so it's JSON-serializable for memory_store and safe to
    replay back into ollama.chat()."""
    func_info = _get(call, "function")
    return {
        "function": {
            "name": _get(func_info, "name"),
            "arguments": dict(_get(func_info, "arguments") or {}),
        }
    }


def _sniff_fake_tool_call(content: str) -> dict | None:
    """The small local model occasionally writes a tool call as plain JSON
    text (e.g. '{"name": "get_weather", "parameters": {"location": "..."}}')
    instead of using Ollama's structured tool_calls field -- seen with both
    a real tool name and a hallucinated one, and seen both as the *entire*
    reply and (found live, with open_application) tacked onto the end of
    an otherwise normal sentence ("Let me try to open Spotify for you.
    {"name": "open_application", ...}"). Detect either shape so it can be
    recovered as a real call instead of showing broken JSON to the user --
    finds the first '{' rather than requiring the whole message to be
    JSON, so a real prose prefix doesn't hide it from this check."""
    text = (content or "").strip()
    start = text.find("{")
    if start == -1:
        return None
    candidate = text[start:].strip()
    if not candidate.endswith("}"):
        return None
    try:
        data = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    args = data.get("parameters", data.get("arguments", {}))
    if not isinstance(args, dict):
        return None
    return {"name": data["name"], "arguments": args}


def _drop_unexpected_args(func, args: dict) -> dict:
    """Keep only the keyword arguments `func` actually accepts.

    An 8B model passes junk arguments regularly -- observed 2026-09-23, it
    called list_tasks(task='the N20 motor') when list_tasks takes none. That
    raised a TypeError, and rather than reporting the error the model
    *invented* a plausible-looking task list, including an item that has
    never existed. A fabricated answer is far worse than a dropped argument,
    so extras are discarded and the call goes ahead.

    Functions that take **kwargs are left alone.
    """
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return args
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return args
    allowed = {
        n for n, p in params.items()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                      inspect.Parameter.KEYWORD_ONLY)
    }
    return {k: v for k, v in args.items() if k in allowed}


def _required_params(func) -> list:
    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return []
    return [n for n, p in params.items()
            if p.default is inspect.Parameter.empty
            and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           inspect.Parameter.KEYWORD_ONLY)]


def _rescue_arg_names(func, args: dict) -> dict:
    """Put a value back when the model got the argument's NAME wrong.

    _drop_unexpected_args exists because junk arguments used to raise a
    TypeError and the model covered the error by inventing a result. But
    dropping is the wrong answer when the name was wrong rather than the
    argument spurious: on 2026-10-26 the model called
    find_files(filename='passport'), the value was discarded, the call
    died with "missing 1 required positional argument: 'query'", and
    Denisse told Chuen the file wasn't on his machine. It was. She had
    simply never searched for it -- twice, because read_file then got the
    same treatment.

    Only the unambiguous case is repaired: exactly one required parameter
    left unfilled and exactly one scalar value dropped. Anything less
    clear-cut is left alone for _run_tool to report honestly.
    """
    kept = _drop_unexpected_args(func, args)
    if kept == args:
        return kept
    dropped = [v for k, v in args.items() if k not in kept]
    missing = [p for p in _required_params(func) if p not in kept]
    if len(missing) == 1 and len(dropped) == 1 and isinstance(
            dropped[0], (str, int, float)):
        kept = dict(kept)
        kept[missing[0]] = dropped[0]
    return kept


# Tools that change something and are almost never wanted twice in one
# turn. Completing a task is the case that bit: told "I've ordered the N20
# motor, tick it off", the model closed that task and then closed an
# unrelated one as well, three runs out of three (2026-09-23). Marking work
# done that Chuen never mentioned is quiet, plausible-looking data loss --
# he'd only find out when the thing he thought was tracked didn't happen.
_ONCE_PER_TURN = {"complete_task", "remove_task"}


def _run_tool(name: str, args: dict, confirm, used: set | None = None) -> str:
    func = tools.TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"Error: unknown tool '{name}'"
    if used is not None and name in _ONCE_PER_TURN:
        if name in used:
            return (f"Refused: '{name}' has already run once this turn. "
                    "Only act on the one thing Chuen actually named. If he "
                    "meant something else too, ask him.")
        used.add(name)
    if confirm is not None and not confirm(name, args):
        return f"User declined to run '{name}'."
    call_args = _rescue_arg_names(func, args)
    missing = [p for p in _required_params(func) if p not in call_args]
    if missing:
        # Say what to do about it. The raw TypeError told the model the
        # call had failed but not how to fix it, so it gave up and
        # reported the thing as not found instead of calling again.
        return (f"Error running '{name}': no value was given for the "
                f"required argument '{missing[0]}'. Call '{name}' again "
                f"with {missing[0]}=<value>. Do not invent a result.")
    try:
        return func(**call_args)
    except Exception as e:
        # Be explicit that this failed and that nothing may be invented to
        # cover it -- the model's instinct on a tool error is to make
        # something up that looks like a plausible result.
        return (f"Error running '{name}': {e}. Tell the user this failed. "
                "Do not invent a result.")


class Brain:
    def __init__(self):
        try:
            local_models = [m.get("model", m.get("name", "")) for m in ollama.list().get("models", [])]
        except Exception as e:
            raise RuntimeError(
                f"Can't reach the local Ollama server: {e}. "
                "Make sure Ollama is installed and running (it should start "
                "automatically -- check for its icon in the system tray)."
            )
        if not any(MODEL in m for m in local_models):
            raise RuntimeError(
                f"Model '{MODEL}' isn't downloaded yet. Run: ollama pull {MODEL}"
            )

    def _ask_which(self, request, terms, reads, wrong_reply, confirm, messages):
        """The question to ask after a rejected read, or None to fall back
        to the retry. Only when the rejected answer visibly came from the
        evidence, or his words pick out several items in it."""
        import clarify
        cands, by_terms = clarify.candidates(reads, terms)
        gone = clarify.rejected(cands, wrong_reply)
        if not gone and not (by_terms and len(cands) >= 2):
            return None
        others = [c for c in cands if c not in gone]
        if not others:
            # Nothing else in what was read: one other permitted source for
            # the same subject, as in the empty-lookup repair -- not more.
            tried, offered = {c["name"] for c in reads}, _permitted_reads()
            for alt in dict.fromkeys(a for c in reads for a in _RECOVERY_SOURCES.get(c["name"], ())):
                if alt in tried or alt not in offered or not terms:
                    continue
                args = {"query": terms} if alt == "search_vault" else {"key": terms}
                res = str(_run_tool(alt, args, confirm, set()))
                messages += [{"role": "assistant", "content": "",
                              "tool_calls": [{"function": {"name": alt, "arguments": args}}]},
                             {"role": "tool", "content": res}]
                if not _no_evidence(res):
                    seen = {g["text"] for g in gone}
                    others = [c for c in clarify.candidates([{"name": alt, "arguments": args, "result": res}],
                                                            terms)[0] if c["text"] not in seen]
                    cands = cands + others
                break
        if not others:
            return clarify.missing(terms, gone or cands)
        asked = clarify.question(others)
        clarify.offer(request, asked, others, cands, terms)
        return asked

    def _move_new_task(self, text, last_said):
        """A bare deadline ("before 2nd of november") right after she added
        a task sets that task's deadline. The reply, or None if it isn't one."""
        if not _DEADLINE_ONLY.fullmatch((text or "").strip()):
            return None
        import tasks
        if tasks.parse_due(text) is None:
            return None
        recent = [t for t in tasks.list_tasks() if not t.get("done_at")]
        if not recent:
            return None
        newest = max(recent, key=lambda t: t.get("created") or "")
        try:
            age = datetime.datetime.now() - datetime.datetime.fromisoformat(newest["created"])
        except (KeyError, TypeError, ValueError):
            return None
        if age > datetime.timedelta(minutes=15) or newest["text"].lower()[:30] not in (last_said or "").lower():
            return None
        updated = tasks.update_task(newest["id"], due=text.strip())
        return f"Moved: {tasks.describe(updated)}." if updated else None

    def _settle_which(self, p, text):
        """His answer to "which one?": the answer from the item he picked,
        another question, or None when it's a new request."""
        import clarify
        kind, got = clarify.pick(text, p, complaint=bool(_COMPLAINT.fullmatch((text or "").strip())))
        if kind == "unrelated":
            return None
        if kind == "none":
            return clarify.missing(p["terms"], p["seen"])
        if kind == "pick":
            evidence = clarify.evidence(got)
            reply = ""
            try:  # one call, with only the item he picked: it can't repeat the rejected one
                r = ollama.chat(model=MODEL, keep_alive=-1, options={"temperature": 0.2, "num_ctx": NUM_CTX},
                                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                                          {"role": "user", "content": p["request"]},
                                          {"role": "system", "content": f"He means {clarify.spoken(got)}. From his "
                                           f"notes: {evidence}\nAnswer his question from this alone, in one or "
                                           "two sentences. Don't add anything it doesn't say."}])
                reply = _strip_role_prefix(_get(_get(r, "message"), "content", "") or "").strip()
            except Exception as exc:
                logging.warning("Clarified answer fell back to the evidence: %s", exc)
            return reply if clarify.supported(reply, got, p["seen"]) else clarify.quote(got)
        options = got if kind == "ambiguous" else p["options"]
        asked = ("That isn't one I found. " if kind == "unsupported" else "") + clarify.question(options)
        clarify.offer(p["request"], asked, options, p["seen"], p["terms"])
        return asked

    def respond(self, messages: list, confirm=None, extra_context: str = "") -> tuple[str, list]:
        """
        Run one turn of the tool-use loop against `messages` (role/content
        dicts, not including the system prompt). Returns
        (final_text, updated_messages).

        `confirm`, if given, is called as confirm(tool_name, args) -> bool
        before each tool call and can veto it (see safety.py). With no
        confirm callback, every tool call runs unchecked.

        `extra_context`, if given, is appended to the system prompt for
        *this call only* -- it is never added to `messages`, so it's never
        persisted by memory_store.save_history(). Used by wake_word.py's
        short rolling buffer of recently-heard speech (deliberately
        memory-only, never written to disk -- see its own module docs).
        """
        system_content = SYSTEM_PROMPT
        if extra_context:
            system_content += "\n\n" + extra_context
        # Disabled (2026-09-02): injecting the shared Obsidian Memory/
        # folder here measurably broke reply quality -- tested directly
        # against a real question (news summarization): 0/4 clean even
        # trimmed to 600 chars (one run hallucinated a fake save action),
        # 4/4 clean with this off. obsidian_sync.read_claude_memory()
        # itself is untouched -- re-enable by uncommenting below if the
        # underlying content quality improves.
        # claude_context = obsidian_sync.read_claude_memory()
        # if claude_context:
        #     system_content += (
        #         "\n\nAdditional context Claude Code/Claude Desktop have saved "
        #         "to the shared Obsidian memory vault:\n\n" + claude_context
        #     )
        full_messages = ([{"role": "system", "content": system_content}]
                         + _style_examples() + messages)
        latest_user = next((m.get("content", "") for m in reversed(messages)
                            if m.get("role") == "user"), "")
        if speech_control.is_stop_request(latest_user):
            speech_control.stop()
            # Silence is the requested behaviour. Do not ask a model to
            # manufacture a spoken 'I'll stop talking' acknowledgement.
            messages.append({"role": "assistant", "content": ""})
            return "", messages
        if _NEWS_OFF.search(latest_user):
            # Act on it rather than describe it -- see _NEWS_OFF.
            import news
            news.set_briefing_enabled(False)
            answer = ("Okay, no more daily news -- I've switched the briefing "
                      "off. Ask me whenever you do want the headlines.")
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if _SELF_MODEL.search(latest_user):
            answer = _model_identity()
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        last_said = next((m.get("content") or "" for m in reversed(messages[:-1])
                          if m.get("role") == "assistant" and (m.get("content") or "").strip()), "")
        if (last_said.rstrip().endswith(_PATH_ASK) and getattr(self, "_last_paths", None)
                and _PATH_YES.fullmatch(latest_user.strip())):
            answer = "\n".join(self._last_paths[:5])
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        moved = None if getattr(self, "_retrying", False) else self._move_new_task(latest_user, last_said)
        if moved:
            messages.append({"role": "assistant", "content": moved})
            return moved, messages
        if _style_note(latest_user) and not getattr(self, "_retrying", False):
            # His words: "just say 'alright noted'". See _STYLE_NOTE.
            _write_style_note(latest_user)
            answer = "Alright, noted."
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        # An open "which one do you mean?" (clarify.py) is settled first, so
        # no other path -- a quick "yes", a planner confirmation -- answers
        # it, and it answers nothing else. Only her last reply can be it.
        import clarify
        open_q = None if getattr(self, "_retrying", False) else clarify.take(next(
            (m.get("content") or "" for m in reversed(messages[:-1]) if m.get("role") == "assistant"), ""))
        if open_q:
            answer = self._settle_which(open_q, latest_user)
            if answer is not None:
                messages.append({"role": "assistant", "content": answer, "clarify": clarify.is_open(answer)})
                return answer, messages
        if _COMPLAINT.fullmatch(latest_user.strip()) and not getattr(self, "_retrying", False):
            # Redo what he actually asked. The wrong answer and the complaint
            # are left out of the retry, so she can't imitate the miss.
            prev_i = _last_real_request(messages)
            if prev_i is not None:
                prev = (messages[prev_i].get("content") or "").strip()
                wrong_reply = next((m.get("content") or "" for m in reversed(messages[prev_i + 1:-1])
                                    if m.get("role") == "assistant"), "")
                shown = prev if len(prev) <= 80 else prev[:77].rstrip() + "..."
                attempt = _corrected_attempt(prev, messages, prev_i)
                # What kind of miss it was decides the repair (2026-10-30;
                # live baseline: 3/9 recovered, 0/3 blocked said why, and
                # 0/3 kept from adding the same task twice).
                done = [c for c in attempt if c["name"] in brain_rules.MUTATING_TOOLS
                        and not _confirms_nothing(c.get("result", ""))]
                if done:
                    # An action that already happened. The complaint may be
                    # about the wording or the meaning; redoing it is never
                    # the fix. Say what was done and ask -- no model call.
                    what = "; ".join(str(c.get("result", ""))[:120] for c in done)
                    answer = (f'You asked: "{shown}". That part is already done ({what}), and I haven\'t '
                              "done it again. What did you want instead?")
                    messages.append({"role": "assistant", "content": answer})
                    return answer, messages
                seeded, checked = [], []
                terms = _lookup_terms(prev)
                # The read found something and he rejected the answer: if the
                # evidence holds other plausible answers, ask which one
                # instead of answering again (0/5 live before; clarify.py).
                reads = [c for c in attempt if c["name"] not in brain_rules.MUTATING_TOOLS
                         and not _no_evidence(c.get("result", ""))]
                asked = self._ask_which(prev, terms, reads, wrong_reply, confirm, messages) \
                    if reads and wrong_reply else None
                if asked:
                    # Marked so replay() keeps this turn: his answer refers to it.
                    messages.append({"role": "assistant", "content": asked, "clarify": asked.endswith("?")})
                    return asked, messages

                def irrelevant(c):
                    # A lookup that returned something, none of it about his
                    # subject (list_known_facts -> his coffee order, asked
                    # about his gym), found nothing either.
                    res = str(c.get("result", ""))
                    # Substring, not tokens: "Note 'Sponsor meeting':" must
                    # count as mentioning "meeting" (a token match missed it
                    # and the reply then said no note mentioned it).
                    return _no_evidence(res) or (c["name"] in _RECOVERY_SOURCES and terms != "" and
                                                 not any(t in res.lower() for t in terms.split()))
                if attempt and all(irrelevant(c) for c in attempt) \
                        and any(c["name"] in _RECOVERY_SOURCES for c in attempt):
                    # A lookup that found nothing: check one other permitted
                    # read-only source for the same subject before the model
                    # tries again. Another spelling of the same call is not
                    # a new source, so tools already tried are skipped.
                    # Permitted = defined, read-only and switched on. Not "what
                    # the router offered": the router trims for the model's
                    # attention, and live it hid search_vault from "do i have
                    # anything saved about the dentist".
                    offered = _permitted_reads()
                    tried = {c["name"] for c in attempt}
                    checked = [(c["name"], str(c.get("result", ""))) for c in attempt]
                    for alt in dict.fromkeys(a for c in attempt for a in _RECOVERY_SOURCES.get(c["name"], ())):
                        if alt in tried or alt not in offered or not terms:
                            continue
                        args = {"query": terms} if alt == "search_vault" else {"key": terms}
                        res = str(_run_tool(alt, args, confirm, set()))
                        seeded += [{"role": "assistant", "content": "",
                                    "tool_calls": [{"function": {"name": alt, "arguments": args}}]},
                                   {"role": "tool", "content": res}]
                        checked.append((alt, res))
                        if not _no_evidence(res):
                            break
                    if not seeded or _no_evidence(seeded[-1]["content"]):
                        messages.extend(seeded)
                        answer = f'You asked: "{shown}". ' + _blocked_reply(terms or "that", checked)
                        messages.append({"role": "assistant", "content": answer})
                        return answer, messages
                # 2026-10-30: the retry got the same input as the miss, so it
                # could make the very call that had just failed. A call
                # that found nothing is refused if repeated unchanged; one
                # that worked may run again (the complaint may be the wording).
                self._retry_avoid = {_call_key(c["name"], c["arguments"])
                                     for c in attempt if _no_evidence(c.get("result", ""))}
                # A read that found something, and he still said no: the facts
                # were there, the reading was wrong. Without them the retry
                # answered "when is the meeting" with the clock (0/5 live).
                # Hand back what was already read -- nothing is run again --
                # and say which answer he rejected.
                reread = []
                reads = [c for c in attempt if c["name"] not in brain_rules.MUTATING_TOOLS
                         and not _no_evidence(c.get("result", ""))]
                if not seeded and reads and wrong_reply:
                    for c in reads:
                        reread += [{"role": "assistant", "content": "",
                                    "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}]},
                                   {"role": "tool", "content": str(c.get("result", ""))}]
                    reread.append({"role": "system", "content":
                                   f'He said your answer "{wrong_reply[:200]}" was not what he asked. Use the '
                                   "results above. If more than one thing in them could be what he means, ask "
                                   "which one in one short question. Don't repeat that answer."})
                self._retrying = True
                try:
                    retry, retry_msgs = self.respond([dict(m) for m in messages[:prev_i + 1]] + seeded + reread,
                                                     confirm=confirm, extra_context=extra_context)
                finally:
                    self._retrying = False
                    self._retry_avoid = set()
                retry_msgs = retry_msgs[:prev_i + 1] + retry_msgs[prev_i + 1 + len(reread):] if reread else retry_msgs
                if seeded and _SAYS_NOTHING.search(retry or ""):
                    # Checked against the evidence: she found it, so "I don't
                    # have anything" is wrong. Quote the source instead.
                    retry = "Here's what your notes say: " + seeded[-1]["content"][:400].strip()
                # Keep the repair's tool calls in this turn. They used to be
                # dropped, so the stored repair looked like a bare claim
                # ("claimed an action with no tool call") and his "thats it"
                # could never confirm a lesson. replay() still sends prose only.
                for m in retry_msgs[prev_i + 1:]:
                    if m.get("role") == "tool" or (m.get("role") == "assistant" and m.get("tool_calls")):
                        messages.append(dict(m))
                if wrong_reply and " ".join(retry.lower().split()) == " ".join(wrong_reply.lower().split()):
                    # Checked against the miss: the same answer again is
                    # not a repair, and saying it twice isn't honest.
                    retry = ("I got the same answer again, so I haven't fixed it: " + retry +
                             " Tell me what you meant and I'll do that instead.")
                answer = f'You asked: "{shown}". {retry}'.strip()
                messages.append({"role": "assistant", "content": answer})
                return answer, messages
        import planner
        if _PLAN_YES.fullmatch(latest_user.strip()) and planner.pending():
            last_said = next((m.get("content") or "" for m in reversed(messages[:-1])
                              if m.get("role") == "assistant"), "")
            if "to your tasks?" in last_said:
                answer = planner.accept()
                messages.append({"role": "assistant", "content": answer})
                return answer, messages
        quick = _quick_reply(latest_user, next((m.get("content") or "" for m in reversed(messages[:-1])
                                                if m.get("role") == "assistant"), ""))
        if quick:
            messages.append({"role": "assistant", "content": quick})
            return quick, messages
        if _FOLLOW_ON.fullmatch(latest_user.strip()):
            # Carry the job over from the turn before -- his request or her
            # offer -- so the checks below see what he means. See _FOLLOW_ON.
            prev_i = _last_real_request(messages)
            prev_user = (messages[prev_i].get("content") or "") if prev_i is not None else ""
            last_said = next((m.get("content") or "" for m in reversed(messages[:-1])
                              if m.get("role") == "assistant"), "")
            if _BRIEFING.search(prev_user) or _BRIEFING.search(last_said):
                latest_user = "give me the briefing"
            elif _PLAN_STUDY.search(prev_user) or _PLAN_STUDY.search(last_said):
                latest_user = "plan my study modules"
        import invest
        # Her previous reply goes with it: a pending instrument choice answers
        # only the question she has just asked, so an early reply above
        # ("thanks" -> "Any time.") still closes it.
        said = invest.chat(latest_user.strip(), last_said=next(
            (m.get("content") or "" for m in reversed(messages[:-1]) if m.get("role") == "assistant"), ""))
        if said is not None:
            messages.append({"role": "assistant", "content": said})
            return said, messages
        if _WATCHLIST_SHOW.fullmatch(latest_user.strip()) or _WATCHLIST_BRIEF.fullmatch(latest_user.strip()):
            if _WATCHLIST_SHOW.fullmatch(latest_user.strip()):
                answer = invest.list_text()  # a corrupt store says so, never "empty"
            else:
                answer = invest.brief()
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        import study_brief
        brief_module = study_brief.match_module(latest_user)
        last_said = next((m.get("content") or "" for m in reversed(messages[:-1])
                          if m.get("role") == "assistant"), "")
        if (_STUDY_BRIEF.search(latest_user)
                or (brief_module and _PLAN_STUDY.search(latest_user))
                or (brief_module and last_said.startswith(study_brief.WHICH_MODULE))):
            # One module, done as a job: notes read, brief written into his
            # vault, read back and checked -- see study_brief.py. Before the
            # daily briefing ("brief me on signals") and the hand-off ("prep
            # me for...") so neither swallows it.
            answer = study_brief.run(latest_user)
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if _BRIEFING.search(latest_user):
            # Built from his tasks and coursework notes -- see planner.py.
            answer = planner.briefing()
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if _PLAN_STUDY.search(latest_user):
            text, steps = planner.study_plan()
            question = planner.offer(steps)
            answer = text + (f"\n\n{question}" if question else "")
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if _should_delegate(latest_user):
            # A writing/research/solutions job: Claude does it and the result
            # lands in his vault -- see delegate.py. She says so plainly.
            import delegate
            context = [{"role": m["role"], "content": m.get("content") or ""}
                       for m in messages[:-1]
                       if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()]
            answer = delegate.start(latest_user.strip(), context)
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if latest_user.strip().lower() in ("/good", "/bad"):
            answer = ("Thanks — that tells me the last answer was helpful." if latest_user.strip().lower() == "/good"
                      else "Got it — the last answer was wrong. Tell me what should happen instead.")
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        if tool_router.feedback_only(latest_user) and re.search(r"\bdone\b", latest_user, re.I):
            # A complaint about 'Done' isn't permission for another action.
            # Don't turn it into an invented task, fact, path or promise.
            answer = "I shouldn't have said 'Done' without verifying it. The last action is unverified."
            messages.append({"role": "assistant", "content": answer})
            return answer, messages
        last_tool_result = ""  # fallback for _unleak_json
        used_once = set()      # guards _ONCE_PER_TURN tools; per turn, not per round
        turn_results = []      # every tool result this turn, for the _CLAIMS_DONE check
        successful_tools = set()  # attempts and successful actions are different
        tool_outputs = {}
        news_result = ""       # for _has_news_content
        honesty_nudged = False # that check sends her back at most once per turn
        catch_retried = False  # the "didn't catch that" retry, also once
        # Only the tools this turn could plausibly need -- see tool_router.py
        # for why sending all of them costs reply quality on an 8B model.
        turn_tools = tool_router.select(TOOL_DEFINITIONS, messages)
        offered_names = {d["function"]["name"] for d in turn_tools}
        request_text, request_groups = tool_router.request_context(messages)
        file_request = ("files" in request_groups
                        and bool(re.search(r"\b(?:file|document|cv|r[eé]sum[eé]|passport|invoice)\b",
                                           request_text, re.I)))
        expected_actions = set()
        if file_request and _OPEN_DOCUMENT_REQUEST.search(request_text):
            expected_actions = {"open_application"}
        elif "timer" in request_groups:
            if re.search(r"\b(?:stop|cancel)\b|turn.{0,15}off", request_text, re.I):
                expected_actions = {"cancel_timer"}
            elif re.search(r"\b(?:start|set|begin)\b", request_text, re.I):
                expected_actions = {"set_timer"}
        completion_attempted = False
        search_forced = False  # the fallback file search runs at most once a turn
        vault_forced = False   # and so does the fallback vault search
        # A find/open request, as opposed to e.g. "how do I save a document".
        file_lookup_request = file_request and bool(
            _OPEN_DOCUMENT_REQUEST.search(request_text)
            or re.search(r"\b(?:find|locate|where(?:'s|\s+is|\s+are)|search\s+for|look\s+for"
                         r"|called|named)\b",
                         request_text, re.IGNORECASE))

        def execute(name, args):
            # Enforce the offered set, including complaints filtered by the
            # router. The model can hallucinate calls in structured or raw
            # JSON form; neither is permission to run an unrelated tool.
            if last_round or name not in offered_names:
                result = f"Refused: '{name}' isn't available for this request. Answer the user's actual request."
            elif _call_key(name, args) in getattr(self, "_retry_avoid", ()):
                # Naming alternatives here was tried live on 2026-10-30: 0/5
                # found the answer (she offered to search instead), ~2x slower.
                result = (f"Refused: this exact '{name}' call already failed and he said the answer "
                          "was wrong. Do something different, or tell him plainly what you can't do.")
            elif (name == "open_application" and file_request
                  and not _known_document_path(args.get("app_name", ""), messages, tool_outputs)):
                result = ("Refused: no verified document path was supplied. Use find_files to locate "
                          "the requested file, then open_application with the full path it returns.")
            elif name == "get_current_time" and not _TIME_REQUEST.search(latest_user):
                result = ("No tool needed here. Answer his question directly, in your "
                          "own words, from what you know.")
            elif name == "write_file" and not _WRITE_REQUEST.search(latest_user):
                result = ("Refused: he didn't ask you to write or save anything. Don't write a file; "
                          "do what he said or answer him.")
            elif name == "write_file" and _inside_denisse(args.get("path", "")):
                result = ("Refused: that's your own program folder, and your settings there are his "
                          "to edit. Don't write there.")
            elif name == "add_task" and not _TASK_REQUEST.search(latest_user):
                result = ("Refused: he didn't ask for a task or reminder. If he told you how to "
                          "talk to him, just say you've got it.")
            elif (name == "run_shell_command"
                  and _DESTRUCTIVE_SHELL.search(str(args.get("command") or args.get("cmd") or ""))):
                result = ("Refused: that command deletes, moves or overwrites his files, and you "
                          "don't do that. Tell him plainly you can't, and name the files if that helps.")
            elif name == "list_skills" and not _SKILL_QUESTION.search(latest_user):
                result = ("Not needed: he didn't ask about your skills. Do what he asked with "
                          "your other tools, or answer directly. Don't mention skills or tools.")
            elif name == "read_skill" and not _is_known_skill(args.get("name") or ""):
                result = (f"There's no skill called '{args.get('name')}'. Don't mention skills "
                          "or tools to him -- do what he asked with your other tools, or "
                          "answer directly.")
            elif name == "calculate" and not _MATHS_REQUEST.search(latest_user):
                # Worded as what to do next, not as a fault in his question:
                # "there's no number in what he said" made her answer "I
                # didn't catch that" to a perfectly clear question.
                result = ("No tool needed here. Answer his question directly, in your "
                          "own words, from what you know.")
            else:
                if name == "search_vault":
                    import instincts
                    if instincts.wants_vault_overview(latest_user):
                        args = {**args, "query": "current projects"}
                if name == "add_task" and args.get("due") and not _due_said(str(args["due"]), latest_user):
                    args = {**args, "due": ""}  # see _due_said
                result = str(_run_tool(name, args, confirm, used_once))
                q = str(args.get("query") or args.get("pattern") or "")
                if name == "find_files" and result.startswith("No files matching") and re.search(r"[*?]", q):
                    # "*.cv" found nothing on 2026-10-31; he has hundreds of
                    # files with "cv" in the name. Try the plain word.
                    plain = re.sub(r"[*?]+", " ", q).strip(" .")
                    if plain and plain != q:
                        result = str(_run_tool(name, {"query": plain}, confirm, used_once))
                vq = _vault_query(latest_user)
                if (name == "recall" and vq and "search_vault" in offered_names
                        and "search_vault" not in tool_outputs):
                    # "the projects im doing" -> recall('projects') -> one stale
                    # saved fact. His projects are in his notes; read those too.
                    vres = str(_run_tool("search_vault", {"query": vq}, confirm, used_once))
                    tool_outputs["search_vault"] = vres
                    turn_results.append(vres)
                    if not _confirms_nothing(vres):
                        successful_tools.add("search_vault")
                    result += "\n\nFrom his notes:\n" + vres
            turn_results.append(result)
            tool_outputs[name] = result
            if not _confirms_nothing(result):
                successful_tools.add(name)
            return result

        for _round in range(MAX_TOOL_ROUNDS + 1):
            # Last round: stop offering tools, so the model has to answer in
            # words instead of asking for another call it won't get. See
            # MAX_TOOL_ROUNDS for why this is bounded at all.
            last_round = _round == MAX_TOOL_ROUNDS
            round_tools = None if last_round else turn_tools

            # keep_alive=-1: keep the model loaded in memory indefinitely.
            # Ollama's default unloads it after 5 minutes idle -- measured a
            # cold reload at ~4-6s vs ~0.5s warm, a real hit for something
            # like the wake-word listener that's used sporadically.
            # temperature=0.2: Ollama's own default (~0.8) is high-variance
            # enough that the exact same question could come back as a
            # correct summary of real tool data one run and a complete
            # non-sequitur ("doing alright, you?" to a news question) the
            # next -- measured directly (get_news: inconsistent/often wrong
            # at default temperature, 4/4 clean and on-topic at 0.2, no
            # loss of personality on casual chat).
            response = ollama.chat(
                model=MODEL, messages=full_messages, tools=round_tools, keep_alive=-1,
                options={"temperature": 0.2, "num_ctx": NUM_CTX},
            )
            msg = _get(response, "message")
            tool_calls = _get(msg, "tool_calls") or []

            if not tool_calls:
                raw_text = _strip_role_prefix(_get(msg, "content", "") or "")
                fake_call = _sniff_fake_tool_call(raw_text)
                final_text = raw_text if fake_call else _unleak_json(raw_text, last_tool_result)
                open_result = tool_outputs.get("open_application", "")
                if (fake_call is None and file_request and not completion_attempted and not last_round
                        and _OPEN_DOCUMENT_REQUEST.search(request_text)
                        and (not open_result or open_result.startswith("Refused: no verified document"))):
                    path = _single_document_path(tool_outputs.get("find_files", ""))
                    if path:
                        # He already asked to open it. With one verified
                        # match there is no new choice to ask him to make.
                        # Use the normal executor/confirmation/history path.
                        fake_call = {"name": "open_application", "arguments": {"app_name": path}}
                        completion_attempted = True
                if (fake_call is None and file_lookup_request and not search_forced
                        and not last_round and "find_files" not in tool_outputs
                        and not successful_tools & {"read_file", "open_application"}):
                    # She is about to answer a file request without having
                    # looked. Every way that went this week was wrong: asking
                    # him for the path, "Done." with nothing done, "I didn't
                    # find a file called passport" with no search run, and
                    # two invented PDFs. So the code looks for her, once, and
                    # she answers from what came back.
                    query = _file_query(request_text) or _file_query(latest_user)
                    if query:
                        fake_call = {"name": "find_files", "arguments": {"query": query}}
                        search_forced = True
                vq = _vault_query(latest_user)
                if (fake_call is None and vq and not vault_forced and not last_round
                        and "search_vault" in offered_names and "search_vault" not in tool_outputs):
                    # About his internships or projects, and she's answering
                    # without having read his notes. Same move as the file
                    # search above: look first, then answer from it.
                    fake_call = {"name": "search_vault", "arguments": {"query": vq}}
                    vault_forced = True
                place = _weather_place(latest_user, request_groups)
                if (fake_call is None and place and not last_round
                        and "get_weather" in offered_names and "get_weather" not in tool_outputs):
                    fake_call = {"name": "get_weather", "arguments": {"location": place}}
                if (fake_call is None and not catch_retried and not turn_results
                        and _DIDNT_CATCH.search(final_text) and _clear_message(latest_user)):
                    catch_retried = True
                    # Same question, history left out, no tools: the clean
                    # condition, where this answer was fine. Kept only if
                    # it's an actual answer.
                    retry = ollama.chat(
                        model=MODEL,
                        messages=([full_messages[0]] + _style_examples()
                                  + [{"role": "user", "content": latest_user}]),
                        keep_alive=-1,
                        options={"temperature": 0.2, "num_ctx": NUM_CTX},
                    )
                    retry_text = _strip_role_prefix(
                        (_get(_get(retry, "message"), "content", "") or "").strip())
                    if retry_text and not _DIDNT_CATCH.search(retry_text):
                        final_text = retry_text
                if fake_call is None and _TOOL_NARRATION.match(final_text):
                    # One retry, no tools, with the nudge as a system note
                    # for this call only -- never added to `messages`, so it
                    # is never persisted. Keep the retry if it's usable.
                    retry = ollama.chat(
                        model=MODEL,
                        messages=full_messages + [{"role": "system", "content": _PLAIN_REPLY_NUDGE}],
                        keep_alive=-1,
                        options={"temperature": 0.2, "num_ctx": NUM_CTX},
                    )
                    # The retry, ending on a system note, comes back as
                    # "assistant\n\nNo worries." -- 3/3 traced 2026-10-21.
                    retry_text = _strip_role_prefix(
                        (_get(_get(retry, "message"), "content", "") or "").strip())
                    if retry_text and not _TOOL_NARRATION.match(retry_text):
                        final_text = retry_text
                elif fake_call is None:
                    final_text = _strip_tool_talk(final_text)
                if news_result and fake_call is None and not _has_news_content(final_text, news_result):
                    final_text = _speak_headlines(news_result) or final_text
                # A clock lookup or calculation is not evidence that an
                # action happened. A failed timer call isn't evidence either.
                unconfirmed = not (successful_tools & tool_router.MUTATING_TOOLS)
                wrong_tool = _claim_without_its_tool(final_text, successful_tools)
                if (_CLAIMS_DONE.search(final_text) and expected_actions
                        and not successful_tools & expected_actions):
                    wrong_tool = True
                if fake_call is None and ((unconfirmed and _CLAIMS_DONE.search(final_text)) or wrong_tool):
                    if not honesty_nudged and not last_round:
                        # Back round the loop, tools still on offer, with the
                        # nudge in this call only -- never in `messages`, so
                        # the false claim and the nudge are never persisted.
                        honesty_nudged = True
                        if not turn_results:
                            nudge = _NO_TOOL_CLAIM_NUDGE
                        elif wrong_tool and not unconfirmed:
                            nudge = _WRONG_TOOL_CLAIM_NUDGE.format(ran=", ".join(sorted(successful_tools)))
                        else:
                            nudge = _UNCONFIRMED_CLAIM_NUDGE.format(results="; ".join(r[:120] for r in turn_results))
                        full_messages.append({"role": "system", "content": nudge})
                        continue
                    if turn_results and all(_confirms_nothing(r) for r in turn_results):
                        # Still claiming success over nothing but errors: say
                        # what the tool said, the one thing here that's true.
                        final_text = "I couldn't complete that action."
                    else:
                        # The retry is best effort, never a licence to let
                        # the same unsupported 'Done' escape a second time.
                        final_text = "I haven't confirmed that action, so I can't say it's done."
                if fake_call is None:
                    # Checked after an open too. It used to be skipped once
                    # open_application had succeeded, so she could open the
                    # real file and then report a different, invented one
                    # ("Opened C:\MadeUp\Passport.pdf") -- caught by
                    # test_pet_chat on 2026-10-29. What was actually opened
                    # is in the tool result, so say that instead.
                    if file_request and "read_file" not in successful_tools:
                        said_paths = re.findall(r"(?:[A-Za-z]:[\\/]|\\\\)[^\r\n]+", final_text)
                        if any(not _known_document_path(p.strip().rstrip('"\'.,;`'), messages, tool_outputs)
                               for p in said_paths):
                            lookup = tool_outputs.get("find_files", "")
                            if "open_application" in successful_tools:
                                final_text = tool_outputs["open_application"]
                            else:
                                final_text = (lookup if lookup.startswith(("Found ", "No files matching")) else
                                              "I don't have verified file-search results for that request yet.")
                    if file_request and _FILE_ABSENCE.search(final_text):
                        lookup = tool_outputs.get("find_files", "")
                        if lookup.startswith("Found "):
                            final_text = lookup
                        elif lookup.startswith("No files matching"):
                            final_text = lookup + " That only covers the folders searched."
                        else:
                            final_text = "I couldn't complete the file lookup, so I can't tell whether it's there."
                    lookup = tool_outputs.get("find_files", "")
                    if (file_request and lookup.startswith("Found ")
                            and not successful_tools & {"read_file", "open_application"}):
                        paths = re.findall(r"(?:[A-Za-z]:[\\/]|\\\\)[^\r\n]+", lookup)
                        said = final_text.replace("/", "\\").lower()
                        if paths and not any(p.replace("/", "\\").lower() in said for p in paths):
                            final_text += "\n\nMatching files:\n" + "\n".join(paths[:3])
                    skills_result = tool_outputs.get("list_skills", "")
                    # Only when he asked about skills. Without this check, on
                    # 2026-10-29 "plan my internship applications" -> she
                    # looked at her skills -> the reply was replaced with the
                    # skills list (a regression from 9287f33).
                    if ("list_skills" in successful_tools and _SKILL_QUESTION.search(latest_user)
                            and not _names_a_skill(final_text, skills_result)):
                        final_text = _skills_reply(skills_result)
                    task_result = tool_outputs.get("list_tasks", "")
                    if ("list_tasks" in successful_tools
                            and not successful_tools & {"add_task", "complete_task", "remove_task"}
                            and not _has_task_content(final_text, task_result)):
                        final_text = task_result
                    for name in ("add_task", "complete_task", "remove_task"):
                        result = tool_outputs.get(name, "")
                        confirmed = re.split(
                            r"\.\s+(?:That was the last one|\d+ other tasks? still open)", result, maxsplit=1)[0]
                        # A new task is always confirmed in the task list's own
                        # words: "I'll remind you on the 6th of October at 2 PM"
                        # named the task right and invented the date.
                        if (name in successful_tools and result.startswith(("Added:", "Done:", "Marked done:", "Removed:"))
                                and (name == "add_task"
                                     or not _has_task_content(final_text, "- " + confirmed.partition(":")[2]))):
                            final_text = confirmed
                    vault_result = tool_outputs.get("search_vault", "")
                    if vault_result.startswith("From your notes (dated context"):
                        # Preserve dates and recorded-status caveats as well
                        # as topics; keyword overlap cannot verify a summary.
                        final_text = vault_result
                    if (vault_result.startswith("From your notes")
                            and _VAULT_ACCESS_DENIAL.search(final_text)):
                        excerpt = vault_result.partition("\n\n")[2]
                        final_text = "I can search your vault. Here's what I found:\n\n" + excerpt[:900]
                    if vault_result.startswith("From your notes") and re.search(r"\bdue\b|\bdeadline\b", latest_user, re.I):
                        # Preserve an unambiguous due-year from the retrieved
                        # evidence, not the note's unrelated update date.
                        years = set(re.findall(
                            r"\bdue\s+(?:(?:mon|tue|wed|thu|fri|sat|sun)\w*\s+)?"
                            r"\d{1,2}\s+[A-Za-z]{3,9}\s+(20\d{2})\b", vault_result, re.I))
                        if len(years) == 1 and not years & set(re.findall(r"\b20\d{2}\b", final_text)):
                            final_text += " Your notes specify " + next(iter(years)) + "."
                    if "get_brain_location" in successful_tools:
                        # Current settings are inspectable facts, not a
                        # persona for the model to invent or paraphrase away.
                        final_text = tool_outputs["get_brain_location"]
                    if "get_weather" not in successful_tools:
                        final_text = _strip_unfetched_weather(final_text)
                    if "get_current_time" not in successful_tools:
                        final_text = _correct_unfetched_clock(final_text)
                    # Last, after every check above has compared paths.
                    final_text, hidden = _tidy_reply(final_text, latest_user)
                    if hidden:
                        self._last_paths = hidden
                    messages.append({"role": "assistant", "content": final_text})
                    return final_text, messages

                # A recovered JSON call, or completion of an explicit
                # single-match file-open request. Record the real call and
                # result, then let the model answer from that evidence.
                assistant_msg = {"role": "assistant", "content": "", "tool_calls": [{"function": fake_call}]}
                messages.append(assistant_msg)
                full_messages.append(assistant_msg)
                result = execute(fake_call["name"], fake_call["arguments"])
                last_tool_result = str(result)
                if fake_call["name"] == "get_news":
                    news_result = last_tool_result
                tool_msg = {"role": "tool", "content": str(result)}
                messages.append(tool_msg)
                full_messages.append(tool_msg)
                continue

            # Model wants to use one or more tools. Keep the tool_calls on the
            # stored assistant message -- without it, the "tool" messages that
            # follow have nothing to answer when this history is replayed
            # (e.g. after a restart), which confuses the model.
            assistant_msg = {
                "role": "assistant",
                "content": _get(msg, "content", "") or "",
                "tool_calls": [_tool_call_to_dict(c) for c in tool_calls],
            }
            messages.append(assistant_msg)
            full_messages.append(assistant_msg)

            for call in tool_calls:
                func_info = _get(call, "function")
                name = _get(func_info, "name")
                args = dict(_get(func_info, "arguments") or {})
                result = execute(name, args)
                last_tool_result = str(result)
                if name == "get_news":
                    news_result = last_tool_result
                tool_msg = {"role": "tool", "content": str(result)}
                messages.append(tool_msg)
                full_messages.append(tool_msg)
            # loop again so the model can use the tool results

        # Only reachable if the final round still produced nothing usable.
        # Say so plainly rather than returning an empty string, which the
        # popup would render as a blank bubble.
        give_up = "I got stuck working that one out -- could you rephrase it?"
        # If a search already came back, that is a real answer: show it
        # rather than asking him to rephrase a request that was fine. On
        # 2026-10-29 "pull up my passport" ran out of rounds 3/3 in the
        # clean eval while holding all six matches.
        lookup = tool_outputs.get("find_files", "")
        if lookup.startswith(("Found ", "No files matching")):
            give_up = lookup
        give_up, hidden = _tidy_reply(give_up, latest_user)
        if hidden:
            self._last_paths = hidden
        messages.append({"role": "assistant", "content": give_up})
        return give_up, messages
