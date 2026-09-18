---
description: Turn a described workflow into a skill, after asking what the description leaves out
---

# Skill Plus Plus — new skill from a description

The developer wants a skill for something they are telling you about rather
than something you watched them do. Everything after the command name is the
description.

## 1. Register it

```bash
skillpp dictate --text "<their description verbatim>" --json
```

This parses the description into ordered steps and runs a completeness check,
returning up to three things the description does not yet specify — a format
referred to but never given, a missing trigger, unconstrained sources, absent
failure handling, or a procedure too thin to be a procedure.

Pass their words through unedited. Your paraphrase is not the thing to check.

## 2. Answer what you can yourself

Before asking anything, look for the answer where it might already exist:

- A format they have used before in this repo, or in an existing skill.
- Conventions in `CLAUDE.md`, a style guide, a template file.
- An existing skill that already covers part of this.

If an existing skill covers the same ground, say so and propose extending it
rather than adding a near-duplicate.

## 3. Ask — at most three, each with a suggested answer

Ask only what is genuinely unresolved, in the order the check returned them.
Give your best guess with each so they can confirm rather than compose.

If a described format is missing, ask for an **example of the output**, not a
description of it. One worked example is worth a paragraph of specification and
takes them less time to give.

Do not exceed three questions. Anything else becomes `## Open questions`.

## 4. Confirm before writing

Show them, compactly:

- the steps as parsed, in order
- their answers folded in
- what will remain an open gap
- the skill name and where the file will go

Ask for a yes. Do not write on inference — this is a skill they will run
later without re-reading, so a wrong assumption here is expensive and silent.

## 5. Write it

```bash
skillpp scaffold <id> --name <skill-name> --description "<one line>" \
  --answers '{"when_to_use": "...", "output_format": "...", "sources": "..."}' \
  --out .claude/skills/<skill-name>/SKILL.md
```

Then edit the scaffold into something worth reading:

- The description line decides whether the skill ever loads. Make it name the
  trigger condition in the developer's own vocabulary, not a summary of the
  body.
- Put the output format in as a **literal example block**, not prose about the
  format.
- Keep the steps in the order given. If a step needs judgement, say what the
  judgement is rather than flattening it into an instruction.
- Where the workflow calls for research or verification, write down what the
  developer said counts as sufficient — that is the part that makes it their
  skill and not a generic one.

## 6. Record and report

```bash
skillpp promote <id> --skill-path .claude/skills/<skill-name>/SKILL.md
```

Tell them the path, the tier (`provisional` — it earns `trusted` through use,
not through review), and any gaps still open. If they skipped a question, name
it: that part of the skill is still a guess.
