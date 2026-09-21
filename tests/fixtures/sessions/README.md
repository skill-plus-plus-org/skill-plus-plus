# Recorded sessions

Real Claude Code sessions, captured by the hooks while doing real work, stored
with hand-written ground truth. They are the only yardstick for detection and
matching: the synthetic corpus in `tests/benchmarks/cases.py` was written by
whoever was also writing the detector, so it measures internal consistency.
Recorded work does not, and it has repeatedly caught what the corpus could not.
One task banked as six fragments, for example, and a commit made with
`git -C <path> commit` was invisible to marker detection.

## Public and private sets

The sessions in this directory are public work and ship with the repo. A set
recorded on work that cannot be published (anything from an employer, a client,
or with other people's data in it) lives outside the repo, and is scored by
pointing skillpp at it:

```bash
SKILLPP_FIXTURES=~/path/to/private/sessions python3 -m unittest discover -s tests
SKILLPP_FIXTURES=~/path/to/private/sessions python3 tests/fixtures/sessions/score.py
```

Everything that reads sessions goes through `score.load`, so this one variable
covers the unit suite, `score.py`, `recurrence.py` and the benchmarks in
`tests/benchmarks/`. With no sessions here and the variable unset, the tests
that need them skip and say why.

Numbers that belong to one set, and would be wrong for any other, live beside
its sessions in `expected.json`:

| key | meaning |
| --- | --- |
| `family_sizes` | how many banked episodes each procedure family should hold (`recurrence.py`) |
| `judge_prompts` | `count` and `sha256` of every judge prompt at the defaults, so a change that alters what the judge reads is seen |

## What is in a file

| key | meaning |
| --- | --- |
| `procedure` | what the developer was actually doing, in their words |
| `truth.episodes` | how many candidates a correct run banks |
| `truth.boundary_after` | the work steps after which a new task starts, when there is more than one |
| `truth.title` | what the entry should be called |
| `truth.must_contain` | substrings that must survive into a banked episode: the steps the procedure exists for |
| `truth.families` | one procedure family per banked episode, for `recurrence.py` |
| `why` | why the ground truth is what it is, argued rather than asserted |
| `history` | what the pipeline did before, so a regression is recognisable |
| `steps` | the step stream, with `$HOME` templated out |

`truth.must_contain` is the load-bearing field. Episode counts can be right for
the wrong reasons; these name the steps whose loss would make the captured skill
a different and worse procedure than the one performed.

**`truth.episodes` counts candidates banked, not episodes cut.** Capture runs
three stages: `segment` cuts the session up, `fold_session` discards the flagged
episodes, and `_fold_steps` discards anything under two substantive steps. Only
what survives all three is a candidate, and that is the number to write here. A
trailing `git status` after a commit is cut as an episode and never banked, so
it does not count.

`score.py` enforces that by calling `fold_session` and reading the ledger, rather
than calling `segment` and counting. Do not reintroduce a private copy of the
discard rules here; when the fold stage learns to throw something else away,
this should follow without an edit.

## Running them

```bash
python3 tests/fixtures/sessions/score.py          # all sessions
python3 tests/fixtures/sessions/score.py 1c3c9422 # one, by tag prefix
```

They also run in the unit suite (`TestLiveSessions`), so a change that breaks a
recorded session fails the suite rather than being noticed three weeks later.

## Adding one

Do real work in a project with the hooks installed, end the session explicitly
so `SessionEnd` fires, then build the fixture from its transcript:

```bash
python3 tests/fixtures/sessions/from_transcript.py <tag> --name <short-name>
```

It writes beside the others (or into `SKILLPP_FIXTURES`, when set), scrubbed,
and refuses to write while your home path or account name is still in it.

Three rules, all learned the hard way:

1. **Only public work goes in this directory.** Nothing from an employer, a
   client or a private repo, and no other people's names. Everything here is
   published with the code.
2. **Read it before committing.** The scrubber catches keys, tokens, emails and
   `$HOME`; it does not catch names, hostnames or proprietary content in prose.
3. **Write the ground truth before running the pipeline over it.** Deciding what
   "correct" means after seeing the output is how the synthetic corpus ended up
   measuring itself.
