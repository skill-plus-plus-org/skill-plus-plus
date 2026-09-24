<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/banner-dark.svg">
  <img alt="Skill++: repeated work becomes a reviewed skill" src="docs/images/banner-light.svg" width="720">
</picture>

# Spot the work you keep repeating. Turn it into skills.

**Detection runs on your machine and costs nothing. Your agent writes a skill only when you ask.**

<!-- VIDEO/GIF: the review page, candidate → Promote → Draft Skill → answer a question → Install; at most 880 px wide -->

<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT license"></a>
<img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python 3.10+">
<img src="https://img.shields.io/badge/detection-runs_locally-orange?style=flat-square" alt="Detection runs locally">
<img src="https://img.shields.io/badge/for-Claude_Code-8A2BE2?style=flat-square" alt="For Claude Code">

⚡ **Three commands, no API key, no account.** **[→ Quick Start](#-quick-start)**

</div>

---

<div align="center">

**[See it](#-see-it) · [Why](#-why-this-exists) · [How it works](#-how-it-works) · [Quick Start](#-quick-start) · [Your first skill](#-your-first-skill) · [The numbers](#-the-numbers) · [When to use](#-when-to-use--when-to-skip) · [Docs](docs/usage.md)**

</div>

---

## 👀 See it

<table>
<tr>
<th width="45%">What you typed, in one of three sessions</th>
<th width="55%">What your agent drafted from it</th>
</tr>
<tr>
<td valign="top">

1. *Look at textkit/wordfreq.py and suggest how to add a --min-length option that skips words shorter than N letters. Don't change anything yet.*
2. *Go ahead and implement it.*
3. *Add tests for --min-length and run all the tests.*
4. *Commit it.*

</td>
<td valign="top">

**adding-a-cli-flag-to-an-argparse-tool**<br>
*Use when adding a new flag/option to an existing Python argparse-based CLI: plan the flag, thread it through the call chain, cover it with tests, then commit.*

1. Plan before touching code
2. Implement on approval
3. Smoke test
4. Add tests, then run the whole suite
5. Commit only when asked
6. Flag repo hygiene as a heads-up, not a silent fix

</td>
</tr>
</table>

Four prompts about one option became a procedure for any option: the plan
before the code, the approval, the full test run, the commit. That is the
method you followed, written down once. The example is one of the public
recordings in [tests/fixtures/sessions/](tests/fixtures/sessions/), and anyone
can draft it again.

---

## 🌍 Why this exists

Every team has procedures it runs again and again: shipping a small feature with
its tests, turning a document into a talk, adding an eval case. An agent skill
(`SKILL.md`) makes an agent follow such a procedure the same reliable way every
time, but almost nobody writes them: by the time a procedure is worth a skill,
you have done it three times and moved on.

Skill++ finds those procedures for you. It watches your Claude Code sessions,
notices when you repeat the same kind of work within a project, and shows it to
you as a candidate. Promote one, and your own agent drafts the skill from what
you actually did. Nothing is installed without you.

**Where it is going.** The goal is skills shared across a team, so that a
procedure one person worked out is done the same way by everyone: faster, and
without the mistakes each person would otherwise make on their own. This first
version works end to end for one person, and its skills already reach a team
the simple way: install one into the repo's `.claude/skills/`, commit it, and
everyone working in the repo has it.

---

## 🔧 How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/how-it-works-dark.svg">
  <img alt="How Skill++ works: 1, capture while you work: hooks, scrub, session buffer. 2, fold in the background after the session: cut, segment, extract, match. 3, review and draft when you choose: ledger, review page, Draft Skill, you." src="docs/images/how-it-works-light.svg">
</picture>

1. **Capture.** While you work, hooks record your prompts, the tools that ran,
   and the agent's replies into one file per session. Secrets, tokens and
   emails are scrubbed before anything is written. When the session ends, it is
   handed on; a session that could not be (the app was quit, say) is picked up
   the next time one starts.
2. **Fold.** A background worker asks a local model, at each point where you
   typed something, whether a new task started there, and splits the session
   into tasks. Each task is compared with the candidates of the same project:
   the same procedure again adds to its count, anything else becomes a new
   candidate.
3. **Review and draft.** Seen three times, a candidate is ready on the review
   page, where you promote or dismiss it. **Draft Skill** hands its first run to
   your own agent (`claude -p` by default), with a note from you if you like.
   The agent writes the procedure in its own words, and anything it could not
   tell from the run becomes an open question. Answer them, then install the
   skill into the project or just for you.

---

## ⚡ Quick Start

You need macOS or Linux, Python 3.10+, [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
logged in, and [Ollama](https://ollama.com) running (`brew install ollama` on a Mac).

```bash
pipx install git+https://github.com/himanshu096/skill-plus-plus
skill-plus-plus install --project ~/code/my-repo --apply   # hooks, slash commands and the two local models
skill-plus-plus doctor                                     # everything green?
```

Then start a new Claude Code session in that repo (a new chat in the CLI, or a
new Code session in the desktop app) and work as usual.

<details>
<summary><strong>More ways in</strong> · every project, dry runs, removing it, from a clone</summary>

<br>

- `install` without `--apply` only shows what it would do, downloads included.
  Your settings file is backed up before it is changed.
- `--user` instead of `--project` captures every project on the machine.
- The two models are about 10 GB on disk, and cutting a session needs about
  10 GB of free memory. `--no-models` leaves Ollama alone.
- `skill-plus-plus install --project ~/code/my-repo --remove --apply` takes the hooks
  and slash commands out again.
- From a clone: `pip install -e .`, or run `python3 bin/skill-plus-plus` without
  installing anything.

</details>

---

## 🕐 Your first skill

1. **Work as usual.** Each session is cut into tasks when it ends, in the
   background.
2. **Open the review page:** `skill-plus-plus web`. It is served on `127.0.0.1` and only
   this machine can reach it.
3. **Pick the project** in the menu beside the tabs, if you have more than one.
<!-- SCREENSHOT: docs/images/review-candidates.png, a candidate opened, its steps grouped under the requests they served -->
4. **Candidates** lists what you repeated, most-seen first, each with a summary
   and its steps grouped under the requests they served. At three runs, a
   candidate can be promoted or dismissed.
5. **Promote** it, then press **Draft Skill**. The optional note tells the agent
   what the later runs taught you: it reads only the first.
<!-- SCREENSHOT: docs/images/draft-questions.png, a draft with its open questions to answer -->
6. **Answer its open questions** on the Drafts tab. Each answer is folded back
   into the skill; **Revise** sends any other instruction.
<!-- SCREENSHOT: docs/images/install.png, the Install in <project> / Just for me buttons -->
7. **Install in your project** (commit `.claude/skills/` to share it), or **just
   for you**. A later revision reaches it with **Update**.

**Faster than three repeats:** `SKILL_PLUS_PLUS_RECURRENCE=1` makes a candidate ready the
first time it is seen. **Describing a procedure instead of doing it** (work in
progress): run `/skill-plus-plus-new` in a session and describe it.

---

## 💸 What it costs

Watching and detecting run entirely on your machine, with two local models
through Ollama: no API calls, no cost, and nothing leaves your laptop. The
frontier model is called only at the very end, once per skill: after you have
promoted a candidate and pressed **Draft Skill**. Only then does that one run's
conversation go to it.

---

## 📊 The numbers

Measured on 22 public recordings of code and knowledge work, with the defaults
(`gemma4:e4b`, `nomic-embed-text`). The weak rows stay in the tables.

**Where one task ends**: 19 of 22 sessions right; 6 of 9 task switches caught;
**0 false cuts over 77 prompts**, reviews, corrections and side questions
included.

| Check | Code | Knowledge work |
|---|---|---|
| one task, several follow-ups | 6/6 | 6/6 |
| reviews and corrections stay in the task | 1/1 | 6/6 |
| a switch nobody announced | 1/1 | 1/1 |
| an announced switch | **0/1** | 1/1 |
| three tasks in one chat | **0/1** | 1/1 |
| a second task of the same kind | **0/1** | – |

**Do repeats become one candidate**: 0 wrong merges.

| How alike the runs are | Code | Knowledge work |
|---|---|---|
| the same prompts | 3/3 pairs | 3/3 pairs |
| the same goal, driven differently | 3/3 | 3/3 |
| the same procedure, another subject | **1/23** | 12/12 |

> The bold rows are real. Code switches are what the judge misses, and code on
> a new subject is what matching keeps apart: it prefers a duplicate you can see
> to a wrong merge that mixes two procedures into one skill.

**Drafts**: on four recorded sessions, every draft's method was right. A
Claude judge checked each one, and every yes it gave quotes the line of the
draft that backs it. The latest
change to the draft prompt took the drafts whose description says when to use
the skill from 2/4 to 4/4, and the drafts that leaked a path from the run from
1 to 0.

Reproduce them from [tests/fixtures/sessions/](tests/fixtures/sessions/): `score.py`,
`recurrence.py`, `tests/benchmarks/judge_replay.py` and
`tests/benchmarks/draft_check.py`. The earlier measurements are in
[docs/research/](docs/research/).

---

## 🧭 When to use · when to skip

**Good fit if you** repeat procedures in Claude Code (the CLI or the desktop
app's Code tab), on macOS or Linux, with about 10 GB of memory to spare for the
local model.

**Skip it if you** mostly do one-off work, run Windows, or use another agent:
capture is Claude Code only for now. Drafting can use any agent CLI
(`SKILL_PLUS_PLUS_AGENT`).

**Known limits**
- A task continued in a new chat becomes two half-tasks; nothing links one chat
  to the next.
- Two tasks in one prompt look like one: the judge only looks where you typed
  something.
- A draft reads one run, the first. The note on **Draft Skill** carries what the
  later ones taught you.

---

## 🔒 Privacy

Everything Skill++ captures stays in `~/.claude/skill-plus-plus/` on your machine,
scrubbed of keys, tokens, cookies, connection strings and email addresses.
Names, phone numbers and the content of your files are **not** recognised, and
the folder you work in is kept as it is, so treat it like your shell history. Something leaves your machine only when you press **Draft
Skill** or **Revise**. Nothing expires automatically.
[docs/privacy.md](docs/privacy.md) says exactly what is stored where.

## 🤝 Contributing

The most useful contribution is a recorded session of real, public work,
especially two tasks in one chat or a task finished in a second chat.
[CONTRIBUTING.md](CONTRIBUTING.md) explains how to record one, run the tests (no
model needed, about ten seconds) and measure a change.

## 📜 License

[MIT](LICENSE).

---

<sub>
<strong>Docs:</strong>
<a href="docs/usage.md">Usage, commands and settings</a> ·
<a href="docs/privacy.md">Privacy</a> ·
<a href="docs/architecture.md">Architecture</a> ·
<a href="docs/research/">Research log</a> ·
<a href="CONTRIBUTING.md">Contributing</a> ·
<a href="SECURITY.md">Security</a> ·
<a href="CHANGELOG.md">Changelog</a>
</sub>
