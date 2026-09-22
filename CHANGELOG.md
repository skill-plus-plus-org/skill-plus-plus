# Changelog

All notable changes to skillpp. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

The first public release.

### What it does

- Captures Claude Code sessions through four hooks, scrubbed on write.
- Cuts each finished session into tasks with a local model (`gemma4:e4b`),
  asked once per prompt gap, in a detached worker after the session ends.
- Recognises repeated work with embeddings (`nomic-embed-text`), preferring a
  missed merge to a wrong one.
- A local review page: candidates with their steps grouped under the requests
  they served, promote and dismiss, **Draft Skill** with a note for the agent,
  drafts with open questions to answer, revisions and download.
- Drafts written by your own agent (`claude -p` by default), never installed
  without you.

### Changed before release

- Installable with `pipx`, with a `skillpp` console script; an installed copy
  can draft.
- The review page refuses requests made by other pages in the same browser.
- Describing every tool call with the local model is off by default; it cost
  2-11 seconds a call and nothing read it.
- `install` copies all three slash commands, and `--remove` takes them out again.
- More git commands that discard uncommitted work count as destructive.
- The test suite needs no model and runs in about ten seconds.
