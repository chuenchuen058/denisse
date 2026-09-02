---
name: take_notes
description: How to capture notes and facts so they're easy to recall later
---

# Taking notes well

When the user tells you something worth remembering (a preference, a
decision, a fact about their life or work, an ongoing project):

1. Use `remember` immediately, don't wait to be asked. Silent capture is
   better than asking permission for routine facts.
2. Pick short, consistent, lowercase_with_underscores keys, e.g.
   `favorite_coffee`, `current_project`, `sisters_birthday`. Reuse an
   existing key (via `recall` first) rather than creating a near-duplicate
   with a slightly different name.
3. Keep the value itself short and factual -- one clause, not a paragraph.
4. For a fact that will change over time (like "current_project"), overwrite
   the old value with `remember` rather than creating `current_project_2`.
5. When unsure whether something is worth remembering, prefer saving it --
   it's cheap, and `list_known_facts` makes it easy to review/prune later.
