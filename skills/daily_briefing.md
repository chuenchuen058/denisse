---
name: daily_briefing
description: How to give a quick daily briefing/check-in
---

# Daily briefing

When asked for a briefing, check-in, or "what's up today":

1. Call `get_current_time` to anchor the briefing in the real date/time.
2. Call `get_news` (no topic, for general top headlines) and weave 2-3 of
   the real headlines into the briefing in your own words.
3. Call `list_known_facts` to recall what you know about the user's
   ongoing projects, preferences, and commitments.
4. Summarize in 2-4 short spoken-friendly sentences (this is often read
   aloud in voice mode) -- no bullet lists, no headers, just plain
   conversational sentences.
5. If there's nothing relevant remembered yet, say so plainly and ask one
   good question to learn something worth remembering (then use
   `remember` to save the answer).
6. Do not fabricate calendar events, emails, or tasks, or invent news
   headlines `get_news` didn't actually return -- only report what you
   actually know or were actually told by a tool.
