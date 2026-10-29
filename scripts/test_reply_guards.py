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

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
