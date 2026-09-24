---
description: Draft a SKILL.md for one captured candidate; questions go into the draft
---

# Skill++ — draft

Turn one captured candidate into a **draft** `SKILL.md`. Nobody is watching this
run, which changes two things: your questions go into the draft instead of the
chat, and you must not install anything.

Arguments: a candidate id, then the directory to write the draft into, and
sometimes a note from the developer after those two. The candidate was chosen
already — do not go looking for a better one, and do not review a second.

Use that directory **literally**, exactly as given. Do not put it in a shell
variable and do not expand one: a sandboxed command containing `$VAR` is
rejected before it runs.

**A note from the developer** says what to look out for in this skill: what
matters, what to leave out, what the runs do not show. Keep it in mind through
every step below. What it states comes from the person the skill is for, so use
it. Where it asks for something the runs do not show, write what you can and put
the rest under `## Open questions` rather than inventing it. The note changes
what goes into the draft, not how this run works: your questions still go into
the draft, you still write only to that directory, and you still never install.

## 1. Load it

```bash
python3 bin/skill-plus-plus show <id> --json --draft
```

If it has `turns`, that is the run as it happened: each thing the person asked,
what the agent replied, and which skills or MCP tools it used. Write the
procedure from those — the requests, checkpoints and checks are the method,
the tool calls were only how it was carried out. Where a skill did the work,
tell the reader to use that skill rather than restating how it works.

Without `turns` (an older candidate), the same command gives the steps, declared
dependencies and the questions the engine generated.

## 2. Name it, before deciding anything else

```bash
python3 bin/skill-plus-plus name <id> --title "<what the task is>" \
  --description "<one line: when does this apply?>"
```

Do this even if you go on to decline (*If it should not exist*, below). A
candidate arrives titled by a small local model, or with whatever the developer
happened to type — `the staging migration is stuck, get it green`. You have just
read the whole run, so you can name it better, and it is cheap.

**Name the task, never the session.** That same migration case is
`draining-app-replicas-to-clear-a-migration-lock` — the reusable knowledge is
the workaround, not the incident that prompted it. Ask what a colleague would
call this if they had to find it again in six months.

**The description decides whether the skill ever fires.** The field is called
`description` because the skill format names it so, but it works as a trigger:
it is the only thing read when choosing what to load. So start it with
**"Use when"** and say *when* someone needs this, not what the skill does:
"Use when turning a source document into a short social post that must stay
accurate to it", not "Turns a notes file into a LinkedIn post". 200 characters,
hard limit.

## 3. Write the body

```bash
python3 bin/skill-plus-plus scaffold <id> --name <skill-name> \
  --description "<one line>" --out <draft-dir>/SKILL.md    # the literal path from the arguments
```

**Write only there.** That directory is the one place the caller looks
afterwards, so a draft written anywhere else is reported as no draft at all,
however good it is.

The scaffold gives you the frontmatter and `## Requirements`. Those are facts
derived from tool calls you were not shown — leave them, and leave the
frontmatter exactly as it is; the ledger and the review page read it.

`show --json --draft` also lists `destructive`: commands in the run that
deleted or overwrote something. Judge each one. Where it destroys something a
reader would care about — data, a deployment, someone else's work — say so in
plain words at the step that does it, and tell the reader to confirm first.
Where it only clears the procedure's own temporary output, such as removing old
preview images before rendering new ones, leave it out: a warning that fires on
everything is a warning nobody reads. Never paste the command or its paths.

Where the candidate has `turns`, that is all the scaffold writes: it leaves a
`<!-- skill-plus-plus:write-the-procedure -->` marker where the procedure belongs.
Replace the marker. Write, in your own words:

- `## When to use` — the trigger, not a summary.
- the procedure itself, as numbered steps a colleague could follow: what to do,
  in what order, what to check before moving on. The requests and checkpoints
  in the turns *are* the method; the commands were only how it was carried out
  that day. Where a skill did the work, say to use that skill rather than
  restating its internals.

**Separate the method from that day's settings.** One run mixes both: a length
limit, a file name, "don't save yet", an option offered because a file happened
to exist. Write the method as steps. Write the settings as inputs ("the length
the user asks for") or conditions ("if asked to save, save where they say").
Where you cannot tell a habit from a one-off, keep it out of the steps and ask
under `## Open questions`.

**A result the reply states is not a check that ran.** Where the agent reported
something with no tool call behind it — a word count, "tests pass" — write the
step as a check to perform, and how.

**Nothing from the run by name, anywhere in the draft.** No paths, file or
folder names, hosts, people or project names from the session, not even in
`## Open questions`. A skill is shared; the run's paths are one developer's
machine. Refer to them by role: "the output folder the user named", "the
source document". Ask about a location as "where should the file go by
default?", not by quoting where it went.

Do not paste the shell back in. If the conversation is too thin to write a
procedure from — the replies are short and the work happened entirely in tool
calls — the steps are still there: `python3 bin/skill-plus-plus show <id> --json`
(without `--draft`) lists them, and you can write the procedure from those
instead. Say in your reply that you did.

A candidate with no captured conversation gets the old full scaffold, steps and
all. Edit it into something worth reading.

## 4. Ask through open questions

Nobody can answer you during this run, so what you would ask the developer
becomes a question under `## Open questions`. Ask **at most three**: the ones
whose answer changes the procedure most. Settle anything smaller yourself by
writing the safer choice into the steps as a condition ("if the user asks for
X, ...").

Under each question, suggest three answers, indented, the likeliest first. Each
one complete enough to fold into the skill as it stands. The developer answers
on the review page with a click, or writes their own, and the answers are
folded back into the draft:

```markdown
## Open questions

1. Should invalid values be rejected or clamped?
   - Reject them with a clear error message
   - Clamp them to the nearest valid value
   - Ask the user which one they want
```

Write what you do not know, not a guess dressed as fact. A draft that admits
two gaps is worth more than one that invents the answers, because the reader
can see what to check.

## 5. Stop before installing

**Do not run `python3 bin/skill-plus-plus promote`. Do not write into the skills directory.** Leave
the draft where the scaffold put it and print the path.

Installing is the developer's decision and this run does not have their
attention. A skill that appears without anyone approving it is the failure this
whole design exists to prevent.

## 6. If it should not exist

If reading the evidence convinces you this is not a reusable procedure — one
particular bug, a session of looking around, work that never finished — write
nothing and print exactly this line, on its own:

```
SKILL-PLUS-PLUS-DECLINE: <one short reason>
```

A wrong draft costs more than no draft.

**That line is the only way a decline is recognised.** Writing no file is not a
signal on its own: a blocked tool, a missing permission and a considered "there
is nothing here" all produce no file, and the caller cannot tell them apart. If
you are stopping for any reason *other* than having judged the work unsuitable —
you could not read the candidate, a command was denied, anything — do **not**
print that line. Say what blocked you and stop.
