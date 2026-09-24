# Architecture

How a session becomes a candidate and a candidate becomes a skill, and where
each part lives. Read with the code: every module opens with a docstring saying
why it exists, and the comments cite the measurement behind each choice.

## The pipeline

```
Claude Code session
  │  UserPromptSubmit, PostToolUse           capture.handle_prompt / handle_tool
  ▼
~/.claude/skill-plus-plus/sessions/<id>.json   scrubbed on write (sanitize.scrub)
  │  SessionEnd: mark ended, start a detached `skill-plus-plus fold-session <id>`
  │  SessionStart: the same for sessions held or never ended (fold-pending)
  ▼
fold_session_now                               capture, one lock per session
  ├─ boundary.judge_session                    the local model, once per prompt gap:
  │                                            "does the new message start a new task?"
  ├─ segment.segment                           cut at the judged boundaries
  ├─ capture._fold_steps                       drop thin episodes, keep the work
  ├─ matching.find_same                        embed and compare with every entry
  └─ ledger                                    new candidate, or one more occurrence
  ▼
skill-plus-plus web / review / show            web.collect_state, summary
  │  promote → Draft Skill
  ▼
skill-plus-plus draft                          cli.cmd_draft: your agent, in a temp home,
  │                                            reads `show --json --draft`, writes SKILL.md
  ▼
~/.claude/skill-plus-plus/drafts/<id>/SKILL.md open questions answered, revised, downloaded
```

Nothing that calls a model runs inside a hook: the judge, the embeddings and
naming all run in the detached fold worker. A session whose model did not answer
is **held**, not guessed at, and banked by the next `SessionStart`.

## Modules

| Module | Role |
| --- | --- |
| `capture` | The hook handlers, the session buffer, and the fold: from a finished session to ledger entries. |
| `boundary` | The boundary judge. Builds one question per prompt gap from `prompts/new_job.md` and asks the local model. |
| `segment` | Cuts a judged session into episodes, and decides what counts as work (`is_read_only`, `is_prompt`). |
| `matching` | Embeds an episode and decides whether it is the same procedure as an existing entry. |
| `ledger` | Candidates as readable markdown files: steps, the first run's conversation, occurrences, status. |
| `local` | One question to a small local model through Ollama, with context sizing and timing. |
| `episode` | `sift`: whether a candidate is a method or one particular job. |
| `signals` | What a candidate does (`effects`: commands, writes, destructive steps) and the questions its trace raises. |
| `summary` | The review surface, the one-line summaries, and the `SKILL.md` scaffold. |
| `web` | The review page: state, the grouped steps, drafts, and the actions, each run as a CLI subprocess. |
| `cli` | Every command, including `draft`/`revise` and the agent they start. |
| `install` | Wiring hooks into a settings file and removing them, and copying the slash commands. |
| `lifecycle` | Tiers (hot, cold, archived) and staleness for installed skills. |
| `normalize` | Parameterising paths and ids, and the shape of a step used for comparison. |
| `sanitize` | Scrubbing secrets and addresses from every captured string. |
| `decisions` | The append-only record of what you promoted and dismissed. |
| `config` | Paths and every `SKILL_PLUS_PLUS_*` setting, with the reason for each default. |
| `similar` | Pieces shared by the matching and merging code. |

`skill_plus_plus/prompts/` holds what the local model is asked. `skill_plus_plus/commands/`
holds the slash commands: `skill-plus-plus-draft.md` is handed to the drafting agent,
the other three are copied into your settings by `install`.

## Rules the code keeps

- **A hook never raises.** Every handler logs and exits 0; capture is never worth
  breaking someone's session over.
- **Nothing changes without `--apply`.** Editing settings and spending a model
  call are dry runs by default.
- **Nothing installs itself.** A drafted skill is only ever written to
  `drafts/`; promoting and installing stay your decision.
- **Scrub before disk.** There is no moment when an unscrubbed trace is written.
- **Hold rather than guess.** Without a judge verdict or an embedding, a session
  waits; boundaries guessed from git verbs measured worse than no cuts at all.
- **Precision over recall when merging.** A missed merge leaves a duplicate you
  can see; a wrong one mixes two procedures into one skill.

## One place for each rule

Several questions are asked in more than one place. Each has a single answer in
the code; reuse it rather than writing a second one.

| Question | Answered by |
| --- | --- |
| Does this step only look at things? | `segment.is_read_only` |
| Did this command destroy something? | `signals.DESTRUCTIVE` |
| What does the judge see at a gap? | `boundary.judge_gap` (live and in `judge_replay.py`) |
| Which hooks are wired here? | `install.installed_events` |
| What does a session bank? | `capture.fold_session`, which `score.py` calls rather than copying |
| How are sessions loaded for scoring? | `tests/fixtures/sessions/score.load` (reads `SKILL_PLUS_PLUS_FIXTURES`) |

## Measuring a change

The unit suite (`python3 -m unittest discover -s tests`) needs no model: the
judge, the embeddings, the describer and naming are all stubbed. It says whether
the code still does what it did.

Whether a change makes detection or matching better is a separate question,
answered on recorded sessions (`tests/fixtures/sessions/`), and these do need
Ollama:

| Script | Measures | Needs |
| --- | --- | --- |
| `tests/fixtures/sessions/score.py` | each session cut and banked as its ground truth says | the embedding model |
| `tests/fixtures/sessions/recurrence.py` | repeated work ending up as one candidate, with no wrong merge | the embedding model |
| `tests/benchmarks/judge_replay.py` | the judge alone, gap by gap, with any input slot varied | the local model |
| `tests/benchmarks/merge_ladder.py` | one change at a time to what matching embeds | the embedding model |

Change one thing at a time, compare against the recorded baseline in
`expected.json`, and write the result into `docs/research/benchmarks.md`.
