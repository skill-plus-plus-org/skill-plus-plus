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
