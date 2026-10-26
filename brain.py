"""
The local brain: talks to a model running on this machine via Ollama
(http://localhost:11434), defines the tool schema, and runs the tool-use
loop that turns a user message into a final text reply (after any tool
calls).

No API key, no internet required after the model is downloaded once.
"""

import inspect
import json
import ntpath
import os
import platform
import re

import ollama
from dotenv import load_dotenv

import learning
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
    "through, so skip it whenever a direct answer is possible. When you learn "
    "something worth remembering about "
    "the user (their name, preferences, ongoing projects), use the "
    "'remember' tool to save it without being asked. You also have a "
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
            "description": "Report the real filesystem/Obsidian-vault paths where your own code and memory live. Call this instead of guessing or typing out a path yourself if asked where your brain/code/memory is.",
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
            "up, his societies. 'When is the CM coursework due', 'who lectures "
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
    on = os.environ.get("DENISSE_STYLE_EXAMPLES", "1").lower() not in ("0", "false", "off", "no")
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


def _claim_without_its_tool(text: str, ran: set) -> bool:
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

        def execute(name, args):
            # Enforce the offered set, including complaints filtered by the
            # router. The model can hallucinate calls in structured or raw
            # JSON form; neither is permission to run an unrelated tool.
            if last_round or name not in offered_names:
                result = f"Refused: '{name}' isn't available for this request. Answer the user's actual request."
            elif (name == "open_application" and file_request
                  and not _known_document_path(args.get("app_name", ""), messages, tool_outputs)):
                result = ("Refused: no verified document path was supplied. Use find_files to locate "
                          "the requested file, then open_application with the full path it returns.")
            else:
                result = str(_run_tool(name, args, confirm, used_once))
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
                    task_result = tool_outputs.get("list_tasks", "")
                    if ("list_tasks" in successful_tools
                            and not successful_tools & {"add_task", "complete_task", "remove_task"}
                            and not _has_task_content(final_text, task_result)):
                        final_text = task_result
                    for name in ("add_task", "complete_task", "remove_task"):
                        result = tool_outputs.get(name, "")
                        confirmed = re.split(
                            r"\.\s+(?:That was the last one|\d+ other tasks? still open)", result, maxsplit=1)[0]
                        if (name in successful_tools and result.startswith(("Added:", "Done:", "Marked done:", "Removed:"))
                                and not _has_task_content(final_text, "- " + confirmed.partition(":")[2])):
                            final_text = confirmed
                    vault_result = tool_outputs.get("search_vault", "")
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
        messages.append({"role": "assistant", "content": give_up})
        return give_up, messages
