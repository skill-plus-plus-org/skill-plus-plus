---
description: Save the work just done as a skill candidate, without ending the session
---

# Skill Plus Plus — keep

The developer has decided that what just happened is worth keeping. Bank it now.

```bash
python3 bin/skillpp keep
```

That folds the session buffer as it stands, cuts it into episodes, and records
each as a candidate marked `kept`. The buffer is cleared, so the rest of the
session accumulates fresh rather than being folded twice.

**Guards do not apply here.** Capture normally discards work with nothing to show
for itself — no completion marker, or nothing but reading around. Those rules
exist to stop a detector banking noise, and they should not overrule someone who
has read the work and asked for it.

## Then name it

A candidate is titled with whatever was typed, which is rarely what the task
should be called. Name it while the work is fresh:

```bash
python3 bin/skillpp draft <id> --apply
```

That gives it a task-shaped name and a description saying when it applies, and
writes a draft `SKILL.md` — without installing anything.

## Why this exists

`SessionEnd` is otherwise the only thing that folds, so there was no way to keep
something without closing the session. And recurrence — the automatic route to a
candidate — has never fired on real work, which leaves an explicit save as the
shortest path from doing something to having a skill for it.
