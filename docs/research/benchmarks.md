# Benchmarks

> Part of the [research log](README.md), which says where things stand now.
> The sessions cited below by tag are a private set, not in the repo.

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

`max_markerless_steps` (25) flagged a long stretch that had nothing to show for
itself. Conditioned on the absence of a marker on purpose — *length is not the
failure, never finishing is*, and a fifty-step migration ending in a commit is
one recipe. `big` banked **1 candidate instead of a 61-step blob at ×8**.

That rule has since been removed — see *The length rule is gone* at the end of
this file. `big` is a synthetic case and is no longer evidence; the live
sessions say the rule cost three ledgers and saved none.

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

**Historical**, like the section that follows it: this measures the judge that
was asked on every tool call. What ships now asks once per prompt gap — see
*The question moved* at the end of this file. The configurations below are still
the record of how the context, the goal and the span were sized, and several of
those findings carried over.

The proposal, at the time: replace `is_marker`'s vocabulary of git verbs with a
local model asked, on every tool call, whether the task ended there. `6ca48a5` above
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
session gained episodes: expense-report 1→4, failed-retry 1→4, long-session 1→8,
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

### The tenth and eleventh configurations, and what actually helped

Two more runs, both on the same ten live sessions (`263d65ce` skipped — it is a
third of the corpus by step count and the slowest by far).

| | fixtures | what the judge reads | result |
| --- | --- | --- | --- |
| A | as recorded | raw commands | 3 fixed / 5 broken |
| B | re-extracted | raw commands | **3 fixed / 3 broken** |
| C | re-extracted | generated summaries | 3 fixed / 5 broken |

**B is the best the judge has ever scored**, and the improvement has nothing to
do with the describer.

*Re-extracted* means the fixtures were rebuilt from their original transcripts
with the current `_KEEP_INPUT`, which now stores `content` for a `Write`,
`old_string`/`new_string` for an `Edit`, and a bounded `tool_returned` for
everything. Those reach the judge through `render_step`'s leftover-field
rendering. That widening was made for the describer's benefit and never measured
against the judge; it is worth two sessions on its own:

    71448e61   BROKE -> ok
    d5fd2e59   BROKE -> ok

*Summaries* then give one session back and lose three:

    241955c7   still wrong -> FIXED
    71448e61   ok -> BROKE
    a7be1ef5   ok -> BROKE
    fb505861   FIXED -> still wrong

This is with the summary prompt rewritten to its minimal form — tool, input,
reply, and "in one sentence of at most 20 words, say what that did; describe it,
do not continue or reproduce any content shown above". That prompt is a large
improvement on its predecessor as a *record*: 87-141 characters, 1.7s per step,
no fabrication, where the previous one produced 2,182 characters and invented
eight topics that were not in the file it was describing. It still does not help
the judge.

So: capturing more of each step helps. Describing each step does not. The
describer earns its place as a record and has never earned it as judge input.

### A failure mode the score hides

`a8b61dae` reports `0/1` under summaries — not a wrong episode count, **no
candidate at all**. The judge marked zero endings across 26 work steps, so
`segment` produced one markerless episode, `max_markerless_steps` (25) flagged
it, and `fold_session` dropped it. One step over the threshold and the session
disappears with nothing to review.

That threshold was calibrated when "markerless" meant "no git verb appeared".
Under the judge it means "the model said no 26 times", which is a different
claim. Worth re-deriving before the judge is trusted by default.

`judge_replay.py --verbose` was no help here: it prints only steps the judge
called endings, so a run with zero endings prints nothing and looks identical to
a run that never executed.

### The length rule is gone

Removed `max_markerless_steps` outright. Two measurements, both against the live
sessions:

**Deterministic path — the rule never fired.** `score.py` over all eleven
fixtures is byte-identical with it on and off:

```
rule ON  (25):  6/6 sessions pass, 5 known gap(s)
rule OFF  (0):  6/6 sessions pass, 5 known gap(s)
```

Every session long enough to trip 25 steps — `1c3c9422` (54), `2095a8af` (50),
`263d65ce` (110) — contains a `git commit`, so `has_marker` is true and the rule
skips it. It has never once fired on real captured work.

**Judged path — the rule was the whole remaining deficit.** `judge_replay.py`
at `_VALUE_CHARS=80`, ten sessions, `263d65ce` skipped:

| session | work steps | rule ON | rule OFF |
| --- | --- | --- | --- |
| `1c3c9422` expense-report | 54 | **BROKE** 0/1 | ok 1/1 |
| `2095a8af` timesheet | 50 | **BROKE** 0/1 | ok 1/1 |
| `a8b61dae` failed-commit-then-retry | 28 | **BROKE** 0/1 | ok 1/1 |
| `5c7b0f81` coverage-writeup-run2 | | FIXED | FIXED |
| `95b6bde7` mcp-retrieval-then-compare | | FIXED | FIXED |
| `fb505861` coverage-writeup | | FIXED | FIXED |
| `71448e61`, `a7be1ef5`, `d5fd2e59` | | ok | ok |
| `241955c7` two-unrelated-tasks | | still wrong 1/2 | still wrong 1/2 |
| | | **3 fixed, 3 broken** | **3 fixed, 0 broken** |

All three broken sessions are the same shape, and it is the one the section
above describes: judge marks no endings → one markerless episode → flagged →
`foldable` drops it → **empty ledger**. Not a wrong count, no candidate at all.
Every one of the three is over 25 steps; the three that already worked are all
under it.

This is the first configuration where the judge beats the vocabulary outright.

**What the removal gives up.** The rule was written for a shape this corpus does
not contain: the 433 KB session that produced nine ~61-step markerless episodes.
That measurement is real, but it came from a synthetic case, and no live fixture
reproduces it — `263d65ce` is the closest by size and it ends in a marker. The
trade is deliberate: a defence against an unrepresented shape, for a fix to three
represented ones. Nothing in the test suite covered the rule, which is part of
why it survived this long.

If that shape ever shows up in a real capture, the fix is not a length cap. It is
that the judge found no ending in sixty steps, and the length cap only hid it.

**Untouched by this.** `241955c7` still merges two unrelated tasks into one
episode — a genuine judge miss, and the sign that the removal is not papering
over judge errors. The trailing-session-end flag stays; it is conditioned on
what the work *did*, not on how long it ran. The read-only flag went next — see
below.

### Reading is not evidence that nothing happened

`95b6bde7` is a real session: pull the ADK reference docs through MCP, compare
them against `cases.json` and `generate_fixtures.py`, report. It banked **one
candidate — the right count — containing none of the retrieval the procedure
exists for.** The count being right is what hid it; only `must_contain` could
see it.

```
episode 1  ended_by=prompt  flagged=TRUE   work=4   <- dropped
     mcp__adk-docs__list_doc_sources
     mcp__adk-docs__fetch_docs  llms.txt
     mcp__adk-docs__fetch_docs  evaluate/index.md
     mcp__adk-docs__fetch_docs  criteria/index.md
episode 2  ended_by=session-end  flagged=false  work=6   <- banked
     the comparison, titled after the second prompt
```

A mid-task prompt (*"compare that against how it actually works here"*) cut the
session, leaving the four retrievals alone in a read-only episode, which the
all-read-only flag then discarded. `trim_leading_exploration` was not at fault —
its MCP exemption works; a second rule defeated it one stage later.

**Four variants, measured across all eleven sessions.**

| variant | `95b6bde7` | corpus |
| --- | --- | --- |
| baseline | 1/1, **missing** `mcp__adk-docs` | 6/6, 5 gaps |
| A — delete the flag only | **2/1**, still missing | 6/6, 5 gaps |
| B — absorb read-only forward | 1/1, kept all | 7/7, 4 gaps |
| **C — both (shipped)** | **1/1, kept all** | **7/7, 4 gaps** |

A fails for a reason worth writing down: `must_contain` is checked against the
**largest banked** candidate, so un-flagging rescues the retrieval episode from
deletion but at four steps it loses to the six-step comparison, and the count is
now wrong as well. The retrieval has to end up *inside* the episode the check
reads.

B and C score identically. The only read-only episodes anywhere in the corpus
are three one-step trailers — a status check after the commit, in `2095a8af`,
`a8b61dae` and `d5fd2e59` — dropped by the under-two-steps rule either way. The
corpus cannot distinguish them.

**C was chosen on consistency, not on score.** B leaves two rules contradicting
each other — *read-only work is preamble, keep it* and *read-only work has no
method, drop it* — resolved only by which runs later in `segment()`. And the
flag's sole justification was `reading-around` in `tests/benchmarks/cases.py`:
five hand-authored steps written alongside the detector, exercised by no live
session. Same standing as `max_markerless_steps` above.

**What it gives up.** An aimless reading session now banks a candidate instead
of vanishing — a row left in the ledger to review rather than a silent discard.
Noise is already filtered without guessing at content: `recurrence_threshold = 3`
means a one-off never reaches `ready`. The corpus's `episodes == 0` negative
cases drop from two to one; the surviving shape, work that concluded nothing
*and* ended the session, is the only one that still banks nothing.

**Scope.** Under the judge this session was never broken — verdicts stop prompts
from cutting, so there was one episode and the retrieval survived. The defect
was on the vocabulary path, which is what runs when the local model is
unreachable.

### The vocabulary stops being a segmenter

`skillpp` segmented two different ways depending on whether a local model
answered. With verdicts, the judge decided. Without them a **parallel** system
took over: cut at every new prompt following two substantive steps, and at every
git completion verb.

That parallel system produced the cuts three separate passes existed to undo —
`_absorb_before_commit`, `_absorb_read_only_preamble`, and
`trim_leading_exploration`'s MCP exemption — each added after a real session lost
work to a boundary nobody wanted. **Now, when nothing judged the steps, skillpp
is offline: it does not segment and does not bank.**

**Measured across the eleven live sessions:**

| | correct |
| --- | --- |
| keep the vocabulary fallback | 7/11 |
| no verdicts, no cuts (one episode) | 9/11 |
| no verdicts, nothing banked, fixtures unjudged | 0/11 |
| **no verdicts, nothing banked, fixtures carrying verdicts** | **10/11** |

The 0/11 row is an artefact, not a result. Every fixture is a projection of a
Claude Code transcript, and a transcript has no `end` field — so the corpus was
"unjudged" by construction and scored zero against a pipeline that requires
verdicts. `judge_replay.py --write` bakes real verdicts in, which is what the
last row measures and what a live capture would have carried all along.

**Baking alone moved the board from 7 correct to 10.** No behaviour changed —
the fixtures simply stopped exercising the fallback and started exercising the
path that ships. Three of the four gaps under investigation that morning were
never defects in shipping code; they were the vocabulary failing on recordings
production would never produce.

What closed:

- `5c7b0f81` and `fb505861` — the same procedure recorded twice. The prompt rule
  cut at *"Write that list to REPORT.md"*, severing the deliverable from the
  investigation that produced it. Two symptoms from one cut, decided only by how
  many steps followed the prompt: one banked a single episode missing the file,
  the other banked two and `must_contain` read the larger one.
- `263d65ce` — 110 steps, never scored against the judge before because every
  replay skipped it for cost. E4B marks exactly **one** ending in 110 steps and
  puts it in the right place: 2/2.

**What stays: 3 endings in 357 steps.** The judge is sparse. That is not by
itself wrong — nine of eleven truths are a single episode — but it is why
`241955c7` remains open, and why `two-chores-one-sitting`,
`two-chores-then-nothing` and two `TestSegmentBeforeAfter` tests are now
recorded as needing a verdict that does not exist yet. Each is the same shape:
two pieces of work with nothing observable between them.

**`has_marker` stays verdict-based.** Making it observational —
`any(is_marker(...))` instead of `any(is_end(...))` — was planned and dropped on
measurement: it breaks `263d65ce` from 2/2 back to 1/2. Under verdicts that
session's first episode counts as concluded because the judge said so; under
observation it has no git commit, so `_absorb_before_commit` folds it into the
one that does. `_absorb_before_commit` asks whether an episode *concluded*, not
whether a commit happened, and a verdict is the better answer to that question.

**Accepted costs.**

- A commit alone never ends an episode; only a verdict does. Measured neutral —
  cutting on verdicts *plus* observed markers scores an identical 9/11, because
  `_absorb_before_commit` re-merges the marker cuts anyway.
- With no model, a session banks nothing. The session file is kept and stamped
  `held` instead of deleted, so being offline costs the candidate and never the
  record, and `skillpp stats` reports what is waiting. Ollama was down twice
  during the day this landed, so the path is not hypothetical.
- The marker vocabulary survives as a **test double**, standing in for a
  reachable model across the suite. That is where a hardcoded heuristic belongs.

### The third repair pass goes too

`_absorb_before_commit` folded every markerless episode forward into the next
one that had a marker. It was written for the prompt rule's damage — on one real
six-turn session that rule *"cut five times and banked five fragments plus the
commit, none of them the procedure"*, and this pass swept them back together.

With the prompt rule gone there are no fragments to sweep. Instrumented across
all eleven live sessions it **never fires once**, the board is identical with it
disabled, and the full suite passes with it stubbed out — 294 tests, no
failures. Deleted.

It is the third pass removed that existed only to repair the vocabulary's cuts,
after `_absorb_read_only_preamble` and the length rule. Each was added after a
real session lost work to a boundary nobody wanted, and each stopped having a
job the moment the thing drawing those boundaries was removed.

**This is also what made `has_marker` look load-bearing.** `_absorb_before_commit`
was its only meaningful consumer, and the reason making `has_marker`
observational "broke" `263d65ce` is that it woke a dormant pass and had it eat a
correct boundary. The two readings are exactly inverted on that session:

```
judge said ENDING at step 98: Write .../article/sections/08-one-ag…

  ep1   87 steps   verdict-based has_marker=True    observed has_marker=False
  ep2   23 steps   verdict-based has_marker=False   observed has_marker=True
```

ep1 is the article work, ending in a `Write` the judge called an ending —
not a git verb, so observation sees nothing. ep2 holds the `git commit`, which
the judge did not call an ending. Neither reading was "the fact"; they answer
different questions, and the pass asking was already dead.

`has_marker` now has one consumer, the trailing-investigation flag.

**Two tests named for the pass were passing vacuously** and have been rewritten
rather than deleted: one asserted a fold that can no longer happen because there
is only ever one episode, the other asserted that trailing work was *spared* by
a pass that never touched it. Both now say what actually holds — a verdict cuts,
and nothing re-merges afterwards.

**A harness that scored green while measuring nothing.** `judge_replay.py`
reported `ok` with `0.00s per step` when Ollama was down: every `judge` call
returned `None`, no verdict was written, and the "judged" row was the vocabulary
row printed twice. It now refuses to start without a reachable model, and aborts
if the model answers none of a session's steps. This is the same failure as the
`--verbose` note above — a success indicator that cannot tell *finished* from
*never started*.

## The per-step judge on `241955c7`: what was ruled out

**Historical.** This is the judge that asked, once per tool call, whether the
request was finished. It was replaced — see *The question moved* below. Kept
because everything here was measured and none of it should be retried.

The last remaining gap at the time. Two unrelated jobs in one sitting — investigate why some
tutorial cards render blank (read-only, nothing written), then separately add
the missing `chart` eval case and commit. Truth is 2 episodes; the pipeline
banks 1, because the judge marks no ending anywhere.

Every step was replayed through `gemma3n:e4b` at `_VALUE_CHARS=80`,
`CONTEXT_STEPS=20`. **0 endings in 24 steps**, including step 24, which is
`git add …cases.json …fixtures.json && git commit -m "$(cat <<'EOF'`.

The boundary the judge has to find is after step 6, the last step of task one.

### The prompt at the boundary, and what is fixed versus inserted

`skillpp/prompts/task_end.md` — since deleted, replaced by `new_job.md` — was 16
lines with three slots. Lines 1, 5, the words "Just now, they", and 10-16 were
constant on all 357 judgements ever made.
`{GOAL}` is every prompt in the span, `{PRIOR}` the last 20 steps rendered by
`render_step`, `{STEP}` the step being judged.

```
- TITLES in page.py can stage tutorial cards whose body has no matching key
  in toolTutorials.ts … Work out which ones, and tell me before changing
  anything.                                    <- inserted: forbids an artifact

Just now, they read the file `… page.py`       <- inserted

A task ends when everything the developer asked for has been produced — a change
that outlasts the session.                     <- fixed: requires an artifact
It has not ended while … they are gathering information, preparing, or
checking their work.                           <- fixed: names this task exactly
```

The request asks to be *told* something before anything changes; the definition
requires a change that outlasts the session and excludes gathering information.
By the rule as written this task can never end.

### Rewording the definition does not help

Four definitions, tested on the steps that discriminate — 6 must be an ending,
3, 5, 14 and 20 must not:

| definition | 3 | 5 | 6 | 14 | 20 |
| --- | --- | --- | --- | --- | --- |
| current ("a change that outlasts the session") | no | no | **no** | no | no |
| "where they asked to be told something, the answer is the deliverable" | no | no | **no** | no | no |
| "the change they wanted made, or the question they asked answered" | no | no | **no** | no | no |
| "working something out and reporting it is a complete task" | no | no | **no** | no | no |

The reason no wording can work is visible in the rendered steps:

```
step 3: read the file `…/backend/acme_agent/page.py`
step 6: read the file `…/backend/acme_agent/page.py`
```

Byte-identical. Step 3 is mid-investigation, step 6 finishes it. The only
difference reaching the model is three extra lines in `{PRIOR}`.

### Nor does more of the step

`render_step` emits `step["input"]` only. Of 1024 characters stored on step 6,
**85 reach the model** — the `tool_returned` (400) and the `closing_note` (400)
are dropped. Supplying them changes nothing:

| shown | 3 | 6 |
| --- | --- | --- |
| as today (8% of the step) | no | **no** |
| + what the tool returned | no | **no** |
| + the completion report ("Scan done. All 4 … resolve to keys") | no | **no** |
| + both | no | **no** |

Note also that the completion report is **not available** when its own step is
judged: `handle_tool` writes `closing_note` onto step *N-1* (capture.py:427) and
judges step *N* (capture.py:462). Showing it would require judging one step late.

### Nor a different question, nor less context

"Is this a finishing step", "Was that the last step of this task" — both score
identically to the current question on all six steps. Removing `{PRIOR}`
entirely, or trimming it to two steps, also changes nothing at step 6.

### Polarity changes everything, which means it is not judging

Same model, same context, same steps. Only the direction of the question
differs — "is every part of the request done" against "is more work needed",
with the second inverted:

```
endings: direct 0/24, inverted 17/24     (truth: 1, at step 6)
```

The inverted question answers "more work needed" for steps 1-7 and "no more work
needed" for **every step from 8 to 24**, without reverting. Step 8 is where
`{GOAL}` first holds two prompts and `{PRIOR}` first holds a finished task.

Neither answer is a judgement of the step. Both are a constant response to the
shape of the question against the shape of the context. This is not sampling
noise: `local.ask` sends `temperature: 0`, and eight repeats of the identical
prompt gave eight identical answers at steps 6 and 24.

### The model is also not self-consistent

Two of the three endings it has ever marked across the whole corpus are the same
command shape as the one it refuses here:

```
a7be1ef5  end=True    git add …cases.json …fixtures.json && git commit -m "$(cat <<'EOF'
241955c7  end=False   git add …cases.json …fixtures.json && git commit -m "$(cat <<'EOF'
```

Trimming `241955c7`'s goal to just "Commit both changes together." still gives
`no`.

### The ratchet, and the bootstrapping trap

`window`'s docstring already records the cost of using every prompt in the span:
*"miss one ending and the next prompt joins the same goal … That is why its
failures are all `got 1`."* `241955c7` is that cost arriving.

The span is *steps* since the last `end: True`, so with zero endings nothing
ever resets. By step 8 the goal is both jobs welded into one request and
`{PRIOR}` presents task one's six investigation steps as "what they have done on
it so far"; by step 24 the goal is all four prompts.

**Context reset is already implemented** — `window` starts the span after the
last ending. What is missing is any way to recover once an ending is missed.
The reset is gated on a verdict, and the verdict is the thing that fails.

Measured separately: contamination is *not* why step 6 is missed. Step 6's
context is clean — one goal bullet, five prior lines, all task one — and it is
missed anyway. The ratchet is a second, downstream problem that begins at step 8.

### Where it went next

Not another model: the constraint was deliberate and this one turned out to be
capable — of a different question. Two of the three directions listed here were
taken, and the third was not needed.

**Re-judging at session end** was the unlock. Every intervention above is
forward-only, and the one formulation that ever answered `yes` at step 6 —
asking, when the next prompt arrives, whether it starts a new task — needs
information that does not exist yet at step 6.

**A first, untuned prompt-pair form scored 5/11**, over-firing on continuations:
it cut `fb505861` at "Write that list to REPORT.md" and shattered `263d65ce`
into nine. That was the starting point, not the answer.

**Whether an ending must be closable only by the judge** never had to be
answered. It stopped mattering once the judge fired where it should.

## The question moved

Asked in a different place, of a different thing, the same model gets the corpus
right. **Only ask where a boundary can be** — a gap between two tool calls that
an instruction landed in — and make it a comparison rather than an assessment:
here is what they asked for, here is what they just said, here is what they did
next; is that a new job?

```
                                              correct   model calls
per-step "is the request done"                 10/11        357
gated, {NEXT} = 1 step                          7/11         33
gated, {NEXT} = 3 steps                        11/11         33
gated, {NEXT} = 5 steps                         8/11         33
gated, majority of 5 at temperature 0.7        10/11        165
```

`{NEXT}` is a window, not a knob. One step cannot tell two jobs apart — `find
cases.json` belongs to either. Five reaches far enough into the next task to
echo the old one. `{PRIOR}` at 3 beats the old 20: a long history made every
late gap read as a continuation, proved by swapping the prompt text between an
early and a late gap while holding everything else — **the verdict followed the
position, not the words.**

Temperature buys nothing. Voting scores the same and, at `{NEXT}`=2, abandons
the correct boundary in `241955c7` (2/5) to cut in the middle of task two, while
still scoring 2/2.

### Every slot earns its place

Ablated across all eleven sessions:

| | correct |
| --- | --- |
| full | **11/11** |
| without the `Then they say: {PROMPT}` block | 8/11 |
| prompt and goal only, no steps at all | 4/11 |
| `{GOAL}` cut to the latest prompt | 10/11, `1c3c9422` splits into three |

The signal is a conjunction. The instruction says what the developer *intends*;
the following steps say what actually *happened*. Without the instruction,
`241955c7` cuts at "Regenerate the fixtures" as well as at the announced switch — it
cannot tell which gap matters. Without the steps, it fires in 25 of 33 gaps.

An empty `{PRIOR}` must render `(nothing yet)` rather than a blank block: on
`95b6bde7` that alone flips the verdict. Deleting the section entirely fails the
same way. A session with nothing behind it reads as one that has not started.

Note the tension with the ratchet above — the accumulated goal helps here and
ruins the per-step judge. The difference is that this is asked three times a
session, not 357, so the goal never grows far.

### Two bugs the score could not see

**The old judge cut `263d65ce` at step 98** — deep inside the article work — and
scored 2/2 for it, because the count was right and both `must_contain` needles
landed in the larger half. The first task ends at **step 9**. On placement the
per-step judge was 1/2 and the gated one is 2/2, so "10/11 either way" was
flattering it.

`score.py` now pins `truth.boundary_after` on the two multi-task fixtures. It
is the fourth measurement this year that agreed with a wrong answer, after
counting pre-fold episodes, the harness printing `ok` with Ollama down, and a
`min_steps` guard silently swallowing a bad cut in `95b6bde7`.

**`gaps()` showed only the first prompt in a gap.** `1c3c9422` is the only gap
in the corpus holding two, and it was the only gap that failed:

```
1. a question about how the agent had gone about it
2. the opening request, restated: look the format up in the docs rather than
   answer from memory
```

Only the first reached the model, which read it as a new job. Measured on that
gap: first alone → new job (wrong); last alone, or both in order → same job.
That session is the *before* of the procedure `2095a8af` and `71448e61` record —
the agent answered from sixteen local `grep`/`Read` calls without touching MCP,
was challenged, and the request was restated. Both later sessions open with the
restated wording. Provenance that is not recoverable from the steps, and without
it those two prompts look like a topic change rather than one correction.

### What it costs and what it gives up

One call per prompt instead of one per tool call, and **off the hot path**: the
judge runs at `SessionEnd`, because the question needs the steps that came after
a gap. That removes a synchronous ~1.5s model call from every tool call a
developer makes. `skillpp keep` folds mid-session, so it judges the buffer first,
and banks the work as one task if no model answered — an explicit save is a
person saying "save this", not a detector guessing.

Given up: **a boundary with no prompt in the gap** — a task ending where the
developer says nothing. No live session shows that shape, and the per-step judge
could see it in principle, so this is a trade rather than a free win.

### Framings rejected, and what a call costs

Moved here from `skillpp/boundary.py`, so that it is not tried again. On a
13-case probe of the gap question:

- the step alone, with no goal and no span: 4/13, and every correct answer was a
  "no" — it answered "no" to everything, as the first attempt at this did;
- the same plus the goal and the steps behind it: also 4/13, same shape. The
  context alone changes nothing;
- adding what an *ending is*: 11/13. This is the whole difference;
- adding a deterministic prior ("that step only looked things up") for the model
  to confirm or override: 10/13, and it broke a case that had been passing. Not
  kept.

Latency, measured warm on `gemma3n:e4b`, the default until 2026-09:

- the one-word answer instruction took a call from 4.42 s to 0.49 s, because
  generation length dominates, not prompt processing; ~0.73 s per call with the
  full context prompt;
- `think=False` changed nothing on that model, which cannot think: 1.10 s
  against 1.09 s over three runs each. An earlier 11.5 s was the model loading,
  and `think=True` returns HTTP 400. On a model that can think, the same
  one-word question took 113.8 s against 0.5 s (`qwen3.5:9b`, `local.ask`),
  which is why the judge keeps thinking off on `gemma4:e4b`.

## gemma4:e4b as the judge: more to read, one input at a time

Measured 21 Sep 2026 on the 21 live sessions, `tests/benchmarks/judge_replay.py`
with `SKILLPP_MATCH=0` so a cut episode is never merged back before it is
counted, one model resident, every run valid (45/45 gaps answered).

**Read the gaps, not the sessions.** Only 2 of the 45 gaps are real boundaries
(`241955c7` step 6, `263d65ce` step 9). A judge that answers "no" everywhere
scores 19/21 — gemma3n's score, and gemma4's at the default settings, where it
answers "no" to all 45. A judge that cuts exactly at the two scores 21/21. Both
checked with no model before any run was trusted.

### Thinking off: each input alone, against the defaults

| dimension | values | effect |
|---|---|---|
| steps before the gap | 4, 5, 6, 8, 10, 15, 20, all | none — all history for both boundaries changed nothing |
| steps after the gap | 4, 5, 6, 7 | none; one false cut at 6 (`263d65ce` step 31), gone at 7 |
| the developer's prompt | 800, 1600, full | none (2 of 45 prompts exceed 400) |
| other step fields | 200, 400, full | none |
| **tail of the assistant's last reply before the gap** | 200, **400, 800**, 1600, 3200, full | **catches `241955c7` at 400–800, no false cuts** |
| head of its reply to the new instruction | 200, 400, 800, 1600, full | none |
| what the step before the gap returned | 200, 400 | catches `241955c7` at 400 — unconfirmed |

**Best: `--reply-before 400` — 20/21 against gemma3n's 19/21, 0 false cuts.**
It catches the "Separate job:" boundary and keeps its near-twin, `71448e61`
step 5 (the same instruction, not a new job), at "no". At 400 characters the
tail is the assistant delivering findings on the previous task and then asking
a follow-up the developer ignores; at 1600 and beyond it reaches back into the
unfinished investigation and the signal is gone. It works in a window, not as
a trend.

Controlled at the same slot, position and length on that gap: the real tail
flips the verdict to "yes"; a tail from an unrelated session and neutral filler
both leave it "no". Content, not length. The step-output catch has no such
control and its text is raw source, so it is not counted.

`263d65ce` step 9 was never caught with thinking off.

### Thinking on, at the defaults

`think=True`, a 4,096-token answer reserve and a 180s timeout: **2/2 real
boundaries caught — `263d65ce` step 9 for the first time — with 3 false cuts**:
`698c7529` steps 4 and 5 (the "Two things. Check every command…" review prompt
and the "go" after it, gemma3n's own mistake) and `2095a8af` step 35 ("Before
you commit — check the docstring"). 19/21. Clean answers on all 45; median
2,061 characters of reasoning, ~506 tokens, 15.1s a gap against 1.3s.

Timing caution: the reserve makes `num_ctx` differ on every call, and 44 of 45
calls reloaded the model (6.4s each, 311s of the run's 1,013s). Pin the context
before timing thinking runs.

### Thinking on, with the reply tail

The two levers fail in opposite directions — the reply tail makes gemma4 more
discerning, thinking makes it more willing — so the combination was measured.
On `263d65ce` alone it is right: step 9 caught, cut in the right place, and all
seven later turns of that writing session ("give me 10 title drafts", "a few
changes for the .md files") kept at "no". On the whole corpus it is not:

| gap | real | thinking | reply 400 + thinking | reply 400 |
|---|---|---|---|---|
| `241955c7` step 6 | ✓ | yes | yes | yes |
| `263d65ce` step 9 | ✓ | yes | yes | — |
| `698c7529` step 4, "Two things. Check every command…" | ✗ | yes | yes | — |
| `698c7529` step 5, "Looks good. What's next? go" | ✗ | yes | yes | — |
| `2095a8af` step 35, "Before you commit — check the docstring" | ✗ | yes | yes | — |
| `95b6bde7` step 1, a prompt redirecting the lookup to a docs tool | ✗ | — | yes | — |
| sessions | | 19/21 | 19/21 | **20/21** |

With thinking on, the same three false cuts appear whether the tail is shown or
not, and the tail adds a fourth (harmless to the score: a cut after the first
step leaves an episode too small to bank, and gemma3n cuts there too). The bet
that the tail would keep the review prompt at "no" is refuted. Thinking is the
only thing that ever finds `263d65ce` step 9, and on this corpus it always
pays for it with the review-prompt session.

**The best configuration measured stays `--reply-before 400`, thinking off.**

### Thinking on: what "What they do next" does

Why ask: at the review prompt, gemma4's reasoning agreed the instruction
continued the task ("This is still focused on refining the content for the
presentation"), then read the three steps after it — `ls`, `pytest`,
`npm test` — as "a shift from content proposal/review to code testing/
debugging" and answered "new job". The question's "that" comes straight after
the steps, and thinking resolves it to them.

Screened with thinking on, the context pinned at 8,192 tokens (per-prompt
sizing reloaded the model on 44 of 45 calls), on the seven gaps that decide it,
each judged under correct history — cuts only at the real boundaries:

| gap | V0 3 steps | V1 none | V2 1 step | V3 2 steps | V4 relabelled | V5 + reply | V6 reply instead |
|---|---|---|---|---|---|---|---|
| `241955c7` step 6 ✓ | ok | ok | ✗ | ✗ | ok | ✗ | ok |
| `263d65ce` step 9 ✓ | ok | ok | ok | ok | ok | ✗ | ok |
| `698c7529` step 4, review | ✗ | ok | ok | ok | ✗ | ✗ | ok |
| `698c7529` step 5, "go" | ok | ✗ | ✗ | ✗ | ✗ | ✗ | ok |
| `2095a8af` step 35, docstring | ✗ | ✗ | ok | ✗ | ✗ | ✗ | ✗ |
| `71448e61` step 5, the twin | ok | ✗ | ok | ok | ✗ | ok | ✗ |
| `263d65ce` step 31 | ok | ok | ok | ok | ok | ok | ✗ |
| **right** | **5/7** | 4/7 | **5/7** | 4/7 | 3/7 | 2/7 | 4/7 |

V4 relabels the section "In answer to that, the assistant then:"; V5 adds the
head of the assistant's reply to the new instruction (400 characters); V6 shows
that reply instead of the steps.

- **Thinking needs the section.** Without it the review prompt is fixed, but
  the "Separate job" twin and the "go" after the review become false cuts: the
  steps are what show a look-up turning straight into the add it prepared, and
  "go" turning into building the deck.
- **Rewording did not help.** Tying the steps to the instruction (V4) made
  gemma4 cut more, as if invited to judge whether the steps fit the request.
  The assistant's own reply, added (V5), made every new request sound like a
  continuation — "Using that as template. Added…" — and lost both real
  boundaries; at the review prompt, "I checked all 30 commands…" was read and
  the cut made anyway.
- **The steps mislead at one gap and are load-bearing at the others.** Every
  gap is right under some variant and no variant gets them all: these gaps sit
  near gemma4's edge with thinking on, and small input changes flip them in
  inconsistent directions.

One pattern is visible and deliberately not claimed: cutting only where V0 and
V6 both say "yes" gets 6/7, each vetoing the other's false cuts. It was found
by looking at these same seven gaps, and would double the thinking cost.

The step-count results also show why 698c7529 step 5 was a false cut in the
full run: step 4 had just been cut, so step 5 was judged as the start of a
fresh task. Given correct history, the default reads it as a continuation.

**Conclusion unchanged: thinking on, the defaults are as good as any framing
of the next steps; the best configuration remains `--reply-before 400`,
thinking off.** Nothing here changes a default: the
production judge is still gemma3n with no reply shown, and gemma3n has not been
measured with the reply tail.

## Same procedure, decided by embedding

Whether a saved episode is a repeat of an existing candidate used to be decided
by a lexical **signature** — the steps reduced to `read | edit:.json |
bash:python3 | bash:git add` — compared with `SequenceMatcher`, with a background
pass that let only pairs above 0.40 reach an embedding. On the real ledger:

- Six entries a person tagged good, all adding eval cases, scored 0.12–0.66
  against each other. Incidental steps — `ls`, `cd`, `source`, `git log` —
  outweighed the procedure.
- `similarity()` was asymmetric. The two "cover course reimbursement fact" entries
  scored 0.365 in ledger order against the 0.40 filter, 0.410 the other way, so an
  embedding that rates them 0.98 never saw them.
- The background pass had not run since the day Ollama went down.
- Ids were the signature, so a miss on identical work overwrote the existing
  entry, parked or promoted status included.

The reason given for lexical matching — no model inside a hook — stopped holding
when `SessionEnd` began waiting on the boundary judge.

**Now:** each episode is embedded once at fold time and compared by cosine with a
cached vector for every entry of any status; at or above `SKILLPP_MATCH_FLOOR` it
joins that entry. Ids are random. The background pass is gone.

### The yardstick

`tests/fixtures/sessions/recurrence.py` folds all eleven live sessions into one
ledger in the order they started and scores where each banked episode lands
against a hand-written family label:

| family | runs |
| --- | --- |
| add-eval-case | 7 — the three tutorial-card sessions, `a8b61dae`, `241955c7` task 2, and the two glossary fact sessions `d5fd2e59`, `a7be1ef5` |
| coverage-writeup | 2 |

Card and fact cases were two families for a while. Compared step by step
(`d5fd2e59` against `241955c7`'s chart case) they are the same work — read
`cases.json`, edit it, check the JSON, regenerate, commit — and the steps that
differ do the same job with other commands (`ls` and `head` against `find` and
`grep`, both locating `generate_fixtures.py`). What tells them apart is only in
the prompt and in the JSON written, and two fact runs were too few to hold a
family only the prompt could separate.

Scored as **wrong runs** — a run placed in an entry started by a different
procedure — and **missing merges** — how many extra entries a procedure is spread
over. Wrong runs must be zero. Pair counts, used first, grew with the square of a
family's size, so the eval-case family was most of every number.

### What is embedded

The first shipped text was `as_text`: the first prompt, then each step's command.
The prompt was the problem. `_intents_for` puts first the last prompt before any
work, which in three card-case sessions was the same scripted sentence, an
instruction to look the format up with a docs tool. That sentence held those three
together at 0.97 and pulled `95b6bde7` — a docs comparison that edits nothing,
opening with the same lookup — to 0.914, six thousandths under the floor.

Now the steps alone, one numbered line each, cut at 120 characters:

```
1. Read ${HOME}/ai_projects/acme/backend/acme_agent/eval/cases.json
2. Edit ${HOME}/ai_projects/acme/backend/acme_agent/eval/cases.json
3. Bash python3 -c "import json; json.load(open('${HOME}/ai_projects/acme/backend/acme_agent/eval/cases.json')
```

The text is cut on a step boundary at 5,000 characters. That was meant to keep it
inside nomic-embed-text's 2,048 tokens, and did not: real runs cost 2.11 to 2.4
characters a token, and two ledger entries of 4,886 and 4,979 characters (174 and
47 steps before the cut) were refused. The legacy `/api/embeddings` endpoint
answered HTTP 500, read as "model unreachable", so every fold after them in the
same project crashed. Embeddings now go through `/api/embed` with `truncate`,
which cuts at the model's own limit and keeps the head, and the log records each
truncation. Same vectors on the same text: cosine 1.000000 between the two
endpoints, and a real pair scores 0.563799 on both.

Over the full uncapped text, 4 of 52 texts exceed the limit (30 ledger entries,
11 held session files, 11 fixtures): three ledger entries whose sessions were
banked as one long episode, and the 99-step fixture. At least the first 43 steps
of each fit.

### Measured

Wrong runs / missing merges, eleven live sessions, one eval-case family:

| floor | first prompt + commands | **numbered steps** | signature, embedded |
| --- | --- | --- | --- |
| 0.86 | 3 / 2 | 1 / 2 | 1 / 1 |
| 0.90 | 1 / 3 | 1 / 2 | 1 / 4 |
| 0.92 | 0 / 4 | 1 / 3 | 0 / 4 |
| **0.93 (shipped)** | 0 / 4 | **0 / 4** | 0 / 4 |

The wrong run left from 0.86 to 0.92 is `95b6bde7` joining the card cases at
0.921. At 0.93 the eval cases sit in four entries: the three long card runs that
open with the doc lookup (0.939–0.969 to each other), `241955c7` task 2 with the
course case (0.930), and the holidays case and `a8b61dae` alone. The long and short
eval-case runs never score above 0.822 against each other: the long ones carry a
dozen lookups and searches the short ones do not. The two coverage write-ups
score 0.846 and stay apart.

**The floor is set by wrong runs, not by merge count.** A wrong merge silently
mixes two procedures into one skill; a missed merge leaves a duplicate a person
can still see and fold.

Tried and rejected on the same sessions: the signature string embedded (card and
fact runs look identical, and `95b6bde7` shares its first three shapes with the
card cases); structured steps without commands (anything nearly empty scores 1.0
against anything else); padding short runs with placeholders (short runs grow
alike, one more wrong run at 0.92 and 0.95); prompts in any selection (the
prompt of the first edit did best, but 36 of 98 real prompts are five words or
fewer — "option 1", "do it again" — which would embed identically).

### Conversation text replaces commands where a run has replies

Measured once capture kept each prompt's reply (`capture._turns`), on fourteen
live sessions — the eleven above plus three runs of one conversation-driven
procedure, *propose slide content, fact-check it, build the deck* — banked as 18
episodes with the real fold order replayed per input. "Danger" is the highest
score between two different procedures:

| embedded | danger | no wrong merge at | correct merges of 32 | largest entry: eval case / presentation / coverage |
| --- | --- | --- | --- | --- |
| numbered steps (commands) | 0.921 | 0.93 | 4 | 3 / 1 / 1 |
| step descriptions | 0.846 | 0.85–0.90 | 4–7 | 3 / 2 / 1 |
| prompts only | 0.864 | 0.88 | 3 | 2 / 2 / 2 |
| **prompts and replies** | **0.854** | **0.84–0.88** | **6–7** | **3 / 2 / 2** |
| descriptions and prompts | 0.868 | 0.88 | 5 | 2 / 2 / 2 |

Shipped: prompts and replies at **0.85**, for runs that carry a reply; steps at
0.93 otherwise. What is embedded was then narrowed further — see *One change at
a time* below. Only like is compared with like — the two texts score on
different scales. The real fold reproduces the replay: 6 of 32 merged, 0 wrong,
the coverage write-ups in one entry for the first time, an eval-case entry at 3.

### One change at a time: what the conversation text keeps

Five more live runs (two talk decks, three LinkedIn posts, over three of the
project's documents) made the failure measurable: **two procedures over
one document scored higher against each other (0.852) than two runs of one
procedure over different documents (0.831).** The text was following the
material.

`tests/benchmarks/merge_ladder.py` scores one change at a time over every live
session — 23 episodes from 19 sessions — at the *safe floor*, the first floor
above the highest score between two different procedures:

| step | rendering | danger | safe | merged | 2-proc-2-file gap | kept |
| --- | --- | --- | --- | --- | --- | --- |
| R0 | prompts and whole replies | 0.854 | 0.86 | 10/46 | -0.021 | baseline |
| R1 | + file names → `<file>` | 0.848 | 0.85 | 12/46 | +0.022 | ✅ |
| R2 | R1 + deliverable blocks removed by markdown shape | 0.854 | 0.86 | 10/46 | +0.098 | ✗ |
| R3 | R1 + each reply cut to 300 chars | 0.849 | 0.85 | 12/46 | +0.057 | ✅ shipped |
| R4 | R3 without replies | 0.837 | 0.84 | 19/46 | +0.161 | ✗ |

**R2** moved the danger line onto a coding pair (`add-eval-case` ~
`compare-adk-docs`, 0.854): coding replies carry their topic in plain sentences,
which a markdown-shape filter cannot see, so removing bullets and drafts only
un-diluted the topic where it was already invisible.

**R4** looked best and measures the test, not the procedure: all seven added
merges were `create-presentation` pairs whose prompts were scripted and pasted
word for word, while the unscripted coding sessions gained nothing (4/21 either
way). Two later runs phrased by the developer scored 0.882 and 0.837 against the
same candidate — the spread prompts alone cannot survive.

After the removals, ingredients were added one at a time, on top of R3:

| step | adds | danger | safe | merged | gap | kept |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | the skills/MCP a turn used | 0.848 | 0.85 | 13/61 | +0.071 | ✅ |
| A2 | the file kinds produced, and whether one was handed over | 0.849 | 0.85 | **14/61** | +0.072 | ✅ shipped |
| A3 | the tool sequence | 0.849 | 0.85 | 14/61 | +0.075 | ✗ |
| A4 | the agent's step descriptions | 0.850 | 0.86 | 12/61 | +0.083 | ✗ |
| A5 | one topic-free sentence per turn from `gemma3n:e4b` | 0.855 | 0.86 | 14/61 | +0.063 | ✗ |

A3 adds nothing: two ways of building a deck share no tokens (`Skill, Bash x4,
Write, Bash x11, SendUserFile` against `Artifact x2, Bash, Write x7, Artifact
x2`), so it only strengthens runs that already executed alike. A4 raises
same-procedure pairs *and* one different-procedure pair past the 0.85 boundary,
which steps the floor to 0.86 and costs two merges. A5 raises unrelated pairs
faster than related ones: asked to describe the kind of work without subjects, a
small model writes one house style ("Generated a presentation outline…",
"Drafted a short social media update…"), and the shared frame is similarity no
procedure earned.

The wall is a real pair, not noise: `assemble-article ~ create-presentation` at
0.849. Writing an article from documents and building a deck from documents are
close procedures, and nothing measured here separates them further — which is
what sets the floor at 0.85.

**R3 shipped, then A1 and A2.** The lowest floor with no wrong merge now equals the safe floor,
so no merge survives on fold order; the text is 73% shorter (104k → 28k
characters over 19 sessions), so nothing reaches the embedding's token limit;
and the real fold unifies the three LinkedIn runs and the two coverage
write-ups with no wrong merge. With A1 and A2 the corpus reaches 14 of 61 with
the danger line unmoved, and on the real ledger the two scripted presentation
runs cross the floor (0.856). The unscripted Artifact-built decks stay apart
(0.745-0.807): every ingredient that helped describes execution, and they
executed differently.

Why commands lost here: three runs of the presentation procedure scored
0.66–0.83 on them — scratchpad paths, `sed` against `Read`, and a run that also
fixed the docs. Why the floor moved down safely: 0.88 clears the danger line by
0.026, against 0.009 for commands at 0.93. The presentation pieces reach 2, not
3: two of the three runs are cut at the review prompt (see the live fixtures'
`expected_fail`), and a piece does not look like a whole run.
