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
