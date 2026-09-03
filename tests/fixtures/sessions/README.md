# Live sessions

Real Claude Code sessions, captured by the hooks while doing real work, stored
with hand-written ground truth.

This exists because the synthetic corpus in `tests/benchmarks/cases.py` was
written by whoever was also writing the detector, so it measures internal
consistency. These were not. Every one of them has already caught something the
corpus could not:

* `1c3c9422` — six turns of one task banked as **six fragments**, one of them the
  documentation lookup alone. Led to `_absorb_before_commit`.
* `2095a8af` — the same procedure, run after that fix. Its commit was invisible
  to marker detection because the agent used `git -C <path> commit`, which
  normalised to a bare `git`. Led to `normalize._skip_pre_subcommand_flags`, and
  to finding 76 real `git -C … status/log/diff` calls that counted as work.

## What is in a file

| key | meaning |
| --- | --- |
| `procedure` | what the developer was actually doing, in their words |
| `truth.episodes` | how many candidates a correct run banks |
| `truth.title` | what the entry should be called |
| `truth.must_contain` | substrings that must survive into a banked episode — the steps the procedure exists for |
| `why` | why the ground truth is what it is, argued rather than asserted |
| `history` | what the pipeline did before, so a regression is recognisable |
| `steps` | the step stream, `$HOME` templated out |

`truth.must_contain` is the load-bearing field. Episode counts can be right for
the wrong reasons; these name the steps whose loss would make the captured skill
a different and worse procedure than the one performed.

**`truth.episodes` counts candidates banked, not episodes cut.** Capture runs
three stages: `segment` cuts the session up, `fold_session` discards the flagged
episodes, and `_fold_steps` discards anything under two substantive steps. Only
what survives all three is a candidate, and that is the number to write here. A
trailing `git status` after a commit is cut as an episode and never banked — it
does not count.

`score.py` enforces that by calling `fold_session` and reading the ledger, rather
than calling `segment` and counting. It used to count the cut, which made three
fixtures fail for a defect that was the scorer's, and let `2095a8af` pass on a
ground truth of 2 that its own `why` described as banking one. Do not reintroduce
a private copy of the discard rules here; when the fold stage learns to throw
something else away, this should follow without an edit.

## Running them

```bash
python3 tests/fixtures/sessions/score.py          # all sessions
python3 tests/fixtures/sessions/score.py 1c3c9422 # one
```

They also run in the unit suite (`TestLiveSessions`), so a change that breaks a
real session fails CI rather than being noticed three weeks later.

## Adding one

Do real work in a project with the hooks installed, end the session explicitly
so `SessionEnd` fires, then find the transcript tag under
`~/.claude/projects/<slug>/` and write the fixture the same shape as these.

Two rules, both learned the hard way:

1. **Scrub before storing.** Check for tokens, keys and `$HOME` — an earlier
   attempt at fixtures shipped a token-shaped string into a repo file.
2. **Write the ground truth before running the pipeline over it.** Deciding what
   "correct" means after seeing the output is how the synthetic corpus ended up
   measuring itself.
