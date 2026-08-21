---
description: Draft a SKILL.md for one captured candidate, without asking questions
---

# Skill Plus Plus — draft

Turn one captured candidate into a **draft** `SKILL.md`. Nobody is watching this
run, which changes two things: you cannot ask anything, and you must not install
anything.

Argument: a candidate id. It was chosen already — do not go looking for a better
one, and do not review a second.

## 1. Load it

```bash
python3 bin/skillpp show <id> --json
```

That gives the effect summary, the evidence it came from, declared dependencies,
and the questions the engine generated.

## 2. Write the body

```bash
python3 bin/skillpp scaffold <id> --name <skill-name> \
  --description "<one line>" --out <draft-dir>/SKILL.md
```

The scaffold gives structure, dependencies and the verbatim steps. Your job is
the half code cannot do: prose worth reading, a name someone would recognise,
and a description that says when the skill applies.

Then edit that file into something a colleague could follow.

**The description is the only thing read when deciding whether to load a skill.**
A candidate titled after a greeting or a shell command is a real failure mode
here — name the *task*, never the prompt that happened to start it.

## 3. Do not ask — record instead

The interactive review asks the developer up to three questions. You cannot, so
every question you would have asked becomes a line under:

```markdown
## Open questions
```

Write what you do not know, not a guess dressed as fact. A draft that admits two
gaps is worth more than one that invents the answers, because the reader can see
what to check.

## 4. Stop before installing

**Do not run `python3 bin/skillpp promote`. Do not write into the skills directory.** Leave
the draft where the scaffold put it and print the path.

Installing is the developer's decision and this run does not have their
attention. A skill that appears without anyone approving it is the failure this
whole design exists to prevent.

## 5. If it should not exist

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
