# Benchmarks

Every earlier measurement in this repo used fixtures written by whoever was also
writing the detector, which tests internal consistency more than anything else.
This corpus is written the other way round: from what the work actually looks
like, in two domains, with the answer decided before the pipeline was run
against it.

    python3 tests/benchmarks/run.py --no-model      # segmentation, free
    python3 tests/benchmarks/run.py                 # + ranking, needs Ollama
    python3 tests/benchmarks/run.py --kind productivity
    python3 tests/benchmarks/run.py --json          # for tracking over time

## What is in it

14 cases — 8 programming, 6 productivity — in `tests/benchmarks/cases.py`.

**Programming** is Bash-heavy with closing markers the segmenter recognises:
releasing a service, rotating a credential, onboarding a repository, a migration
that fails on a lock and is worked around, an exploration ending in a one-line
fix, an investigation that concludes nothing, two unrelated tasks in one sitting,
and a hotfix for one specific crash.

**Productivity** is MCP-shaped, where no `git commit` ever arrives and the only
boundary is the next request: the weekly status email, monthly expenses, support
triage, meeting prep, answering one question, and reading around without
deciding anything.

**Four cases are negative** — two that should bank nothing, and several that
should bank real work and rank it low. Without those, a detector that keeps
everything scores perfectly.

## Two scores, kept apart

They fail for different reasons and cost different amounts.

**Segmentation** — given a session, does it bank the right number of candidates?
Free, deterministic, and run inside the unit suite, because a wrong episode count
is the one error nothing downstream recovers: merged episodes hide procedures
inside each other, split ones destroy the recurrence count, and an episode that
should not exist becomes a candidate titled after whatever question started it.

**Ranking** — of the candidates banked, are the reusable ones marked `method`?
Needs a local model, so it is a benchmark rather than a test. Scored only on
cases that segmented correctly, since ranking episodes that should not exist
measures nothing.

## What it found immediately

**Segmentation 12/14 → 14/14.** Both misses were sessions that should bank
nothing and banked one. `segment.py` only flagged trailing work when a session
split into several episodes, so a session that was *entirely* exploration sailed
through. An exploration-only guard fixed both. It also required teaching
`is_read_only` about MCP verbs — `search_messages` and `get_event` look,
`send_message` and `append_rows` do not — as a prefix heuristic that treats
anything unrecognised as work, so a wrong guess keeps an episode rather than
discarding one.

**Ranking 7/12, and the misses were a clean domain split:**

| Domain | Methods correctly ranked |
| --- | --- |
| Programming | 4 of 5 |
| **Productivity** | **0 of 4** |

Every productivity procedure — weekly email, expenses, triage, meeting prep —
was ranked `one-off`. The cause was in the prompt, not the model: every example
in `prompts/reusable.md` was an engineering one ("filing a defect, cutting a
release, rolling out a service, rotating a credential, onboarding a
repository"), so the model reasonably inferred that a method is an engineering
procedure. The examples now span both domains and the prompt says outright that
the domain and the tooling are irrelevant.

That is the whole argument for having a corpus that is not written by the same
hand as the detector. A programming-only benchmark would have scored this bug at
100%.

## Ground truth is deliberately coarse

`episodes` is how many candidates a correct run banks; `methods` is how many of
those a person would follow again. Anything finer would encode the current
implementation's opinions as truth, which is how a benchmark stops being able to
find anything.

A case marked `methods=0` with `episodes=1` is not a detection failure — it is
real work that happened once and should be ranked low, not discarded.

---

## Scored against `feat/pattern-detection`

The runner takes `--repo`, so the corpus can be pointed at any checkout exposing
the three hook handlers and a `Ledger`. Segmentation only — ranking is this
branch's concept and that one has no equivalent.

| | this branch | `pattern-detection` |
| --- | --- | --- |
| Segmentation | **17 of 17** | **12 of 17** |
| Multi-task sessions | **3 of 3** | **0 of 3** |

Its five misses are all one shape: it banks exactly one candidate per session.
That is right whenever a session held one task, and wrong the moment it held two
— it merged a release with a CI bump, three morning tasks into one, and a weekly
update with the expenses that followed it. The two sessions that should have
banked nothing each banked one.

**The first version of this corpus could not see that.** 13 of its 14 cases held
a single task, where banking one entry is correct by construction, and
`pattern-detection` scored 11 of 14 — a detector that does no segmentation at
all, looking respectable. Three multi-task cases were added for that reason, and
they are what separates the two designs.

A corpus that cannot distinguish *segments correctly* from *never segments* is
not measuring segmentation. Worth re-checking whenever a case is added.

## What the productivity half found in this branch

Two defects that the programming cases could not reach, because both are about
work that never produces a `git commit`.

**A finished task was discarded for lacking a marker.** `two-chores-one-sitting`
drafts the weekly email, then does the expenses. The expenses episode ended at
session end with no marker — MCP work never produces one — and the flagging rule
threw it away as work that trailed off. So *any session whose last task was a
productivity one lost that task.*

The rule now only flags a trailing markerless episode if it was **entirely**
looking around. An episode that changed something finished, whether or not a
regex can see it, and pure exploration is caught by its own guard regardless of
how it ended.

**One test had to change its mind, and that is worth recording.**
`test_a_single_episode_session_is_never_flagged` asserted that a lone episode is
never flagged, on the reasoning that a session which did one thing needs no
artifact to be believable. Its example was `kubectl logs` then `kubectl top` —
pure reading. The rule is right for work and wrong for looking around, so it is
now two tests: a single episode that *did something* is not flagged, and one
that only looked around is. The old assertion was load-bearing for the two
"nothing here" cases failing.

Extending the read-only vocabulary along the way — `kubectl top`, `explain`,
`version`, `docker inspect` — was found by that same test failing for the right
reason.

---

## Ported from `feat/pattern-detection`

Two things were worth taking, and one of them turned out not to be what it
looked like.

### Windowing: the principle, not the module

`window.py` splits a *transcript* into pieces small enough for a local model to
read. Its unit is a developer request — which is already this branch's episode
boundary — so porting it would have added nothing to segmentation. It also sits
unwired on that branch, built to feed a local writer that was measured out of
reach.

What did transfer is its measurement: prompt-to-prompt segments run a median of
290 tokens and p90 of 1,458, so **a procedure is a small number of steps.**

That diagnosed `big` properly. Segmentation was producing **nine episodes of
~61 steps**, split only at prompts because no completion marker fired in 500
steps; lexical matching then merged eight of them into one candidate at ×8. Not
one defect but two.

`max_markerless_steps` (25) flags a long stretch that has nothing to show for
itself. Conditioned on the absence of a marker on purpose — *length is not the
failure, never finishing is*, and a fifty-step migration ending in a commit is
one recipe. `big` now banks **1 candidate instead of a 61-step blob at ×8**.

### Embeddings: a tie-breaker for one band

Lexical similarity is robust to everything realistic. Measured against a release
procedure repeated with variation:

| Variation | Lexical |
| --- | --- |
| Same procedure, later version | 1.000 |
| One edit added mid-procedure | 0.897 |
| One step reordered | 0.880 |
| Two exploration steps prepended | 1.000 *(after the trim)* |
| **One step served by a different tool** | **0.786 — misses** |
| Wrapped in a script the second time | 0.207 *(arguably correct to miss)* |

One shape fails: `npm test` against `pytest -q` in an otherwise identical
release. They share not one token and land at 0.786 against a 0.85 threshold —
the worst place for a signal to sit.

`nomic-embed-text` separates that pair at **0.912**, against **0.451** for a
genuinely different procedure. End to end, two sessions doing that release two
ways: lexical 0.747, embedding 0.972, merged to one entry at ×2 — which is what
puts it on the path to a threshold at all.

**It is a tie-breaker, not a replacement.** Only pairs already in the near-miss
band (0.70–0.85) cost a call, so almost every comparison stays free. And it runs
from `skillpp merge`, not from a hook: matching happens during `SessionEnd`, and
a hook that waits on a model adds that wait to every session.

Folding is not symmetrical — the second entry's evidence moves into the first
and the second is deleted — so an unreachable model merges nothing, and a dry
run is the default.

One bug worth recording: the first `fold_into` set
`occurrences = occurrences + 1`, which is the same double-count this project
corrected once before. Occurrences are the size of the **session union**, never
a sum, because two sightings inside one session are one occurrence. A test now
pins both directions.

---

## `feat/pattern-detection`'s real product, finally measured

Every earlier comparison scored only its hook path, which is vestigial by
design. Its actual product reads a transcript with a frontier model, one call per
session, and needed an authenticated CLI to run at all.

`tests/benchmarks/as_transcript.py` renders each case as a Claude Code
transcript, so both designs see the same 17 sessions in their own native input
and the comparison is not measuring an adapter. One `/log-session` call per case,
a **fresh store each time** — the pilot showed why: run two cases against one
store and the second recognises what the first taught it, which measures
recurrence rather than detection.

Scored on the **methods** axis, because that branch records only what it judges
worth keeping, whereas this one banks candidates and ranks them. Comparing its
proposals against our episode counts would be comparing different things.

| | this branch | `pattern-detection` |
| --- | --- | --- |
| Methods axis | **14 of 17** | **14 of 17** |
| Splitting multi-task sessions | **3 of 3** | 1 of 3 |
| False positives on one-offs | none | 1 — a grep sweep recorded as a procedure |
| Missed real procedures | 2 | 0 |
| Over-split | 1 | 0 |
| Naming | the developer's prompt, verbatim | the task, named |

**A tie, with opposite failure modes.** This branch splits sessions correctly and
under-calls methods. That one judges a single procedure correctly and cannot
split a session at all. Its two misses on multi-task sessions and its one false
positive are precisely what its own `docs/bakeoff.md` conceded losing on.

### Naming is the difference that is not a tuning gap

`migration-with-a-lock` is the clearest case. It named the procedure
**`draining-app-replicas-to-clear-a-migration-lock`** — identifying that the
reusable knowledge is the workaround, not the incident. This branch titles the
same session `the staging migration is stuck, get it green`.

Others from the same run: `cutting-a-signed-release`,
`rotating-a-database-credential-in-kubernetes`,
`bootstrapping-a-local-dev-environment`,
`compiling-meeting-agenda-and-posting-to-slack`.

A skill's description is the only thing read when deciding whether to load it, so
a correctly-detected candidate carrying a prompt for a name is still dead. This
is structural rather than fixable by tuning: code can only reuse a string it
observed, and a model can name what it read. It is the one defect that has
survived every fix here, and the strongest argument for the two designs being
complementary rather than competing.

### Caveat on this branch's numbers

The 17 of 17 segmentation score followed fixing four defects this corpus found,
so it measures a detector shaped by the corpus. The 14 of 17 methods figure is
the less-tuned one, and it is where the tie is.

---

## Closing the naming gap

The one difference measured as *not* a tuning gap: this branch titled a candidate
with whatever the developer typed, because capture can only reuse a string it
observed. `skillpp draft` now names it, since a frontier reader is already in the
loop there and naming is the half code cannot do.

`skillpp name <id> --title … --description …` writes both back to the ledger, and
the draft prompt does it **before** deciding whether to draft at all — a name is
worth having even on a candidate the agent then declines. The description is
capped at 200 characters, the skill frontmatter limit, because one that will not
fit cannot become a skill and refusing here beats discovering it at promotion.

Measured on exactly the case that motivated it:

| | |
| --- | --- |
| Before | `the staging migration is stuck, get it green` |
| After | `draining-app-replicas-to-clear-a-migration-lock` |
| Description | *When a staging DB migration hangs or fails because the running app holds a lock on the table being migrated* |

It reached the same name `feat/pattern-detection` produced independently, from
the same reasoning: the reusable knowledge is the workaround, not the incident.

The draft's `## Open questions` is the part worth reading. It recorded that the
causal mechanism was never confirmed — only that the migration failed with
replicas up and succeeded at zero — that `--replicas=3` is an observed value
rather than a known-correct one, that staging is fully down between two steps,
and that this was seen once in staging. None of them invented.

## Five defects, all found by running it

Nothing below was reachable from a fixture. Every one appeared the first time
the command was pointed at a live agent, and each was a seam between this code
and that one.

| Defect | Cause |
| --- | --- |
| Agent allowed nothing | prompt said `skillpp show`, tool scope permitted `python3 bin/skillpp` |
| A blocked agent read as a decline | inferred "nothing here" from an absent file |
| Candidate not found | `--root` never reached the agent's own `skillpp` calls |
| Draft written to the wrong place | prompt said `<draft-dir>` and nothing substituted it |
| Draft could not be written at all | `$SKILLPP_DRAFT_DIR` in a sandboxed Bash call is rejected as "Contains expansion" |

The last is the one worth generalising: **an environment variable is fine for a
Python process to read and unusable inside a sandboxed shell command**, because
an allowed-tools pattern cannot be checked against text that is not yet known.
`SKILLPP_ROOT` works for that reason and the draft directory does not — it is
passed as a literal argument in the prompt instead.

Two safeguards earned their place along the way. The decline sentinel meant a
blocked agent was reported as *"most likely blocked rather than unconvinced"*
instead of a considered judgement, twice, on failures that had not been
anticipated. And the agent itself refused to fabricate a draft or to route
around a sandbox restriction every single time — it was the only party in the
loop behaving correctly.

---

## A failed attempt to fix the ranker (recorded so it is not repeated)

`sift` ranks `migration-with-a-lock` and `meeting-prep` as `one-off` when both
are methods. The diagnosis looked easy: one rule in `prompts/reusable.md` reads
*"one broken deploy … finding out why something specific was wrong and fixing it
is `no`"*, which describes the migration case superficially even though the
drain/migrate/restore technique generalises. Two attempts, both worse:

| Prompt | Ranking |
| --- | --- |
| **Baseline** | **12 of 15** |
| Rewritten around "would this save a colleague an afternoon?" | 8 of 15 |
| Baseline plus one narrow workaround exception | 9 of 15 |

The second attempt is the useful one. A single added paragraph, scoped to
workarounds, with a worked example on each side — *draining replicas is a
method; adding a null check is not* — **did not fix its target** and broke
`onboard-a-repository`, `answer-one-question` and `two-chores-one-sitting`, none
of which it mentions. At temperature 0, so that is the added text shifting
unrelated judgements rather than sampling noise.

Which reproduces the lesson `feat/pattern-detection` recorded as its most
expensive: **every auxiliary hint in a prompt gets read as a rule.** Seven
revisions of its locator prompt, each removing a hint the model had started
treating as sufficient on its own.

So the prompt is at a local optimum and these two cases sit on the real boundary
between *this instance* and *this kind of task* — the same boundary `retry` sat
on, unmoved across five attempts in different shapes.

**Why that is tolerable rather than a blocker.** `sift` ranks and never
discards, so both stay in the queue, ranked low and visible. And `draft` already
judges `migration-with-a-lock` correctly — it named it
`draining-app-replicas-to-clear-a-migration-lock` with four honest open
questions. The pipeline has a stage that gets it right; a conservative ranker
costs a position in a list, not the candidate.

**The direction worth trying is the opposite one:** let `draft` correct the hint
when it disagrees with `sift`, rather than making the cheap stage cleverer. The
expensive stage is the one with the context to be right.

---

## Can a local model find the boundaries? Two framings, measured

The proposal: the hook already appends "this happened" to a session buffer, so
let a local model read that log and mark where tasks start, end or are abandoned,
instead of relying on markers and prompt boundaries.

Worth taking seriously — the architecture it assumes is the one this branch
already has, and `feat/pattern-detection` measured a closely related question
(`settled.md`, "did the change work?") at 20/21 and 18/21 on a free 7B.

**Framing decided the result, by a wide margin.**

*Per-step binary* — for each step, "does this begin a new task?" — found **zero
boundaries across three cases.** Its one correct answer was a case with no
boundary, which a detector hardwired to "no" also gets. It missed a boundary the
markers see trivially (`git push` closing an episode).

*Positional* — "which step number begins the second task, or `none`?" — **3 of
5**, including case C, the one shape code cannot split:

| Case | Truth | Got | |
| --- | --- | --- | --- |
| release only | none | none | ✓ |
| release + CI bump | 3 | 3 | ✓ |
| **rollout + smoke (case C)** | **2** | **2** | ✓ |
| migration workaround (one procedure) | none | 2 | ✗ |
| release then unrelated fix | 5 | none | ✗ |

Same lesson as the first Ollama probe on this project: the shape of the question
matters more than the model behind it.

### Why it is still not wired in

The natural gate is "ask only where code found no marker", since that is where
code is blind. Traced through the cases, that gate **fixes case C and breaks the
migration workaround** — both are markerless, so both get asked, and the model
splits the workaround at its `migrate → scale → migrate → scale` repetition.

+1 and −1. And the wrong half is the more valuable one: the workaround is a real
procedure that currently banks correctly as a single candidate, and reading
repetition as a boundary would shatter it.

Patching the prompt against that is the obvious next move and is not being
attempted, because two attempts at exactly that on `reusable.md` an hour earlier
went 12/15 → 8/15 → 9/15, with a single scoped paragraph destabilising three
cases it never mentioned.

### The generalisation worth keeping

Across everything measured on this project, a local model answers questions
**about a span it is handed** and fails at **finding the span**:

| Question | Local model |
| --- | --- |
| Is this session worth reading? | works |
| Did this change land? | 18–21/21 |
| Is this junk or a method? | ranks usefully |
| Are these the same procedure? | 0.972 against 0.451 |
| Where does one task end? | finds it when told one exists; cannot tell whether one does |

The last row is the whole difficulty. A segmenter needs both halves.

### Splitting works, and the restraint matters more than the action

`skillpp split`, reached from the draft prompt, closes case C. Three live runs:

| Input | Should split | Did |
| --- | --- | --- |
| `migrate → scale → migrate → scale → test` | no — one workaround | **no** ✓ |
| helm rollout → smoke test | arguably one | **no**, named "Roll out API to staging and verify with smoke test" ✓ |
| helm rollout → `git log` → write → Gmail draft | **yes, at index 2** | **yes, at index 2** ✓ |

Its reasoning on the third: *"two unrelated procedures glued together by 'and
then'"*. It drafted the first half as
`Deploy the API service hotfix via Helm to staging` and left the second as its
own candidate.

**The first row is the result that matters.** A positional prompt on a local
model split that same sequence at its repetition; the frontier stage did not.
That is the argument for putting the judgement where it can afford to be right,
rather than teaching the cheap stage a rule — which was tried and cost 12/15 →
9/15 on an unrelated prompt.

The second row is a lesson about the corpus rather than the code. That case was
written as a two-procedure case while noting out loud that it was arguable, and
then scored against. Ambiguous ground truth cannot falsify anything; the doubt
should have been the signal not to score it.

**One loose end by design:** the second half inherits the original title and is
not named until it gets its own `draft`. It sits in the queue titled after the
developer's prompt in the meantime.

---

## Ground truth that maintains itself

The corpus above is 17 hand-written cases whose truth was authored by whoever
wrote the detector. That measures internal consistency, and it already hid one
defect: case C was written as a two-procedure case *while noting out loud that
it was arguable*, and then scored against.

`feat/pattern-detection` solved this and its `truth.py` says why: labels read out
of reviews the pipeline already wrote are *"what the fixtures are not, and the
reason three separate defects in this work were invisible until real data."*

Ported as `decisions.jsonl` — append-only, one line per human decision, never
read by the capture path. Statuses are overwritten in place, so without it every
judgement is lost the moment it is superseded.

The line records **the ranker's hint and the person's decision together**, which
is what makes it a measurement rather than history:

| Person did | Means | Scored against |
| --- | --- | --- |
| `promote` | it was a method | the hint at that moment |
| `dismiss` | it was not | the hint at that moment |
| `reopen` | a model parked something they wanted back | a false drop, caught in the act |
| `sift --park` | the model's own act | **never truth** — that would be grading its own homework |

`skillpp accuracy` reports the tally and lists the disagreements. On a seeded
run it correctly surfaced the one that matters:

    agreed 2/3 (67%)
      ranker said one-off · you promoted · drain replicas then migrate

Which is the known miss — the case two prompt rewrites failed to fix — now
recorded from a decision rather than from ground truth I wrote.

Decisions made before `sift` ran are counted separately as `unranked` rather than
folded in, and the latest decision per candidate wins, because parked → reopened
→ promoted is one judgement with a history, not three.

---

## Five more cases, and the score went down

The corpus was 17 cases scoring 17 of 17 — while **three shipped capabilities
measured exactly zero on it.** `merge`, `split` and recurrence were all
invisible, recurrence most importantly of all: occurrences count sessions, every
case was one session, so the promotion gate the whole design rests on had never
been tested.

Fixed by giving `Case` a `follow` — a second session played into the same ledger
— and scoring a third axis.

| | before | after |
| --- | --- | --- |
| Cases | 17 | **22** |
| Multi-session | 0 | 2 |
| Axes | 2 | **3** |
| Segmentation | 17/17 | **20/22** |
| Ranking | 12/15 | **15/18** |
| Recurrence | not measured | **1/2** |

### The mistake worth recording

The first version of the two new cases set `episodes` to **what the code
currently does** — 1 for the unsplit deploy-then-email, 2 for the unmerged
release pair. Both passed, and the corpus stayed at 22 of 22.

That is encoding the implementation's opinion as ground truth, which this
document already warns against two sections above, and it is the second time the
same error has appeared here. Corrected to what is *correct*, both fail and the
gaps are visible.

A benchmark that agrees with the code is not measuring the code.

### Both gaps are closed downstream, and verified

**`the-same-release-different-runner`** — capture banks two entries at ×1;
`merge` folds them: lexical 0.747, embedding 0.945, one entry at **×2**. Run
live, not assumed.

**`deploy-then-status-email`** — capture banks one; `draft` splits at index 2,
demonstrated 3 for 3 including the two cases that must *not* split.

So segmentation alone is 20 of 22 and the pipeline handles 22 of 22 — but only
if the commands are run, and the benchmark is right to score the stages
separately rather than blur that into one flattering number.

### The suite now names the gaps instead of tolerating them

`test_the_known_gaps_are_still_exactly_the_known_gaps` asserts the failing set
is *exactly* those two. A suite that silently tolerates a documented gap cannot
tell you when the gap closes, and a stale exclusion is how a benchmark quietly
stops measuring.

---

## The near-miss floor, and moving the check off the hook

The gap above — *"`merge` folds them, run live, not assumed"* — closed one case
and hid the shape of the problem. `the-same-release-different-runner` scores
**0.747** lexically, which is inside the `[0.70, 0.85)` band `merge` already
looks at. It was never evidence that the band was wide enough; it was the one
case that happened to land inside it.

`the-same-release-two-steps-different` is the same procedure with the test
runner *and* the fetch swapped. It scores **0.531**. `merge` returns *zero*
pairs on it — not a wrong verdict, no verdict at all, because the pair never
reaches an embedding. The embedding separates it at **0.925** when finally
asked.

That is the shape behind three sightings of one procedure sitting at ×1 each and
a recurrence threshold none of them reach.

### Why the floor was 0.70, and why that stopped being the right number

The floor is a cost guard, and the cost it guards is not the model — the
embedding is local, free, and sub-second. It is that `merge` is a command a
person types and then waits on: a wide band makes a live run long to read.

The check therefore moved somewhere nothing is waiting on it. `SessionEnd`
records which entry it touched and decides nothing. `SessionStart` spawns a
detached process that runs the same `near_misses`/`same_procedure` comparison at
a floor of **0.40** and writes a report. `skillpp near-misses` reads it.
Folding still needs `--apply`.

`merge` keeps its own 0.70 default, unchanged. Two knobs rather than one
widened knob: the manual command does not have this problem and should not pay
for the fix.

### Measured, full corpus, `gemma3n:e4b` + `nomic-embed-text`

| | before | after |
| --- | --- | --- |
| segmentation | 20 of 22 | **22 of 23** |
| ranking | 15 of 18 | **17 of 20** |
| recurrence | 1 of 2 | **3 of 3** |
| cases | 22 | 23 |

Two runs, 12s each, same corpus, same models.

**Read the ranking row as a denominator change, not an improvement.** Ranking is
scored only on cases that segmented correctly. `different-runner` used to fail
segmentation and was excluded; it now segments right, enters scoring and passes.
Numerator and denominator both +2. The ranker is untouched by this work and its
own misses — `meeting-prep`, `three-tasks-one-morning` — are unchanged.

Per-case, exactly two things moved: `different-runner` went from failing all
three axes to passing all three, and the new case passes all three. **Nothing
else changed on any axis.** `deploy-then-status-email` remains the one
segmentation gap, untouched.

### What was deliberately not built

Stated because each was considered and rejected on a reason, not overlooked:

- **No lockfile around the background pass.** It reports and never folds, so two
  racing passes recompute the same free local answer and the later write wins.
  Waste, not a wrong result — and a stale-pid reclaim mechanism is real
  complexity bought for a cosmetic problem.
- **No atomic write on the report.** `load_near_miss_report` already treats an
  unparseable file as "no report yet", so a crash mid-write costs one deferred
  check.
- **No shortened per-call embed timeout.** Nothing waits on this pass, so a
  precisely-enforced deadline buys nothing.

The queue drain *does* write atomically. Losing track of what still needs
checking, silently, is the one failure here with a cost nothing downstream would
report.

### Fail-safe, measured

One unreachable-host answer stops the whole pass rather than rediscovering the
same outage once per pair: **0.13s** against a closed port, queue byte-identical,
ledger untouched. Neither a timeout nor a dead model drains the queue, so a
partial pass retries the whole backlog instead of dropping the pairs it never
reached.

### The fold no longer waits to be asked

The queued pass above wrote a report and a person ran `--apply`. That gate was
redundant with one already further down the pipeline, and removing it cost
nothing measurable.

`fold_into` keeps **both** entries' intents and variants on the survivor rather
than discarding the loser's evidence, and a folded entry is still only a
*candidate* — it has to pass `review`/`show`, which print those intents, and
then an explicit `promote`. So a wrong fold does not vanish: it arrives at
review as a candidate whose intents plainly do not belong together. The person
was always going to look there. Asking them twice bought nothing.

The pass now folds inline, and `decisions.jsonl` records each one with the
dropped entry's id and title — the only place that identity survives once its
file is gone. Deliberately *not* one of `decisions._TRUTH`'s labels: that dict
scores a ranker's hint against a person's verdict, and a fold is neither.
Verified — `skillpp accuracy` reports nothing after an auto-fold.

The report file and `skillpp near-misses` are deleted rather than repurposed.
The report had exactly one reader, the command deciding whether to apply it;
with nothing left to decide there is nothing left to read, and
`decisions.jsonl` is a better record anyway — every pass, not just the last,
kept whether or not anyone runs a command.

**The scoreboard is unchanged, which is the point:**

| | `--apply` era | auto-fold |
| --- | --- | --- |
| segmentation | 22 of 23 | 22 of 23 |
| ranking | 17 of 20 | 17 of 20 |
| recurrence | 3 of 3 | 3 of 3 |

Zero per-case deltas across all three axes. This change moves *when* a fold
happens, not *which* pairs are recognised, and the corpus confirms it.

Fail-safe re-verified after the change: an unreachable model folds nothing,
records no decision, leaves the queue byte-identical, and returns in 0.10s.

---

## The boundary judge: nine configurations, measured

The proposal, again: replace `is_marker`'s vocabulary of git verbs with a local
model asked, on every tool call, whether the task ended there. `6ca48a5` above
recorded the first attempt and did not wire it in. This is the second, wired in
behind `SKILLPP_JUDGE` and measured properly.

**Read this before proposing a tenth.** Every row is a real run against
`tests/fixtures/sessions/`, which is the only yardstick here — the synthetic
corpus was written by whoever wrote the detector, and rewards the opposite goal
shape because its tasks run 3-4 steps against 20+ in real spans.

| what the judge was given | live sessions |
| --- | --- |
| the step alone, no goal, no span | 0 of 5 |
| + the goal and the steps behind it | 0 of 5 |
| + **what an ending is** | this is the whole difference |
| goal = latest prompt only | 0 of 5 — read 54 steps as 27 endings |
| goal = first prompt of the span | 0 of 5 — 54 steps, 17 episodes |
| goal = every prompt in the span, 6 steps of context | 2 of 5 |
| …10 steps of context | 3 of 5 |
| …**20 steps of context** | **4 of 5** — the best it ever reached |
| + the developer's own `description` per step | 2 of 5 |
| + a deterministic prior ("this step only looked") | worse, and broke a passing case |
| + steps rendered as generated summaries | **1 fixed / 6 broken** |

The vocabulary it is trying to beat scores 3 of 5 and costs nothing.

### What each failure was, so it is not rediscovered

**Latest prompt as the goal** — a task is stated across several prompts, so the
last one is a sub-step, and a sub-step is satisfied by a single edit. The goal
named a file, the step touched that file, "delivered?" was honestly yes, every
time.

**First prompt of the span** — once an ending fires the span resets, and the
next span has no prompt in it at all. The goal renders "(not stated)" and the
context renders "(nothing yet)", so the model is asked whether a request it
cannot see is finished. It says yes, which fires another ending, which empties
the next span. A 50-step session became 13 episodes.

**Every prompt in the span** — the shape that works, and it carries a ratchet:
miss one ending and the next prompt joins the same goal, so the question becomes
"is *every* part done" over two tasks and is harder to answer yes than the first
was. Its failures are all `got 1` whatever the truth was.

**Context size is the strongest lever measured.** 6 steps 2/5, 10 steps 3/5,
20 steps 4/5. At 20 the one multi-task live session came out right for the first
time — the work that had finished was simply scrolling out of a smaller window.
It buys nothing on the synthetic corpus because every task there already fits in
six steps, which is why that corpus cannot see this.

**The developer's `description`** — 2/5 against 4/5 without. Tried before the
command and after it; byte-identical results, so not a phrasing effect. The
extra detail itself pushes the model toward "delivered".

**Generated summaries as the judge's context** — 1 fixed / 6 broken, and every
session gained episodes: desk-booking 1→4, failed-retry 1→4, long-session 1→8,
with three losing `must_contain` steps as content scattered across fragments.
The cause is in the summaries: each ends by tying the step to the request —
"fulfilling the developer's request", "informing the developer's next task" —
and the judge reads twenty of those before being asked whether the request is
done. The phrasing that makes a summary readable reads as completion.

Cost: 3.2s per step, and 8.6s on the 110-step session as summaries fill the
window. Two model calls per tool call at that rate is not viable on a hook.

### The one thing the judge has ever done better than code

`241955c7` — two unrelated jobs, one commit — is 1 episode under the vocabulary
against a truth of 2, and the judge gets it right, with content intact, in the
summaries configuration. That is the case this whole thread exists for, and it
is the only one. It cost six correct sessions to buy.

### Where it stands

Wired in, default on, `SKILLPP_JUDGE=0` to disable. `render_step` deliberately
does **not** use `step["summary"]`; see the comment there. The describer stays —
it produces a better record, verified separately — it simply does not feed this
prompt.

Two traps that produced false readings during this work, recorded so the next
person does not pay for them again:

* **Measure warm.** A first call is ~11s and that is the model loading. It put a
  false `think=False` claim in `boundary.py` (11.5s vs 4.2s; warm it is 1.10s vs
  1.09s) and an 11.15s reading for a step that costs 2.49s.
* **`gemma3n:e4b` cannot think.** `think=True` returns HTTP 400. The flag stays
  because it is free here and worth 113.8s against 0.5s on a model that can.
