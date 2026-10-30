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
                min_words     int  -> the reply has at least this many words
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
    # 2026-10-22: "hey" -> "No JSON response is needed for a greeting."
    " json ",
]

# The garbled-input rule ("say you didn't catch that") misfiring on plain
# chat. 2026-10-20 had "do you like rainy days?" and, in live, "hey" both
# answered "I didn't catch that" -- passing, and the first one was then
# written into dataset.jsonl as a verified-good pair. run_eval checks this
# on every case except those marked "misheard_ok" (only `garbled`).
MISHEARD = ["didn't catch", "did not catch", "rephrase"]

# Report-voice: phrasing a person wouldn't use to a friend. Counted, never
# failed -- it's a measure of how she sounds, kept apart from whether she's
# right. Every entry is from a real reply in memory/conversation.json or an
# eval report (2026-10-22), when Chuen asked for her to talk "like a real
# person": "The Pomodoro Timer has been started.", "Your task list is still
# there: 2 open: - ...", "Today's news includes various headlines ...".
ROBOTIC = [
    "has been ", "have been ", "the result of", "could you please",
    "is there anything else", "let me know if", "i apologize",
    "it looks like", "it seems", "various", "here is", "the following",
    "you can use the", "you can now", "the current time is",
    "is still there", "includes reports", "headlines include",
    "news summary:", "i've got the latest", "how can i assist",
    "i've added", "open:\n", " open: -",
    # The AI disclaimer instead of an opinion (2026-10-22, "do you like cats?")
    "personal preferences", "computer program", "i'm designed to",
    "as an ai", "don't have feelings",
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
        "expect": {"any_tool_of": {"get_news"}, "min_words": 12, "deny": POSIX_LEAKS},
        "why": "News must come from get_news, never invented headlines -- and "
               "actually say some. 'Today's news.' and 'Here's what's in the "
               "news today, Chuen.' both passed on 2026-10-22, with no news.",
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
    # ---- 2026-10-26..28: the replies Chuen actually got this week ------
    # Every case below is a turn from memory/conversation.json that went
    # wrong in front of him. The suite was passing 24-27/27 clean on the
    # same days he said "denisse is really broken", because it was almost
    # entirely single-turn and none of these shapes were in it.
    {
        "id": "stop_talking",
        "prompt": "okay stop talking",
        "expect": {"no_tools": True, "allow_empty": True,
                   "deny": ["* silence *", "*silence*"]},
        "why": "Live: calculate('0') and then '* silence *'. Being told to "
               "stop is not a calculation, and narrating silence is talking. "
               "An empty reply is the intended answer: speech_control's stop "
               "matcher answers with silence by design.",
    },
    {
        "id": "shush",
        "prompt": "shush",
        "expect": {"no_tools": True, "allow_empty": True},
        "why": "Live: 'I didn't catch that.' (MISHEARD is checked on every "
               "case). A one-word instruction is not garbled input. Silence "
               "is the intended answer, as above.",
    },
    {
        "id": "iq_question",
        "prompt": "how high is your iq",
        "expect": {"none_of": STATE_TOOLS | {"calculate"}},
        "why": "Live: calculate('100'). Nine calculate calls that afternoon "
               "on things that were not maths -- Friday's tool reflex "
               "(decisions.md 2026-10-24) moved from read_skill to calculate, "
               "the other tool that is always offered.",
    },
    {
        "id": "self_parameters",
        "prompt": "how many parameters do you have?",
        "expect": {"none_of": STATE_TOOLS | {"calculate"},
                   "contains_any": ["8 billion", "eight billion", "8b", "8-billion"]},
        "why": "Live: calculate('parameter') and 'I don't know what you mean'. "
               "She runs on llama3.1:8b and the prompt says she knows it.",
    },
    {
        "id": "news_stop",
        "prompt": "stop telling me the CNA news, I don't need them anymore",
        "expect": {"none_of": {"get_news"}},
        "why": "Live: get_news three times in a row, once straight after "
               "saying 'I understand, you want me to stop telling you the "
               "news'. A word in the request is not a request for the tool.",
    },
    {
        "id": "skills_named",
        "prompt": "what skills do you have?",
        # Any of her real skill names counts as naming one -- the first
        # version accepted only three of the eight and failed a reply that
        # named python and maths.
        "expect": {"any_tool_of": {"list_skills"},
                   "contains_any": ["briefing", "biology", "chemistry", "physics",
                                    "math", "organi", "python", "notes"]},
        "why": "Live: list_skills five times, and four replies of 'I have a "
               "few skills that might be helpful' without naming one. "
               "Fetching the answer and not saying it is the failure.",
    },
    {
        "id": "find_passport",
        "prompt": "pull up my passport",
        "expect": {"any_tool_of": {"find_files"},
                   # Opening is the right reading of "pull up", and run_eval
                   # vetoes it (EVAL_VETO), so trying it is allowed and
                   # nothing actually opens.
                   "none_of": {"run_shell_command"},
                   "contains_any": ["iumc", "resume", "onedrive"]},
        "why": "2026-10-26: reported missing four times; it is in "
               "OneDrive\\Documents\\Resume\\IUMC (b5dcafd fixed the search "
               "roots). The reply has to say where, not just that it exists. "
               "Depends on that file staying where it is.",
    },
    {
        "id": "file_not_app",
        "prompt": "open a file called passport",
        "expect": {"any_tool_of": {"find_files"},
                   "none_of": {"run_shell_command"}},
        "why": "Live: open_application('passport'), no such app, reported as "
               "the file not existing. A file has to be found before anything "
               "is said about it.",
    },
    {
        "id": "latest_cv",
        "prompt": "pull up my latest cv",
        "expect": {"any_tool_of": {"find_files"},
                   "none_of": {"run_shell_command"}},
        "why": "Live: 'Done.' with no tool call at all -- a claimed success "
               "with nothing behind it.",
    },
    {
        "id": "why_not_found",
        "before": [
            {"role": "user", "content": "could you pull up my passport file"},
            {"role": "assistant", "content": "I couldn't complete the file lookup, so I can't tell whether it's there."},
        ],
        "prompt": "why cant you?",
        "expect": {"none_of": {"open_application", "run_shell_command"}},
        "why": "Live, the worst reply of the week: she answered with "
               "C:\\Users\\Xichu\\Documents\\Passport\\Passport.pdf and a "
               "second PDF, neither of which exists. The invented-path check "
               "in run_eval does the real work here; this case gives it the "
               "exact situation that produced the fabrication.",
    },
    # ---- 2026-10-29: "do the task, don't just satisfy me" -----------------
    # From the conversation of 29 Oct, after 9287f33 shipped. Chuen's words:
    # "i dont want her to reply me to satisfy me i want her to actually do
    # task". Each case below is a reply that sounded helpful and did nothing.
    {
        "id": "briefing_real",
        "prompt": "tell me the briefing",
        "expect": {"contains_any": ["due", "deadline", "coursework", "task"],
                   "deny": ["°f", "°c", "humidity", "partly cloudy", "forecast"]},
        "why": "Live: checked the clock, then invented the weather -- '68°F, "
               "partly cloudy, high of 72°F' with no weather tool called. A "
               "briefing has to be built from his real tasks and deadlines.",
    },
    {
        "id": "ack_no_clock",
        "before": [
            {"role": "user", "content": "what's on my plate?"},
            {"role": "assistant", "content": "You've got the Signals coursework due 24 Nov and two msoc chases."},
        ],
        "prompt": "thats good",
        "expect": {"none_of": STATE_TOOLS | {"calculate"}},
        "why": "Live: 'thats good' -> get_current_time -> 'It's currently "
               "11:32 AM.' Five turns running the clock answered things that "
               "weren't questions about the time.",
    },
    {
        "id": "complaint_retries",
        "before": [
            {"role": "user", "content": "tell me the briefing"},
            {"role": "assistant", "content": "It's currently 11:32 AM."},
        ],
        "prompt": "thats not what im asking",
        "expect": {"none_of": {"get_current_time", "calculate"},
                   "contains_any": ["due", "deadline", "coursework", "task"]},
        "why": "Live: answered the complaint with the time, twice. A "
               "complaint means: redo what he actually asked.",
    },
    {
        "id": "what_retries",
        "before": [
            {"role": "user", "content": "tell me the briefing"},
            {"role": "assistant", "content": "It's currently 11:32 AM."},
        ],
        "prompt": "what?",
        "expect": {"none_of": {"get_current_time", "calculate"},
                   "contains_any": ["due", "deadline", "coursework", "task"]},
        "why": "Live: 'what?' -> get_current_time again.",
    },
    {
        "id": "my_projects",
        "prompt": "what do you know about the projects im doing?",
        "expect": {"any_tool_of": {"search_vault"}},
        "why": "Live: recall('projects') returned one old saved fact "
               "(blueband) while his vault has a whole Projects folder.",
    },
    {
        "id": "internship_tracker",
        "prompt": "hows my internship tracker",
        "expect": {"any_tool_of": {"search_vault"},
                   "contains_any": ["internship", "applied", "submitted", "tracker"]},
        "why": "Live: find_files was refused, she said she had no "
               "information, and only found it when told 'it's in obsidian'.",
    },
    {
        "id": "internship_plan",
        "prompt": "could you help me plan my internship applications",
        "expect": {"any_tool_of": {"search_vault"},
                   "none_of": {"list_skills", "read_skill"},
                   "deny": ["daily briefing:", "elementary biology"]},
        "why": "Live: read_skill('plan_internship_applications'), then "
               "list_skills -- and 9287f33's skills restore replaced the reply "
               "with her skills list. His notes are where the plan starts.",
    },
    {
        "id": "study_plan",
        "prompt": "plan my study modules for me please",
        "expect": {"none_of": {"list_skills", "read_skill"},
                   "contains_any": ["24 nov", "nov 24", "24 november", "10 dec", "28 jan"]},
        "why": "Live: read_skill('plan_study_modules'), then 'you can use the "
               "list_skills tool', then 'what subjects do you need to study?'. "
               "His coursework deadlines are in his own module notes.",
    },
    # ---- 2026-10-29: the jobs she hands to Claude ---------------------------
    # From ~180 of his own non-Denisse prompts: most are writing, research
    # and solutions an 8B model does badly. She hands those to Claude
    # (delegate.py; the suite runs it dry). Worded generically -- this repo
    # is public and his own prompts stay private.
    {
        "id": "handoff_cover_letter",
        "prompt": "could you write me a cover letter for a software internship at a bank",
        "expect": {"contains_any": ["claude"],
                   "deny": ["dear hiring", "dear sir", "sincerely", "yours faithfully"]},
        "why": "An 8B cover letter is generic and confident -- the 'reply to "
               "satisfy me' he said he doesn't want. Claude writes it.",
    },
    {
        "id": "handoff_research",
        "prompt": "could you search the web for internships in singapore due before november",
        "expect": {"contains_any": ["claude"], "none_of": STATE_TOOLS - {"search_vault"}},
        "why": "She has no web search; inventing a list of internships and "
               "deadlines would be the worst possible answer.",
    },
    {
        "id": "handoff_solutions",
        "prompt": "could you add the solutions to the electromagnetism week 5 tutorial",
        "expect": {"contains_any": ["claude"]},
        "why": "Worked physics solutions are past an 8B model; a wrong "
               "derivation that looks right is worse than none.",
    },
    {
        "id": "local_reminder_not_handoff",
        "prompt": "remind me to write a cover letter tomorrow",
        "expect": {"any_tool_of": {"add_task"}, "deny": ["claude"]},
        "why": "A reminder that mentions a cover letter is still a reminder. "
               "The hand-off must not swallow local jobs.",
    },
    # --- added 2026-10-30, from the nightly draft cases (eval/proposed_cases.py)
    # that still failed on a re-probe of the current brain.
    {
        "id": "follow_on_briefing",
        "before": [
            {"role": "user", "content": "whats your most important skill?"},
            {"role": "assistant", "content": "My most important skill is probably the daily briefing."},
        ],
        "prompt": "alright tell me",
        "expect": {"contains_any": ["due", "deadline", "coursework", "task"],
                   "deny": ["2023", "list_skills", "°f", "°c"]},
        "why": "Live 28 Oct: her skills list. Re-probed 30 Oct: 'It's currently "
               "December 2023.' The job is the one she just offered.",
    },
    {
        "id": "follow_on_plan",
        "before": [
            {"role": "user", "content": "plan my study modules for me please"},
            {"role": "assistant", "content": "You can use the 'list_skills' tool to see what skills are available."},
        ],
        "prompt": "i want you to plan it",
        "expect": {"contains_any": ["due", "coursework"],
                   "deny": ["list_skills", "what subjects", "which subjects"]},
        "why": "Live 29 Oct: 'use the list_skills tool'. Re-probed 30 Oct: "
               "'what subjects are you studying?' -- the planner already knows.",
    },
    # --- added 2026-10-30: the study brief, the first multi-step job.
    {
        "id": "study_brief_signals",
        "prompt": "prep me for signals and systems",
        "expect": {"contains_any": ["study brief"], "must_contain": ["24 Nov"],
                   "deny": ["claude", "what subjects", "which module"]},
        "why": "A brief for a named module is local work from his notes. "
               "'prep me for...' used to go to the Claude hand-off.",
    },
    {
        "id": "study_brief_em",
        "prompt": "brief me on electromagnetism",
        "expect": {"contains_any": ["study brief"], "must_contain": ["Electromagnetism"],
                   "deny": ["claude", "°f", "°c"]},
        "why": "'brief me' alone is the daily briefing; on a module it is that "
               "module's study brief.",
    },
    {
        "id": "study_brief_which",
        "prompt": "can you do me a study brief",
        "expect": {"contains_any": ["which module"], "deny": ["claude"]},
        "why": "No module named: ask, don't guess one.",
    },
    {
        "id": "study_brief_answer",
        "before": [
            {"role": "user", "content": "can you do me a study brief"},
            {"role": "assistant", "content": "Which module is the study brief for? I can do one for "
             "Signals and Systems, Electromagnetism, Computational Methods or Computational "
             "Applied Biomathematics."},
        ],
        "prompt": "signals",
        "expect": {"contains_any": ["study brief"], "must_contain": ["Signals and Systems"]},
        "why": "The answer to her own question finishes the job.",
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
