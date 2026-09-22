# Privacy

skillpp records what you do with Claude Code so it can recognise the work you
repeat. This page says exactly what that record holds, where it is kept, what
is removed from it, and the one way any of it leaves your machine.

## What is recorded

For every session in a project where the hooks are wired:

| What | Kept | Limit |
| --- | --- | --- |
| Your prompts | the text you typed | 2,000 characters each |
| Tool inputs | `Bash`: the command and its description. `Write`: the path and the content. `Edit`: the path and both strings. `Read`, `Glob`, `Grep`: the path or pattern. `WebFetch`, `WebSearch`: the URL, prompt or query. `Skill`, `Task`: the name and arguments. MCP tools: up to five plain arguments. Other tools: the name only. | 2,000 characters a field |
| Tool results | the start of what came back | 400 characters |
| The agent's words | what it said around each tool call, and its reply to each prompt, read from Claude Code's transcript of the session | 400 characters around a call, 8,000 per reply |
| Context | the session id, the working directory, timestamps, whether a step failed | |

It does not record files it did not see go through a tool, your environment, or
anything from sessions in projects where the hooks are not wired.

## Where it lives

Everything is under `~/.claude/skillpp/` (or `SKILLPP_ROOT`):

| Path | Holds |
| --- | --- |
| `sessions/` | the buffer of each session until it is folded, and any session held because Ollama did not answer |
| `ledger/` | one file per candidate: its steps, its first run's conversation, how often it was seen, your decision |
| `drafts/<id>/` | drafted skills, earlier versions in `.revisions/`, and `agent.log`, the drafting agent's transcript |
| `embeddings.json` | the vectors used to recognise repeats |
| `review_summaries.json` | the one-line summaries the review page shows |
| `decisions.jsonl`, `usage.json` | what you promoted and dismissed, and which skills were used |
| `cold/`, `archive/` | skills you moved out of the loaded index |
| `skillpp.log` | errors and one line per fold |

**Nothing expires on its own.** A held session waits until it can be folded;
drafts and their logs stay until you delete them. `skillpp expire` removes
candidates that stopped recurring, and `rm -rf ~/.claude/skillpp/` removes
everything.

## What is removed before anything is written

Every captured string is scrubbed on write, so an unscrubbed copy never lands on
disk. Matches become a typed placeholder such as `[REDACTED:github-token]`:

- private keys; AWS, GitHub, Slack, Anthropic, OpenAI and Google keys; JWTs and
  bearer tokens
- database connection strings, URLs with a password in them, and `key=value`
  credentials
- email addresses, and URLs on `.internal`, `.corp`, `.intranet`, `.local` and
  `.lan` hosts
- any other long, random-looking string, as a fallback

**What it does not recognise:** people's names, phone numbers, IP addresses,
hostnames outside the domains above, and anything sensitive written in plain
prose or inside a file's content. Treat the ledger as you would your shell
history.

## What leaves your machine

The local models run on your machine through Ollama, and the review page is
served on `127.0.0.1` only. It refuses requests that come from any other page in
your browser.

Something leaves only when you ask for a draft. **Draft Skill** and **Revise**
(or `skillpp draft` / `skillpp revise` with `--apply`) start your agent, `claude
-p` by default, which reads that one candidate, its first run's conversation and
steps, and your note or instruction, and sends them to its model provider as any
prompt you type would be. Nothing else from the ledger is sent, and nothing is
sent without `--apply` or a click.

Two things you can do that publish more than that, and should read first:

- **Uploading a skill** through Customize → Skills puts it in your Claude
  account.
- **Committing a skill or a bundle** to a shared repo publishes whatever the
  procedure mentions.

## Leaving things out

- One session: start it as `SKILLPP_INTERNAL=1 claude`.
- One project: wire the hooks with `--project` into the repos you want captured,
  rather than `--user` into every project.
- A candidate: dismiss it on the review page, or delete its file in `ledger/`.
