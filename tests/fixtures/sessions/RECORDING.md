# Recording the public sessions

Twenty-two Claude Code sessions, in two kinds:

- **Code:** one real procedure in a small seeded git repo, `textkit`: explore
  an idea → implement it → run the tests → commit.
- **Procedure:** knowledge work from a source file: read → write → check. The
  varied case is a talk deck: outline, recheck, build only after approval.

About 2 hours in total. Each session stands alone, so split it across as many
sittings as you like.

The ground truth for every session is fixed in `catalogue.json` before
anything is recorded (the rule in `README.md`). The prompts below are copied
from it, and a test keeps the two identical.

## Before you start

- **One session, one chat.** Run its `setup.sh` line, start the chat as
  written, type the prompts in order exactly as written, and let each one
  finish before sending the next.
- **Use the same Claude model** in the CLI and the Desktop app.
- **If the agent asks something,** answer in the fewest words and write the
  answer down next to the session. It becomes part of the recording.
- **Run `setup.sh` from the repo root.** It creates
  `~/skillpp-recordings/<session>/`. Code sessions get a fresh `textkit` repo,
  committed once by a neutral author (`Recorder <recorder@example.com>`), so
  your name never reaches a transcript. To record a session again, delete its
  folder first.
- **Keep your own ledger clean.** If skillpp is installed for your user
  (`skillpp install --user`), start CLI chats as `SKILLPP_INTERNAL=1 claude`.
  The Desktop app can't set that, so record there only while it isn't
  installed for the user.

## Code sessions (explore → implement → test → commit)

### C-F1, C-F2, C-F3: Identical prompts, three times

The same four prompts in three fresh repos. The upper bound: if identical prompts don't become one candidate, nothing will.

*Expected:* 1 saved task(s) each · cut at prompt: none · families: `add-feature-with-tests`

**C-F1** · CLI

```bash
tests/fixtures/sessions/recording/setup.sh C-F1
```
Start `cd ~/skillpp-recordings/C-F1 && claude`, and end with `/exit`.

1. `Look at textkit/wordfreq.py and its tests, and suggest how to add a --top option that sets how many words are printed. Don't change anything yet.`
2. `Sounds good, implement it.`
3. `Add a test for --top and run the whole test suite.`
4. `Commit it with a short message.`

**C-F2** · Desktop

```bash
tests/fixtures/sessions/recording/setup.sh C-F2
```
Start a new Code session in the Desktop app, folder `~/skillpp-recordings/C-F2` (local, not a worktree).

1. `Look at textkit/wordfreq.py and its tests, and suggest how to add a --top option that sets how many words are printed. Don't change anything yet.`
2. `Sounds good, implement it.`
3. `Add a test for --top and run the whole test suite.`
4. `Commit it with a short message.`

**C-F3** · CLI

```bash
tests/fixtures/sessions/recording/setup.sh C-F3
```
Start `cd ~/skillpp-recordings/C-F3 && claude`, and end with `/exit`.

1. `Look at textkit/wordfreq.py and its tests, and suggest how to add a --top option that sets how many words are printed. Don't change anything yet.`
2. `Sounds good, implement it.`
3. `Add a test for --top and run the whole test suite.`
4. `Commit it with a short message.`

### C-V1: Same goal, different executions

All three add the same feature, a `--min-length` option, but you drive it differently each time.

*Expected:* 1 saved task(s) each · cut at prompt: none · families: `add-feature-with-tests`

**C-V1** · Desktop

```bash
tests/fixtures/sessions/recording/setup.sh C-V1
```
Start a new Code session in the Desktop app, folder `~/skillpp-recordings/C-V1` (local, not a worktree).

1. `Look at textkit/wordfreq.py and suggest how to add a --min-length option that skips words shorter than N letters. Don't change anything yet.`
2. `Go ahead and implement it.`
3. `Add tests for --min-length and run all the tests.`
4. `Commit it.`

**C-V2** · CLI

```bash
tests/fixtures/sessions/recording/setup.sh C-V2
```
Start `cd ~/skillpp-recordings/C-V2 && claude`, and end with `/exit`.

1. `i want wordfreq to skip words shorter than N letters with a --min-length flag. write the test for it first`
2. `now make it pass`
3. `run all tests`
4. `commit`

**C-V3** · Desktop

```bash
tests/fixtures/sessions/recording/setup.sh C-V3
```
Start a new Code session in the Desktop app, folder `~/skillpp-recordings/C-V3` (local, not a worktree).

1. `How would you add a --min-length option to textkit/wordfreq.py? Read the code first and propose something, no edits yet.`
2. `Do it.`
3. `Two things. Check that --min-length 0 behaves exactly like before, and that the README mentions the new option.`
4. `Make the default 1 instead of 0, and rerun the tests.`
5. `Looks good, commit.`

### C-N1: Must not merge: a refactor

The same four phases as a feature, but a different goal: no new behaviour. It must not join the feature candidate.

*Expected:* 1 saved task(s) each · cut at prompt: none · families: `refactor-keeping-tests-green`

**C-N1** · CLI

```bash
tests/fixtures/sessions/recording/setup.sh C-N1
```
Start `cd ~/skillpp-recordings/C-N1 && claude`, and end with `/exit`.

1. `Read textkit/slugify.py and tell me what you would simplify without changing its behaviour. Don't edit yet.`
2. `Do it.`
3. `Run the tests to show nothing changed.`
4. `Commit it.`

### C-2said: Two tasks, switch announced

The task changes at prompt 5, and you say so.

*Expected:* 2 saved task(s) each · cut at prompt: 5 · families: `add-feature-with-tests`, `write-makefile`

**C-2said** · CLI

```bash
tests/fixtures/sessions/recording/setup.sh C-2said
```
Start `cd ~/skillpp-recordings/C-2said && claude`, and end with `/exit`.

1. `Look at textkit/wordfreq.py and suggest how to add a --json flag that prints the counts as JSON. Don't change anything yet.`
2. `Implement it.`
3. `Add a test and run all the tests.`
4. `Commit it.`
5. `Okay, now we will do something else. I want a Makefile for this project — look at how the tests are run and suggest the targets. Don't create it yet.`
6. `Sounds right, create it with a test target and a clean target that removes __pycache__.`
7. `Run both targets.`
8. `Commit the Makefile.`

### C-2unsaid: Two tasks, switch not announced

The task changes at prompt 5 without a word. A refactor and a feature side by side.

*Expected:* 2 saved task(s) each · cut at prompt: 5 · families: `refactor-keeping-tests-green`, `add-feature-with-tests`

**C-2unsaid** · Desktop

```bash
tests/fixtures/sessions/recording/setup.sh C-2unsaid
```
Start a new Code session in the Desktop app, folder `~/skillpp-recordings/C-2unsaid` (local, not a worktree).

1. `Read textkit/titlecase.py and tell me what you would simplify without changing its behaviour. Don't edit yet.`
2. `Do it.`
3. `Run the tests to show nothing changed.`
4. `Commit it.`
5. `Look at textkit/slugify.py and suggest how to keep accented letters (é becomes e) instead of dropping them. Don't change anything yet.`
6. `Go ahead.`
7. `Add tests for accented letters and run all the tests.`
8. `Commit it.`

### C-3: Three tasks

The task changes at prompt 5 (announced) and at prompt 9 (not announced). All three tasks have the same four phases.

*Expected:* 3 saved task(s) each · cut at prompt: 5, 9 · families: `add-feature-with-tests`, `write-makefile`, `refactor-keeping-tests-green`

**C-3** · CLI

```bash
tests/fixtures/sessions/recording/setup.sh C-3
```
Start `cd ~/skillpp-recordings/C-3 && claude`, and end with `/exit`.

1. `Look at textkit/titlecase.py and suggest how to handle apostrophes, so "don't" stays "Don't" and not "Don'T". Don't change anything yet.`
2. `Implement it.`
3. `Add a test and run all the tests.`
4. `Commit it.`
5. `Okay, now let's do something else. I'd like a Makefile for this project — look at how the tests are run and suggest which targets it should have. Don't create it yet.`
6. `Good, create it.`
7. `Run each target to check they work.`
8. `Commit it.`
9. `Read textkit/wordfreq.py and tell me what you would simplify without changing its behaviour. Don't edit yet.`
10. `Do it.`
11. `Run the tests to show nothing changed.`
12. `Commit it.`

### C-same: Two tasks of the same kind

Two features in a row. A second feature is a new job, but not an unrelated one: the hardest cut in the set.

*Expected:* 2 saved task(s) each · cut at prompt: 5 · families: `add-feature-with-tests`, `add-feature-with-tests`

**C-same** · Desktop

```bash
tests/fixtures/sessions/recording/setup.sh C-same
```
Start a new Code session in the Desktop app, folder `~/skillpp-recordings/C-same` (local, not a worktree).

1. `Look at textkit/slugify.py and suggest how to add a max_length argument that cuts the slug at a word boundary. Don't change anything yet.`
2. `Implement it.`
3. `Add tests and run all the tests.`
4. `Commit it.`
5. `Now the same kind of thing for titlecase: suggest how to add a small_words argument, so words like "of" and "the" stay lowercase unless they come first. Don't change anything yet.`
6. `Implement it.`
7. `Add tests and run all the tests.`
8. `Commit it.`

### L1: Look only

Only reading and answering. Nothing should be saved.

*Expected:* 0 saved task(s) each · cut at prompt: none · families: none

**L1** · CLI

```bash
tests/fixtures/sessions/recording/setup.sh L1
```
Start `cd ~/skillpp-recordings/L1 && claude`, and end with `/exit`.

1. `Read textkit/slugify.py and explain in three sentences how it works. Don't change or run anything.`
2. `What would it return for an empty string?`
3. `And for a string of only punctuation?`

## Procedure sessions (read → write → check)

### P-F1, P-F2, P-F3: Identical prompts, three times

The same five prompts, three times.

*Expected:* 1 saved task(s) each · cut at prompt: none · families: `post-from-source`

**P-F1** · Desktop

```bash
tests/fixtures/sessions/recording/setup.sh P-F1
```
Start a new Code session in the Desktop app, folder `~/skillpp-recordings/P-F1` (local, not a worktree).

1. `Read notes.md and write a LinkedIn post of 120 to 180 words from it into post.md: a hook in the first line, three short takeaways, and a question at the end. Only use facts from the notes.`
2. `Count the words with wc -w and fix the post if it is outside the limit.`
3. `Check every claim in the post against notes.md and tell me which ones aren't backed by it.`
4. `Make the hook punchier and fix anything that wasn't backed by the notes.`
5. `Count the words again.`

**P-F2** · CLI

```bash
tests/fixtures/sessions/recording/setup.sh P-F2
```
Start `cd ~/skillpp-recordings/P-F2 && claude`, and end with `/exit`.

1. `Read notes.md and write a LinkedIn post of 120 to 180 words from it into post.md: a hook in the first line, three short takeaways, and a question at the end. Only use facts from the notes.`
2. `Count the words with wc -w and fix the post if it is outside the limit.`
3. `Check every claim in the post against notes.md and tell me which ones aren't backed by it.`
4. `Make the hook punchier and fix anything that wasn't backed by the notes.`
5. `Count the words again.`

**P-F3** · Desktop

```bash
tests/fixtures/sessions/recording/setup.sh P-F3
```
Start a new Code session in the Desktop app, folder `~/skillpp-recordings/P-F3` (local, not a worktree).

1. `Read notes.md and write a LinkedIn post of 120 to 180 words from it into post.md: a hook in the first line, three short takeaways, and a question at the end. Only use facts from the notes.`
2. `Count the words with wc -w and fix the post if it is outside the limit.`
3. `Check every claim in the post against notes.md and tell me which ones aren't backed by it.`
4. `Make the hook punchier and fix anything that wasn't backed by the notes.`
5. `Count the words again.`

### P-V1: Same goal, different executions: a talk deck

Outline first, you recheck it, and the deck is built only after you approve. The source, the audience and your wording change each time.

*Expected:* 1 saved task(s) each · cut at prompt: none · families: `talk-deck-from-docs`

**P-V1** · CLI

```bash
tests/fixtures/sessions/recording/setup.sh P-V1
```
Start `cd ~/skillpp-recordings/P-V1 && claude`, and end with `/exit`.

1. `I need to give a 10-minute talk to new users about skillpp. The material is in README.md. Propose the content for the slides — a title and 3 to 4 bullets per slide — but don't build anything yet.`
2. `Two things. Check every command and number on the slides against README.md and tell me which ones aren't backed by it. And cut it to at most 6 slides.`
3. `Fix the ones that aren't backed, then show me the final outline.`
4. `Looks good. Build it as slides.pptx.`
5. `Open the file and check it has the slides from the outline, in order.`

**P-V2** · Desktop

```bash
tests/fixtures/sessions/recording/setup.sh P-V2
```
Start a new Code session in the Desktop app, folder `~/skillpp-recordings/P-V2` (local, not a worktree).

1. `hey i need a short talk for teammates next week on how to install and use skillpp day to day. use docs/usage.md. give me an outline first`
2. `quick question: for a 5 minute talk how many slides is reasonable?`
3. `ok. for me its important everything is from usage.md. can you check the outline against it`
4. `keep it but shorten to 5 slides and add one slide on troubleshooting`
5. `ok go ahead and build it as a pptx`

**P-V3** · CLI

```bash
tests/fixtures/sessions/recording/setup.sh P-V3
```
Start `cd ~/skillpp-recordings/P-V3 && claude`, and end with `/exit`.

1. `Prepare a 5-minute talk for engineers on how skillpp works inside, from docs/architecture.md. Start with an outline of at most 6 slides for me to approve.`
2. `Drop the slide on the module map and add one on the design invariants instead.`
3. `Approved — build slides.pptx.`
4. `The title slide should say "How skillpp works". Fix it and check the other slides didn't change.`

### P-N1: Must not merge: release notes

Reads a source, writes short text, checks it against the source, like the post, but a different procedure.

*Expected:* 1 saved task(s) each · cut at prompt: none · families: `release-notes-from-changelog`

**P-N1** · Desktop

```bash
tests/fixtures/sessions/recording/setup.sh P-N1
```
Start a new Code session in the Desktop app, folder `~/skillpp-recordings/P-N1` (local, not a worktree).

1. `Read CHANGELOG.md and write release notes for version 1.4.0 for end users into release-notes.md: what's new and what's fixed, nothing internal.`
2. `Check that every line comes from the changelog and that the internal changes were left out.`
3. `Group them under "New" and "Fixed", one short sentence each.`
4. `Add a one-line intro that names the version.`

### P-2said: Two tasks, switch announced

The task changes at prompt 3, and you say so.

*Expected:* 2 saved task(s) each · cut at prompt: 3 · families: `action-items-from-notes`, `release-notes-from-changelog`

**P-2said** · Desktop

```bash
tests/fixtures/sessions/recording/setup.sh P-2said
```
Start a new Code session in the Desktop app, folder `~/skillpp-recordings/P-2said` (local, not a worktree).

1. `Read meeting-1.md, write the action items into actions.md with owner and due date, and check each against the notes.`
2. `Sort them by owner.`
3. `Okay, now we will do something else. Read CHANGELOG.md and write user-facing release notes for 1.4.0 into release-notes.md, leaving out internal changes.`
4. `Check them against the changelog.`
5. `Add a one-line intro that names the version.`

### P-2unsaid: Two tasks, switch not announced

A deck, then action items, with no word in between. The task changes at prompt 4.

*Expected:* 2 saved task(s) each · cut at prompt: 4 · families: `talk-deck-from-docs`, `action-items-from-notes`

**P-2unsaid** · CLI

```bash
tests/fixtures/sessions/recording/setup.sh P-2unsaid
```
Start `cd ~/skillpp-recordings/P-2unsaid && claude`, and end with `/exit`.

1. `Propose an outline for a 5-minute talk on what skillpp keeps private, from docs/privacy.md. At most 5 slides; don't build it yet.`
2. `Add a slide on what leaves the machine when you draft a skill.`
3. `Good, build it as privacy.pptx.`
4. `Read meeting-2.md and write the action items with owner and due date into actions.md.`
5. `Check each one against the notes.`
6. `Sort them by due date.`

### P-3: Three tasks

The task changes at prompt 3 (announced) and at prompt 5 (not announced).

*Expected:* 3 saved task(s) each · cut at prompt: 3, 5 · families: `post-from-source`, `action-items-from-notes`, `release-notes-from-changelog`

**P-3** · Desktop

```bash
tests/fixtures/sessions/recording/setup.sh P-3
```
Start a new Code session in the Desktop app, folder `~/skillpp-recordings/P-3` (local, not a worktree).

1. `Read notes.md and write a 120 to 180 word LinkedIn post into post.md; check the length with wc -w.`
2. `Make the hook punchier.`
3. `Okay, now something else. Write the action items from meeting-3.md into actions.md with owner and due date.`
4. `Check them against the notes.`
5. `Read CHANGELOG.md and write release notes for 1.4.0 into release-notes.md for end users.`
6. `Check nothing internal slipped in.`

## Left out on purpose

Two known gaps are not recorded, because the pipeline can't pass them by
construction and a recording would only confirm that:

- **Two tasks in one prompt.** The judge is only asked where you type
  something, so a change inside one prompt is invisible to it.
- **A task continued in a new chat.** Each chat is its own session and
  nothing links them, so it always becomes two half-tasks.

## After recording

Each transcript lands in `~/.claude/projects/`, under a folder named after
the session folder. So `from_transcript.py --session <id>` finds it and fills
in the truth from `catalogue.json`:

```bash
python3 tests/fixtures/sessions/from_transcript.py --all-sessions
```

Then replay the judge (`tests/benchmarks/judge_replay.py --write`) and score
with `score.py` and `recurrence.py`. Each asks a local model, so only with a
go. Their numbers become the baseline in `expected.json`.
