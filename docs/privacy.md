# Privacy

skill-plus-plus records what you do with Claude Code so it can recognise the work you
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

Everything is under `~/.claude/skill-plus-plus/` (or `SKILL_PLUS_PLUS_ROOT`):

| Path | Holds |
| --- | --- |
| `sessions/` | the buffer of each session until it is folded, and any session held because Ollama did not answer |
| `ledger/` | one file per candidate: its steps, its first run's conversation, how often it was seen, your decision |
| `drafts/<id>/` | drafted skills, earlier versions in `.revisions/`, and `agent.log`, the drafting agent's transcript |
| `embeddings.json` | the vectors used to recognise repeats |
| `review_summaries.json` | the one-line summaries the review page shows |
| `decisions.jsonl`, `usage.json` | what you promoted and dismissed, and which skills were used |
| `cold/`, `archive/` | skills you moved out of the loaded index |
| `skill-plus-plus.log` | errors and one line per fold |

**Nothing expires on its own.** A held session waits until it can be folded;
drafts and their logs stay until you delete them. `skill-plus-plus expire` removes
candidates that stopped recurring, and `rm -rf ~/.claude/skill-plus-plus/` removes
everything.

## What is removed before anything is written

Your prompts, tool inputs and results, and the agent's words are scrubbed on
write, so an unscrubbed copy of them never lands on disk. Matches become a
typed placeholder such as `[REDACTED:github-token]`:

- private keys, PGP included; AWS, GitHub, Slack, Anthropic, OpenAI, Google,
  Stripe, GitLab and Hugging Face keys; JWTs; `Authorization` headers (Bearer,
  Basic and Token)
- cookies, from a `Cookie:` header or curl's `-b` / `--cookie`
- database connection strings, a password in a URL of any scheme, and
  credentials written as `key=value` or `key: value`, quoted and bracketed keys
  included (`"password":`, `user[password]=`); a key with a prefix when it is an
  environment variable in capitals (`DB_PASSWORD=`) or its value is quoted
  (`"client_secret": "…"`)
- email addresses, URL-encoded ones (`%40`) included, and URLs on `.internal`,
  `.corp`, `.intranet`, `.local` and `.lan` hosts
- any other random-looking string of 40 characters or more, as a fallback,
  also when it is written with `%2F`, `%2B` or `%3D`

**Kept as they are:** the working directory, the transcript's path and the
session id, because the project and the conversation are found from them; and
an SSH or scp target (`git@github.com:acme/api.git`, `deploy@host:/srv/`), which
names a server rather than a person.

**What it does not recognise:** people's names, phone numbers, IP addresses,
hostnames outside the domains above, a password or token passed as a flag's
value without `=` (`-p secret`, `--password secret`, `curl -u user:secret`),
a lower-case prefixed or camelCase key with an unquoted value
(`client_secret: abc123`, `dbPassword=abc123`), a value that reads as code
(`password = getpass()`), secrets written in hexadecimal (such as `openssl rand -hex` output, which looks
the same as a commit hash), long strings made of only one or two kinds of
character, and anything sensitive written in plain prose or inside a file's
content. Treat the ledger as you would your shell history.

## What leaves your machine

The local models run on your machine through Ollama, and the review page is
served on `127.0.0.1` only. It refuses requests that come from any other page in
your browser.

Something leaves only when you ask for a draft. **Draft Skill** and **Revise**
(or `skill-plus-plus draft` / `skill-plus-plus revise` with `--apply`) start your agent, `claude
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

- One session: start it as `SKILL_PLUS_PLUS_INTERNAL=1 claude`.
- One project: wire the hooks with `--project` into the repos you want captured,
  rather than `--user` into every project.
- A candidate: dismiss it on the review page, or delete its file in `ledger/`.
