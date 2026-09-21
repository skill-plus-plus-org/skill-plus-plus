---
description: Draft a SKILL.md for one captured candidate, without asking questions
---

# Skill Plus Plus — draft

Turn one captured candidate into a **draft** `SKILL.md`. Nobody is watching this
run, which changes two things: you cannot ask anything, and you must not install
anything.

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
the rest under `## Open questions` (step 5) rather than inventing it. The note
changes what goes into the draft, not how this run works: you still cannot ask,
you still write only to that directory, and you still never install.

## 1. Load it

```bash
python3 bin/skillpp show <id> --json --draft
```

If it has `turns`, that is the run as it happened: each thing the person asked,
what the agent replied, and which skills or MCP tools it used. Write the
procedure from those — the requests, checkpoints and checks are the method,
the tool calls were only how it was carried out. Where a skill did the work,
tell the reader to use that skill rather than restating how it works.

Without `turns` (an older candidate), the same command gives the steps, declared
dependencies and the questions the engine generated.

## 2. One procedure, or two?

Code cuts a session only where it sees a completion marker or a new request.
Where a single request did two things and neither finished in a way a regex
recognises — a deploy then a smoke test, an MCP call then another — both arrive
here as one candidate.

Read the turns (or the steps) and ask whether they are **one procedure or two**.
If two, say where the second begins and split before doing anything else —
`show <id> --json` lists the steps with the indices `split` takes:

```bash
python3 bin/skillpp split <id> --at <index of the first step of the second procedure>
```

Then name and draft **the first half only**, and say in your reply that the other
half is now a separate candidate awaiting its own draft.

**Default to one.** Repetition is not a boundary: `migrate → scale → migrate →
scale` is one workaround, not two procedures, and a retry after a failure
belongs to the attempt it retried. Split only when the second half would still
make sense as a procedure with the first half deleted — and if you are weighing
it up at all, do not split.

## 3. Name it, before deciding anything else

```bash
python3 bin/skillpp name <id> --title "<what the task is>" \
  --description "<one line: when does this apply?>"
```

Do this even if you go on to decline in step 5. A candidate arrives titled with
whatever the developer happened to type — `the staging migration is stuck, get
it green`, or a raw `git add` line — because capture can only reuse a string it
observed. Naming is the half it cannot do, and it is cheap: you have just read
the evidence.

**Name the task, never the session.** That same migration case is
`draining-app-replicas-to-clear-a-migration-lock` — the reusable knowledge is
the workaround, not the incident that prompted it. Ask what a colleague would
call this if they had to find it again in six months.

**The description decides whether the skill ever fires.** It is the only thing
read when choosing what to load, so write the trigger, not a summary: *when* does
someone need this? 200 characters, hard limit.

## 4. Write the body

```bash
python3 bin/skillpp scaffold <id> --name <skill-name> \
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
`<!-- skillpp:write-the-procedure -->` marker where the procedure belongs.
Replace the marker. Write, in your own words:

- `## When to use` — the trigger, not a summary.
- the procedure itself, as numbered steps a colleague could follow: what to do,
  in what order, what to check before moving on. The requests and checkpoints
  in the turns *are* the method; the commands were only how it was carried out
  that day. Where a skill did the work, say to use that skill rather than
  restating its internals.

Do not paste the shell back in. If the conversation is too thin to write a
procedure from — the replies are short and the work happened entirely in tool
calls — the steps are still there: `python3 bin/skillpp show <id> --json`
(without `--draft`) lists them, and you can write the procedure from those
instead. Say in your reply that you did.

A candidate with no captured conversation gets the old full scaffold, steps and
all. Edit it into something worth reading.

## 5. Do not ask — record instead

The interactive review asks the developer up to three questions. You cannot, so
every question you would have asked becomes a line under:

```markdown
## Open questions
```

Write what you do not know, not a guess dressed as fact. A draft that admits two
gaps is worth more than one that invents the answers, because the reader can see
what to check.

## 6. Stop before installing

**Do not run `python3 bin/skillpp promote`. Do not write into the skills directory.** Leave
the draft where the scaffold put it and print the path.

Installing is the developer's decision and this run does not have their
attention. A skill that appears without anyone approving it is the failure this
whole design exists to prevent.

## 7. If it should not exist

If reading the evidence convinces you this is not a reusable procedure — one
particular bug, a session of looking around, work that never finished — write
nothing and print exactly this line, on its own:

```
SKILLPP-DECLINE: <one short reason>
```

A wrong draft costs more than no draft.

**That line is the only way a decline is recognised.** Writing no file is not a
signal on its own: a blocked tool, a missing permission and a considered "there
is nothing here" all produce no file, and the caller cannot tell them apart. If
you are stopping for any reason *other* than having judged the work unsuitable —
you could not read the candidate, a command was denied, anything — do **not**
print that line. Say what blocked you and stop.
