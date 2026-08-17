# Skill Plus Plus on Claude Code

A practical guide to using Skill Plus Plus across Claude's surfaces. Key upfront:
**passive capture works only in the terminal**; Claude Desktop has no capture
mechanism, but skills created in the terminal can be uploaded to Desktop for use.

For the design rationale, see the [README](../README.md).

> Named `claude-code.md`, not `CLAUDE.md` — the latter is Claude Code's own
> project-instructions file and has nothing to do with this.

---

## 1. What happens during a session

```
  you type a prompt
        │
        ├──► UserPromptSubmit hook ──► records what you asked for
        │
  Claude runs tools (Bash, Edit, MCP calls…)
        │
        ├──► PostToolUse hook ──────► records what actually ran,
        │                              scrubbed before it touches disk
  session ends
        │
        └──► SessionEnd hook ───────► folds the session into one
                                       ledger candidate, deletes the buffer
```

Nothing interrupts you. No popup, no proposal mid-task. The candidate sits in
the ledger until you go looking for it.

The pairing of the first two hooks is the point: `UserPromptSubmit` captures
**intent**, `PostToolUse` captures **execution**. A shell-history tool only ever
gets the second half, which is why its output needs so much more explaining
afterwards.

---

## 2. Install

```bash
python3 bin/skillpp install
```

Dry run — prints the exact changes and writes nothing. When it looks right:

```bash
python3 bin/skillpp install --apply
```

That does three things:

1. Adds the three hooks to `~/.claude/settings.json`, **backing up the existing
   file first** and appending to any hooks already configured rather than
   replacing them.
2. Copies `/skillpp-review` into `.claude/commands/`.
3. Leaves the ledger at `~/.claude/skillpp/`, outside any repo.

Copy `/skillpp-new` across too if you want the dictation command:

```bash
cp commands/skillpp-new.md .claude/commands/
```

**Restart Claude Code afterwards.** Hooks are read at session start, so an
already-running session will not pick them up.

---

## 3. Where everything lives

| Path | What |
| --- | --- |
| `~/.claude/settings.json` | The three hook registrations |
| `~/.claude/skillpp/ledger/` | Candidate workflows, one markdown file each |
| `~/.claude/skillpp/sessions/` | In-flight session buffers, deleted at session end |
| `~/.claude/skillpp/cold/`, `archive/` | Demoted skills — moved, never deleted |
| `~/.claude/skillpp/usage.json` | How often each skill actually gets invoked |
| `~/.claude/skillpp/skillpp.log` | Hook errors, and the only place they surface |
| `.claude/skills/<name>/SKILL.md` | The skills themselves — where Claude Code reads them |
| `.claude/commands/skillpp-*.md` | The slash commands |

Everything is local. Nothing is uploaded anywhere.

---

## 4. Daily use

Two commands, both pull-based — they run when you choose, never on their own.

**`/skillpp-review`** — work through captured candidates. Shows what a proposed
skill would *do* (commands, writes, destructive steps, network calls), the
sessions it came from, and at most three questions about the parts the trace
cannot explain. On approval it writes the `SKILL.md`.

**`/skillpp-new <description>`** — go the other way: describe a process you want
a skill for, and it checks the description for what is missing (a format you
referenced but never gave, an absent trigger, unconstrained sources, no failure
handling), asks up to three questions, confirms, then writes.

Underneath, if you prefer the CLI:

```bash
python3 bin/skillpp review          # what is ready
python3 bin/skillpp show <id>       # the full proposal
python3 bin/skillpp search deploy   # your own past work, searchable
python3 bin/skillpp stats           # ledger size and counts
```

A new skill lands in `.claude/skills/` and Claude Code picks it up
automatically — no registration step.

---

## 5. Check that capture is actually working

Worth doing once, because **the failure mode is silent**. Hooks are built to
never disrupt your session, which means a broken one logs and exits 0 rather
than complaining.

```bash
python3 bin/skillpp stats           # note the entry count
# …use Claude Code normally for a session, then:
python3 bin/skillpp stats           # the count should have moved
```

If it has not:

```bash
cat ~/.claude/skillpp/skillpp.log             # hook errors land here
ls ~/.claude/skillpp/sessions/                # buffers mid-session
python3 -c "import json;print(json.load(open('$HOME/.claude/settings.json'))['hooks'].keys())"
```

Then confirm the hook command runs standalone:

```bash
echo '{"session_id":"probe","tool_name":"Bash","tool_input":{"command":"echo hi"}}' \
  | PYTHONPATH=. python3 -m skillpp hook --event PostToolUse -v
ls ~/.claude/skillpp/sessions/                # probe.json should exist
```

**Verification status.**

All three hooks are confirmed working against live payloads in a terminal
session:

| Hook | Status |
| --- | --- |
| `PostToolUse` | **Confirmed.** `Bash` and `Edit` calls recorded with commands and file paths parsed correctly, zero parse failures. |
| `UserPromptSubmit` | **Confirmed.** Prompts captured verbatim under the `prompt` field. |
| `SessionEnd` | Confirmed by direct invocation; folds a buffer into a ledger entry. |

```bash
python3 -c "import json,glob;d=json.load(open(glob.glob('$HOME/.claude/skillpp/sessions/*.json')[0]));print('prompts:',len(d['prompts']),'steps:',len(d['steps']))"
```

Both numbers should climb as you work.

## 5a. Capture Coverage: Terminal Only

**Passive capture works only in the terminal.** Hooks fire at `SessionEnd` in
Claude Code CLI sessions and populate the ledger. No other surface captures work.

**Desktop sessions produce no ledger entries.** Confirmed by direct observation:
a chat session in Claude Desktop produced no session buffer, no ledger entry,
and no log entry — the hook was never invoked at all. The desktop app ships its
own Claude Code runtime under
`~/Library/Application Support/Claude/claude-code-vm/`, which does not read the
host's `~/.claude/settings.json` where the hooks are registered.

**This is a hard architectural boundary**, not a configuration matter. It is not
possible to add passive capture to Desktop without changing how Desktop sources
its runtime.

**What this means for you:**

If most of your work happens in Desktop chat:
- Passive capture will never fire — the ledger stays empty.
- Use `/skillpp-new` in the terminal to create skills from descriptions.
- Upload them to Desktop via Customize → Skills to use them in chat.
- Or upload `/skillpp-new` itself to Desktop and invoke it there manually when you want to create a skill.

If you work in the terminal:
- Hooks fire automatically. Skills build passively as you work.
- `/skillpp-review` surfaces proposals at 3 occurrences.
- Skills appear in `~/.claude/skills/` ready to use.
- Optionally upload to Desktop if you want the same skills available in chat.

## Workflows by Surface

### Terminal (Claude Code CLI)

**End-to-end automated.** Hooks fire automatically at session end.

```
1. Work normally in a session
   ↓
2. SessionEnd hook summarizes into the ledger
   ↓
3. After 3 occurrences, /skillpp-review surfaces a proposal
   ↓
4. Approve and the skill lands in ~/.claude/skills/
   ↓
5. Claude Code picks it up immediately — no additional steps
```

Or use `/skillpp-new` to describe a skill directly (bypasses the 3-occurrence threshold).

### Claude Desktop

**Neither skills nor capture cross over automatically.**

Skills must be uploaded; hooks never fire at all:

```
1. Create a skill in the terminal (/skillpp-new or /skillpp-review)
   ↓
2. It lands in ~/.claude/skills/<name>/SKILL.md — terminal only
   ↓
3. skillpp bundle --format upload --out ~/skill-uploads
   ↓
4. Upload via Customize → Skills
   ↓
5. Now invocable in Desktop chat
```

Capture is worse than manual — it is unavailable. Hooks never fire in Desktop
(§5a), so the ledger only ever fills from terminal sessions.

Two further limits on the authoring skills themselves: `/skillpp-new` and
`/skillpp-review` shell out to the `skillpp` CLI, and Desktop chat has no shell.
Uploading them would make Desktop *load* them and then fail at the first command.
Authoring stays a terminal activity until a shell-free variant exists.

---

### Getting skills into Claude Desktop

**Claude Desktop does not read `~/.claude/skills/`. Skills reach it by upload.**

Established by controlled test: two skills were written to `~/.claude/skills/`
with identical bodies, differing only in whether the frontmatter carried a
`when_to_use` key. After a full Desktop restart, a fresh chat listed **neither**
— while continuing to list a skill that had been uploaded through
Customize → Skills. Desktop's skill list is account-level (uploaded plus
Anthropic-managed), not a read of the local directory.

> Two earlier readings of this were wrong and are recorded here because the
> mistakes are instructive. First, the conclusion that upload was required was
> reached by *inference* from the managed-skills plugin cache, without a test.
> Then a single successful Desktop invocation of a local skill was taken as
> proof of local reading — when the real explanation was that the same skill had
> also been uploaded. A skill working in Desktop says nothing about *why* it
> works. Only a skill that exists in exactly one place is evidence.

**The upload route: one ZIP per skill.**
[Custom skills](https://support.claude.com/en/articles/12512198-how-to-create-custom-skills)
are uploaded through **Customize → Skills**, as one ZIP per skill with the skill
folder at the archive root:

```
weekly-manager-update.zip
└── weekly-manager-update/
    └── SKILL.md
```

`skillpp bundle --format upload` produces exactly that, and validates against
the published limits first — `name` ≤ 64 characters, `description` ≤ 200 —
because a description that reads well is easy to write past the limit and the
failure would otherwise surface at upload time:

```bash
python3 bin/skillpp bundle --format upload --out ~/skill-uploads \
  --skills-dir ~/.claude/skills
```

Available on Free, Pro, Max, Team and Enterprise, and in Claude Code (beta).

> **This means skills leave your machine.** Uploaded skills are account-level,
> not local files. The ledger stays local, but a skill you upload does not —
> which turns sanitize-on-write from hygiene into the only thing standing
> between a captured trace and a cloud upload. Read a generated skill before
> uploading it.

> The support article writes the filename as `skill.md`; every skill on disk
> here — Anthropic's own included — uses `SKILL.md`, which is what the bundler
> emits. If an upload is rejected, try renaming.

**Capture is a different matter, and does not reach Desktop at all.** The
desktop app ships its own Claude Code runtime under `claude-code-vm/`, which
never invoked the hooks registered in the host's `~/.claude/settings.json`. No
buffer, no ledger entry, no error. Passive capture is terminal-only.

**Nor can skills be sideloaded into Desktop's cache.** It provisions its
managed skills into a per-session directory:

```
~/Library/Application Support/Claude/local-agent-mode-sessions/
  skills-plugin/<plugin-id>/<session-id>/
    .claude-plugin/plugin.json     → {"name": "anthropic-skills", …}
    skills/{docx,pptx,schedule,…}/SKILL.md
```

The manifest names it *"Anthropic-managed skills for Claude Desktop"*, the
contents differ between sessions, and the path is keyed by session id. Writing
a skill in there reaches one stale session and is gone at the next. It is a
cache of managed content, not an extension point.

That cache holds Anthropic's *managed* skills. It is not where your own skills
go, and writing into it would reach one stale session directory — but that is
irrelevant, because `~/.claude/skills/` already works (see above). The cache's
existence is what led to the earlier incorrect conclusion that Desktop had no
local skills path.

### The two bundle formats

```bash
# One <name>.zip per skill — Customize → Skills
python3 bin/skillpp bundle --format upload --out ~/skill-uploads

# .claude-plugin/ + skills/ — Claude Code, team distribution (README §13)
python3 bin/skillpp bundle --format plugin --out ~/my-skills \
  --plugin-name my-skills --with-commands --zip
```

| | `upload` | `plugin` |
| --- | --- | --- |
| Shape | `<name>.zip` → `<name>/SKILL.md` | `.claude-plugin/` + `skills/` |
| Consumer | Customize → Skills (Desktop) | Claude Code terminal, team PRs (Phase 2) |
| Scope | Account-level, syncs across devices | Local files or a git repo |
| Manual step | Customize → Skills UI | None (available in Claude Code immediately) |
| Validated | name ≤ 64, description ≤ 200 | — |

Two thresholds explain most "nothing appeared" cases, and both are working as
designed: a session needs at least **2 substantive steps** to be recorded at
all, and a workflow needs **3 occurrences** before it is proposed. Check
progress with `skillpp review --all`, which includes below-threshold
candidates.

---

## 6. Housekeeping

```bash
python3 bin/skillpp expire                # drop unapproved candidates past TTL
python3 bin/skillpp lifecycle -v          # tiers, usage, stale references
python3 bin/skillpp tier <name> cold      # demote out of the loaded index
python3 bin/skillpp check --name <name>   # dependencies present? (exit 2 if not)
```

Demotion moves a skill to `~/.claude/skillpp/cold/`. Claude Code indexes
everything under the skills directory, so getting something out of the index
means moving the file — there is no flag for it. Nothing is ever deleted.

Skills are never expired on disuse: the runbook you need twice a year is
exactly the one a disuse timer would remove. Staleness is judged by whether the
paths and commands a skill references still resolve.

---

## 7. Turning it off

Delete the three `skillpp` entries from `hooks` in `~/.claude/settings.json`, or
restore the backup the installer left beside it. Capture stops immediately at
the next session; skills already written keep working, since they are ordinary
`SKILL.md` files with no dependency on this tool.

To remove the data as well:

```bash
rm -rf ~/.claude/skillpp/
```

---

## 8. What is on disk

Every captured string is scrubbed **before** it is written, not before it is
read — API keys, tokens, connection strings, JWTs, private keys, emails and
internal hostnames become typed placeholders like `[REDACTED:github-token]`.
There is no window in which an unscrubbed trace exists on disk.

Raw traces are never persisted at all. A session becomes one compact markdown
summary, which is why the ledger stays in the same size range as the skill
library rather than the tens of megabytes raw tool output would take.

The scrubber is pattern-based, with a conservative high-entropy fallback for
credentials it has no rule for. It is a good net, not a guarantee — read a
generated skill before opening a pull request with it.
