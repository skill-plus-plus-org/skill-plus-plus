<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/banner-dark.svg">
  <img alt="Skill++: repeated work becomes a reviewed skill" src="docs/images/banner-light.svg" width="720">
</picture>

# Spot the work you keep repeating. Turn it into skills.

**A local model cuts your Claude Code sessions into tasks and spots the procedures you repeat. On the third run, your own agent writes one down as a skill.**

https://github.com/user-attachments/assets/e18baeb6-2ec4-4b33-be42-d77eb7d6af0a

<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="MIT license"></a>
<img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python 3.10+">
<img src="https://img.shields.io/badge/detection-runs_locally-orange?style=flat-square" alt="Detection runs locally">
<img src="https://img.shields.io/badge/for-Claude_Code-8A2BE2?style=flat-square" alt="For Claude Code">

📏 **51 recorded sessions: 14 of 15 task switches found, 0 false cuts in 143 places.** **[→ Under the hood](#-under-the-hood)**

⚡ **Three commands, no API key, no account.** **[→ Quick Start](#-quick-start)**

</div>

---

<div align="center">

**[See it](#-see-it) · [Why](#-why-this-exists) · [How it works](#-how-it-works) · [Under the hood](#-under-the-hood) · [Quick Start](#-quick-start) · [Your first skill](#-your-first-skill) · [The numbers](#-the-numbers) · [When to use](#-when-to-use--when-to-skip) · [Docs](docs/usage.md)**

</div>

---

## 👀 See it

Every sprint, the same review deck: read what was merged and closed on GitHub,
outline the slides, check every bullet against the tickets, build it on the
company template. The third time, Skill++ has it ready.

<table>
<tr>
<td width="33%" valign="top"><a href="docs/images/session-cut.png"><img src="docs/images/session-cut.png" alt="The end of a session: three jobs in one chat, then Skill++ reports the session cut into 3 tasks, the sprint review seen 3 times and ready to review"></a></td>
<td width="33%" valign="top"><a href="docs/images/review-candidate.png"><img src="docs/images/review-candidate.png" alt="The review page: the sprint review candidate at 3x, its five requests with the tools each used, and the three sessions it was seen in"></a></td>
<td width="33%" valign="top"><a href="docs/images/skill-installed.png"><img src="docs/images/skill-installed.png" alt="The drafted SKILL.md, installed in the project: when to use it, the GitHub MCP tools it requires, and the procedure"></a></td>
</tr>
<tr>
<td valign="top"><b>① You work as usual.</b><br><sub>Three jobs in one chat. When it ends, a local model cuts it into tasks and counts the sprint review a third time.</sub></td>
<td valign="top"><b>② It shows you the repeat.</b><br><sub>The candidate at 3×: every request with the tools it used, and the three sessions it came from.</sub></td>
<td valign="top"><b>③ Your agent writes the skill.</b><br><sub>Drafted from the first run, with the GitHub MCP tools it needs, installed in the project.</sub></td>
</tr>
</table>

This is the demo in the video above, on a demo repo. Click an image for the
full size.

---

## 🌍 Why this exists

Every team has procedures it runs again and again: the sprint review deck,
shipping a small feature with its tests, turning a document into a talk. An agent skill
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

## 🧠 Under the hood

Three problems stand between a chat log and a skill. Skill++ solves the first
two on your machine, and every answer below is measured on recorded sessions:
22 public recordings anyone can replay, 21 sessions of private work, and 8
recordings held out while the method was tuned.

### 1. Where does one task end?

One chat is rarely one task. At every message you typed between two tool calls,
Skill++ asks a local model (`gemma4:e4b`, through Ollama) one question: 158
questions for 979 tool calls across the recordings. The question sets the
earlier task and your new message side by side in named sections, says what
counts as a new task, and asks for one word. It runs in the background after
the session ends, in about a second and a half per message.

**14 of 15 task switches found, 0 false cuts in 143 places** where a review, a
correction or a follow-up must stay in its task.

### 2. Is this the same procedure again?

Each task is embedded (`nomic-embed-text`) as its conversation: your requests,
the first 300 characters of each reply, the skills it used and the kinds of
files it produced. File names are masked, so what a run was about does not
decide what it was. A task joins a candidate only at a cosine similarity of
0.85 or more (0.93 when there is no conversation to compare): a duplicate you
can see is cheap, and a wrong merge would mix two procedures into one skill.

**0 wrong merges. The same prompts, and the same goal driven differently,
merged 12 of 12 times.**

### 3. What goes into the skill?

Your own agent (`claude -p` by default) drafts it from the candidate's first run
and your note: the method, in its own words, not the subject of that run. What
it cannot tell from the run becomes an open question, at most three, each with
three suggested answers, and your answers are folded back in. The CLIs and MCP
tools the run needed become the skill's requirements.

**The method was right in all 4 drafts checked**, by a Claude judge that has to
quote the line of the draft behind every yes.

### And the parts you don't see

- Keys, tokens, cookies, connection strings and email addresses are scrubbed
  before anything is written.
- A session the app was quit on is picked up the next time one starts.
- The review page listens on `127.0.0.1` only and refuses requests from other
  pages.
- 530 tests run in about ten seconds, with no model. The
  [research log](docs/research/) keeps every experiment, the failed ones too.

---

## ⚡ Quick Start

You need macOS or Linux, Python 3.10+, [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
logged in, and [Ollama](https://ollama.com) running (`brew install ollama` on a Mac).

```bash
pipx install skill-plus-plus
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
- The latest `main`, before it is released:
  `pipx install git+https://github.com/himanshu096/skill-plus-plus`.
- From a clone: `pip install -e .`, or run `python3 bin/skill-plus-plus` without
  installing anything.

</details>

---

## 🕐 Your first skill

1. **Work as usual.** Each session is cut into tasks when it ends, in the
   background.
2. **Open the review page:** `skill-plus-plus web`. It is served on `127.0.0.1` and only
   this machine can reach it.
3. **Pick the project** in the menu at the top right.
4. **Candidates** lists what you repeated, most-seen first, each with a summary
   and its steps grouped under the requests they served (② above). At three
   runs, a candidate can be promoted or dismissed.
5. **Promote** it, then press **Draft Skill**. The optional note tells the agent
   what the later runs taught you: it reads only the first.
6. **Answer its open questions** on the Drafts tab: pick a suggested answer or
   write your own. Each answer is folded back into the skill; **Revise** sends
   any other instruction.

   <img src="docs/images/draft-questions.png" width="560" alt="A draft on the Drafts tab with three open questions, each with three suggested answers and a field for your own; the first answer is picked">

7. **Install in your project** (commit `.claude/skills/` to share it), or **just
   for you** (③ above). A later revision reaches it with **Update**.

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

**Where one task ends**: 22 of 22 sessions right; 9 of 9 task switches caught;
**0 false cuts over 77 prompts**, reviews, corrections and side questions
included.

| Check | Code | Knowledge work |
|---|---|---|
| one task, several follow-ups | 6/6 | 6/6 |
| reviews and corrections stay in the task | 1/1 | 6/6 |
| a switch nobody announced | 1/1 | 1/1 |
| an announced switch | 1/1 | 1/1 |
| three tasks in one chat | 1/1 | 1/1 |
| a second task of the same kind | 1/1 | – |

Beyond the public set, on 21 sessions of private work and 8 recordings held out
while the question was tuned: 5 of 6 task switches found, 0 false cuts in 66
places.

**Do repeats become one candidate**: 0 wrong merges.

| How alike the runs are | Code | Knowledge work |
|---|---|---|
| the same prompts | 3/3 pairs | 3/3 pairs |
| the same goal, driven differently | 3/3 | 3/3 |
| the same procedure, another subject | **4/53** | 12/12 |

> The bold row is real. Code on a new subject is what matching keeps apart: it
> prefers a duplicate you can see to a wrong merge that mixes two procedures into
> one skill.

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

## 🏠 Where it was made

<p align="center">
  <img src="docs/images/headquarters.jpg" width="360" alt="A small glass meeting pod with two benches and a table: the Skill++ headquarters">
</p>
<p align="center"><sub>Skill++ headquarters: one meeting pod, where it all started.</sub></p>

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
