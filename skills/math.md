---
name: math
description: How to answer math questions and word problems accurately
---

# Math

Never compute arithmetic by hand, even something that looks trivial like
`7 * 8` -- local models get multi-digit multiplication, percentages, and
order of operations wrong with real confidence, and there's no reason to
guess when `calculate` gives an exact answer.

1. Translate the question into a plain expression first:
   - "15% of 240" -> `240 * 0.15`
   - "what's 340 minus a fifth of it" -> `340 - (340 / 5)`
   - "square root of 144" -> `sqrt(144)`
   - "12 factorial" -> `factorial(12)`
2. Call `calculate` with that expression. Never present your own mental
   arithmetic as the answer, and never round or "simplify" the tool's
   result yourself -- read it back as given.
3. For a multi-step word problem, it's fine to call `calculate` more than
   once (one call per step) rather than trying to build one giant
   expression that's easy to get wrong.
4. If the question is ambiguous (e.g. "what's 10% of 50 of 200" -- of
   which number first?), ask which order they mean instead of guessing.
5. Answer in one spoken-friendly sentence -- state the result plainly, no
   need to show the expression back unless it helps them follow the
   steps of a multi-step problem.
