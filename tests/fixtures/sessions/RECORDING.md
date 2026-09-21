# Recording the public sessions

Fourteen short Claude Code chats in an empty folder, on generic tasks that make
their own files. No repo and no hooks are needed: fixtures are built from Claude
Code's own transcripts, so nothing lands in your ledger while you record.

The ground truth for every session is fixed below, before anything is recorded
(the rule in `README.md`). It is written in terms of prompts, which are known
in advance; `from_transcript.py` turns "the task changes at prompt 2" into the
work-step index `truth.boundary_after` needs.

## Setup

```bash
mkdir -p ~/skillpp-recordings && cd ~/skillpp-recordings
claude
```

One chat per session. Type the prompts in order, exactly as written, and let
each one finish before sending the next. End the chat with `/exit`, then start
the next one with `claude` in the same folder. Later sessions see the files
earlier ones made; that is intended.

Answer any question the agent asks with the shortest reasonable reply, and note
it next to the session, because it becomes part of the recording.

If skillpp is wired into your user settings (`skillpp install --user`), start
each chat as `SKILLPP_INTERNAL=1 claude`, so the recordings are not also
captured into your own ledger.

## The sessions

### A. One procedure, three times: a small function with its tests

The procedure the matching has to recognise as the same work three times.
Family `add-function-with-tests`; one episode each.

**A1**
1. `Create slugify.py with a function slugify(text) that lowercases the text, replaces spaces with hyphens and drops punctuation. Add test_slugify.py with unittest tests, then run them.`
2. `Add a docstring with two examples to slugify.`

**A2**
1. `Create roman.py with a function to_roman(n) that converts 1 to 3999 into Roman numerals. Add test_roman.py with unittest tests, then run them.`
2. `Add a docstring with two examples to to_roman.`

**A3**
1. `Create word_count.py with a function word_count(path) that counts the words in a text file. Add test_word_count.py with unittest tests, then run them.`
2. `Add a docstring with two examples to word_count.`

### B. One writing procedure, three times: a LinkedIn post

The same procedure with almost no tool calls, which is where matching on the
conversation instead of the commands matters. Family `write-linkedin-post`; one
episode each.

**B1**
1. `Write a LinkedIn post of 120 to 180 words about why a code review checklist helps into post-checklists.md: a hook in the first line, three short takeaways, and a question at the end.`
2. `Count the words and fix it if it is outside the limit.`

**B2**
1. `Write a LinkedIn post of 120 to 180 words about writing the test before the fix into post-tests.md: a hook in the first line, three short takeaways, and a question at the end.`
2. `Count the words and fix it if it is outside the limit.`

**B3**
1. `Write a LinkedIn post of 120 to 180 words about keeping a changelog into post-changelog.md: a hook in the first line, three short takeaways, and a question at the end.`
2. `Count the words and fix it if it is outside the limit.`

### C. Two tasks in one chat, said out loud

Two episodes. The task changes at prompt 2. Families `write-makefile`,
`write-gitignore`.

1. `Create a Makefile for a Python project in this folder with the targets test and clean.`
2. `Separate job: write a .gitignore for a Python project.`

### D. Two tasks in one chat, not said

Two episodes. The task changes at prompt 3. Families `add-function-with-tests`,
`write-haiku`. The first task is a fourth run of family A, so it should join it.

1. `Create is_palindrome.py with a function is_palindrome(text) that ignores case and spaces. Add test_is_palindrome.py with unittest tests, then run them.`
2. `Add a docstring with two examples to is_palindrome.`
3. `Write a haiku about Monday mornings into haiku.txt.`

### E. A review request inside one task

One episode: the second and third prompts continue the first. Family
`write-checklist`. This is the shape the judge has cut wrongly before.

1. `Write a six-step checklist for a new developer's first day into onboarding.md.`
2. `Two things. Check that every step is something a new developer can do on day one, and cut it to at most four steps — merge whatever overlaps.`
3. `Looks good. Add a one-line title at the top.`

### F. Two tasks with no new prompt between them

Two tasks, one prompt. The judge only asks where the developer types something,
so it cannot see this boundary: record it as a known gap
(`expected_fail`), which makes the blind spot measurable. Families
`write-fizzbuzz`, `write-grocery-list`.

1. `Create fizzbuzz.py that prints FizzBuzz for 1 to 30, and separately write a grocery list for a pasta dinner into groceries.md.`

### G. One task continued in a second chat

Two chats, one procedure. Each chat is its own session, so this records what
happens to a task split across chats: two half-episodes that nothing links.
Family `maintain-changelog` for both; one episode each.

**G1**
1. `Start a CHANGELOG.md in the Keep a Changelog format with an Unreleased section, and add an entry saying slugify was added.`

**G2** (a new chat)
1. `Continue the changelog from before: add a 0.1.0 section dated today and move the Unreleased entries into it.`

### H. A question with no work

Nothing to bank: no tool call, or only looking around. Zero episodes.

1. `What is the difference between a list and a tuple in Python? Answer in three bullets.`

### I. A step that fails, then works

One episode, with a failed step in it. Family `run-or-create-script`.

1. `Run python3 hello.py. If it fails, create it so it prints hello, then run it again.`

## After recording

List the transcripts, newest first:

```bash
ls -t ~/.claude/projects/*skillpp-recordings*/*.jsonl
```

Each file name is the session tag. Build a fixture from each with
`from_transcript.py`, fill in `procedure`, `why`, `truth.families` and
`truth.boundary_after` from the table above, and run `score.py` and
`recurrence.py` to record the first baseline in `expected.json`.

| session | episodes | boundary at prompt | families |
| --- | --- | --- | --- |
| A1, A2, A3 | 1 each | | `add-function-with-tests` |
| B1, B2, B3 | 1 each | | `write-linkedin-post` |
| C | 2 | 2 | `write-makefile`, `write-gitignore` |
| D | 2 | 3 | `add-function-with-tests`, `write-haiku` |
| E | 1 | | `write-checklist` |
| F | 2, recorded as a known gap | (inside prompt 1) | `write-fizzbuzz`, `write-grocery-list` |
| G1, G2 | 1 each | | `maintain-changelog` |
| H | 0 | | |
| I | 1 | | `run-or-create-script` |
