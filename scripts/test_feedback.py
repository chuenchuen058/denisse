"""What counts as his feedback -- learning._CORRECTION and learning._THANKS.

Several phrases are his own short feedback from 28-29 Oct -- the ones the
first version missed -- and carry nothing private. Run after touching
either pattern, or brain._COMPLAINT.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import brain  # noqa: E402
import learning  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def bad(text):
    return bool(learning._CORRECTION.search(text))


def good(text):
    return bool(learning._THANKS.search(text))


# -- he is telling her the last reply was wrong ---------------------------
for text in ("no i meant tell me the briefing", "thats not what im asking",
             "still not what im asking", "bro you clearly did not understand me",
             "you didn't answer my question", "what?", "huh", "what did i just say",
             "do you understand what i said?", "is there a problem?",
             "okay its too long stop talking", "no you are still talking",
             "you're not listening", "wrong", "/bad", "nope, the other one"):
    check(f"correction: {text!r}", bad(text))

# -- ...and he isn't --------------------------------------------------------
for text in ("what time is it", "what's the weather in London?", "no worries",
             "no rush", "no thanks", "is there a problem with my code?",
             "do you understand french", "tell me a joke", "okay", "yes please",
             "stop talking", "plan my study modules"):
    check(f"not a correction: {text!r}", not bad(text))

# -- he approves of the reply ----------------------------------------------
for text in ("thats good", "okay that good", "that's it", "perfect", "thanks",
             "thank you denisse", "that worked", "it works now", "exactly",
             "nice one", "yes that's what i wanted", "good", "/good", "well done"):
    check(f"approval: {text!r}", good(text))

# -- agreeing to an offer, or moving on, is not approval -------------------
for text in ("okay", "yes please", "yes open it", "yes tell me", "sure", "alright",
             "good morning", "no thanks", "that's not good", "is that good?",
             "what's a good name for a cat"):
    check(f"not approval: {text!r}", not good(text))

# -- one vocabulary: every complaint she reruns also teaches ---------------
for text in ("thats not what im asking", "still not what im asking", "that's wrong",
             "what?", "huh", "is there a problem?", "you're not listening",
             "you didn't answer my question", "no, not what i meant"):
    check(f"rerun complaint also a correction: {text!r}",
          bool(brain._COMPLAINT.fullmatch(text)) and bad(text))

# -- praise taken back at once is not approval -----------------------------
def convo(*pairs):
    out = []
    for user, reply in pairs:
        out += [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]
    return out


L = learning.label_turns(convo(("tell me the briefing", "It's 11:32 AM."),
                               ("thats good", "Glad to help."),
                               ("what?", "It's 11:32 AM.")))
check("praise then 'what?' -> not good", L[0]["label"] == "unknown"
      and L[0]["reason"] == "praise withdrawn", L[0]["label"])
L = learning.label_turns(convo(("tell me the briefing", "Coursework due: ..."),
                               ("thats good", "Glad to help."),
                               ("set a timer for 10 minutes", "Timer set.")))
check("praise that stands -> good", L[0]["label"] == "good", L[0]["label"])

print()
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
