# Skill Plus Plus — Technical & Product Overview

**Skill Plus Plus** is a background-observing knowledge engine for developers and technical teams. It watches how work actually gets done, keeps a searchable **ledger** of candidate workflows, and — only on explicit human approval — promotes them into modular, enterprise-ready `SKILL.md` files.

Capture is passive. Promotion is always deliberate.

```bash
./examples/demo.sh
```

That runs the whole loop against a scratch ledger — three captured sessions, a
redacted credential, generated questions, a scaffolded skill — and touches
nothing real. See §12 for what is built and what is not.

---

## 1. Core Vision & Value Proposition

* **Zero-Friction Capture:** Routine developer work (terminal pipelines, MCP tool chains, refactoring sequences) and dictated natural-language workflows land in the ledger automatically, with no interruption and no prompt engineering.
* **Bottom-Up Intelligence:** Operational knowledge is derived from what the team actually does, rather than from documentation someone was supposed to write.
* **Nothing Unreviewed Ships:** The ledger holds *candidates*, not skills. A `SKILL.md` is synthesized only after a human reads what it will do and approves it.
* **Open Standard Native:** Output is the standard `SKILL.md` format, compatible with Claude Code, Cursor, OpenCode, and Microsoft Agent Framework. (See §7 for the honest limits of that portability.)

---

## 2. System Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                        DAILY INPUTS                          │
│   • Passive MCP & terminal execution traces                  │
│   • Active natural-language text / voice dictation           │
└───────────────────────────────┬──────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────┐
│                        SEGMENTATION                          │
│   Session cut into task episodes — one workflow per entry    │
│   Boundaries: completion markers · new prompt                │
│   No marker and nothing shipped → flagged, not proposed      │
└───────────────────────────────┬──────────────────────────────┘
                                │ summarized + sanitized on write
                                ▼
┌──────────────────────────────────────────────────────────────┐
│                         THE LEDGER                           │
│   Compact markdown candidate entries — never raw traces      │
│   Searchable · local-first · unapproved entries expire 7–14d │
└───────────────────────────────┬──────────────────────────────┘
                                │
             ┌──────────────────┴──────────────────┐
             ▼                                     ▼
 recurrence signal (N ≥ 3)              user ledger query
 batched — never a popup          "that rollback last Tuesday"
             └──────────────────┬──────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────┐
│                     REVIEW & PROMOTION                       │
│   Pull-based: review command · session end · weekly digest   │
│   Effect summary → source traces → 2-question interview      │
└───────────────────────────────┬──────────────────────────────┘
                                │ approved
                                ▼
┌──────────────────────────────────────────────────────────────┐
│                          SYNTHESIS                           │
│   Parameterize · dedup (>85% → merge into v1.x) · compose    │
│   Emit: SKILL.md + scripts/ + declared deps in metadata      │
└───────────────────────────────┬──────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────┐
│                  DISTRIBUTION & LIFECYCLE                    │
│   provisional → trusted (earned through successful use)      │
│   hot → cold → archived (demoted, never auto-deleted)        │
│   Local `.claude/skills/` · team PR · dep check at pull time │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. How It Works

### Step 1 — Ingestion & Observation

* **Passive listener:** A lightweight background daemon or terminal hook observes execution traces — commands, MCP calls, file edit sequences.
* **Active dictation:** Workflows can be dictated in plain English (*"when we update X, run Y, check Z, then notify the on-call"*), for quick-thinking developers and non-technical contributors alike.

### Step 2 — Segment into tasks

A session is not a workflow. One sitting routinely holds several unrelated tasks — a deploy, an unrelated bug fix, an investigation that goes nowhere — and fingerprinting the whole thing as one unit is why a workflow performed three times can register as three unrelated one-offs that never reach the threshold. **Developers reuse tasks, not sessions**, so the unit of comparison has to be the unit of reuse.

Sessions are therefore cut into episodes before anything is fingerprinted, on two signals that cost nothing to compute:

* **Completion markers** — a command whose success means the developer's *goal* is done, not merely that a step worked. Version-control verbs qualify: nobody commits halfway through a thought. Infrastructure commands (`terraform apply`, `kubectl apply`, `npm publish`) deliberately do not — see §5b.
* **A new prompt** — the developer stating a fresh goal, recognised by its position in the recorded stream rather than by any clock. No idle-gap timer: a threshold needs tuning per person and misfires the moment somebody reads documentation mid-task.

A boundary that would leave an episode of fewer than two substantive steps is ignored, because one step is not a workflow. An episode that ends only because the session did, with no marker, is **flagged rather than proposed** — that is an investigation with nothing to show for itself.

### Step 3 — Summarize on Write (not on read)

Raw traces are **never persisted**. At capture time each observation is compressed into a compact markdown ledger entry and scrubbed in the same pass:

* **Compression** keeps the ledger the same order of magnitude as the skill library itself, rather than the tens of megabytes raw MCP payloads and file diffs would consume.
* **Sanitization happens once, on write.** An AST/regex scan strips API keys, tokens, credentials, internal URLs, and customer PII before anything touches disk — so the ledger is never a liability sitting in a buffer waiting to be cleaned later.
* **Searchability comes for free**, because entries are already text.

### Step 4 — Candidate Surfacing (two entry points)

The ledger is not just a suggestion queue; it is a searchable record of your own work.

* **Recurrence signal:** Workflows recurring **3+ times** across sessions are flagged as proposal candidates.
* **User query:** Developers can search the ledger directly — *"that migration rollback from last Tuesday"* — and promote a one-off themselves. This matters because value and frequency correlate only weakly: the highest-value procedures (incident response, cert rotation, quarterly release) are often rare by nature.

Unapproved candidates expire and self-delete after 7–14 days.

### Step 5 — Review & Promotion (pull, never push)

**No interrupting popups.** Candidates persist in the ledger, so review is something the developer pulls when they have attention to spare: an explicit review command, a prompt at session end, a weekly digest, or a nudge at PR time.

Review is designed to take seconds, not minutes:

1. **Effect summary first.** The proposal leads with what the skill will *do* — commands it runs, paths it writes, anything destructive, any network calls — not what it's *for*. A purpose summary ("deploys to staging and runs smoke tests") can be perfectly accurate while the underlying steps are wrong; effect summaries are what make a fast review a real one.
2. **Evidence one keypress away.** The candidate is shown against the ledger entries it was derived from, with diverging steps highlighted. Confirming "yes, that's what I did" is far faster than auditing free-floating instructions.
3. **Targeted clarification.** Because synthesis happens *at approval*, the system can ask what traces can never reveal — but only where the trace is genuinely ambiguous. See §4.

Nothing is written to the skill library without passing this gate.

### Step 6 — Synthesis

Only after approval is a `SKILL.md` generated.

* **Parameterization:** Local paths (`/Users/dev/project/...`) and environment-specific values become template variables (`${PROJECT_PATH}`).
* **Deduplication:** Semantic similarity check against the existing library; matches above 85% expand an existing skill's `v1.x` rather than spawning a near-duplicate.
* **Hierarchical composition:** Atomic sub-routines (e.g. `git-commit`) are extracted once and invoked as sub-skills by higher-level orchestrators, forming a DAG rather than a flat pile of prompts.

---

## 4. Clarification at Approval

A trace records what happened, not why. The missing half — the diagnosis behind a retry, the rule behind a parameter, the check that happened in a browser — lives only in the developer's head, and approval is the one moment they are already looking at the workflow. Skill Plus Plus uses that moment to close the gap.

It is not a questionnaire. A fixed set of questions gets skipped by the third proposal. Instead **the ambiguity in the trace generates the question**, which means a clean candidate asks nothing and a messy one asks precisely about the part that is messy.

### Where the questions come from

| Trace signal | What is missing | Question generated |
| --- | --- | --- |
| **Failure then retry** — a command exits non-zero, a variant succeeds | The diagnosis, not the fix | *"What tells you to reach for `--force-lock` here?"* |
| **Divergence across occurrences** — run 1 hit staging, runs 2–3 hit prod | The rule behind the variable | *"Is this always the current branch, or does it vary?"* |
| **Workflow ends off-trace** — capture stops at deploy | The verification step | *"How do you know it worked?"* |
| **Unparameterizable literal** — a bare ID or URL | Whether it is fixed or per-run | *"Is this account ID constant across environments?"* |
| **Step present in some runs only** | Whether it is conditional or incidental | *"Is the cache clear required, or was that a one-off?"* |

The failure-then-retry case is the highest-value one: recovery behavior is the entire difference between a skill and a shell script, and it is the part a trace shows without explaining.

### Three disciplines that keep review fast

1. **Cap at three questions.** If synthesis has ten, the candidate is not ready — return it to the ledger rather than interrogating the developer. The question count is a quality signal about the candidate, not a budget to spend.
2. **Pre-fill a guess.** *"Staging — right?"* answered with Enter is a confirmation. An empty text box is composition, and composition is what people skip.
3. **Skipping never blocks.** Unanswered gaps still produce a skill, with an explicit `## Known gaps` section, landed as **provisional**. Better than blocking, and far better than guessing silently — and it gives provisional→trusted promotion something concrete to resolve, since the gap closes the first time someone runs the skill and hits that branch.

### When there is no trace

A dictated workflow — *"create a skill for this: I give you information, you
search online about the facts, give me in that format"* — has no execution
history to mine. Retries and divergence do not exist, so the signal table above
has nothing to work on.

What remains mechanically checkable is **completeness**: whether the
description covers trigger, procedure, output shape and failure handling. That
requires no understanding of the domain.

| Missing | Detected by | Question generated |
| --- | --- | --- |
| **A format named but never given** | The text refers to "this format" / "the usual structure" with no example, list or block anywhere in it | *"You refer to a format but never give one. What should the output look like — fields, order, an example?"* |
| **Trigger** | No `when` / `if` / `after` / `every time` condition | *"When should this fire?"* |
| **Source standard** | Research or verification is mentioned with no constraint on what counts | *"What counts as a good enough source, and how many need to agree?"* |
| **Failure handling** | No `otherwise` / `if not found` / `conflict` branch | *"What should happen when a step fails or comes back empty?"* |
| **A procedure that is one step** | Fewer than two stages parse out | *"What are the actual stages, in order?"* |

Dictated candidates skip the recurrence threshold. It exists to filter noise,
and an explicit request is not noise.

Ask for an **example of the output**, never a description of one. A worked
example costs the developer less time and specifies more.

### The agent answers first

Where review runs inside an agent session (see §8), the agent resolves what it can before involving the developer: does `deploy.sh` still exist, does it accept `--env`, what does `package.json` call the test script. Only what the repository cannot answer reaches a human.

On a clean candidate that is often zero questions. When it is three, all three are genuine judgment — which is the only kind worth a developer's attention.

---

## 5. Output Formats

A `SKILL.md` is an instruction file, not a tool definition. It cannot declare a tool or provision an MCP server. Skill Plus Plus therefore emits along three tracks:

| Captured pattern | Emitted as | Why |
| --- | --- | --- |
| Deterministic command pipeline | `scripts/` + thin `SKILL.md` wrapper | Captured determinism should come back as code, not as prose an agent re-derives (and re-fumbles) each run. Also far easier to review. |
| Judgment-shaped procedure | Instruction-only `SKILL.md` | Decision points, conventions, and escalation paths belong in prose. |
| MCP-dependent workflow | `SKILL.md` referencing tools by name + declared deps | Skills reference the host's existing tools; they never install them. |

**MCP handling.** Skill Plus Plus references only MCP servers already connected in the session, and never attempts to bundle or provision one. Three rules keep that safe once a skill travels to a teammate:

* **Prefer the portable path.** Where the ledger shows the same outcome is reachable through a CLI (`gh` instead of a GitHub MCP, `psql` instead of a Postgres MCP), the shell form is generated — it runs anywhere. MCP references are reserved for capabilities with no CLI equivalent.
* **Declare dependencies** — required servers and CLIs — in the `metadata` frontmatter key, which is already supported in the wild and requires no spec extension.
* **Check at pull, not at run.** On install, declared deps are diffed against the teammate's connected servers and missing ones are reported immediately. A skill whose dependency is absent must state what is missing and stop cleanly — never improvise a workaround.

### 5b. Why the marker list is short

Segmentation (§3, step 2) cuts on completion markers, and the temptation is to treat every "work landed" command as one. Infrastructure verbs are excluded on purpose, and the reason generalises.

Take a deploy that runs `terraform apply` and then `./scripts/deploy.sh <target>`. Treat `terraform apply` as a marker and the episode closes one step early, leaving `deploy.sh` as a fragment below the minimum size, which is then discarded. The truncated prefix is *identical across every occurrence*, so it still merges, still reaches the recurrence threshold, and still presents as a clean candidate — one that builds and provisions but never deploys, with nothing anywhere to flag it as incomplete.

**Over-cutting is worse than under-cutting.** An under-cut candidate is visibly wrong — a sprawling signature, a title naming the wrong task — and dies at review. An over-cut one looks correct and is silently missing its payload. A marker therefore has to mean *the developer's goal is done*, not *a step succeeded*.

Tests going red→green are excluded for the same reason: green tests mean the goal was met only when testing *was* the goal. Usually they are mid-task verification, and `signals.py` already mines the failure-then-retry pattern for question generation, which is the right use of it.

---

## 6. Lifecycle, Decay & Storage

**Approved skills are never auto-deleted.** Disuse is a poor proxy for value; a timer that removes anything untouched for 60 days preferentially destroys the incident runbook and keeps the command you would have typed from memory. On a shared skill it is worse still, since usage is distributed across a team.

The real cost of an unused skill is index bloat, not disk. So skills are **demoted, not deleted**:

| Tier | Behavior |
| --- | --- |
| **Hot** | Present in the always-loaded `name` + `description` index. |
| **Cold** | Dropped from the index; still discoverable by search and loadable on demand. |
| **Archived** | Retained, surfaced only by explicit lookup. |

* **Staleness ≠ disuse.** A skill rots when the script it calls is renamed or the flag it passes is removed. Decay is detected by checking whether referenced paths, commands, and tools still resolve — a cheap, accurate signal that a timer cannot approximate.
* **Expiry applies to the ledger, not the library.** Unapproved candidates disappear after 7–14 days; anything a human blessed is kept.
* **Storage.** Skill files average 1.5–3 KB; a full organizational library stays under 5 MB. The ledger stays in the same range because entries are summarized on write rather than stored as raw traces.
* **Context cost.** Agents load only the lightweight `name` + `description` index, pulling full instructions into the context window on demand.

---

## 7. Portability: The Honest Limits

Instruction-shaped skills travel cleanly across Claude Code, Cursor, OpenCode, and Microsoft Agent Framework. Tool-bound skills degrade:

* **MCP tool names are host-namespaced.** `mcp__github__create_pr` is not the same identifier in every runtime.
* **Frontmatter extensions vary.** `name` and `description` are universal; everything beyond them is host-specific.

Portability is therefore a property of the *skill*, not of the format. The generation rules in §5 exist to keep as many skills as possible in the portable class.

---

## 8. Claude Code Integration

**Terminal (CLI) is the capture surface. Desktop is the distribution surface.**

Claude Code in the terminal is the reference host for passive capture: hooks fire automatically, the ledger builds from every session, and skills land in `~/.claude/skills/` ready to use. Desktop has no passive capture—the desktop app runs its own Claude Code runtime that never reads the host's `~/.claude/settings.json`—but it does have an upload path for finished skills.

> Installation, verification, and the complete terminal→Desktop workflow are
> covered in [docs/claude-code.md](docs/claude-code.md). This section
> outlines the architecture.

### Capture: hooks, not a daemon

Claude Code fires hooks — shell commands receiving JSON on stdin — at `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`, `PreCompact`, and `Stop`, configured through `settings.json`. `PostToolUse` supplies the tool name, its input, and its result: a structured trace stream, considerably cleaner than parsing shell history. `SessionEnd` is the natural batching point for ledger writes and the pull-review nudge. No OS daemon, no separate install, and a far smaller infosec surface than a background listener.

**The decisive advantage is `UserPromptSubmit`.** It captures what the developer *asked for* next to what actually *ran*. Intent is the half of the picture a raw command log can never recover, and having it in the same session materially improves synthesis — it is what reduces §4's clarification pass from an interview to a confirmation.

It earns its keep twice over, because a prompt is also a **task boundary**. Prompts are recorded into the ordered step stream, not a separate list, so position alone records which prompt preceded which work — no timestamps, no gap threshold, nothing to tune. A shell-history tool has neither half.

### Surface mapping

| Skill Plus Plus concept | Claude Code primitive |
| --- | --- |
| Trace capture | `PostToolUse` / `PreToolUse` hooks |
| Intent capture | `UserPromptSubmit` hook |
| Task boundaries | `UserPromptSubmit` — recorded in the step stream, so a prompt's position marks where one task ends and the next begins |
| Segmentation + ledger write + review nudge | `SessionEnd` hook |
| Pull-based review UI | `.claude/commands/skillpp-review.md` → `/skillpp-review` |
| Skill output | `.claude/skills/<name>/SKILL.md` + `scripts/` |
| Dependency check at pull | Diff declared deps against `.mcp.json` and connected `mcp__<server>__<tool>` names |
| Progressive disclosure | Native — `name` + `description` indexed, body loaded on demand |

### Two constraints to design around

**Capture is terminal-only.** Hooks fire in Claude Code CLI sessions; the desktop
and web chat surfaces run their own runtimes that don't read the host's
`~/.claude/settings.json`, so they produce no ledger entries. This is a hard
boundary (§5a in docs/claude-code.md), not a configuration matter. Measured
directly: a chat session produced no buffer, no error, and no log entry — the
hook was never invoked at all.

**There is no built-in cold tier.** Claude Code indexes everything under the skills directory, so the hot/cold/archived model in §6 is implemented by physically moving files to a sibling directory (`.claude/skillpp/cold/`) with a retrieval skill that searches it. Demotion is a file move, not a flag.

### Packaging & Distribution

Two formats, two use cases:

| Format | Use | How |
| --- | --- | --- |
| **Upload ZIP** | Claude Desktop | `skillpp bundle --format upload`, then Customize → Skills |
| **Plugin** | Claude Code terminal, team (Phase 2) | `skillpp bundle --format plugin` |

A plugin bundles hooks, the review command, and retrieval skill. It's also the
upgrade path for MCP-dependent skills (§5) — plugins can declare `mcpServers`,
whereas a bare `SKILL.md` cannot.

> Hook names and payload shapes should be confirmed against current documentation before building. That surface evolves faster than the skill format does.

---

## 9. Key Differentiators

| Metric | Dust.tt | Superpowers | IDE-native memory (Cursor, Copilot, Claude Code) | **Skill Plus Plus** |
| --- | --- | --- | --- | --- |
| **Primary focus** | Team knowledge RAG | Engineering process rules (TDD, planning) | Per-developer context recall | **Operational workflow capture** |
| **Creation effort** | High (manual prompting) | Manual (maintainer-authored) | Low, but per-session and personal | **Passive capture, deliberate promotion** |
| **Input source** | Web forms & docs | Static GitHub repo | Chat transcripts | **Live traces + direct dictation** |
| **Retrospective search** | Documents only | N/A | Limited | **Full searchable work ledger** |
| **Security handling** | Space-level ACLs | N/A | Varies by vendor | **Sanitized on write, before disk** |
| **Skill structure** | Flat assistant prompts | Flat prompt files | Flat memory entries | **Sub-skill composition (DAG)** |
| **Team distribution** | Native | Manual repo sync | Weak / personal by design | **One-click PR + dep check at pull** |

The competitive pressure worth taking seriously is the fourth column: memory and rule-generation features bundled free with the IDE. Skill Plus Plus differentiates on the two things those do not do — a searchable ledger of past work, and team-grade distribution with dependency and lifecycle management.

---

## 10. Target Outcomes

1. **Self-building repository:** The operational capability library grows as a by-product of daily engineering work.
2. **Zero context bloat:** Tiering plus progressive disclosure keeps agent context windows fast and cheap regardless of library size.
3. **Nothing unreviewed, nothing lost:** Every skill was read and approved by a human; nothing approved is ever silently discarded.
4. **Portable by default:** Generation actively prefers forms that survive the trip to another developer's machine.

---

## 11. Open Questions

* **Does fast review stay real review?** The effect-summary and evidence design targets a 20-second review. If approval rates approach 100%, the gate has become a rubber stamp and the design has failed.
* **Is N ≥ 3 the right trigger?** Recurrence is a weak proxy for value. The searchable ledger hedges this, but the balance between pushed suggestions and pulled searches needs measurement.
* **Do developers actually answer the clarifying questions?** §4 assumes three pre-filled questions get answered rather than skipped. If the skip rate is high, most skills land permanently provisional with open `## Known gaps`, and the judgment layer never materializes.
* **Will infosec approve a background listener?** Sanitize-on-write and local-first storage are the mitigations. Hook-based capture (§8) sidesteps this almost entirely by removing the daemon, which is an argument for shipping the Claude Code integration first. This remains the primary enterprise adoption risk for the OS-daemon path.
* **Provisional → trusted promotion:** Landing skills as hints that earn trust through successful use makes shallow review safe. The promotion threshold is unvalidated.

---

## 12. Implementation

Python 3.9+, standard library only — no dependencies, because a hook that has
to import a third-party package is a hook that breaks somebody's session.

```
skillpp/
  config.py      paths and thresholds, all env-overridable
  sanitize.py    secret/PII scrubbing, applied on write
  segment.py     cuts a session into task episodes
  normalize.py   parameterisation + workflow signatures
  ledger.py      candidate entries: markdown body, JSON payload
  recurrence.py  lexical similarity and merge
  signals.py     gap detection, question generation, effect summaries
  capture.py     hook handlers (fail-safe: always exit 0)
  summary.py     review surface, SKILL.md scaffold, dependency check
  lifecycle.py   hot/cold/archived tiering, staleness, usage tracking
  install.py     settings.json wiring (dry run by default)
  cli.py         command dispatch
commands/skillpp-review.md   /skillpp-review — review captured candidates
commands/skillpp-new.md      /skillpp-new    — build a skill from a description
examples/demo.sh             end-to-end walkthrough on a scratch ledger
tests/fixtures/messy_session.py  demo.sh's sessions, polluted with unrelated work
tests/test_skillpp.py        88 tests
```

### Division of labour

The CLI does everything deterministic: capture, scrub, deduplicate, detect
gaps, summarise effects, manage tiers. The `/skillpp-review` command drives an
agent through everything that needs judgement — resolving what the repository
can answer, asking the developer at most three questions, and writing prose
worth reading. Neither half is useful alone.

### Commands

| Command | Purpose |
| --- | --- |
| `skillpp hook --event <E>` | Hook entry point; reads JSON on stdin, always exits 0 |
| `skillpp dictate --text "…"` | Create a candidate from a description instead of a trace |
| `skillpp review [--all]` | Candidates at or above the recurrence threshold |
| `skillpp sift [--apply]` | Ask a local model which candidates are methods rather than one-off jobs; parks the rest. Dry run without `--apply`. See [docs/episode-filter.md](docs/episode-filter.md) |
| `skillpp reopen <id>` | Undo a sift verdict |
| `skillpp merge [--apply]` | Merge candidates that are the same procedure worded differently, by embedding. Dry run without `--apply` |
| `skillpp split <id> --at N` | Split a candidate holding two procedures; the original is kept, not deleted |
| `skillpp name <id> --title … --description …` | Give a candidate a task-shaped name; written by the agent during `draft` |
| `skillpp accuracy` | How often the ranker agreed with your own promote/dismiss decisions |
| `skillpp keep` | Save the work so far as a candidate, without ending the session |
| `skillpp reconcile` | Report promoted skills whose file is gone; reports only, never decides |
| `skillpp draft <id> [--apply]` | Have your own agent write a draft `SKILL.md`; never installs it. Dry run without `--apply` |
| `skillpp show <id>` | Effect summary, evidence, open questions |
| `skillpp search <words>` | Search the ledger of your own past work |
| `skillpp scaffold <id> --name <n>` | Generate a starting `SKILL.md` |
| `skillpp promote <id> --skill-path <p>` | Mark a candidate promoted |
| `skillpp dismiss <id>` / `expire` | Dismiss one / delete unapproved past TTL |
| `skillpp lifecycle` / `tier <name> <tier>` | Inventory and demotion |
| `skillpp check --name <n>` | Dependency check at pull time (exit 2 if missing) |
| `skillpp bundle --out <dir> [--format upload\|plugin]` | Package skills: `upload` = one zip per skill for Customize → Skills; `plugin` = `.claude-plugin/` + `skills/` |
| `skillpp install [--apply]` | Wire Claude Code hooks; dry run without `--apply` |

### Built

Capture with intent, segmentation into task episodes, sanitize-on-write (typed
placeholders that keep signatures stable), the ledger with lexical dedup and
TTL expiry, all five trace gap signals from §4 with the three-question cap and
duplicate suppression, the dictation path with its completeness check and
threshold bypass, effect-first proposals, scaffolding with declared deps and
`## Known gaps` that close when answered, pull-time dependency checking,
hot/cold/archived demotion, staleness by reference resolution, and usage
tracking driven by observed `Skill` calls.

**Segmentation, measured.** `tests/fixtures/messy_session.py` takes the three
`demo.sh` deploy sessions, keeps the deploy byte-identical across all three so
it genuinely recurs, and surrounds each occurrence with different unrelated
work. Folded as whole sessions, the deploy scores 0.358–0.475 against the 0.85
threshold and is filed as three unrelated one-offs, each titled after whatever
happened to come first. Segmented, it is recovered as a single candidate at
three occurrences with a signature identical to the unpolluted baseline, and
titled after the deploy. Both halves are asserted, because the whole-session
path survives as `_fold_steps` and is still taken by single-episode sessions —
so the regression is a live test rather than a git archaeology exercise.

### Not built

Deliberately deferred — see §13 for phasing.

* **Team PR sync.** Phase 2. Only the receiving half exists today: declared
  dependencies and `skillpp check`.
* **Semantic deduplication.** Similarity is lexical — sequence and token
  overlap over normalised step shapes. No embeddings, because this runs inside
  a hook where a network round-trip is unacceptable. Semantic overlap is the
  reviewing agent's job, and `/skillpp-review` instructs it accordingly.
* **Script extraction.** The slash command tells the agent to lift
  deterministic pipelines into `scripts/run.sh`; the CLI does not do it
  automatically.
* **Automatic provisional→trusted promotion.** The tier is recorded and
  readable; usage counts are tracked; nothing promotes on them yet.
* **Voice input.** Dictation is text-only — `skillpp dictate` and
  `/skillpp-new`. Speech-to-text is somebody else's job; the parser does not
  care how the words arrive.
* **The OS-level shell daemon.** Capture is Claude Code hooks only, which is
  the sequencing argued for in §11.
* **Episode labelling.** Segmentation cuts and titles episodes from their own
  prompts, but nothing names the *varying parameter* — `staging` versus `prod`
  — which is what a synthesised skill needs to parameterise. That is judgement,
  and it belongs with the reviewing agent alongside semantic dedup.
* **Non-linear segmentation.** Cutting is linear, so a task interrupted by a
  second task and then resumed is mis-attributed. Recorded as a known
  limitation in the fixture rather than papered over.

### Verification status

Installed and confirmed firing in a live Claude Code terminal session. All
three hooks verified against real payloads: `PostToolUse` parses `Bash` and
`Edit` calls cleanly, `UserPromptSubmit` captures prompts verbatim, and
`SessionEnd` folds a buffer into a ledger entry.

Coverage is narrower than §8 originally claimed: chat-surface sessions are not
captured at all. See
[docs/claude-code.md](docs/claude-code.md#5a-which-sessions-get-captured).

Two limits worth stating plainly about segmentation:

* **`git commit` is the only marker with test coverage.** The other eight, and
  the artifact-delivery path, are implemented but unexercised.
* **It has never run against a real session.** Every result above comes from a
  fixture written for the purpose. The known risk — a mid-task "continue" or
  "fix that" cutting an episode in half — is precisely the thing a hand-written
  fixture cannot demonstrate, since its prompts are one-per-task by
  construction. Replaying real transcripts is the next thing that could show
  the design is wrong rather than merely incomplete.

---

## 13. Roadmap

### Phase 1 — single developer (built)

Capture, ledger, review, promotion, lifecycle. Everything a developer needs to
turn their own work into their own skills, on their own machine. The ledger
never leaves the laptop.

### Phase 2 — team distribution

The receiving half already exists: skills declare their dependencies and
`skillpp check` verifies them at pull time. What Phase 2 adds is the sending
half — exporting an approved skill as a pull request against a shared library,
with the lifecycle and dedup machinery extended across a team rather than a
directory.

Two design questions to settle before writing any of it, because both are
easier to get right at the start than to retrofit:

**What travels, and what stays.** A ledger entry holds project paths, session
ids, and the developer's own prompts. None of that belongs in a team
repository. A promoted skill's `metadata.provenance` currently points at a
local ledger id that a teammate cannot resolve — useful locally, meaningless
after the trip. Phase 2 needs an explicit split between the skill (travels) and
its provenance (stays), rather than letting the current field quietly leak
context into a PR.

**Whether provisional skills should travel at all.** Tiering assumes trust is
earned through successful use (§6). But use by whom? A skill that one developer
has run twice is not validated for a team, and shipping `tier: provisional`
into a shared library either means nothing or means "do not rely on this" —
which is not obviously a thing worth distributing. The plausible rule is that
only `trusted` skills open a PR, which makes automatic promotion a Phase 2
prerequisite rather than a nice-to-have.

**Team-level dedup is the real work.** Lexical similarity is adequate for one
person's ledger. Across a team, the same workflow arrives written five
different ways, and catching that is a judgement problem — which points at the
reviewing agent, not at a similarity threshold.

### Later

Voice input, the OS-level shell daemon, automatic script extraction.
