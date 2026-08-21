---
description: Draft a SKILL.md for one captured candidate, without asking questions
---

# Skill Plus Plus — draft

Turn one captured candidate into a **draft** `SKILL.md`. Nobody is watching this
run, which changes two things: you cannot ask anything, and you must not install
anything.

Arguments: a candidate id, then the directory to write the draft into. The
candidate was chosen already — do not go looking for a better one, and do not
review a second.

Use that directory **literally**, exactly as given. Do not put it in a shell
variable and do not expand one: a sandboxed command containing `$VAR` is
rejected before it runs.

## 1. Load it

```bash
python3 bin/skillpp show <id> --json
```

That gives the effect summary, the evidence it came from, declared dependencies,
and the questions the engine generated.

## 2. One procedure, or two?

Code cuts a session only where it sees a completion marker or a new request.
Where a single request did two things and neither finished in a way a regex
recognises — a deploy then a smoke test, an MCP call then another — both arrive
here as one candidate.

Read the steps and ask whether they are **one procedure or two**. If two, say
where the second begins and split before doing anything else:

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

The scaffold gives structure, dependencies and the verbatim steps. Your job is
the half code cannot do: prose worth reading, a name someone would recognise,
and a description that says when the skill applies.

Then edit that file into something a colleague could follow.

**The description is the only thing read when deciding whether to load a skill.**
A candidate titled after a greeting or a shell command is a real failure mode
here — name the *task*, never the prompt that happened to start it.

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
