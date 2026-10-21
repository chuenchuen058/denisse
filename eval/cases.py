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
                contains_any  list -> at least one of these in the reply
                deny          list -> substrings that must NOT appear
    before    optional scripted earlier turns (role/content dicts), for
              follow-ups that only make sense after another message
    why       the regression this case exists to catch

Every case really runs its tools, every night. So only cases whose tools
are read-only, or write to the eval's scratch task store, belong here --
nothing that plays music, opens an app, writes a file or cleans temp files
at 3am. A case that needs one of those must assert it is NOT called.
"""

# Tools that read or change the machine. A greeting reaching for any of
# these is the exact failure found on 2026-09-04.
STATE_TOOLS = {
    "recall", "read_file", "list_directory", "find_files", "run_shell_command",
    "get_current_time", "get_news", "get_weather", "get_system_status",
    "list_skills", "read_skill", "list_known_facts", "play_song",
    "search_vault",
}

# Implementation talk leaking into a reply -- she should answer like a
# person, not narrate her own function-calling.
LEAKS = [
    "function call", "tool call", "i don't have a specific function",
    "as an ai language model", "system prompt",
    # 2026-10-21: "I called the get_weather tool to get..." and "The tool
    # call returned a list of news headlines" both passed, because only the
    # greeting cases checked LEAKS. run_eval now checks these on every case,
    # and also rejects any tool's own name (get_weather, current_track).
    "i called the", "tool returned", "the tool",
]

# The garbled-input rule ("say you didn't catch that") misfiring on plain
# chat. 2026-10-20 had "do you like rainy days?" and, in live, "hey" both
# answered "I didn't catch that" -- passing, and the first one was then
# written into dataset.jsonl as a verified-good pair. run_eval checks this
# on every case except those marked "misheard_ok" (only `garbled`).
MISHEARD = ["didn't catch", "did not catch", "rephrase"]

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
        "expect": {"no_tools": True,
                   "deny": LEAKS + POSIX_LEAKS
                   + ["doing alright", "how are you", "how about you"]},
        "why": "Short acknowledgment, same rule -- and answered as thanks. "
               "2026-10-20 clean got 'Doing alright, you?', the prompt's "
               "first greeting example copied onto the wrong message.",
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
    # The 2026-10-20 timer cases. Live, "start timer" got
    # run_shell_command("start") and "The timer has started.", and "stop
    # timer" got "Done." with no tool. set_timer only holds an in-process
    # countdown with no notifier here, so these touch nothing real.
    {
        "id": "timer_start",
        "prompt": "start timer",
        "expect": {"any_tool_of": {"set_timer"}, "none_of": {"run_shell_command", "open_application"}},
        "why": "A timer with no length must go to set_timer -- never a shell command. "
               "Clean, she asks how long; with live history she picks 10 minutes "
               "herself and says so, and either is fine: the tool result is real.",
    },
    {
        "id": "timer_set",
        "prompt": "set a timer for 10 minutes",
        "expect": {"any_tool_of": {"set_timer"}, "none_of": {"run_shell_command"}},
        "why": "A timer with a length must actually start one.",
    },
    {
        "id": "timer_stop",
        "prompt": "stop timer",
        "expect": {"any_tool_of": {"cancel_timer"}},
        "why": "'Done.' with no tool call stopped nothing; stopping must call cancel_timer.",
    },
    {
        "id": "vault_deadline",
        "prompt": "when is computational methods coursework 1 due?",
        "expect": {"any_tool_of": {"search_vault"}, "must_contain": ["2027"],
                   "deny": POSIX_LEAKS},
        "why": "The date (28 Jan 2027) exists only in his own module hub, so "
               "'2027' in the reply can only have come from the vault. Added "
               "2026-10-15 with search_vault -- the second attempt at vault "
               "read-back, after prompt injection measured 0/4 on 2026-09-02.",
    },
    {
        "id": "vault_not_for_general",
        "prompt": "what's the boiling point of water in fahrenheit?",
        "expect": {"none_of": {"search_vault"}, "deny": POSIX_LEAKS},
        "why": "search_vault must not become the thing she reaches for on "
               "every question. General knowledge has no business in his "
               "notes, and a wasted search costs him real seconds.",
    },
    {
        "id": "garbled",
        "prompt": "the do way or take a bite white the way",
        "misheard_ok": True,
        "expect": {"none_of": {"get_news", "play_song", "run_shell_command"},
                   "deny": POSIX_LEAKS},
        "why": "A mangled voice transcript should get 'didn't catch that', "
               "not a guessed tool call with nonsense arguments.",
    },

    # --- added 2026-10-18, after the tool router: one case per routed
    # group, so a keyword gap in tool_router.py shows up as a failure here.
    {
        "id": "greet_morning",
        "prompt": "good morning",
        "expect": {"no_tools": True, "deny": LEAKS + POSIX_LEAKS},
        "why": "A third greeting shape. 'hey' and 'thanks' alone let a "
               "greeting-specific fix pass without the general rule holding.",
    },
    {
        "id": "small_talk",
        "prompt": "do you like rainy days?",
        "expect": {"none_of": STATE_TOOLS - {"get_weather"},
                   "deny": LEAKS + POSIX_LEAKS},
        "why": "Chat about the weather is not a request for files, memory or "
               "notes. get_weather is tolerated: the router offers it on "
               "'rainy', and checking is a defensible reading.",
    },
    {
        "id": "time_elsewhere",
        "prompt": "what time is it in Tokyo?",
        "expect": {"any_tool_of": {"get_current_time"}, "deny": POSIX_LEAKS},
        "why": "The timezone argument path of get_current_time.",
    },
    {
        "id": "weather",
        "prompt": "is it going to rain in London today?",
        "expect": {"any_tool_of": {"get_weather"}, "deny": POSIX_LEAKS},
        "why": "Weather must come from the tool, never a guess.",
    },
    {
        "id": "weather_follow_up",
        "before": [
            {"role": "user", "content": "what's the weather like in London?"},
            {"role": "assistant", "content": "It's 14°C and cloudy in London right now."},
        ],
        "prompt": "and in Penang?",
        "expect": {"any_tool_of": {"get_weather"}, "deny": POSIX_LEAKS},
        "why": "'and in Penang?' names no tool group on its own; the router "
               "has to carry weather over from the previous message.",
    },
    {
        "id": "news",
        "prompt": "what's in the news today?",
        "expect": {"any_tool_of": {"get_news"}, "deny": POSIX_LEAKS},
        "why": "News must come from get_news, never invented headlines.",
    },
    {
        "id": "now_playing",
        "prompt": "what song is playing right now?",
        "expect": {"any_tool_of": {"current_track"},
                   "none_of": {"play_song", "skip_song", "pause_music",
                               "resume_music", "previous_song", "open_application"},
                   "deny": POSIX_LEAKS},
        "why": "A question about the music must read it, not change it.",
    },
    {
        "id": "system_status",
        "prompt": "my laptop feels slow, how's the cpu and memory?",
        "expect": {"any_tool_of": {"get_system_status"},
                   "none_of": {"clean_temp_files", "disable_startup_program",
                               "run_shell_command"},
                   "deny": POSIX_LEAKS},
        "why": "Asking how the machine is doing must check it, not start "
               "changing it.",
    },
    {
        "id": "recall_british",
        "prompt": "what's my favourite programming language?",
        "expect": {"any_tool_of": {"recall", "list_known_facts"},
                   "none_of": {"remember"},
                   "must_contain": ["python"], "deny": POSIX_LEAKS},
        "why": "The fact is stored as favorite_programming_language. A "
               "British-spelled question used to need the model to guess "
               "that exact key for recall() to find it.",
    },
    {
        "id": "recall_loose",
        "prompt": "which code editor do I use?",
        "expect": {"any_tool_of": {"recall", "list_known_facts"},
                   "none_of": {"remember"},
                   "must_contain": ["neovim"], "deny": POSIX_LEAKS},
        "why": "Stored as favorite_editor; 'code editor' / 'editor_used' "
               "are the keys a model actually guesses. First run (2026-10-18) "
               "the router didn't offer recall at all, and she called "
               "remember('code_editor', 'Visual Studio Code') instead -- an "
               "invented fact. A question must never write memory.",
    },
    {
        "id": "self_voice",
        "prompt": "what voice are you using right now?",
        "expect": {"any_tool_of": {"get_voice_settings"}, "deny": POSIX_LEAKS},
        "why": "She has no built-in knowledge of her voice; she must check.",
    },
    {
        # Keep last: it closes the seeded task, which the earlier task
        # cases in this condition still need open.
        "id": "task_complete_one",
        "prompt": "I've filed the Spritzer paperwork, tick it off",
        "expect": {"any_tool_of": {"complete_task"}, "none_of": {"remove_task"},
                   "contains_any": ["spritzer", "paperwork"],
                   "deny": POSIX_LEAKS},
        "why": "Done work is completed (kept on record), not deleted -- and "
               "the 2026-09-23 bug closed an unrelated task as well. Since "
               "2026-10-21 the reply must say which task it closed: it had "
               "been answering with the dentist task instead, and passing.",
    },
]
