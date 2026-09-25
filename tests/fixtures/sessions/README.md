# Recorded sessions

Real Claude Code sessions, captured by the hooks while doing real work, stored
with hand-written ground truth. They are the only yardstick for detection and
matching: the synthetic corpus in `tests/benchmarks/cases.py` was written by
whoever was also writing the detector, so it measures internal consistency.
Recorded work does not, and it has repeatedly caught what the corpus could not.
One task banked as six fragments, for example, and a commit made with
`git -C <path> commit` was invisible to marker detection.

## The public set

The public sessions are recorded from a plan fixed in advance, `catalogue.json`:
22 sessions of two kinds of work, each written to test named functions, and the
8 demo recordings described below.
- **Code:** one procedure in a small seeded git repo, `recording/seed-repo/`:
  explore → implement → test → commit.
- **Procedure:** knowledge work from a source file: read → write → check.

`RECORDING.md` is the kit you record from, and `recording/setup.sh <id>`
prepares each session's folder. The catalogue holds every prompt, the prompt
numbers where a new task starts, and the family and subject of every task.
From the catalogue:
- `from_transcript.py --session <id>` finds the recording by its folder and
  writes the truth into the fixture;
- `score.py` reports every detection check per kind of work, and the judge's
  cuts per prompt role;
- `recurrence.py` reports merged pairs by how alike the runs are: identical,
  same goal, or different subject.

The scorers fold every session with no working directory, so all of them count
as one project and runs recorded in different folders can merge. In real use a
candidate is bound to its project (the git repo the work was done in): the
recording folders `C-F1`, `C-F2` and `C-F3` would be three projects there.

`draft_cases.json` holds what a drafted skill must contain for four of these
sessions. `tests/benchmarks/draft_check.py` prepares each draft and checks it.
The drafting itself is a frontier-model call, so you run it.

### The demo recordings

Eight more sessions, `*-demo-*.json`, are the recordings behind the README
video: five runs of a sprint review deck built from GitHub through the GitHub
MCP, two takes that do action items, a LinkedIn post and the sprint review in
one chat, and one run of the `--min-length` feature. The repo they read,
`lucazagaia/skycast`, was made for the recording: its milestones, issues and
pull requests are invented, and the notes they read are copies of
`recording/meeting-1.md` and `recording/notes.md`.

They are not in the catalogue, because they were recorded outside
`~/skillpp-recordings/`, but their truth was fixed the same way, before
recording, by the video's script, and each fixture's `why` states it. They were
held out while the judge's question was restructured and judged only after it
(`docs/research/benchmarks.md`, "The question restructured").

## Public and private sets

The sessions in this directory are public work and ship with the repo. A set
recorded on work that cannot be published (anything from an employer, a client,
or with other people's data in it) lives outside the repo, and is scored by
pointing Skill++ at it:

```bash
SKILL_PLUS_PLUS_FIXTURES=~/path/to/private/sessions python3 -m unittest discover -s tests
SKILL_PLUS_PLUS_FIXTURES=~/path/to/private/sessions python3 tests/fixtures/sessions/score.py
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
| `truth.subjects` | what each banked episode worked on, e.g. `wordfreq --top`: two runs of one family on different subjects are the hard merge |
| `truth.level` | `identical` or `same-goal` for the runs made to be alike; pairs across levels count as different subject |
| `truth.roles` | one role per recorded prompt (`explore`, `correct`, `switch`, `answer`…), so the judge is scored by kind of prompt |
| `session`, `kind`, `surface`, `checks` | the catalogue entry it was recorded from: which session, code or procedure, CLI or desktop, and what it tests |
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

For the public set, record from `RECORDING.md`, then:

```bash
python3 tests/fixtures/sessions/from_transcript.py --all-sessions
```

For any other session, build the fixture from its transcript and give the truth
by hand. `--cut-before-prompt N` saves counting steps:

```bash
python3 tests/fixtures/sessions/from_transcript.py <tag> --name <short-name> \
    --family add-feature-with-tests --family write-makefile --cut-before-prompt 5
```

It warns when a task has fewer than three work steps: below that, the two-step
floor decides the result, not the judge.

It writes beside the others (or into `SKILL_PLUS_PLUS_FIXTURES`, when set), scrubbed,
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
