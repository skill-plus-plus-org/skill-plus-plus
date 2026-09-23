# Contributing

Thanks for looking. skill-plus-plus is small, standard-library-only Python, and most of
it is explained in its own comments. This page covers setting up, the tests,
the conventions the code follows, and how to show that a change is better, not
just different.

## Setting up

```bash
git clone https://github.com/himanshu096/skill-plus-plus
cd skill-plus-plus
pip install -e .          # puts `skill-plus-plus` on your PATH; or run python3 bin/skill-plus-plus
```

Python 3.10 or newer. Nothing else is needed for the tests. `node`, if you have
it, runs two extra tests of the review page's JavaScript; without it they skip.
Ollama and the two models (see the README) are only needed to measure detection
and matching, and to use skill-plus-plus for real.

## Tests

```bash
python3 -m unittest discover -s tests
```

About ten seconds, with no model: the boundary judge, the embeddings, the
describer and naming are stubbed for the whole suite (`setUpModule` in
`tests/test_skill_plus_plus.py`). If a test you add reaches Ollama, it is missing a stub.

Tests that need recorded sessions skip when there are none; see
[tests/fixtures/sessions/README.md](tests/fixtures/sessions/README.md).

Before opening a pull request, also run the leak guard, which CI runs too:

```bash
python3 scripts/leak_guard.py
```

It fails on home paths, email addresses, UUIDs and credential-shaped strings in
tracked files. If you keep a list of terms that must never appear in public
(an employer, a client, a colleague), pass it with `--denylist` or
`SKILL_PLUS_PLUS_DENYLIST`, from outside the repo.

## How the code is written

- **Comments say why, with the evidence.** Most choices in this code were
  measured, and the comment next to one says what was measured and what the
  alternative did: "on the live sessions it scored 2/5 against 4/5". A change
  that reverses one should say what it measured instead.
- **One answer per question.** Several rules are needed in more than one place.
  Each lives once, and everything else calls it; the table is in
  [docs/architecture.md](docs/architecture.md#one-place-for-each-rule). Do not
  write a second `is_read_only`.
- **Test names are sentences** that say the behaviour, and a test's docstring
  names the real defect it pins down when there was one.
- **The invariants hold**: hooks never raise, nothing changes without `--apply`,
  nothing installs itself, everything captured is scrubbed before disk.
- **No new dependencies.** Standard library only, at runtime and in the tests.
- **Commits** follow Conventional Commits with a scope: `fix(web): …`,
  `feat(judge): …`, `test(ladder): … — measured, not kept`. The body says why.

## Showing a change is better

The unit suite says the code still does what it did. Whether detection or
matching got *better* is answered on recorded sessions, and needs Ollama:

```bash
python3 tests/fixtures/sessions/score.py        # each session cut and banked right?
python3 tests/fixtures/sessions/recurrence.py   # repeats merged, and nothing wrongly?
python3 tests/benchmarks/judge_replay.py -v     # the judge alone, gap by gap
```

Change one thing at a time, and compare against the baseline recorded in the
set's `expected.json`. Three sessions are recorded as known gaps
(`expected_fail`); the suite fails when one of them starts passing, so a fix is
noticed, and the fixture's truth and note are then updated. For merging, a wrong merge counts for more than a missed
one: a missed merge leaves a duplicate you can see, a wrong one mixes two
procedures into one skill. Write what you measured into
[docs/research/benchmarks.md](docs/research/benchmarks.md), including what
did not work.

Synthetic cases (`tests/benchmarks/cases.py`) are useful for regressions, but
they were written alongside the detector, so they are not evidence that it
improved. Recorded sessions are.

## Adding a recorded session

The public set is recorded from a plan fixed in advance,
[tests/fixtures/sessions/catalogue.json](tests/fixtures/sessions/catalogue.json):
every prompt, where each new task starts, and the family of every task.
[RECORDING.md](tests/fixtures/sessions/RECORDING.md) is the kit you type from.

```bash
tests/fixtures/sessions/recording/setup.sh C-F1          # one session's folder
python3 tests/fixtures/sessions/from_transcript.py --all-sessions
python3 tests/benchmarks/judge_replay.py --write          # the judge's verdicts (Ollama)
python3 tests/fixtures/sessions/score.py                  # detection, per check
python3 tests/fixtures/sessions/recurrence.py             # merging, per level
```

To test something new, add a session to the catalogue first, with its truth,
then record it. Only public work goes in this repo, and the ground truth is
written before the pipeline is run over it. The fixtures README describes the
format.

## Changing the draft prompt

A change to `skill_plus_plus/commands/skill-plus-plus-draft.md` is checked on four recorded
sessions. Drafting and judging are Claude calls, so this is not in the unit
suite:

```bash
python3 tests/benchmarks/draft_check.py prepare draft.code.feature   # and the other three
# run the `skill-plus-plus … draft … --apply` line it prints
python3 tests/benchmarks/draft_check.py judge
python3 tests/benchmarks/draft_check.py check
```

Fixed rules check form (name, trigger description, nothing from the run
leaked). A Claude judge answers the method questions in
`tests/fixtures/sessions/draft_cases.json`, and a yes counts only with a quote
found in the draft. Compare against the previous run of the same four cases.

## Pull requests

- [ ] `python3 -m unittest discover -s tests` passes
- [ ] `python3 scripts/leak_guard.py` passes
- [ ] a behaviour change has a test, and a detection or matching change a
      measurement on recorded sessions
- [ ] the README or `docs/` say what changed, if a user would notice

Security problems: please report them privately; see [SECURITY.md](SECURITY.md).
By taking part you agree to the [code of conduct](CODE_OF_CONDUCT.md).
