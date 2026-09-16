---
description: Review captured workflow candidates and promote the good ones into skills
---

# Skill Plus Plus — review

Walk the developer through reviewing captured workflow candidates. The CLI has
already done the deterministic work: capture, scrubbing, deduplication, gap
detection, effect summaries. Your job is the judgement half.

Arguments (optional): a candidate id, or a search phrase. With no arguments,
review the highest-occurrence candidate.

## 1. Find the candidate

```bash
skillpp review --json
```

If the developer gave a search phrase instead of an id, use `skillpp search <phrase>`.
If nothing is ready, say so plainly and stop — do not go looking for work to do.

## 2. Load it

```bash
skillpp show <id> --json
```

This returns the effect summary, the derived-from evidence, the declared
dependencies, and up to three generated questions.

## 3. Answer what you can yourself

**Before asking the developer anything**, resolve what the repository can
answer. Read the relevant files. Check whether referenced scripts still exist,
whether a flag is still valid, what the test command is actually called in
`package.json` or `Makefile`.

Only questions the repo genuinely cannot answer reach a human. On a clean
candidate that is often zero.

## 4. Check for an existing skill first

List the existing skills. If one already covers this workflow, propose
**extending that skill** rather than creating a near-duplicate. The CLI matches
repeated runs by embedding, but its floor is deliberately strict — a wrong merge
is worse than a missed one — so overlap it left apart is yours to catch.

## 5. Present it — effects first

Show the developer:

1. **What it will do** — the commands, writes, destructive steps and network
   calls from the effect summary. Never lead with what it is *for*; a purpose
   summary can be accurate while the steps underneath are wrong.
2. **Where it came from** — occurrence count and the stated intents.
3. **At most three questions**, each with your suggested answer pre-filled so
   they can confirm rather than compose.

If you find yourself with more than three questions, the candidate is not
ready. Say so and leave it in the ledger rather than running an interrogation.

Then ask for a decision: promote, skip, or dismiss.

## 6. Write the skill

On approval, generate the scaffold and then edit it into something worth
reading:

```bash
skillpp scaffold <id> --name <skill-name> --description "<one line>" --out <path>/SKILL.md
```

Then improve it:

- Rewrite the description so it triggers accurately — that line is the only
  thing an agent sees before deciding to load the skill.
- Turn the raw step list into instructions with the judgement the developer
  just gave you (when it breaks, when not to use it).
- **If the workflow is a deterministic pipeline, extract it into
  `scripts/run.sh` next to the SKILL.md** and have the skill invoke it.
  Captured determinism should come back as code, not as prose that gets
  re-derived on every run.
- Prefer a portable CLI over an MCP call wherever the trace shows both would
  work (`gh` over a GitHub MCP, `psql` over a Postgres MCP).
- Keep any unanswered questions under a `## Known gaps` heading. Do not guess
  and do not silently drop them.

Skills land as `tier: provisional`. They earn `trusted` through successful use,
not through review.

## 7. Record it

```bash
skillpp promote <id> --skill-path <path>/SKILL.md
skillpp check --name <skill-name>
```

The dependency check confirms the skill can actually run here before anybody
relies on it.

## 8. Report

State what was created, where, what tier, and what gaps remain open. If the
developer skipped questions, say which — those are the parts of the skill that
are still guesses.
