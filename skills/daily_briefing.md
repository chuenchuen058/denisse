---
name: daily_briefing
description: How to give a quick daily briefing/check-in
---

# Daily briefing

When asked for a briefing, check-in, or "what's up today":

1. Call `get_current_time` to anchor the briefing in the real date/time.
2. Call `list_known_facts` to recall what you know about the user's
   ongoing projects, preferences, and commitments.
3. Summarize in 2-4 short spoken-friendly sentences (this is often read
   aloud in voice mode) -- no bullet lists, no headers, just plain
   conversational sentences.
4. If there's nothing relevant remembered yet, say so plainly and ask one
   good question to learn something worth remembering (then use
   `remember` to save the answer).
5. Do not fabricate calendar events, emails, or tasks -- Denisse doesn't
   have those tools yet. Only report what you actually know.
