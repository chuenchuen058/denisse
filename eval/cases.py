"""
The prompt suite Denisse is graded on.

Deterministic checks only -- no LLM judge. Every assertion is something a
script can decide from the reply text and the tools that were called, so a
pass tonight means the same thing as a pass next week. That matters more
than sophistication here: the point is detecting *drift*.

Each case:
    id        short stable name (the column in the trend file)
    prompt    what to send
    expect    checks, all of which must hold:
                no_tools      True -> the turn must call no tool at all
                any_tool_of   set  -> at least one call from this set
                none_of       set  -> none of these may be called
                must_contain  list -> substring(s) required in the reply
                deny          list -> substrings that must NOT appear
    why       the regression this case exists to catch
"""

# Tools that read or change the machine. A greeting reaching for any of
# these is the exact failure found on 2026-09-04.
STATE_TOOLS = {
    "recall", "read_file", "list_directory", "find_files", "run_shell_command",
    "get_current_time", "get_news", "get_weather", "get_system_status",
    "list_skills", "read_skill", "list_known_facts", "play_song",
}

# Implementation talk leaking into a reply -- she should answer like a
# person, not narrate her own function-calling.
LEAKS = [
    "function call", "tool call", "i don't have a specific function",
    "as an ai language model", "system prompt",
]

# A Windows machine. A POSIX path in a reply means she invented one.
POSIX_LEAKS = ["/home/", "/usr/", "/var/", "/etc/"]

CASES = [
    {
        "id": "greet_hey",
        "prompt": "hey",
        "expect": {"no_tools": True, "deny": LEAKS + POSIX_LEAKS},
        "why": "Greetings must not trigger a tool. This is the 2026-09-04 bug.",
    },
    {
        "id": "greet_thanks",
        "prompt": "thanks",
        "expect": {"no_tools": True, "deny": LEAKS + POSIX_LEAKS},
        "why": "Short acknowledgment, same rule.",
    },
    {
        "id": "identity",
        "prompt": "what model are you running on?",
        "expect": {"must_contain": ["llama"], "deny": POSIX_LEAKS},
        "why": "She should be plain about being a local Llama, not cagey.",
    },
    {
        "id": "time",
        "prompt": "what time is it?",
        "expect": {"any_tool_of": {"get_current_time"}, "deny": POSIX_LEAKS},
        "why": "A real request should reach the right tool.",
    },
    {
        "id": "math",
        "prompt": "what is 17 times 23?",
        "expect": {"any_tool_of": {"calculate"}, "must_contain": ["391"]},
        "why": "The prompt forbids doing arithmetic in her head. 17*23=391.",
    },
    {
        "id": "find_file",
        "prompt": "find my japan flight pdf",
        "expect": {"any_tool_of": {"find_files"}, "deny": POSIX_LEAKS},
        "why": "find_files exists so she doesn't need an exact path.",
    },
    {
        "id": "windows_paths",
        "prompt": "what folders are in my home directory?",
        "expect": {"deny": POSIX_LEAKS},
        "why": "Invented POSIX paths on a Windows box -- fixed 2026-09-04 "
               "by stating the machine facts in the system prompt.",
    },
    {
        "id": "task_add",
        "prompt": "remind me to call the dentist tomorrow at 3pm",
        "expect": {"any_tool_of": {"add_task"}, "deny": POSIX_LEAKS},
        "why": "A 'remind me to X by Y' must become a task, not a reply that "
               "promises to remember and then doesn't.",
    },
    {
        "id": "task_list",
        "prompt": "what's on my plate?",
        "expect": {"any_tool_of": {"list_tasks"}, "deny": POSIX_LEAKS},
        "why": "Asking what's outstanding must read the real task list "
               "rather than being answered from context.",
    },
    {
        "id": "garbled",
        "prompt": "the do way or take a bite white the way",
        "expect": {"none_of": {"get_news", "play_song", "run_shell_command"},
                   "deny": POSIX_LEAKS},
        "why": "A mangled voice transcript should get 'didn't catch that', "
               "not a guessed tool call with nonsense arguments.",
    },
]
