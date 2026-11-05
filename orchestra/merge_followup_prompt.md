# Merge the work of {slug} onto current master
Queue: auto
Priority: {priority}
Weight: {weight}
Kind: work
Lane: {lane}
Merge-Of: {slug}
Written: {written}

`{slug}` ("{title}") PASSed its gate on branch `{branch}`, but the runner could not merge
it: {why}. This is a merge-only follow-up made by orchestra/conflicts.py. Do not redo the work.

1. Your branch starts from current master. Run `git merge --no-ff {branch}`.
2. Resolve every conflict keeping both sides: what master has now and what the branch adds.
   Both are wanted. Files changed on both sides: {both}.
3. Re-run the gate below and fix only what the merge broke. If the two sides really cannot
   both be kept, say which side you dropped and why under ODD; if the gate cannot pass
   without dropping the branch's work, report PARTIAL.
4. Report PASS once the gate passes. Do not push.

Files the branch touches: {touched}.

## Gate
{gate}
