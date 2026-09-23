# Security

## Reporting a problem

Please report security problems privately, through the repository's
**Security → Report a vulnerability** page, not in a public issue. Say what an
attacker can do, what they need first, and how to reproduce it. The
maintainers aim to reply within a week.

If it is a vulnerability, you get a fix or a mitigation, and a line in the
changelog crediting you by whatever name you choose, or none. If it is not, you
get the reasoning, and you are free to write about it publicly.

Only the latest release is supported.

## What skillpp can touch

Useful context for judging whether something is a vulnerability:

- **The hooks run on every prompt and tool call** of the Claude Code sessions
  they are wired into, as your user. They read the hook's payload and the
  session transcript, and write only under `~/.claude/skillpp/`.
- **The review page** (`skillpp web`) listens on `127.0.0.1` only and has no
  login, because only processes on your machine can reach it. It refuses a
  request whose `Host` is not that address, and a POST that is not JSON or comes
  from another page's `Origin`, because a POST can start your agent or write a
  skill into a repository. **Install** writes one folder, the skill's, into the
  project the candidate belongs to or into your own skills folder; that path is
  built from the ledger, never from the request. **Uninstall** removes only the
  files it recorded installing.
- **The drafting agent** (`skillpp draft`, **Draft Skill**) is whatever
  `SKILLPP_AGENT` names, by default `claude -p` allowed `Read`, `Write`, `Edit`
  and `python3 bin/skillpp`. It runs in a temporary folder and reads one
  candidate. What it can reach beyond that is decided by the agent and its
  permissions, not by skillpp.
- **Scrubbing** removes credential-shaped strings and email addresses from
  prompts, tool calls and replies; the working directory is kept as it is. It is
  a net, not a guarantee; what it does not catch is listed in
  [docs/privacy.md](docs/privacy.md). A secret that gets past it and
  into the ledger is worth reporting.

## Out of scope

- Anything that needs someone to already run code as your user on your machine.
- What your own agent or its model provider does with a prompt you sent.
- Skills you install or upload yourself; read a generated skill before
  installing it.
