# The episode filter

Segmentation answers *where* a task ended. It cannot answer whether the task was
worth keeping, and the difference is arithmetic rather than tuning: **a partition
cannot discard anything.** Cut a session more accurately and every step still
lands in some episode, so the material is unchanged. Measured elsewhere at 0%
reduction on real sessions. Meanwhile the premise of the whole tool is that most
sessions contain nothing worth keeping.

So something has to be able to say no. That is `skillpp sift`.

## The question it asks

Not *"did this finish"*. Finishing is easy to detect and tells you nothing — a
one-off fix ends in a commit exactly like a release procedure does. The question
is:

> Would someone follow these same steps again for a different case?

One episode in, one word out (`prompts/reusable.md`). That shape is the whole
reason a small local model can do it: the same model that returned
`task_count=40` for a two-task session answers a single yes/no correctly.

## Why it is not in the hook

A hook that waits on a model adds that wait to every session and breaks when the
model is not installed. `sift` is a command you run, and it is a dry run unless
`--apply`.

## The fail-safe direction

This is the only step in the pipeline that can throw work away, so every
uncertain answer **keeps** the episode:

| Outcome | Result |
| --- | --- |
| `yes` | kept |
| `no` | parked as `one-off` |
| model unreachable | **kept**, counted under *no opinion* |
| answer neither yes nor no | **kept**, counted under *no opinion* |

An episode wrongly kept costs one line in a review list. An episode wrongly
dropped is never seen again. *No opinion* is reported separately and never folded
into "method" — a dead daemon must not read as "none of this is a procedure".

Nothing is deleted. `one-off` is a status, `skillpp reopen <id>` reverses it, and
`ready()` already gates on status so a parked entry leaves the review queue
without any further wiring.

## Measured

**Four hand-built cases, `gemma3n:e4b`, 1.3–3.5s each — 4 of 4.** Including the
two that matter: a grep sweep ending in a commit was rejected (the case both
competing designs got wrong), and a deploy runbook with no commit at all was
kept, so the filter is not just proxying for "was there a marker".

**The six scenarios, replayed end to end — 4 of 6 became 5 of 6.**

| Scenario | banked | after sift | |
| --- | --- | --- | --- |
| `refine` | 1 | kept | ✓ |
| `retry` | 1 | **dropped** | ✗ **false drop** |
| `distinct-tasks` | 2 | kept 2 | ✓ |
| `explore-then-fix` | 1 (10 steps) | dropped | ✓ |
| `mid-investigation` | 1 | dropped | ✓ fixed a false positive |
| `recurs` | 1 at ×2 | kept, ×2 intact | ✓ |

**Three real candidates from a live ledger — 3 of 3 parked**: a pasted chat
message, an entry titled with a developer's prompt, and a `git add` line. All
three are the junk that made the review queue useless.

## The known miss

`retry` — *"the staging migration is failing, get it green"*, six steps ending in
a passing test — was dropped. It carries reusable knowledge (scale to zero
first, then migrate), so a person would likely keep it. That is a **false drop**,
the expensive direction, and the fail-safe does not catch it because the model
was not uncertain; it was confidently wrong.

Fixing one failing migration genuinely is one particular job, so the prompt is
not obviously wrong either — the boundary between "this instance" and "this kind
of task" is where the remaining error lives. Worth more cases before trusting
`--apply` unattended.

## Still open

- **n is small.** Ten cases total, six of them synthetic and written by someone
  who knew the answer. The three real ones are the only unbiased evidence.
- **One model.** `gemma3n:e4b` only. Whether a 1.2B holds up here, as it did on
  the coarser triage question, is untested.
- **No recurrence interaction.** An episode parked on its first sighting never
  gets to recur. Whether a second sighting should reopen it — as a recurring
  ignored workflow does elsewhere — is undecided.
