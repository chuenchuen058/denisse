"""The deterministic pieces behind the 2026-10-29 reply fixes.

Each of these replaced a model decision that the eval showed going wrong
3 runs out of 3. They are plain functions, so they are tested as plain
functions -- the eval (eval/cases.py, the block dated 2026-10-26..28)
covers what the model then does with them.

Run after touching _NEWS_OFF, _SELF_MODEL, _MATHS_REQUEST, _file_query or
the skills restore in brain.py.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import brain  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


# -- turning the news off is not asking for news --------------------------
for text in ("stop telling me the CNA news, I don't need them anymore",
             "no more news please", "don't give me the news anymore",
             "turn off the daily news briefing", "stop the headlines"):
    check(f"news-off: {text!r}", bool(brain._NEWS_OFF.search(text)))
for text in ("what's in the news today?", "any news on the election?",
             "give me a short summary of today's news.", "stop the timer"):
    check(f"not news-off: {text!r}", not brain._NEWS_OFF.search(text))

# -- questions about what she is ------------------------------------------
for text in ("how many parameters do you have?", "what is your parameter",
             "whats your parameter themn", "what model are you?",
             "which model do you run on"):
    check(f"self-model: {text!r}", bool(brain._SELF_MODEL.search(text)))
for text in ("what parameters does find_files take?", "how are you doing",
             "set the timer parameter to 5"):
    check(f"not self-model: {text!r}", not brain._SELF_MODEL.search(text))
ident = brain._model_identity()
check("identity names the size in words", "8 billion" in ident, ident)

# -- calculate only for things that are maths -----------------------------
for text in ("what's 17 times 4?", "12*4", "what is half of eighty",
             "convert 30 celsius to fahrenheit", "square root of 144"):
    check(f"maths: {text!r}", bool(brain._MATHS_REQUEST.search(text)))
for text in ("how high is your iq", "okay stop talking", "what is your parameter",
             "do you understand what i said?", "its fine now"):
    check(f"not maths: {text!r}", not brain._MATHS_REQUEST.search(text))

# -- what to search for when she skipped the search -----------------------
for text, want in (("pull up my passport", "passport"),
                   ("open a file called passport", "passport"),
                   ("pull up my latest cv", "cv"),
                   ("could you pull up my passport file", "passport"),
                   ("its literally named passport check again", "passport")):
    got = brain._file_query(text)
    check(f"query from {text!r}", got == want, f"got {got!r}")
for text in ("how do I save a document in word",
             r"C:\Users\Xichu\OneDrive\Documents\Resume\IUMC explain this then"):
    got = brain._file_query(text)
    check(f"no guessed query from {text!r}", got is None, f"got {got!r}")

# -- naming the skills she just fetched ------------------------------------
listing = ("daily_briefing: How to give a quick daily briefing/check-in\n"
           "elementary_biology: How to explain elementary-level biology concepts\n"
           "organize_files: How to help organize, rename, or clean up files")
check("vague reply does not count as naming",
      not brain._names_a_skill("I've got a few skills that might come in handy.", listing))
full_listing = listing + "\ntake_notes: How to capture notes and facts"
for vague in ("I can take care of a few things for you.",
              "I can help with your daily stuff and files.",
              "Quick answer: I've got several skills."):
    check(f"generic words are not naming: {vague!r}",
          not brain._names_a_skill(vague, full_listing))
check("a reply that names one counts",
      brain._names_a_skill("I can do a daily briefing, or help with biology.", listing))
restored = brain._skills_reply(listing)
check("restored reply names them all",
      all(w in restored.lower() for w in ("briefing", "biology", "organize")), restored[:80])

# ==== 2026-10-29 afternoon: "actually do the task" ==========================

for text in ("thats not what im asking", "still not what im asking", "what?",
             "is there a problem?", "that's not it", "no, not what I meant", "huh"):
    check(f"complaint: {text!r}", bool(brain._COMPLAINT.fullmatch(text)))
for text in ("what time is it?", "what's the news", "that's not my file, find the cv",
             "is there a problem with my laptop"):
    check(f"not a complaint: {text!r}", not brain._COMPLAINT.fullmatch(text))

convo = [{"role": "user", "content": "tell me the briefing"},
         {"role": "assistant", "content": "It's 11:32 AM."},
         {"role": "user", "content": "thats good"},
         {"role": "assistant", "content": "It's 11:32 AM."},
         {"role": "user", "content": "what?"},
         {"role": "assistant", "content": "It's 11:32 AM."},
         {"role": "user", "content": "thats not what im asking"}]
check("retry target skips acks and complaints", brain._last_real_request(convo) == 0)

for text in ("what time is it in tokyo", "remind me tomorrow", "what's the date today",
             "set it for 5pm"):
    check(f"time question: {text!r}", bool(brain._TIME_REQUEST.search(text)))
for text in ("thats good", "is there a problem?", "what's the capital of japan?",
             "how high is your iq"):
    check(f"not a time question: {text!r}", not brain._TIME_REQUEST.search(text))

fake = ("Today is October 29th, 2026. Here are the current weather conditions:\n"
        "* Temperature: 68°F (20°C)\n* Humidity: 60%\n* Conditions: Partly Cloudy\n"
        "* Forecast: High of 72°F\nYou have two msoc chases overdue.")
cleaned = brain._strip_unfetched_weather(fake)
check("invented weather is removed", "68" not in cleaned and "Humidity" not in cleaned, cleaned)
check("the rest of the reply survives", "msoc chases overdue" in cleaned)
for keep in ("The boiling point of water is 212 degrees Fahrenheit.",
             "I like sunny days, honestly."):
    check(f"non-weather text untouched: {keep!r}", brain._strip_unfetched_weather(keep) == keep)

for text in ("tell me the briefing", "give me my daily briefing", "brief me",
             "what's on today"):
    check(f"briefing: {text!r}", bool(brain._BRIEFING.search(text)))
check("news switch-off is not a briefing request... handled first",
      bool(brain._NEWS_OFF.search("turn off the daily news briefing")))
for text in ("plan my study modules for me polease", "make me a study plan",
             "can you schedule my coursework", "plan my revision"):
    check(f"study plan: {text!r}", bool(brain._PLAN_STUDY.search(text)))
check("internship plan is not a study plan",
      not brain._PLAN_STUDY.search("could you help me plan my internship applications"))
for text in ("yes", "yes please", "add them", "go ahead", "sure, do it"):
    check(f"yes: {text!r}", bool(brain._PLAN_YES.fullmatch(text)))
check("'yes but only signals' is not a plain yes",
      not brain._PLAN_YES.fullmatch("yes but only signals"))

for text, want in (("hows my internship tracker", "internships"),
                   ("could you help me plan my internship applications", "internships"),
                   ("what do you know about the projects im doing?", "current projects"),
                   ("what's 2+2", None)):
    check(f"vault query {text!r}", brain._vault_query(text) == want, brain._vault_query(text))
check("skills question", bool(brain._SKILL_QUESTION.search("what skills do you have?")))
check("internship plan is not a skills question",
      not brain._SKILL_QUESTION.search("could you help me plan my internship applications"))

import datetime as _dt  # noqa: E402
_am = _dt.datetime(2026, 10, 29, 9, 0)
for text, want in (("hey", "Hey Chuen."), ("hi denisse", "Hey Chuen."),
                   ("good morning", "Morning, Chuen."), ("thanks", "Any time."),
                   ("thats good", "Good."), ("okay that good", "Good.")):
    got = brain._quick_reply(text, "You've got two msoc chases overdue.", _am)
    check(f"quick reply {text!r}", got == want, got)
check("an 'ok' answering her own question goes to the model",
      brain._quick_reply("ok", "Want me to open it?", _am) is None)
check("a greeting still answered after her question",
      brain._quick_reply("hey", "Want me to open it?", _am) == "Hey Chuen.")
for text in ("hey can you find my cv", "ok open the passport", "thanks, now add a task"):
    check(f"not a quick reply: {text!r}", brain._quick_reply(text, "", _am) is None)

# The whole retry path through respond(): a complaint after a wrong answer
# reruns the original request. Here that request is the briefing, which
# needs no model, so no Ollama is touched.
b = brain.Brain.__new__(brain.Brain)
answer, _ = b.respond([dict(m) for m in convo])
check("complaint reruns the briefing", answer.startswith('You asked: "tell me the briefing"')
      and "It's " in answer, answer[:100])

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
