"""
The local brain: talks to a model running on this machine via Ollama
(http://localhost:11434), defines the tool schema, and runs the tool-use
loop that turns a user message into a final text reply (after any tool
calls).

No API key, no internet required after the model is downloaded once.
"""

import json
import os
import platform

import ollama
from dotenv import load_dotenv

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
    "intentions, just do it and report the result. IMPORTANT: for a "
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
            "description": "Open a local application by name.",
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
]

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


def _run_tool(name: str, args: dict, confirm) -> str:
    func = tools.TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"Error: unknown tool '{name}'"
    if confirm is not None and not confirm(name, args):
        return f"User declined to run '{name}'."
    try:
        return func(**args)
    except Exception as e:
        return f"Error running '{name}': {e}"


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
        full_messages = [{"role": "system", "content": system_content}] + messages

        while True:
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
                model=MODEL, messages=full_messages, tools=TOOL_DEFINITIONS, keep_alive=-1,
                options={"temperature": 0.2, "num_ctx": NUM_CTX},
            )
            msg = _get(response, "message")
            tool_calls = _get(msg, "tool_calls") or []

            if not tool_calls:
                final_text = _get(msg, "content", "") or ""
                fake_call = _sniff_fake_tool_call(final_text)
                if fake_call is None:
                    messages.append({"role": "assistant", "content": final_text})
                    return final_text, messages

                # Recovered a tool call the model wrote as raw text instead
                # of using Ollama's structured field -- run it for real
                # instead of showing broken JSON, then loop so the model
                # can respond to the (real) result.
                assistant_msg = {"role": "assistant", "content": "", "tool_calls": [{"function": fake_call}]}
                messages.append(assistant_msg)
                full_messages.append(assistant_msg)
                result = _run_tool(fake_call["name"], fake_call["arguments"], confirm)
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
                result = _run_tool(name, args, confirm)
                tool_msg = {"role": "tool", "content": str(result)}
                messages.append(tool_msg)
                full_messages.append(tool_msg)
            # loop again so the model can use the tool results
