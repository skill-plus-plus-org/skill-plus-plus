# Using skillpp

The README has the quickstart. This page covers each part in more depth: where
capture works, installing, daily use, turning a candidate into a skill,
troubleshooting and removing it again.

## What is captured

Capture runs through Claude Code hooks, so it works wherever Claude Code reads
your settings: the `claude` CLI, and the Code tab of the Claude desktop app,
which runs Claude Code on your machine. Plain chats, whether on claude.ai or in
the desktop app's Chat tab, have no hooks and are not captured.

Four hooks are wired:

| Hook | What it does |
| --- | --- |
| `UserPromptSubmit` | records what you asked, scrubbed |
| `PostToolUse` | records the tool, its input (up to 2,000 characters a field) and the start of its result |
| `SessionEnd` | marks the session ended and starts a detached worker that folds it into the ledger |
| `SessionStart` | starts the same worker for sessions that ended without being folded: held because Ollama was down, or never ended (idle 12 hours) |

A hook never blocks you and never fails your session: errors go to
`~/.claude/skillpp/skillpp.log` and the hook exits 0. The local model runs in the
worker after the session, not while you work.

## Installing

```bash
pipx install git+https://github.com/himanshu096/skill-plus-plus
```

Then choose where the hooks go. Every form is a dry run that prints the exact
change until you add `--apply`, and an existing settings file is backed up
beside itself first.

```bash
skillpp install --project ~/code/my-repo --apply   # one repo: <repo>/.claude/settings.json
skillpp install --user --apply                     # every project: ~/.claude/settings.json
skillpp install --settings path/to/settings.json --apply
```

It also copies two slash commands into the matching `commands/` folder,
`/skillpp-review` and `/skillpp-new`, and downloads the two
Ollama models detection needs (`gemma4:e4b` and `nomic-embed-text`, about 10 GB)
if they are missing. The dry run lists what it would download. Ollama itself
has to be installed and running first (https://ollama.com); without it the
hooks go in anyway, and sessions wait until it answers. `--no-models` skips the
download.

Hooks are read when a session starts, so start a new session afterwards (a new
chat in the CLI, or a new Code session in the desktop app), and check:

```bash
skillpp doctor
```

`doctor` reports which hooks are wired in the project and user settings,
whether Ollama answers and has both models, how many sessions are waiting to be
banked, and what the ledger holds.

From a clone instead of `pipx`, `pip install -e .` puts `skillpp` on your PATH
the same way; `python3 bin/skillpp` works without installing anything.

## Daily use

Work as usual. When a session ends, skillpp cuts it into tasks and compares each
with what it has seen before. Two thresholds decide what you see:

- a task needs at least **two substantive steps** to be kept at all, so a lone
  `git status` or a question with no work is not a candidate;
- a candidate needs to be seen **three times** before you can promote it.

```bash
skillpp web            # the review page, http://127.0.0.1:8765
skillpp review --all   # the same queue in the terminal, below-threshold included
skillpp show <id>      # one candidate: effects, evidence, open questions
skillpp search deploy  # everything you did that mentions a word
```

On the page, **Candidates** shows each one with a summary and its steps grouped
under the requests they served; open a request to see every step in order.
Promote what is worth a skill, dismiss what is not (a dismissed one can be
reinstated).

### Shortcuts

- **`/skillpp-new`**, or `skillpp dictate`, describes a procedure instead of
  performing it: the agent asks what the description leaves out, and the
  candidate skips the three-times rule. *Work in progress: how it works may
  change.*
- **`SKILLPP_RECURRENCE=1`** makes every candidate ready at once.

## From a candidate to a skill

1. **Draft Skill** on a promoted candidate. An optional note tells the agent
   what to look out for; it reads only the candidate's first run, so the note is
   where anything the later runs taught you goes.
2. The agent writes `SKILL.md`: when to use it, the procedure as numbered steps,
   and under `## Open questions` whatever it could not tell from the run. It may
   also decline, when the work is not a reusable procedure; the page then says
   why, and you can draft again with a note.
3. In **Drafts**, answer the open questions. The answers go back to the agent,
   which folds each into the skill. **Revise** sends any other instruction.
4. **Download skill** once no question is left, unzip into `~/.claude/skills/`
   (or `<repo>/.claude/skills/`), and record it:

   ```bash
   skillpp promote <id> --skill-path ~/.claude/skills/<name>/SKILL.md
   ```

The same from the terminal:

```bash
skillpp draft <id> --note "what to look out for" --apply
skillpp revise <id> --instruction "what to change" --apply
```

The agent is whatever `SKILLPP_AGENT` names, `claude -p` by default. It runs in
a temporary folder with only the draft instructions and a way to read the
candidate, never installs anything, and writes its transcript to
`~/.claude/skillpp/drafts/<id>/agent.log`.

### Skills in the Claude desktop chat

Skills reach the desktop app's chat by upload, one zip per skill, under
**Customize → Skills**. `bundle` writes them and checks the published limits (a
`name` of at most 64 characters, a `description` of at most 200):

```bash
skillpp bundle --format upload --out ~/skill-uploads
```

An uploaded skill lives in your account, not on your machine. Read it before
uploading. For a team, `--format plugin --plugin-name <name> --with-commands`
writes a Claude Code plugin folder instead.

## Housekeeping

```bash
skillpp expire                 # delete candidates still collecting past SKILLPP_TTL_DAYS
skillpp lifecycle -v           # your skills: tiers, use, references that went stale
skillpp tier <name> cold       # move a skill out of the loaded index, without deleting it
skillpp check --name <name>    # are the programs and MCP servers it needs present?
skillpp reconcile              # promoted skills whose file is gone
```

Nothing runs these for you, and nothing a skill depends on is ever deleted.

## Troubleshooting

**Nothing shows up on the review page.**
- Run `skillpp doctor`. A missing `SessionEnd` hook captures every step and banks
  none of it.
- `skillpp stats` shows sessions held because Ollama did not answer. Start
  Ollama; they are banked at the next session start, or now with
  `skillpp fold-pending`.
- `skillpp review --all` shows candidates below the three-times threshold.
- The log is `~/.claude/skillpp/skillpp.log`.

**A draft failed.** Read `~/.claude/skillpp/drafts/<id>/agent.log`.
- *Not logged in*: run `claude` once, then `/login`.
- *`claude` not found*: it is often not on the PATH a hook or the page sees. Set
  `SKILLPP_AGENT` to its full path, keeping `-p {PROMPT}` and the rest.
- *Inconclusive*: the agent wrote nothing and did not say it was declining. A
  denied tool is the usual cause; its output is in the log.
- *Timed out*: drafts get 900 seconds; `skillpp draft … --timeout N` gives more.

**The page will not start: `Address already in use`.** Another `skillpp web` is
running. Stop it with Ctrl-C in its terminal, or use `skillpp web --port 8766`.

**Tool calls feel slow.** Check that `SKILLPP_DESCRIBE` is not set to `1`; it asks
the local model about every tool call inside the hook.

**Leave one session out.** Start it as `SKILLPP_INTERNAL=1 claude`, and the hooks
do nothing for it.

## Removing it

```bash
skillpp install --project ~/code/my-repo --remove --apply   # or --user
rm -rf ~/.claude/skillpp/                                   # the ledger, sessions and drafts
pipx uninstall skillpp
```

`--remove` takes out only skillpp's own hook entries and the slash commands it
copied, and keeps a command file you edited. Skills you installed keep working:
they are ordinary `SKILL.md` files that do not depend on skillpp.
