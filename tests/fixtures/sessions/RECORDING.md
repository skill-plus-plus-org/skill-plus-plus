# Recording the public sessions

Nine short Claude Code chats in an empty folder, on generic tasks that make
their own files. No repo and no hooks are needed: fixtures are built from Claude
Code's own transcripts, so nothing lands in your ledger while you record.

The ground truth for every session is fixed below, before anything is recorded
(the rule in `README.md`). It is written in terms of prompts, which are known
in advance; `from_transcript.py` turns "the task changes at prompt 2" into the
work-step index `truth.boundary_after` needs.

## Setup

```bash
mkdir -p ~/skillpp-recordings
```

Record some sessions in the `claude` CLI and some in the Code tab of the Claude
desktop app, as marked on each one. Both write the same transcript into
`~/.claude/projects/`, and skillpp captures both, but the desktop app gives the
agent extra tools and a different system prompt. Each repeated family is split
across the two, so matching is also measured across them.

- **CLI:** `cd ~/skillpp-recordings && claude`, and end the chat with `/exit`.
- **Desktop:** a new Code session with the folder `~/skillpp-recordings`
  (local, not a worktree). Start a new session for the next one.

Use the same model in both. One chat per session, in the order below. Type the
prompts in order, exactly as written, and let each one finish before sending
the next. Later sessions see the files earlier ones made; that is intended (F
reads A1's file).

Answer any question the agent asks with the shortest reasonable reply, and note
it next to the session, because it becomes part of the recording.

If skillpp is wired into your user settings (`skillpp install --user`), start
each CLI chat as `SKILLPP_INTERNAL=1 claude`, so the recordings are not also
captured into your own ledger. The desktop app has no way to set that, so
record there only while skillpp is not installed for the user.

## The sessions

### A. One procedure, repeated: a small function with its tests

The procedure the matching has to recognise as the same work. Family
`add-function-with-tests`; one episode each. The third run is the first task of
D, cut out of a two-task chat, so the family still reaches the three runs a
candidate needs.

**A1** (CLI)
1. `Create slugify.py with a function slugify(text) that lowercases the text, replaces spaces with hyphens and drops punctuation. Add test_slugify.py with unittest tests, then run them.`
2. `Add a docstring with two examples to slugify.`

**A2** (Desktop)
1. `Create roman.py with a function to_roman(n) that converts 1 to 3999 into Roman numerals. Add test_roman.py with unittest tests, then run them.`
2. `Add a docstring with two examples to to_roman.`

### B. One writing procedure, three times: a LinkedIn post

The same procedure with almost no tool calls, which is where matching on the
conversation instead of the commands matters, and where it is weakest. Nothing
else supplies runs of this family, so all three are needed. Family
`write-linkedin-post`; one episode each.

**B1** (Desktop)
1. `Write a LinkedIn post of 120 to 180 words about why a code review checklist helps into post-checklists.md: a hook in the first line, three short takeaways, and a question at the end.`
2. `Count the words and fix it if it is outside the limit.`

**B2** (CLI)
1. `Write a LinkedIn post of 120 to 180 words about writing the test before the fix into post-tests.md: a hook in the first line, three short takeaways, and a question at the end.`
2. `Count the words and fix it if it is outside the limit.`

**B3** (Desktop)
1. `Write a LinkedIn post of 120 to 180 words about keeping a changelog into post-changelog.md: a hook in the first line, three short takeaways, and a question at the end.`
2. `Count the words and fix it if it is outside the limit.`

### C. Two tasks in one chat, said out loud (CLI)

Two episodes. The task changes at prompt 2. Families `write-makefile`,
`write-gitignore`.

1. `Create a Makefile for a Python project in this folder with the targets test and clean.`
2. `Separate job: write a .gitignore for a Python project.`

### D. Two tasks in one chat, not said (CLI)

Two episodes. The task changes at prompt 3. Families `add-function-with-tests`,
`write-haiku`. The first task is the third run of family A.

1. `Create is_palindrome.py with a function is_palindrome(text) that ignores case and spaces. Add test_is_palindrome.py with unittest tests, then run them.`
2. `Add a docstring with two examples to is_palindrome.`
3. `Write a haiku about Monday mornings into haiku.txt.`

### E. A review request inside one task (Desktop)

One episode: the second and third prompts continue the first. Family
`write-checklist`. This is the shape the judge has cut wrongly before.

1. `Write a six-step checklist for a new developer's first day into onboarding.md.`
2. `Two things. Check that every step is something a new developer can do on day one, and cut it to at most four steps — merge whatever overlaps.`
3. `Looks good. Add a one-line title at the top.`

### F. Only looking around (Desktop)

Nothing to bank: the agent reads a file and answers, so every step only looks.
Zero episodes. Needs A1's `slugify.py`.

1. `Read slugify.py and explain in two sentences what it does. Don't change or run anything.`

## Left out on purpose

Two known gaps are not recorded, because the pipeline cannot pass them by
construction and a recording would only confirm that:

- **Two tasks in one prompt.** The judge only asks where the developer types
  something, so a boundary inside one prompt is invisible to it.
- **A task continued in a second chat.** Each chat is its own session and
  nothing links them, so it always becomes two half-tasks.

Record them once something is built to handle them, with `expected_fail` until
it does.

## After recording

List the transcripts, newest first:

```bash
ls -t ~/.claude/projects/*skillpp-recordings*/*.jsonl
```

Each file name is the session tag. Build a fixture from each with
`from_transcript.py`, fill in `procedure`, `why`, `truth.families` and
`truth.boundary_after` from the table below, and run `score.py` and
`recurrence.py` to record the first baseline in `expected.json`.

| session | where | episodes | boundary at prompt | families |
| --- | --- | --- | --- | --- |
| A1, A2 | CLI, Desktop | 1 each | | `add-function-with-tests` |
| B1, B2, B3 | Desktop, CLI, Desktop | 1 each | | `write-linkedin-post` |
| C | CLI | 2 | 2 | `write-makefile`, `write-gitignore` |
| D | CLI | 2 | 3 | `add-function-with-tests`, `write-haiku` |
| E | Desktop | 1 | | `write-checklist` |
| F | Desktop | 0 | | |
