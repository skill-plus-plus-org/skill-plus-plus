# The episode filter

> Part of the [research log](README.md), which says where things stand now.
> The sessions cited below by tag are a private set, not in the repo.

Segmentation answers *where* a task ended. It cannot answer whether the task was
worth keeping, and the difference is arithmetic rather than tuning: **a partition
cannot discard anything.** Cut a session more accurately and every step still
lands in some episode, so the material is unchanged. Measured elsewhere at 0%
reduction on real sessions. Meanwhile the premise of the whole tool is that most
sessions contain nothing worth keeping.

So something has to be able to say no. That is `skill-plus-plus sift`.

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

Nothing is deleted. `one-off` is a status, `skill-plus-plus reopen <id>` reverses it, and
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
- **Two models, one of them unusable.** `gemma3n:e4b` works; a 1.2B keeps 79%
  of a junk corpus. Nothing between them has been tried, and nothing larger.
- **No recurrence interaction.** An episode parked on its first sighting never
  gets to recur. Whether a second sighting should reopen it — as a recurring
  ignored workflow does elsewhere — is undecided.

---

## The larger run: 14 real candidates, and the false-drop count

A 1.4 MB ledger from real work — the pre-clear backup of a developer building
this tool. 14 candidates, `gemma3n:e4b`, 1.5–6.7s each.

**All 14 were dropped.** Labelled by hand afterwards:

| | count | |
| --- | --- | --- |
| Correct drops | **11** | diff review, an install smoke-test, "is this a good direction?", 216 steps of SVG and browser work, writing a module, "did it record?" debugging, a 189-step website analysis, hero-copy editing, a Desktop investigation, two bug fixes |
| **Likely false drop** | **1** | `9b7fc39f8457` — grep tracked files for secrets and personal data → check the diff → commit. Pre-commit hygiene is a method someone repeats |
| Borderline | 2 | `9a881ce54ffd`, `c976ef423f6f` |

**False-drop rate: 1 in 14 (7%) strict, 3 in 14 (21%) counting borderline.**

### The positive control exists after all — and it passes

The same backup holds four entries the developer had already judged, which is
real ground truth rather than a fixture: two **promoted** into live skills, one
**dismissed**, one **ignored**.

| Entry | Human | Filter | |
| --- | --- | --- | --- |
| Weekly manager update email | promoted | **keep** | ✓ 1.8s |
| Tool evaluation note | promoted | **keep** | ✓ 2.2s |
| `vim app.py` (an install probe) | ignored | **drop** | ✓ 1.4s |
| Author a new skill from a description | dismissed | keep | ✗ 47.4s |

**3 of 4, and zero false drops on the two known methods.** That is the result
that matters: 14 of 14 was the corpus, not a filter that says no to everything.

The miss is a false *keep* — the cheap direction, one line in a review list —
and it is defensible. That entry is a dictated four-step procedure; it was
dismissed for being redundant with an existing command, which is a different
question from whether it is a method. The 47s also stands out: its steps are
long prose blocks rather than commands, and cost ~20x the usual call.

### What the 14-of-14 number cannot tell you

**There were no keeps, so there is no positive control here.** A filter that
dropped everything unconditionally would score identically on this corpus. The
evidence that it *can* keep is the scenario set, where it kept `refine`,
`distinct-tasks` (both) and `recurs` — not this run.

That said, 14 of 14 is plausible rather than alarming for this corpus: it is a
developer's own feature work, investigations and bug fixes. Genuine repeatable
procedures are rare in that population, which is exactly the premise the tool
rests on. It is also the uncomfortable implication — if a real ledger yields no
methods, the filter is working and the *product* still has nothing to offer.

### The false drop is not a data artefact

The obvious explanation was the title: that entry's `{ASK}` was a raw shell
command rather than a stated intent, so the model had no request to judge
against. Tested by re-asking with the same steps under three framings — the
command as banked, an explicit *"check nothing personal or secret is in tracked
files, then commit"*, and a terse *"commit safely"*.

**`no` all three times.** The title is not the cause; the model consistently
reads that sequence as one particular job. There is a defensible reading behind
it — the generalisable part is a *rule* ("never commit secrets"), not a
multi-step procedure — but by the labelling above it is still a false drop, and
it sits on the same boundary as the `retry` miss: *this instance* versus *this
kind of task*.

### One borderline case is a segmentation failure, not a filter failure

`9a881ce54ffd` is 93 steps of documentation editing with a real, reusable
procedure buried inside it — bundle a skill, open the settings page, upload it.
The filter judged the episode correctly: taken whole, it is not a method. The
defect is that it was ever one episode. Better boundaries would surface the
procedure; no filter can extract it from an episode that large.

## Model size is a constraint here, unlike on the coarser question

The same 14 entries, two models:

| Model | keep | drop | wall clock |
| --- | --- | --- | --- |
| `gemma3n:e4b` | 0 | 14 | 6s (warm cache; 1.5–6.7s per call cold) |
| `lfm2.5-thinking:1.2b` | **11** | 3 | 170s |

The 1.2B kept an install smoke-test, a 216-step SVG session, a website analysis,
"did you test it with different scenarios", "yes fix both bugs" and six more —
**a false-keep rate near 79% on a corpus that is almost entirely one-off work.**
As a filter it is unusable: it says yes to nearly everything.

This is worth stating flatly because the opposite result holds one question
earlier. On *triage* — "is this session worth reading at all" — a 1.2B answered
correctly, and the conclusion drawn there was that model size was not the
constraint. **That does not transfer.** Deciding whether a sequence generalises
is materially harder than noticing a session contains work, and the same model
that handles the first fails the second.

Also note the speed inversion: the 1.2B took 28x longer, because it is a
*thinking* model and spends tokens reasoning before answering. Smaller is not
faster.

One aside: the 1.2B did keep `9b7fc39f8457`, the entry labelled above as a
likely false drop — agreeing with the human label on the single ambiguous case
while getting ten obvious ones wrong. Not evidence of anything except that a
permissive filter is right by accident on the cases where keeping is correct.

## Verdict on running it unattended

**Deliberately, yes. Unattended, not yet.**

For it: zero false drops on the two known methods, both kept in under 3s. The
one ground-truth miss is a false keep, which costs a line in a list. Nothing is
deleted, and `reopen` reverses any verdict.

Against it: 1 likely false drop in 14 unlabelled candidates, and it survives
rephrasing — the model is consistently wrong there rather than uncertain, so the
fail-safe does not catch it. Two known methods is also a thin positive control.

So run it as a command and read what it parked. Do not put it in a cron or a
hook until the labelled set is larger than four.

---

## Who writes the skill

The filter decides *which* candidates deserve a model call. It does not write
anything. That split is the point, and it is measured rather than assumed: a 7B
asked to write a `SKILL.md` body transcribes the run instead of generalising it,
names the repository it happened to touch, and picks procedures a frontier judge
rejects — one call or four decomposed ones.

So the pipeline is four stages, and only the last one costs anything:

| Stage | Who | Cost |
| --- | --- | --- |
| capture | hooks | free |
| cut the session into episodes at `SessionEnd` | `segment.py` | free |
| **discard the one-off ones** | local model, `sift` | free |
| **write the body** | the developer's own agent, `draft` | one call |

### `skill-plus-plus draft <id>`

Invokes whatever agent the developer already uses, via a **command template**
rather than an API call:

    SKILL_PLUS_PLUS_AGENT="claude -p {PROMPT} --no-session-persistence ..."

That is deliberate. No API key is held, no vendor is baked in, and the agent is
already authenticated as the developer — a Cursor user's model writes it in
Cursor. `{PROMPT}` is the only substitution, and the prompt itself is a markdown
file (`commands/skill-plus-plus-draft.md`), not code.

### It drafts; it never installs

Drafts land in `<root>/drafts/` and promotion stays a human act. The prompt says
so twice, and a test asserts that a draft leaves `status` and `skill_path`
untouched.

This matters more here than anywhere else in the tool. `draft` runs when nobody
is watching, so the agent cannot ask the up-to-three questions the interactive
review asks. Instead every question it would have asked becomes a line under
`## Open questions` — a draft that admits two gaps is worth more than one that
invents the answers. And an unapproved skill appearing in the skills directory
is precisely the failure the whole design exists to prevent.

The prompt also permits writing **nothing**: if the evidence says this was one
particular bug after all, the agent says so in a line and stops. That is not
treated as an error, because it is the filter being right in a place the filter
could not see.

### Two wiring facts that will bite

**Installing the CLI does not authenticate it.** `npm install -g
@anthropic-ai/claude-code` puts `claude` on PATH and it still exits 1 with
`Not logged in` until someone runs it once and authenticates. `draft` cannot do
that step and should not try.

**A failed agent and a declining agent look identical from outside.** Both write
no file. The first version of `draft` reported them with one message — "the
agent judged there was nothing here, or it failed" — which meant an
unauthenticated CLI read as a considered judgement that the work was not worth
keeping. The exit code separates them and is now the thing that decides: non-zero
is a failure on stderr with a non-zero return, zero-with-no-file is a decline.
Found by running it for real; no fixture would have produced it.

**`claude` is usually not on PATH**, even on a machine where Claude Code is in
daily use — only a version-pinned binary inside the application bundle. So the
dry run prints whether the agent resolves before you spend anything, rather than
failing halfway through.

**The tool pattern is relative.** `Bash(python3 bin/skill-plus-plus *)` only matches if
the agent runs from the package root, so `draft` sets that cwd itself. An
earlier version allowed `Bash(skill-plus-plus *)` — a binary that does not exist — and
would have allowed the agent nothing at all. `shlex` had also split that
pattern in two at the space inside the parentheses. Both are pinned by tests.

---

## The harder set, and a much worse false-drop rate

The 14-candidate run above was a corpus with almost no real procedures in it, so
it could only measure precision. The seven-fixture set has known procedures, and
the result is far worse.

| Fixture | Ground truth | segmented | after sift | |
| --- | --- | --- | --- | --- |
| `big` (433 KB) | one procedure | 2 (60–61 steps) | **0** | ✗ dropped it |
| `incomplete` | nothing | 2 | **0** | ✓ fixed |
| `two` | two procedures | 4 | **1** | ✗ dropped one |
| `secrets` | one, redacted | 1 ✓ | **0** | ✗ dropped it |
| `barren` | nothing | 2 | **0** | ✓ fixed |

It removed the junk in both fixtures that were meant to yield nothing. It also
**dropped 3 of the 4 real procedures** — a 75% false-drop rate where real
procedures actually exist, against the 7% measured on a corpus that had almost
none.

That is the number to quote, not the 7%.

### The cause is untrimmed exploration, not the judgement

`secrets` is the clearest case. Its title states its own recurrence — *"push
today's metrics to the dashboard **like we did last week**"* — and the method is
three steps: check the API version, POST the metrics, read them back. Those three
sit under **six leading greps**, because `segment.py` has no exploration trim.

Tested directly, same model, same entry:

| Input | Verdict |
| --- | --- |
| as banked — 6 greps then 3 real steps | **drop** |
| leading exploration trimmed — 3 steps | **keep** |

So the filter is not misjudging the procedure. It is judging an episode in which
the procedure is outnumbered two to one by looking around, and answering
reasonably about what it was shown.

`big` fails the same way from the other end: segmentation over-merged it into two
60-step blobs, one of them at ×7. A 60-step over-merged blob genuinely is not a
method, so sift rejected it correctly — and the session's real procedure went
with it. Correct rejection of a badly-cut episode still loses the work.

### What follows

**A filter is only as good as the episodes it is given.** This is the mirror of
the finding that motivated the filter in the first place: a partition cannot
discard, and now — a discard cannot repair a partition. Both stages have to be
right.

The missing piece is already identified and already written elsewhere:
`trim_leading_exploration` on `feat/ignore-list-and-drift-tracking`, which is
that branch's one genuine contribution and the exact thing `segment.py` lacks.
It should fix `secrets` and `explore-then-fix` together, and it is mechanical
code rather than judgement.

Until it is ported, **this branch is not better than `feat/pattern-detection`**
on the evidence available, and the honest comparison is in the next section.

## Comparison with `feat/pattern-detection`: what is and is not measured

| | this branch | `pattern-detection` |
| --- | --- | --- |
| Six symmetric scenarios | **5 of 6**, verified here | 5 of 6, **claimed, not verified** |
| Seven-fixture set | drops 3 of 4 real procedures | claimed to handle them; its own fixtures |
| Cost per session | free until `draft` | one frontier call per session |
| Runs unattended | yes | no — someone types `/log-session` |
| Long sessions | fails: over-merged then dropped | windowing built for exactly this |
| Loop closed on real work | no | claimed, unverifiable on this machine |

The pattern-detection arm cannot be scored here: every case costs a frontier
call, and `claude` is not on this machine's PATH. So the one comparable number is
a tie in which only one side has been checked, and on the harder set this branch
loses outright.

**Verdict: not better yet.** The architecture is the more promising one — free,
unattended, and it gets recurrence — but a 75% false-drop rate where procedures
exist is disqualifying until the trim lands.

---

## The comparison that matters: detection from observation

The section above compares against `feat/pattern-detection` on its own axis —
reading a finished transcript. That is the wrong axis if the goal is to
**observe** rather than to read a transcript once, so here is the other one,
measured rather than argued.

`feat/pattern-detection`'s hook dispatch handles `UserPromptSubmit`,
`PostToolUse` and `SessionEnd`, and its `SessionEnd` calls the **old
`fold_session`** — one ledger entry per whole session, no segmentation, inherited
from `main`. Nothing in its hook path queues or windows anything. All of its
intelligence is in `/log-session`, which needs a transcript, a frontier call, and
somebody to type it.

Same harness, same six fixtures, same three-hook event stream, zero frontier
calls:

| Branch | Detection on the hook path | Score |
| --- | --- | --- |
| `feat/ignore-list-and-drift-tracking` | folds live at `Stop`, plus budgets | **2 of 6** |
| `feat/pattern-detection` | one entry per whole session | **2 of 6** |
| `segmentation-fixture` | segments at `SessionEnd` | **4 of 6** |
| **this branch** | segments, then discards one-offs | **5 of 6** |

What pattern-detection's hook path did, on every one of the six: banked exactly
one entry. So it merged `distinct-tasks`' two tasks into one, banked
`mid-investigation` where nothing should be banked, left `explore-then-fix` at
ten untrimmed steps, and recorded `recurs` at ×1 rather than ×2 — the signature
drift that `segment.py` exists to fix, reproduced.

**This is not a criticism of that branch as designed.** Its hook is deliberately
vestigial; it was never meant to detect. But it does answer the question directly:
if the requirement is observation rather than a one-time transcript job, that
branch does not compete on it, and everything it wins — windowing, long
transcripts, a closed loop — sits on the axis being set aside. Everything it
costs — a frontier call per session, a human typing a command, no unattended
operation — is what the observation path is meant to avoid.

### What still has to land

The 5 of 6 above and the 75% false-drop rate on the seven-fixture set are both
true, and both are about the same missing piece. `segment.py` has no exploration
trim, so a real procedure arrives diluted by the greps that found it, and the
filter — judging what it was shown — says no. Porting
`trim_leading_exploration` is the next thing, not a better prompt.

---

## After porting the exploration trim

`trim_leading_exploration` and its read-only vocabulary are now in `segment.py`,
ported from `feat/ignore-list-and-drift-tracking`. Prompt sentinels survive the
trim — they carry the intent an episode is titled from — and nothing is ever
trimmed below two substantive steps.

All three sets re-run.

### Set 1 — six scenarios, hook path: **5 of 6, unchanged**

The trim did what it was ported for: `explore-then-fix` now banks **2 steps
instead of 10**. It is still dropped by the filter, which is correct — a one-off
bug hunt is not a procedure. `retry` is still the one false drop, and it was
never an exploration problem.

### Set 2 — seven fixtures: 75% → 67% false drops, and two new findings

| Fixture | Ground truth | before | after |
| --- | --- | --- | --- |
| `secrets` | 1 procedure | dropped ✗ | **kept ✓ — fixed** |
| `barren` | nothing | 0 ✓ | **1 kept ✗ — regressed** |
| `new`/`match(a)` | the bug-filing procedure | — | banked, then dropped ✗ |
| `match(b)` | the same, ×2 | — | banked **at ×2**, then dropped ✗ |
| `big` | 1 procedure in 433 KB | 0 ✗ | 0 ✗ |
| `incomplete` | nothing | 0 ✓ | 0 ✓ |
| `two` | 2 procedures | 1 of 2 | 1 of 2 |

**`secrets` confirms the hypothesis end to end.** Nine steps became three, and
the same model flipped from *"one particular job"* to *"a method worth
repeating"*. The filter had been judging a procedure outnumbered two to one by
the greps that found it.

**`barren` regressed, and that is the trim's own cost.** Trimming left a tidy
two-step `merge main in`, which reads like a method precisely because the noise
around it is gone. Cleaner episodes are easier to judge in both directions.

### The finding that matters more than either: recurrence is ignored

`match(b)` banked the bug-filing procedure **at ×2** — recognised as the same
work across two sessions, which is the thing the capture branch never managed at
all. Then `sift` dropped it.

That is backwards. An episode the developer has actually performed twice is
behavioural evidence that it is a method, and it is stronger evidence than a
model's reading of the steps. The filter never sees the count: `is_reusable`
renders intent and steps and nothing else.

The rule that follows is simple and does not need a model: **never discard
something that has been done more than once.** Recurrence is the whole promotion
gate in this design, and the one stage that can discard is currently allowed to
veto it.

### Set 3 — real ledger: unchanged

14 of 14 dropped, 3 of 4 against human labels, both promoted skills kept. The
trim runs at segmentation time, so entries already banked by another branch's
segmentation are unaffected.

### Where the remaining false drops actually come from

| Cause | Fixture | Fixable by |
| --- | --- | --- |
| exploration diluting the method | `secrets` | **done** — the trim |
| recurrence ignored | `match(b)`, `new` | a count check, no model needed |
| episodes over-merged into 60-step blobs | `big` | segmentation, not the filter |
| genuine boundary judgement | `retry` | unresolved; may not be resolvable |

---

## Ranking instead of parking

The measured verdict on letting a local model decide: **it is a good junk
detector and a poor procedure detector.** It dropped 11 of 11 clear junk entries
correctly and 4 of 6 real procedures along with them. Since a false keep costs a
line in a list and a false drop is never seen again, its error was entirely in
the expensive direction, and rephrasing did not help — it was confidently wrong,
not uncertain, so the fail-safe never fired.

So `sift` no longer decides. It **ranks**: writes a `hint` of `method`,
`one-off` or empty onto each candidate, and `skill-plus-plus review` lists repeatable
first and doubtful last. Nothing leaves the queue. `--park` still exists, is
opt-in, and prints what it costs.

**False drops are now structurally impossible in the default path**, which is a
better guarantee than any accuracy number.

### The rule that outranks the model

    if entry.occurrences >= 2:  ->  "method", without asking

An episode the developer has actually performed twice is behavioural evidence,
and it beats a model reading the steps. This was not theoretical: `match(b)`
banked the bug-filing procedure at ×2 — recurrence firing, which the capture
branch never achieved — and the model discarded it anyway. `big`'s ×7 entry went
the same way. Both now rank as methods without a model call, which is also
cheaper.

### All three sets, after the trim and the guard

**Set 1 — six scenarios.** Segmentation **4 of 6 → 5 of 6**: the trim fixed
`explore-then-fix`, which now banks 2 steps rather than 10. Five of six hints
are right; `retry` is ranked `one-off` and should not be. Nothing lost.

**Set 2 — seven fixtures.** Was 4 of 6 real procedures dropped. Now **none are
dropped** and 4 of 6 are ranked `method` — `match(b)` (×2) and `big` (×7) via
the guard, `secrets` via the trim, one of `two` via the model. The two ranked
`one-off` in error are still in the queue, just lower. `barren`'s `merge main
in` is ranked `method` in error, which is the trim's cost: a tidy two-step
episode reads like a method precisely because the noise is gone.

**Set 3 — real ledger.** All 14 stay, all ranked `one-off`, which matches the
hand labelling of 11 clear correct plus 1 likely wrong and 2 borderline — and
the one likely-wrong entry is now merely ranked low rather than parked. Against
the four human labels: **3 of 4**, both promoted skills ranked `method`. The
miss is the dismissed entry ranked `method`, still the cheap direction.

### What is left

`retry` and `barren` are both genuine boundary judgements — *this instance*
versus *this kind of task* — and neither the trim nor the guard touches them.
They are now ordering errors rather than losses, which is the right place for an
unresolved judgement to live.

`big` remains a segmentation problem: two over-merged 60-step blobs. The ×7 one
ranks as a method on recurrence, so it now reaches review, but it reaches it as
a 61-step blob.

---

## What installing it found in the first hour

Two failure modes, neither reachable from a fixture, both in the live ledger
within an hour of the branch going live.

### Automation observed itself

`skill-plus-plus draft` spawns an agent session. That session has hooks too, so its own
poking around was captured and banked — two candidates titled
`/skill-plus-plus-draft 8a494bc02d67`, holding the draft agent's `find`, `ls` and
`echo ok`. **Using the tool manufactured work for the tool.**

`feat/pattern-detection` hit the same thing at a scale that makes the point
better: **158 of 174** queue entries were `claude -p` runs it had queued for
itself. Its cure was pruning entries whose transcript no longer exists, which
suits a transcript-based design and does nothing for a buffer-based one — a
nested session here writes a real buffer and folds it like any other.

The cure here is a marker: `draft` sets `SKILL_PLUS_PLUS_INTERNAL=1` on the agent it
spawns, and the hook returns before doing anything when it sees it. Set by the
process doing the spawning, so nothing has to recognise itself.

### A harness envelope became a candidate title

One entry was titled `<task-notification>`. The harness injects envelopes into
the prompt stream — task notifications, system reminders, command wrappers — and
`UserPromptSubmit` recorded one as stated intent. A skill's description is the
only thing read when deciding whether to load it, so a skill named after a
system message is dead on arrival.

Prompts beginning with a known envelope prefix are now dropped.

### What this says about testing

Both defects survived 122 unit tests, three eval sets and a bakeoff against
another branch's fixtures. Neither is subtle. They needed the thing to be
*installed and used*, which is the one thing no harness reproduces — and it is
the same lesson `feat/pattern-detection` recorded as *"toy fixtures hid three
separate defects, all of which appeared on the first real session."*
