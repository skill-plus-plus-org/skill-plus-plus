# Using Skill++

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
`~/.claude/skill-plus-plus/skill-plus-plus.log` and the hook exits 0. The local model runs in the
worker after the session, not while you work.

## Installing

```bash
pipx install skill-plus-plus
```

The latest `main`, before it is released:
`pipx install git+https://github.com/skill-plus-plus-org/skill-plus-plus`.

Then choose where the hooks go. Every form is a dry run that prints the exact
change until you add `--apply`, and an existing settings file is backed up
beside itself first.

```bash
skill-plus-plus install --project ~/code/my-repo --apply   # one repo: <repo>/.claude/settings.json
skill-plus-plus install --user --apply                     # every project: ~/.claude/settings.json
skill-plus-plus install --settings path/to/settings.json --apply
```

It also copies two slash commands into the matching `commands/` folder,
`/skill-plus-plus-review` and `/skill-plus-plus-new`, and downloads the two
Ollama models detection needs (`gemma4:e4b-it-qat` and `nomic-embed-text`, about
6.4 GB) if they are missing. The dry run lists what it would download. Ollama
itself has to be installed and running first (https://ollama.com); without it
the hooks go in anyway, and sessions wait until it answers. `--no-models` skips
the download.

Hooks are read when a session starts, so start a new session afterwards (a new
chat in the CLI, or a new Code session in the desktop app), and check:

```bash
skill-plus-plus doctor
```

`doctor` reports which hooks are wired in the project and user settings,
whether Ollama answers and has both models, how many sessions are waiting to be
banked, and what the ledger holds.

From a clone instead of `pipx`, `pip install -e .` puts `skill-plus-plus` on your PATH
the same way; `python3 bin/skill-plus-plus` works without installing anything.

## Daily use

Work as usual. When a session ends, Skill++ cuts it into tasks and compares each
with what it has seen before. Two thresholds decide what you see:

- a task needs at least **two substantive steps** to be kept at all, so a lone
  `git status` or a question with no work is not a candidate;
- a candidate needs to be seen **three times** before you can promote it.

A candidate belongs to one project: the git repo the work was done in (the
folder itself outside a repo). The same procedure in two repos is two
candidates, since the skill made from it belongs in that repo. Nothing is
recorded without a project. When the page holds more than one project, a menu
beside the tabs shows one at a time.

```bash
skill-plus-plus web            # the review page, http://127.0.0.1:8765
skill-plus-plus review --all   # the same queue in the terminal, below-threshold included
skill-plus-plus show <id>      # one candidate: effects, evidence, open questions
skill-plus-plus search deploy  # everything you did that mentions a word
```

On the page, **Candidates** shows each one with a summary and its steps grouped
under the requests they served; open a request to see every step in order.
Promote what is worth a skill, ignore what is not. An ignored candidate is
still recognised when you repeat it, so it is never proposed again; **Bring
back** under **Ignored** undoes it. A promoted or drafted candidate can be
ignored too, after a confirmation; an installed skill's folder is removed from
`.claude/skills/` first.

### Shortcuts

- **`/skill-plus-plus-new`**, or `skill-plus-plus dictate`, describes a procedure instead of
  performing it: the agent asks what the description leaves out, and the
  candidate skips the three-times rule. It belongs to the project you run it
  in, like captured work. *Work in progress: how it works may change.*
- **`SKILL_PLUS_PLUS_RECURRENCE=1`** makes every candidate ready at once.

## From a candidate to a skill

1. **Draft Skill** on a promoted candidate. An optional note tells the agent
   what to look out for; it reads only the candidate's first run, so the note is
   where anything the later runs taught you goes.
2. The agent writes `SKILL.md`: when to use it, the procedure as numbered steps,
   and under `## Open questions` whatever it could not tell from the run. It may
   also decline, when the work is not a reusable procedure; the page then says
   why, and you can draft again with a note.
3. On the **Skills** tab, the draft waits under its project, in **To review**.
   Answer the open questions there. The answers go back to the agent, which
   folds each into the skill. **Revise** sends any other instruction.
4. **Install in `<project>`** once no question is left. It writes the skill to
   the repo's `.claude/skills/<name>/`; commit that folder and everyone who
   works in the repo has it. A skill belongs to the project it was made from,
   so there is no install for every project. A folder of the same name that the
   page did not install is never touched.

   Installed, the draft becomes one of the project's skills (below). From then
   on the folder is the skill, and **Edit** on its card changes it. To take a
   skill out, delete its folder: nothing else is kept of the install, and its
   draft comes back to review.

The same from the terminal:

```bash
skill-plus-plus draft <id> --note "what to look out for" --apply
skill-plus-plus revise <id> --instruction "what to change" --apply
```

`revise` changes a draft until it is installed; after that it refuses, and
`edit-skill` (below) changes the skill.

The agent is whatever `SKILL_PLUS_PLUS_AGENT` names, `claude -p` by default. It runs in
a temporary folder with only the draft instructions and a way to read the
candidate, never installs anything, and writes its transcript to
`~/.claude/skill-plus-plus/drafts/<id>/agent.log`.

### Skills in the Claude desktop chat

Skills reach the desktop app's chat by upload, one zip per skill, under
**Customize → Skills**. `bundle` writes them and checks the published limits (a
`name` of at most 64 characters, a `description` of at most 200):

```bash
skill-plus-plus bundle --format upload --out ~/skill-uploads
```

An uploaded skill lives in your account, not on your machine. Read it before
uploading. For a team, `--format plugin --plugin-name <name> --with-commands`
writes a Claude Code plugin folder instead.

## Skills in your projects

The **Skills** tab shows each project on its own: its drafts to review first,
then every skill it has in `.claude/skills/`, three to a row, whoever put it
there: Skill++, you or a teammate. The menu beside the tabs narrows it to one
project. A card is the skill's name and what it is for, with a label while an
edit is running or waiting.

Click a card to open the skill: its `SKILL.md`, and what you can do with it.

- **Edit** asks your agent for a change: say what should change, and it edits
  a copy in a temporary folder. The change comes back as a diff. Nothing in the
  project changes until you press **Apply**; **Discard** drops it. Apply is
  refused if the skill changed after the agent took its copy. The draft a
  skill was installed from stays as it was. A linked skill folder, and one over
  100 files or 1 MB, are not edited from here.
- **Undo**, right after Apply, puts the skill back as it was. It lasts until
  your next action or a reload of the page, and is refused if anything changed
  the skill since.

The same edit from the terminal:

```bash
skill-plus-plus edit-skill <folder> --project ~/code/my-repo --instruction "what to change" --apply
```

Open Claude Code sessions pick up a changed `SKILL.md` at once. After the first
install into a project creates its `.claude/skills/` folder, run
`/reload-skills` there.

## Housekeeping

```bash
skill-plus-plus expire                 # delete candidates not promoted, past SKILL_PLUS_PLUS_TTL_DAYS
skill-plus-plus lifecycle -v           # your skills: tiers, use, references that went stale
skill-plus-plus tier <name> cold       # move a skill out of the loaded index, without deleting it
skill-plus-plus check --name <name>    # are the programs and MCP servers it needs present?
skill-plus-plus reconcile              # promoted skills whose file is gone
```

Nothing runs these for you, and nothing a skill depends on is ever deleted.

## Commands

Every command prints its flags with `skill-plus-plus <command> --help`. Anything that
edits settings or spends a model call is a dry run until you add `--apply`.

| For | Commands |
| --- | --- |
| Setting up | `install`, `doctor` |
| Reviewing | `web`, `review`, `show`, `search`, `stats` |
| Deciding | `promote`, `dismiss` (or `ignore`), `reopen`, `ignored` |
| Drafting | `draft`, `revise`, `name`, `scaffold`, `dictate` (work in progress) |
| Fixing candidates | `split`, `merge`, `retitle`, `sift` |
| Skills you have | `edit-skill`, `lifecycle`, `tier`, `check`, `reconcile`, `bundle`, `expire`, `accuracy` |
| Internal (run by the hooks) | `hook`, `fold-session`, `fold-pending` |

## Memory

The local model is the one large thing Skill++ runs. On an 18 GB Mac, a fold
with `gemma4:e4b-it-qat` and `nomic-embed-text` takes about **7 GB** of free
memory, whatever the session: a one-word question costs as much as a whole
session. `gemma4:e4b`, the default before it, took 12.8 GB, and with other
apps open a fold ran at 92 % memory used while macOS swapped out 4.7 GB in
100 seconds. So Skill++ guards its memory use:

- **It starts only if the models fit.** A fold loads the models only when
  what they take still leaves 2 GB free: with the default models, from about
  9 GB free. What they take is measured on your computer: estimated from
  their size at first, which asks for about 10 GB free, then the largest
  amount a fold has actually used here.
- **It stops when memory runs short.** If free memory falls below 2 GB while a
  fold runs, or macOS reports critical memory pressure, Skill++ stops and
  unloads the models at once.
- **It lets go right away.** When a fold ends, the models are unloaded, rather
  than staying in memory for Ollama's usual five minutes.
- **It never unloads what it didn't load.** A model you had loaded yourself
  stays loaded.

A session that doesn't fit isn't lost. It waits, and is folded:

- **when your computer is idle,** five minutes without keyboard or mouse input,
  once the models fit;
- **at the next session start,** retried at most every ten minutes;
- **when you ask:** **Fold now** on the review page's banner, or
  `skill-plus-plus fold-pending --now`. The models still load only if they fit.

When a fold waits or is stopped, a desktop notification says so: Notification
Center on macOS, `notify-send` on Linux. On a Mac the first one may ask you to
allow notifications for Script Editor, which is what shows them.
`skill-plus-plus doctor` shows what the models take on your computer and from
what free memory a fold starts; `skill-plus-plus stats` counts the sessions
waiting.

## Configuration

All settings are environment variables.

| Variable | Default | What it does |
| --- | --- | --- |
| `SKILL_PLUS_PLUS_ROOT` | `~/.claude/skill-plus-plus` | where the ledger, sessions and drafts live |
| `SKILL_PLUS_PLUS_RECURRENCE` | `3` | times a task must repeat before it can be promoted |
| `SKILL_PLUS_PLUS_TTL_DAYS` | `14` | how long `skill-plus-plus expire` keeps a candidate that is not promoted, counted from its last recognition |
| `SKILL_PLUS_PLUS_OLLAMA` | `http://127.0.0.1:11434` | the Ollama server |
| `SKILL_PLUS_PLUS_LOCAL_MODEL` | `gemma4:e4b-it-qat` | the model that cuts sessions and names candidates |
| `SKILL_PLUS_PLUS_EMBED_MODEL` | `nomic-embed-text` | the model that matches repeats |
| `SKILL_PLUS_PLUS_MATCH_FLOOR` | `0.93` | similarity at which two runs' commands count as the same procedure |
| `SKILL_PLUS_PLUS_MATCH_FLOOR_TURNS` | `0.85` | the same, for runs compared by their conversation |
| `SKILL_PLUS_PLUS_AGENT` | `claude -p {PROMPT} …` | how to invoke your agent for drafts; `{PROMPT}` is replaced |
| `SKILL_PLUS_PLUS_JUDGE` | `1` | `0` stops judging; sessions are then held until it is back on |
| `SKILL_PLUS_PLUS_NAME` | `1` | `0` stops the model naming new candidates |
| `SKILL_PLUS_PLUS_MATCH` | `1` | `0` banks every task without comparing it (for measuring detection) |
| `SKILL_PLUS_PLUS_DESCRIBE` | `0` | `1` asks the model to describe every tool call, inside the hook (slow) |
| `SKILL_PLUS_PLUS_MAX_STEPS` / `SKILL_PLUS_PLUS_MAX_FIELD` | `500` / `2000` | caps per session and per captured field |
| `SKILL_PLUS_PLUS_MEMORY_GUARD` | `1` | `0` turns the [memory guard](#memory) off: folds load the models whatever is free |
| `SKILL_PLUS_PLUS_MEMORY_RESERVE_GB` | `2` | free memory a fold always leaves; it starts only if the models fit with this much to spare, and stops below it |
| `SKILL_PLUS_PLUS_IDLE_MINUTES` | `5` | how long nobody must use the keyboard or mouse before a session waiting for memory is folded |
| `SKILL_PLUS_PLUS_NOTIFY` | `1` | `0` turns off the desktop notices when a fold waits for memory or is stopped |
| `SKILL_PLUS_PLUS_INTERNAL` | unset | set to anything to make the hooks do nothing, e.g. for one session |

## Troubleshooting

**Nothing shows up on the review page.**
- Run `skill-plus-plus doctor`. A missing `SessionEnd` hook captures every step and banks
  none of it.
- `skill-plus-plus stats` shows sessions held because Ollama did not answer. Start
  Ollama; they are banked at the next session start, or now with
  `skill-plus-plus fold-pending`.
- `skill-plus-plus review --all` shows candidates below the three-times threshold.
- The log is `~/.claude/skill-plus-plus/skill-plus-plus.log`.

**A draft failed.** Read `~/.claude/skill-plus-plus/drafts/<id>/agent.log`.
- *Not logged in*: run `claude` once, then `/login`.
- *`claude` not found*: it is often not on the PATH a hook or the page sees. Set
  `SKILL_PLUS_PLUS_AGENT` to its full path, keeping `-p {PROMPT}` and the rest.
- *Inconclusive*: the agent wrote nothing and did not say it was declining. A
  denied tool is the usual cause; its output is in the log.
- *Timed out*: drafts get 900 seconds; `skill-plus-plus draft … --timeout N` gives more.

**An edit failed.** Read
`~/.claude/skill-plus-plus/edits/<project>/<name>/current/agent.log`; the causes
are the same as for a draft. **Dismiss** clears it.

**The page will not start: `Address already in use`.** Another `skill-plus-plus web` is
running. Stop it with Ctrl-C in its terminal, or use `skill-plus-plus web --port 8766`.

**Tool calls feel slow.** Check that `SKILL_PLUS_PLUS_DESCRIBE` is not set to `1`; it asks
the local model about every tool call inside the hook.

**Sessions keep waiting for memory.** `skill-plus-plus doctor` says from what
free memory a fold starts. Close a few apps and press **Fold now** on the
review page, or leave the computer idle for a few minutes. If `doctor` says the
models take more than the machine has, sessions never fold while the guard is
on; `SKILL_PLUS_PLUS_MEMORY_GUARD=0` folds them anyway, at the risk of swapping.

**Leave one session out.** Start it as `SKILL_PLUS_PLUS_INTERNAL=1 claude`, and the hooks
do nothing for it.

## Removing it

```bash
skill-plus-plus install --project ~/code/my-repo --remove --apply   # or --user
rm -rf ~/.claude/skill-plus-plus/                                   # the ledger, sessions and drafts
pipx uninstall skill-plus-plus
```

`--remove` takes out only Skill++'s own hook entries and the slash commands it
copied, and keeps a command file you edited. Skills you installed keep working:
they are ordinary `SKILL.md` files that do not depend on Skill++.
