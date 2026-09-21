"""Command line interface.

Division of labour: this CLI does everything deterministic. The
``/skillpp-review`` slash command drives an agent through the parts that need
judgement — reading the proposal, resolving what the repo can answer, asking
the developer at most three questions, and writing the final prose.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .capture import (fold_dictation, handle_prompt, handle_session_end,
                      handle_tool, log_error, mark_ending)
from .config import Config, default_skills_dir
from .ledger import Ledger, STATUS_DISMISSED, STATUS_PROMOTED
from .lifecycle import move_tier, scan
from .signals import detect
from .summary import (check_dependencies, questions_for, render_proposal,
                      scaffold_skill)


# --------------------------------------------------------------------------
# hook — must never fail loudly
# --------------------------------------------------------------------------

def cmd_hook(args: argparse.Namespace) -> int:
    """Read a hook payload on stdin and record it.

    Always exits 0. A capture failure must never disrupt the developer's
    session; errors go to the log instead.
    """
    # A `draft` run is an agent session like any other, so its own poking
    # around gets captured and banked as a candidate — measured: two junk
    # entries titled `/skillpp-draft <id>` after two runs. Automation observing
    # itself is a feedback loop, and the marker is set by the process that
    # spawns it.
    if os.environ.get("SKILLPP_INTERNAL"):
        return 0

    config = Config(args.root)
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        event = args.event or payload.get("hook_event_name", "")
        config.ensure_dirs()

        if event == "UserPromptSubmit":
            handle_prompt(config, payload)
        elif event == "PostToolUse":
            handle_tool(config, payload)
        elif event == "SessionEnd":
            # Stamp and hand off. The judge and the embeddings used to run
            # here, inside the hook: one cold model call can reach
            # `boundary.DEFAULT_TIMEOUT`, Claude Code's hook budget is about a
            # minute, and quitting the app gives less — so the hook was killed
            # and the session waited out `PENDING_IDLE_HOURS` before anything
            # banked it.
            #
            # `Stop` is deliberately not handled. It fires at the end of every
            # agent turn, not at the end of a session, so folding there would
            # cut one procedure into per-turn fragments — and now that the fold
            # is a detached worker that unlinks the session file, it would also
            # race `handle_tool` still appending to it.
            sid = str(payload.get("session_id", "unknown"))
            result = mark_ending(config, sid, payload.get("transcript_path"))
            if result.get("status") == "ending":
                proc = _spawn_background_process(config, "fold-session", sid)
                result = {"status": "spawned", "session": sid, "pid": proc.pid}
            if args.verbose:
                print(json.dumps(result))
        elif event == "SessionStart":
            # Bank what earlier sessions left behind — held because the local
            # model did not answer at their end, or never ended at all. In the
            # background: a session must not wait on a model to start.
            proc = _spawn_background_process(
                config, "fold-pending", "--exclude", str(payload.get("session_id", "")))
            if args.verbose:
                print(json.dumps({"status": "spawned", "pid": proc.pid}))
        elif args.verbose:
            print(json.dumps({"status": "ignored-event", "event": event}))
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all
        log_error(config, f"hook error: {type(exc).__name__}: {exc}")
    return 0


# --------------------------------------------------------------------------
# ledger inspection
# --------------------------------------------------------------------------

def _agent_argv(config: Config, prompt: str) -> list[str]:
    """The agent command with the prompt as one argument. Raises ValueError."""
    import shlex
    return [prompt if part == "{PROMPT}" else part.replace("{PROMPT}", prompt)
            for part in shlex.split(config.agent_command)]


def _agent_workspace(entry_id: str) -> Path:
    """A directory the drafting agent is actually allowed to write to.

    The draft belongs under `<root>/drafts/<id>/`, which on a default install
    is inside `~/.claude` — and the agent's sandbox refuses `Write` and `Edit`
    on anything under there. Measured, not guessed: the first run after the
    agent's output was kept said so in as many words, having spent 93 seconds
    composing a draft it could not save. Only `skillpp scaffold` got through,
    because that write happens inside an allowed `Bash` subprocess, so every
    draft was the scaffold and nothing else.

    The lab that produced a good draft wrote into a temp directory, which is
    the difference nobody could see. So the agent works in temp and the result
    is moved into place afterwards by this process, which has no such limit.
    """
    import tempfile
    return Path(tempfile.mkdtemp(prefix=f"skillpp-{entry_id[:8]}-"))


def _agent_home(entry_id: str) -> Path:
    """Where the drafting agent runs: a directory holding the two things its
    prompt relies on, `/skillpp-draft` and `python3 bin/skillpp`.

    It used to run from this checkout, where both happen to exist. An installed
    package has neither beside it, so a `pipx` install could draft nothing.
    `bin/skillpp` here runs the skillpp that started the agent, whichever way it
    was installed. Kept apart from the draft workspace, because everything
    beside a written SKILL.md is collected into the draft.
    """
    import shutil
    import tempfile
    from .install import COMMANDS
    home = Path(tempfile.mkdtemp(prefix=f"skillpp-agent-{entry_id[:8]}-"))
    commands = home / ".claude" / "commands"
    commands.mkdir(parents=True)
    shutil.copy2(COMMANDS / "skillpp-draft.md", commands / "skillpp-draft.md")
    shim = home / "bin" / "skillpp"
    shim.parent.mkdir()
    package_parent = Path(__file__).resolve().parent.parent
    shim.write_text(
        "#!/usr/bin/env python3\n"
        '"""The skillpp that started this agent (see `cli._agent_home`)."""\n'
        "import sys\n"
        f"sys.path.insert(0, {str(package_parent)!r})\n"
        "from skillpp.cli import main\n"
        "raise SystemExit(main())\n", encoding="utf-8")
    shim.chmod(0o755)
    return home


def _collect(work: Path, out_dir: Path) -> list[Path]:
    """Move what the agent wrote into the draft directory."""
    import shutil
    written = sorted(work.rglob("SKILL.md"))
    landed = []
    for source in written:
        for path in sorted(source.parent.rglob("*")):
            if not path.is_file():
                continue
            target = out_dir / path.relative_to(source.parent)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            landed.append(target)
    return sorted(p for p in landed if p.name == "SKILL.md")


def _write_agent_log(out_dir: Path, argv: list[str], started: float,
                     said: str, outcome: str) -> None:
    """Keep what the drafting agent said, beside the draft, on every run.

    The draft stage's only other output is the file, and two real runs produced
    a SKILL.md that was pure scaffold — no prose, skillpp's own TODO comment
    shipped verbatim — while exiting 0. Nothing recorded whether a tool call
    was refused, what the agent thought it was doing, or which model answered,
    so the runs could not be diagnosed at all. Written for successes too: a
    failure explains itself, a bad success does not.
    """
    import shlex
    try:
        (out_dir / "agent.log").write_text(
            f"# {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
            f"  {outcome}  {time.time() - started:.1f}s\n"
            f"# {' '.join(shlex.quote(a) for a in argv)}\n\n{said}",
            encoding="utf-8")
    except OSError:
        pass


def cmd_draft(args: argparse.Namespace) -> int:
    """Have the developer's own agent write a draft SKILL.md for a candidate.

    The division of labour this whole tool rests on: code decides *which*
    candidate is worth the call (capture, segmentation, then `sift`), and a
    frontier model writes the body, because that is the half measured out of
    reach of a local one — a 7B transcribes the run instead of generalising it.

    An agent command rather than an API call, so there is no key to hold and no
    vendor baked in: whatever agent the developer already uses does the writing,
    already authenticated.

    Drafts land under `<root>/drafts/` and are never installed. Promotion stays
    a human act — an unapproved skill appearing in the skills directory is the
    failure the design exists to prevent.
    """
    import shlex
    import subprocess

    config = Config(args.root)
    entry = Ledger(config).get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1

    out_dir = config.root / "drafts" / (args.name or entry.id)
    # The directory goes in the prompt as a literal, not as an environment
    # variable for the agent to expand. A sandboxed Bash call containing `$VAR`
    # is rejected outright — "Contains expansion" — because an allowed-tools
    # pattern cannot be checked against a command whose text is not yet known.
    # SKILLPP_ROOT below still works, because that is read by the Python
    # process rather than expanded in a shell.
    work = _agent_workspace(entry.id)
    prompt = f"/skillpp-draft {entry.id} {work}"
    # The developer's note goes in the prompt itself, after the two arguments,
    # not into what `show` prints: a tool's output is read as evidence, and
    # this is an instruction from the person the skill is for. Claude Code
    # splits a slash command on spaces only and hands the agent everything
    # after its name, so a note over several lines arrives whole.
    note = (args.note or "").strip()
    if note:
        prompt += f"\n\nNote from the developer, on what to look out for:\n{note}"
    try:
        argv = _agent_argv(config, prompt)
    except ValueError as exc:
        print(f"SKILLPP_AGENT is not a valid command: {exc}", file=sys.stderr)
        return 1

    import shutil
    found = shutil.which(argv[0])
    print(f"candidate  {entry.id}  x{entry.occurrences}  {entry.title[:60]}")
    print(f"draft dir  {out_dir}")
    print(f"workspace  {work}")
    if note:
        print(f"note       {note[:200]}")
    print(f"agent      {' '.join(shlex.quote(a) for a in argv)}")
    # Said before the call rather than discovered during it: `claude` is often
    # not on PATH even where Claude Code is in use.
    print(f"resolves   {found or 'NO — not on PATH; set SKILLPP_AGENT'}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to spend one model call.")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    # The allowed-tools pattern names `python3 bin/skillpp`, and the prompt is
    # `/skillpp-draft`: both resolve from the agent's home, installed or not.
    home = None if args.cwd else _agent_home(entry.id)
    where = args.cwd or home
    try:
        # Captured rather than streamed so the decline sentinel can be read out
        # of it; echoed below so nothing is hidden.
        # The agent runs `python3 bin/skillpp show <id>` with no --root, so
        # without this it reads the default ledger and cannot find a candidate
        # that lives anywhere else. Passed as the environment variable Config
        # already honours rather than asking the prompt to thread a flag.
        env = dict(os.environ, SKILLPP_INTERNAL="1",
                   SKILLPP_ROOT=str(config.root),
                   # Where the draft belongs. The prompt used to say
                   # `<draft-dir>` with nothing substituting it, so the agent
                   # invented a path in its own scratchpad and the draft was
                   # written correctly to somewhere nobody would look.
                   SKILLPP_DRAFT_DIR=str(work))
        proc = subprocess.run(argv, cwd=where, timeout=args.timeout,
                              capture_output=True, text=True, env=env,
                              # Without this the agent waits on a tty it will
                              # never get, and stalls before starting.
                              stdin=subprocess.DEVNULL)
        said = (proc.stdout or "") + (proc.stderr or "")
        _write_agent_log(out_dir, argv, started, said,
                         f"exit {proc.returncode}")
        print(said, end="")
    except FileNotFoundError:
        _write_agent_log(out_dir, argv, started, "", "agent not found")
        print(f"\nNo such agent: {argv[0]}. Set SKILLPP_AGENT to how yours is "
              f"invoked.", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        _write_agent_log(out_dir, argv, started, "",
                         f"timed out after {args.timeout}s")
        print(f"\nThe agent did not finish within {args.timeout}s.",
              file=sys.stderr)
        return 1
    finally:
        if home:
            shutil.rmtree(home, ignore_errors=True)

    renamed = Ledger(config).get(entry.id)
    if renamed and (renamed.title != entry.title or renamed.description):
        print(f"\nnamed      {renamed.title}")
        if renamed.description:
            print(f"           {renamed.description}")

    written = _collect(work, out_dir)
    if not written:
        # "Declined" and "broke" must not look alike, or an unattended run
        # reports a dead agent as a considered judgement. The exit code is what
        # separates them: an unauthenticated CLI exits 1 and writes nothing,
        # which is indistinguishable from a decline by any other signal.
        if proc.returncode != 0:
            print(f"\nThe agent failed (exit {proc.returncode}) and wrote "
                  f"nothing. Its output is above — if it says it is not logged "
                  f"in, authenticate it once by running `claude` and then "
                  f"`/login`.", file=sys.stderr)
            return proc.returncode
        # A decline has to be *stated*, never inferred from an absent file. A
        # blocked tool, a denied permission and a considered "nothing here" all
        # write no file and can all exit 0 — the first live run hit exactly
        # that, and reporting it as a judgement made a broken agent look like a
        # working filter.
        said = (proc.stdout or "") + (proc.stderr or "")
        for line in said.splitlines():
            if line.strip().startswith("SKILLPP-DECLINE:"):
                reason = line.split(":", 1)[1].strip()
                print(f"\nNo draft — the agent judged there was no reusable "
                      f"procedure here: {reason}")
                return 0
        print("\nInconclusive: no draft, and the agent did not say it was "
              "declining. Read its output above — it was most likely blocked "
              "rather than unconvinced.", file=sys.stderr)
        return 1
    for path in written:
        print(f"\ndrafted {path}")
    print("Read it, then install with: skillpp promote "
          f"{entry.id} --skill-path <path>")
    return proc.returncode


# The instruction is the developer's own words; the rest pins the agent to the
# one file, so a revision can never land somewhere nobody looks or install
# itself. Kept inline rather than as a slash command: it is short, and it needs
# no install step to work.
_REVISE_PROMPT = """Revise a draft skill. The file is {skill_md}

What the developer wants changed:
{instruction}

Rules:
- Edit only {skill_md}, in place. Do not create or write any other file.
- Keep the YAML frontmatter valid: a `name`, and a `description` of at most 200
  characters that says when the skill applies.
- Change what was asked and keep everything else as it is.
- To look at the evidence the draft was written from, run
  `python3 bin/skillpp show {entry_id}`.
- Do not run `python3 bin/skillpp promote` and do not write into any skills
  directory.
- If the request cannot be done, say why in one line starting with
  SKILLPP-DECLINE: and leave the file unchanged."""


def cmd_revise(args: argparse.Namespace) -> int:
    """Have the developer's agent change a draft SKILL.md as instructed.

    Only an existing draft under `<root>/drafts/<id>/` is revised, in place. The
    version before the revision is kept in `.revisions/` beside it, which the
    web page leaves out of the download. Like `draft`, nothing is installed.
    """
    import hashlib
    import subprocess
    from datetime import datetime, timezone

    config = Config(args.root)
    entry = Ledger(config).get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    instruction = (args.instruction or "").strip()
    if not instruction:
        print("Say what to change with --instruction.", file=sys.stderr)
        return 1
    drafted = sorted((config.root / "drafts" / entry.id).rglob("SKILL.md"))
    drafted = [p for p in drafted if ".revisions" not in p.parts]
    if not drafted:
        print(f"No draft for {entry.id}. Create one first: skillpp draft "
              f"{entry.id} --apply", file=sys.stderr)
        return 1
    skill_md = drafted[0]
    # Revised in a workspace for the same reason a draft is written in one:
    # `Edit` is denied on anything under `~/.claude`, so an in-place revision
    # silently changed nothing. See `_agent_workspace`.
    work = _agent_workspace(entry.id)
    working_copy = work / skill_md.name
    working_copy.write_bytes(skill_md.read_bytes())
    prompt = _REVISE_PROMPT.format(skill_md=working_copy, instruction=instruction,
                                   entry_id=entry.id)
    try:
        argv = _agent_argv(config, prompt)
    except ValueError as exc:
        print(f"SKILLPP_AGENT is not a valid command: {exc}", file=sys.stderr)
        return 1
    print(f"candidate  {entry.id}  {entry.title[:60]}")
    print(f"draft      {skill_md}")
    print(f"change     {instruction[:200]}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to spend one model call.")
        return 0

    before = skill_md.read_bytes()
    started = time.time()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    history = skill_md.parent / ".revisions"
    history.mkdir(exist_ok=True)
    (history / f"SKILL.{stamp}.md").write_bytes(before)
    env = dict(os.environ, SKILLPP_INTERNAL="1", SKILLPP_ROOT=str(config.root),
               SKILLPP_DRAFT_DIR=str(work))
    # The prompt says `python3 bin/skillpp show`; see `_agent_home`.
    home = None if args.cwd else _agent_home(entry.id)
    try:
        proc = subprocess.run(argv, cwd=args.cwd or home,
                              timeout=args.timeout, capture_output=True, text=True,
                              env=env, stdin=subprocess.DEVNULL)
        said = (proc.stdout or "") + (proc.stderr or "")
        _write_agent_log(skill_md.parent, argv, started, said,
                         f"revise, exit {proc.returncode}")
        print(said, end="")
    except FileNotFoundError:
        _write_agent_log(skill_md.parent, argv, started, "", "agent not found")
        print(f"\nNo such agent: {argv[0]}. Set SKILLPP_AGENT to how yours is "
              f"invoked.", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        _write_agent_log(skill_md.parent, argv, started, "",
                         f"timed out after {args.timeout}s")
        print(f"\nThe agent did not finish within {args.timeout}s.", file=sys.stderr)
        return 1
    finally:
        if home:
            import shutil
            shutil.rmtree(home, ignore_errors=True)

    if proc.returncode != 0:
        print(f"\nThe agent failed (exit {proc.returncode}).", file=sys.stderr)
        return proc.returncode
    said = (proc.stdout or "") + (proc.stderr or "")
    for line in said.splitlines():
        if line.strip().startswith("SKILLPP-DECLINE:"):
            print(f"\nNot revised: {line.split(':', 1)[1].strip()}", file=sys.stderr)
            return 1
    if not working_copy.exists():
        print("\nThe agent removed the draft; the previous version was kept.",
              file=sys.stderr)
        return 1
    after = working_copy.read_bytes()
    if hashlib.sha256(after).digest() == hashlib.sha256(before).digest():
        print("\nThe agent made no change to the draft.", file=sys.stderr)
        return 1
    # Moved back by this process. The agent cannot write here itself.
    skill_md.write_bytes(after)
    print(f"\nrevised {skill_md}  (previous version in {history.name}/SKILL.{stamp}.md)")
    return 0


# A skill's description is the only thing read when deciding whether to load it,
# and the frontmatter limit is 200 characters. A candidate whose description
# would not fit is not ready to become one.
_MAX_DESCRIPTION = 200
_MAX_TITLE = 80


def cmd_name(args: argparse.Namespace) -> int:
    """Give a candidate a task-shaped name and a description.

    Written by the agent during `skillpp draft`, because this is the half code
    cannot do. Capture can only reuse a string it observed, so an unnamed
    candidate carries whatever the developer typed — and a skill named after a
    greeting never fires, however correct its steps are.
    """
    config = Config(args.root)
    ledger = Ledger(config)
    entry = ledger.get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    if args.title:
        if len(args.title) > _MAX_TITLE:
            print(f"Title is {len(args.title)} characters; keep it under "
                  f"{_MAX_TITLE}. Name the task, not the session.",
                  file=sys.stderr)
            return 1
        entry.title = args.title.strip()
    if args.description:
        if len(args.description) > _MAX_DESCRIPTION:
            print(f"Description is {len(args.description)} characters; the "
                  f"frontmatter limit is {_MAX_DESCRIPTION}. A description that "
                  f"will not fit cannot become a skill.", file=sys.stderr)
            return 1
        entry.description = args.description.strip()
    if not (args.title or args.description):
        print("Nothing to set. Pass --title and/or --description.",
              file=sys.stderr)
        return 1
    ledger.save(entry)
    print(f"{entry.id}  {entry.title}")
    if entry.description:
        print(f"          {entry.description}")
    return 0


def cmd_split(args: argparse.Namespace) -> int:
    """Split a candidate that holds two procedures, at a step boundary.

    Reached from `skillpp draft`, where a frontier model is already reading the
    candidate. Code banks one candidate per episode and cuts only at markers and
    prompt boundaries, so a single request that did two things with no
    recognisable finish between them arrives as one entry. A local model can
    locate that boundary when told one exists but cannot tell whether one does —
    measured 3 of 5 — which is why this is not attempted cheaply.

    The original is kept as `split`, not deleted: the verdict came from a model
    and the steps are the only evidence there was.
    """
    from .ledger import STATUS_CANDIDATE, STATUS_SPLIT, Entry, new_id

    config = Config(args.root)
    ledger = Ledger(config)
    entry = ledger.get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1

    at = args.at
    head, tail = entry.steps[:at], entry.steps[at:]
    floor = config.min_episode_steps
    if len(head) < floor or len(tail) < floor:
        print(f"Refusing: a split at {at} leaves {len(head)} and {len(tail)} "
              f"steps, and {floor} is the minimum for a workflow. One step is "
              f"not a procedure, so this would strand it.", file=sys.stderr)
        return 1

    made = []
    for steps in (head, tail):
        part = Entry(
            id=new_id(config.ledger_dir), status=STATUS_CANDIDATE,
            title=entry.title, steps=steps,
            # Provenance is shared: both halves were observed in the same
            # sessions, and each half was recognized as often as the whole.
            projects=list(entry.projects), sessions=list(entry.sessions),
            intents=list(entry.intents), occurrences=entry.occurrences,
            source=entry.source)
        ledger.save(part)
        made.append(part)

    entry.status = STATUS_SPLIT
    entry.notes = (f"{entry.notes}\nsplit at step {at} into "
                   f"{made[0].id} and {made[1].id}").strip()
    ledger.save(entry)
    print(f"split {entry.id} at step {at}:")
    for part in made:
        print(f"  {part.id}  {len(part.steps)} steps")
    print("Name each half with: skillpp name <id> --title … --description …")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    """Fold existing candidates that are the same procedure.

    Capture already matches each new episode as it is saved. This is for what
    is already in the ledger: entries saved before matching used embeddings,
    entries banked `unmatched` while no model answered, and a check after the
    floor changes.

    Every candidate is compared with every other candidate and with every
    promoted skill, by cosine over cached vectors, at `match_floor` unless
    `--floor` says otherwise. The floor is set where a wrong merge stops
    happening, not where the most merges happen: a wrong merge silently mixes
    two procedures into one skill, while a missed one only leaves a duplicate a
    person can still see.

    Dry run unless ``--apply``, because folding is not symmetrical: the second
    candidate's evidence moves into the first and the second is gone. Every
    applied fold is written to decisions.jsonl, the only record that the
    absorbed entry existed.
    """
    from . import decisions
    from .similar import AUTO_MERGED
    from .ledger import STATUS_CANDIDATE, STATUS_COVERED, STATUS_PROMOTED
    from .local import LocalModelUnavailable, cosine
    from .matching import floor_for, has_conversation, load_cache, save_cache, vector_for
    from .similar import fold_into

    config = Config(args.root)
    ledger = Ledger(config)
    entries = [e for e in ledger.all()
               if e.status in (STATUS_CANDIDATE, STATUS_PROMOTED)]
    cache = load_cache(config)
    try:
        vectors = {e.id: vector_for(e, config, cache) for e in entries}
    except LocalModelUnavailable as exc:
        print(f"No embedding model: {exc}. Nothing was compared.", file=sys.stderr)
        return 1
    finally:
        save_cache(config, cache)

    pairs = []
    for i, a in enumerate(entries):
        for b in entries[i + 1:]:
            if a.status == STATUS_PROMOTED and b.status == STATUS_PROMOTED:
                continue          # two skills: not ours to reconcile
            if has_conversation(a.turns) != has_conversation(b.turns):
                continue          # conversation and steps are not on one scale
            score = cosine(vectors[a.id], vectors[b.id])
            floor = args.floor if args.floor is not None else floor_for(a, config)
            if score >= floor:
                pairs.append((score, a, b))
    # Entries banked without matching go first, then the most similar.
    pairs.sort(key=lambda p: (not (p[1].unmatched or p[2].unmatched), -p[0]))

    chosen, seen = [], set()
    for score, a, b in pairs:
        if a.id in seen or b.id in seen:
            continue              # one merge per entry per run, so folds cannot chain
        chosen.append((score, a, b))
        seen.update({a.id, b.id})

    if not chosen:
        shown = (f"{args.floor:.2f}" if args.floor is not None else
                 f"{config.match_floor_turns:.2f} (conversation) / {config.match_floor:.2f} (steps)")
        print(f"No pair at or above {shown}. Nothing to merge.")
        return 0
    for score, a, b in chosen:
        skill = a if a.status == STATUS_PROMOTED else b if b.status == STATUS_PROMOTED else None
        what = "covered by skill" if skill else "same procedure  "
        print(f"{what} embedding {score:.3f}")
        print(f"          {a.id} {a.title[:56]}")
        print(f"          {b.id} {b.title[:56]}")
    if not args.apply:
        print(f"\nDry run. Re-run with --apply to fold {len(chosen)} pair(s).")
        return 0

    for score, a, b in chosen:
        skill = a if a.status == STATUS_PROMOTED else b if b.status == STATUS_PROMOTED else None
        if skill is not None:
            # A skill is never folded away; being matched means it was used again.
            other = b if skill is a else a
            for sid in other.sessions:
                if sid not in skill.sessions:
                    skill.sessions.append(sid)
            skill.occurrences += other.occurrences
            other.status = STATUS_COVERED
            other.unmatched = False
            other.notes = (f"covered by promoted skill {skill.id} "
                           f"(embedding {score:.3f})\n" + (other.notes or "")).strip()
            ledger.save(skill)
            ledger.save(other)
            decisions.record(config, skill, AUTO_MERGED,
                             note=f"used again — {other.id} ({other.title[:60]}) "
                                  f"is covered by this skill; embedding {score:.3f}")
            print(f"\n{other.id} is covered by skill {skill.id}")
            continue
        keep, drop = a, b
        fold_into(keep, drop)
        keep.unmatched = False
        ledger.save(keep)
        ledger.delete(drop.id)
        decisions.record(config, keep, AUTO_MERGED,
                         note=f"absorbed {drop.id} ({drop.title[:80]}) — "
                              f"embedding {score:.3f}")
        print(f"\nfolded {drop.id} into {keep.id} — now seen {keep.occurrences}x")
    return 0


def cmd_fold_session(args: argparse.Namespace) -> int:
    """Bank one session that has ended. What the `SessionEnd` hook spawns.

    This process is nobody's child and its stdio is `DEVNULL`, so it is the one
    component in the system with no observable output at all. Every run writes
    a line to the log, success or failure, because otherwise "why was my
    session not banked?" has no answer anywhere.
    """
    from .capture import fold_session_now

    config = Config(args.root)
    config.ensure_dirs()
    short = args.session_id[:8]
    try:
        result = fold_session_now(config, args.session_id,
                                  transcript=args.transcript)
    except Exception as exc:  # noqa: BLE001 - detached; a traceback goes nowhere
        log_error(config, f"fold-session {short} failed: "
                          f"{type(exc).__name__}: {exc}")
        return 1
    log_error(config, f"fold-session {short}: {result.get('status')}"
                      + (f" {result['id']}" if result.get("id") else ""))
    if args.verbose:
        print(json.dumps(result))
    return 0


def _spawn_background_process(config: Config, *argv: str):
    """Start a skillpp command detached, and do not wait for it.

    `start_new_session` matters: without it the child is in the hook's process
    group, so the terminal closing — or Claude Code reaping the hook — can
    signal a process that is midway through a model call.
    """
    import subprocess
    package_root = Path(__file__).resolve().parent.parent
    return subprocess.Popen(
        [sys.executable, "-m", "skillpp", "--root", str(config.root), *argv],
        cwd=str(package_root),
        env={**os.environ, "PYTHONPATH": str(package_root)},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True)


def cmd_fold_pending(args: argparse.Namespace) -> int:
    """Bank sessions that ended without being banked (see `capture.fold_pending`)."""
    from .capture import fold_pending

    config = Config(args.root)
    config.ensure_dirs()
    results = fold_pending(config, exclude=args.exclude, idle_hours=args.idle_hours)
    if results == [{"status": "locked"}]:
        print("Another fold-pending is running.")
        return 0
    for r in results:
        episodes = [e for e in (r.get("episodes") or []) if e.get("status") in ("created", "merged")]
        if r["status"] == "live":
            what = "still live, skipped"
        elif r["status"] == "folding":
            what = "a worker is folding it"
        elif r["status"] == "offline":
            what = f"held again: {r.get('reason', '')}"
        elif episodes:
            what = "banked " + ", ".join(f"{e['status']} {e['id']}" for e in episodes)
        else:
            what = r["status"]
        print(f"  {r['session'][:8]}  {what}")
    if not results:
        print("No pending sessions.")
    return 0


def cmd_keep(args: argparse.Namespace) -> int:
    """Save the work so far as a candidate, without ending the session."""
    from .capture import keep_current

    config = Config(args.root)
    config.ensure_dirs()
    result = keep_current(config, args.session_id)
    status = result.get("status")
    if status == "no-session":
        print("No session buffer to keep. Hooks record one as you work, so run "
              "this from inside a Claude Code session.", file=sys.stderr)
        return 1
    if status == "nothing-yet":
        print("Nothing recorded yet in this session.", file=sys.stderr)
        return 1
    episodes = result.get("episodes") or [result]
    kept = [e for e in episodes if e.get("status") in ("created", "merged")]
    if not kept:
        print("Nothing substantial enough to keep — a workflow needs at least "
              f"{config.min_episode_steps} steps.")
        return 1
    print(f"kept {len(kept)} candidate(s):")
    for e in kept:
        print(f"  {e.get('id')}  seen {e.get('occurrences', 1)}x")
    print("Name and draft one with: skillpp draft <id> --apply")
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Serve the ledger as a local page. Loopback only; there is no auth."""
    from .config import default_skills_dir
    from .web import serve

    config = Config(args.root)
    config.ensure_dirs()
    skills_dir = (Path(args.skills_dir).expanduser() if args.skills_dir
                  else default_skills_dir())
    httpd = serve(config, skills_dir, port=args.port,
                  open_browser=not args.no_browser)
    url = f"http://127.0.0.1:{httpd.server_port}/"
    print(f"serving  {url}")
    print(f"ledger   {config.root}")
    print(f"skills   {skills_dir}")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


def cmd_ignored(args: argparse.Namespace) -> int:
    """List what has been parked, and what has happened since.

    Parked entries stay matched by recurrence — otherwise the next occurrence
    opens a fresh candidate and quietly undoes the decision. So their counts keep
    moving, and a count that keeps moving is the one honest signal that a
    parking may have been wrong.

    Nothing is re-proposed here. A decision is not overturned by a counter; this
    only reports that the evidence changed.
    """
    from .ledger import STATUS_DISMISSED, STATUS_ONE_OFF

    config = Config(args.root)
    parked = [e for e in Ledger(config).all()
              if e.status in (STATUS_DISMISSED, STATUS_ONE_OFF)]
    if not parked:
        print("Nothing parked.")
        return 0
    threshold = args.threshold or config.recurrence_threshold
    parked.sort(key=lambda e: -e.recurrences_since_parked())
    print(f"parked      {len(parked)}")
    for entry in parked:
        again = entry.recurrences_since_parked()
        flag = "  ← done again since" if entry.parking_looks_wrong(threshold) else ""
        print(f"  {entry.status:9} {entry.id}  x{entry.occurrences}"
              f"  (+{again} since){flag}")
        print(f"            {entry.title[:56]}")
    wrong = [e for e in parked if e.parking_looks_wrong(threshold)]
    if wrong:
        print(f"\n{len(wrong)} parked {threshold}+ times since. Not a "
              f"re-proposal — put one back with: skillpp reopen <id>")
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Report promoted skills whose file is gone. Reports only."""
    from .lifecycle import reconcile

    config = Config(args.root)
    result = reconcile(Ledger(config), config)
    print(f"promoted    {result['promoted']}   ({result['live']} live)")
    if not result["missing"]:
        print("No drift.")
        return 0
    print(f"missing     {len(result['missing'])}")
    for row in result["missing"]:
        print(f"  {row['id']}  {row['title'][:44]}")
        print(f"            {row['skill_path']}")
    print("\nNothing was changed. A promoted entry with no file keeps matching "
          "future work while never surfacing for review — decide with "
          "`skillpp reopen <id>` or `skillpp dismiss <id>`.")
    return 1


def cmd_accuracy(args: argparse.Namespace) -> int:
    """How often the ranker agreed with you, on decisions you actually made.

    The benchmark in `tests/benchmarks` is 17 hand-written cases whose ground
    truth was authored by whoever wrote the detector — which measures internal
    consistency, and hid a defect until it was scored against real work. This
    measures the thing itself, and it grows on its own: every promote, dismiss
    and reopen adds a label.
    """
    from . import decisions

    config = Config(args.root)
    result = decisions.score(config)
    if not result["judged"]:
        print("No decisions recorded yet. Promote or dismiss a candidate and "
              "this starts filling in.")
        return 0
    print(f"decisions   {result['judged']}")
    if result["unranked"]:
        print(f"unranked    {result['unranked']}   (decided before sift ran; "
              f"not scorable)")
    if result["scored"]:
        pct = 100 * result["agreed"] / result["scored"]
        print(f"agreed      {result['agreed']}/{result['scored']}  ({pct:.0f}%)")
    for miss in result["misses"]:
        print(f"  ranker said {miss['hint']:8} · you {miss['decision']:9} "
              f"· {miss['title'][:44]}")
    return 0


def cmd_reopen(args: argparse.Namespace) -> int:
    """Put a parked candidate back: undo a sift verdict or a decline.

    The filter's judgement comes from a model, so being able to put an entry
    back is what makes parking it acceptable in the first place. A person's
    decline is undone the same way, so nothing declined is lost for good.
    """
    from .ledger import STATUS_CANDIDATE, STATUS_ONE_OFF

    config = Config(args.root)
    ledger = Ledger(config)
    for entry in ledger.all():
        if entry.id != args.id:
            continue
        if entry.status not in (STATUS_ONE_OFF, STATUS_DISMISSED):
            print(f"{entry.id} is {entry.status}, not parked or declined.")
            return 1
        was = entry.status
        entry.status = STATUS_CANDIDATE
        entry.parked_at_occurrences = 0
        ledger.save(entry)
        # A reopen is a false drop caught in the act, and the most informative
        # label there is: something parked that a person wanted back.
        from . import decisions
        decisions.record(config, entry, decisions.REOPENED,
                         "parked by sift, wanted back" if was == STATUS_ONE_OFF
                         else "declined, reinstated")
        print(f"reopened {entry.id} — {entry.title[:60]}")
        return 0
    print(f"No entry {args.id}.")
    return 1


def cmd_retitle(args: argparse.Namespace) -> int:
    """Name candidates the fold could not name, because no model answered.

    Capture asks the local model for a name when it banks a candidate. When the
    model is down the entry keeps the string capture observed — a prompt, or a
    command — and that is what this walks. A title written by the person who
    did the work (`--source commit`) or already written by the model is left
    alone unless `--all` says otherwise.
    """
    from .ledger import STATUS_CANDIDATE
    from .local import LocalModelUnavailable
    from .summary import name_and_sentence, store_summary

    config = Config(args.root)
    ledger = Ledger(config)
    entries = [e for e in ledger.all() if e.status == STATUS_CANDIDATE]
    if args.id:
        entries = [e for e in entries if e.id in set(args.id)]
    if not args.all:
        entries = [e for e in entries if e.title_source not in ("commit", "model")]
    if not entries:
        print("Nothing to retitle.")
        return 0

    renamed = 0
    for entry in entries:
        before = entry.title
        try:
            name, sentence = name_and_sentence(config, entry)
        except LocalModelUnavailable as exc:
            print(f"no local model: {exc}")
            return 1
        if sentence:
            store_summary(config, entry, sentence)
        if not name:
            print(f"  {entry.id}  (no name returned, kept {before!r})")
            continue
        entry.title, entry.title_source = name, "model"
        ledger.save(entry)
        renamed += 1
        print(f"  {entry.id}  {before!r}\n      → {name!r}")
    print(f"{renamed} of {len(entries)} renamed.")
    return 0


def cmd_sift(args: argparse.Namespace) -> int:
    """Rank the review queue: what looks repeatable first, doubtful last.

    This annotates; it does not gate. A local model was measured dropping 4 of 6
    real procedures when it was allowed to decide, and a wrongly dropped episode
    is never looked at again — so its answer became a sort key instead of a
    verdict. `--park` still exists for anyone who wants the old behaviour, and
    says plainly what it costs.

    The rules that do hold need no model: an episode performed more than once is
    never called one-off, whatever the model thinks of its steps.
    """
    from .episode import RECURRENCE_FLOOR, rank, rank_key
    from .ledger import STATUS_CANDIDATE, STATUS_ONE_OFF

    config = Config(args.root)
    ledger = Ledger(config)
    model = args.model or config.local_model
    entries = [e for e in ledger.all() if e.status == STATUS_CANDIDATE]
    if args.id:
        entries = [e for e in entries if e.id in set(args.id)]
    if not entries:
        print("No candidates to rank.")
        return 0

    judged = []
    for entry in entries:
        hint, why = rank(entry, model=model, host=config.ollama_url)
        entry.hint = hint
        ledger.save(entry)
        judged.append((entry, why))

    judged.sort(key=lambda pair: rank_key(pair[0]))
    label = {"method": "repeatable", "one-off": "probably one-off",
             "": "no opinion"}
    for entry, why in judged:
        print(f"{label[entry.hint]:17} {entry.id}  x{entry.occurrences}  "
              f"{entry.title[:48]}")
        print(f"                  {why}")

    counts = {k: sum(1 for e, _ in judged if e.hint == k) for k in label}
    print(f"\n{counts['method']} repeatable · {counts['one-off']} probably "
          f"one-off · {counts['']} no opinion")
    print("Ranking only — nothing was removed. `skillpp review` now lists "
          "these in this order.")

    if not args.park:
        return 0

    # Opt-in and lossy, so say so rather than reporting a tidy number.
    parkable = [e for e, _ in judged
                if e.hint == "one-off" and e.occurrences < RECURRENCE_FLOOR]
    if not parkable:
        print("\nNothing to park.")
        return 0
    from . import decisions
    for entry in parkable:
        decisions.record(config, entry, decisions.PARKED, "sift --park")
        entry.status = STATUS_ONE_OFF
        entry.parked_at_occurrences = entry.occurrences
        ledger.save(entry)
    print(f"\nparked {len(parkable)} on a local model's opinion. Measured at "
          f"roughly 1 in 3 real procedures lost — read them and reopen with: "
          f"skillpp reopen <id>")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    config = Config(args.root)
    ledger = Ledger(config)
    from .episode import rank_key
    entries = sorted(ledger.candidates(ready_only=not args.all),
                     key=rank_key)
    if args.json:
        print(json.dumps([{
            "id": e.id, "title": e.title, "occurrences": e.occurrences,
            "steps": len(e.steps), "last_seen": e.last_seen,
            "questions": len(questions_for(e, config)),
            "deps_cli": e.deps_cli, "deps_mcp": e.deps_mcp,
        } for e in entries], indent=2))
        return 0
    if not entries:
        scope = "candidates" if args.all else f"candidates at {config.recurrence_threshold}+ occurrences"
        print(f"No {scope} in the ledger.")
        return 0
    print(f"{len(entries)} candidate(s) ready for review:\n")
    for entry in entries:
        n_q = len(questions_for(entry, config))
        flag = f"  {n_q} question(s)" if n_q else "  no open questions"
        print(f"  {entry.id}  ×{entry.occurrences}  {entry.title[:58]}")
        print(f"            {len(entry.steps)} steps ·{flag} · last seen {entry.last_seen[:10]}")
    print(f"\nInspect one:  skillpp show <id>")
    return 0


def cmd_dictate(args: argparse.Namespace) -> int:
    """Create a candidate from a description instead of an observed trace."""
    config = Config(args.root)
    config.ensure_dirs()
    text = args.text if args.text else sys.stdin.read()
    result = fold_dictation(config, text, args.title or "")
    if result["status"] == "empty":
        print("Nothing to work with — describe the workflow in a sentence or two.",
              file=sys.stderr)
        return 1
    entry = Ledger(config).get(result["id"])
    if args.json:
        print(json.dumps({
            "id": entry.id, "status": result["status"], "title": entry.title,
            "steps": [s["input"]["text"] for s in entry.steps],
            "questions": [q.to_dict() for q in questions_for(entry, config)],
            "total_questions": len(detect(entry)),
        }, indent=2))
        return 0
    if result["status"] == "merged":
        print(f"That matches an existing dictated candidate: {entry.id}\n")
    print(render_proposal(entry, config))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    config = Config(args.root)
    entry = Ledger(config).get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    if args.json and args.draft and entry.turns:
        # What a draft is written from: the conversation, not the raw steps or
        # the questions generated from them, which buried the procedure under
        # mechanics and session paths when a real run was drafted both ways.
        from .signals import effects, recurring_steps
        from .summary import _cli_of
        steps = recurring_steps(entry)
        print(json.dumps({
            "id": entry.id, "title": entry.title, "occurrences": entry.occurrences,
            "turns": entry.turns,
            # Filtered like the frontmatter is, not the raw stored union —
            # the agent was being handed `')` as a dependency too.
            "deps_cli": _cli_of(entry, steps), "deps_mcp": entry.deps_mcp,
            # The one fact from the tool calls the agent needs and cannot see:
            # what the run deleted or overwrote. It decides whether each one
            # matters and says so in plain words. Pasting these verbatim into
            # the skill put `rm -f slide-*.jpg` — a procedure clearing its own
            # render output in a scratch folder — under a safety heading, with
            # the session's temp paths attached.
            "destructive": [c.replace("\n", " ⏎ ")[:400]
                            for c in effects(steps)["destructive"]],
        }, indent=2))
        return 0
    if args.json:
        print(json.dumps({
            "id": entry.id, "title": entry.title, "occurrences": entry.occurrences,
            "intents": entry.intents, "steps": entry.steps,
            "deps_cli": entry.deps_cli, "deps_mcp": entry.deps_mcp,
            "questions": [q.to_dict() for q in questions_for(entry, config)],
        }, indent=2))
        return 0
    print(render_proposal(entry, config))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    config = Config(args.root)
    results = Ledger(config).search(" ".join(args.query))
    if not results:
        print("Nothing in the ledger matches that.")
        return 0
    for score, entry in results[: args.limit]:
        print(f"  {entry.id}  ×{entry.occurrences}  [{score:.2f}]  {entry.title[:60]}")
        print(f"            last seen {entry.last_seen[:10]} · {len(entry.steps)} steps")
    return 0


def _available_models(config: Config) -> tuple[list[str], str]:
    """(model names Ollama holds, error). One call, read-only."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"{config.ollama_url}/api/tags", timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        return [], str(exc)
    return [str(m.get("name", "")) for m in data.get("models", [])], ""


def _waiting_sessions(config: Config) -> dict:
    """Session files grouped by what is happening to them."""
    from .capture import (_FOLD_LOCK_SECONDS, _is_pending, _lock_alive,
                          _lock_file, PENDING_IDLE_HOURS)
    import time as _time

    out = {"held": [], "waiting": [], "folding": [], "live": []}
    for path in sorted(config.sessions_dir.glob("*.json")):
        sid = path.stem
        try:
            session = json.loads(path.read_text(encoding="utf-8"))
            idle = (_time.time() - path.stat().st_mtime) / 3600
        except (OSError, json.JSONDecodeError):
            continue
        if _lock_alive(_lock_file(config, sid), _FOLD_LOCK_SECONDS):
            out["folding"].append(sid)
        elif session.get("held"):
            out["held"].append(sid)
        elif _is_pending(session, idle, PENDING_IDLE_HOURS):
            out["waiting"].append(sid)
        else:
            out["live"].append(sid)
    return out


def cmd_doctor(args: argparse.Namespace) -> int:
    """Is skillpp actually running? The question nothing could answer.

    Hooks were wired into one project and nowhere else for weeks, and the only
    symptom was an empty review page — which looks exactly like having done no
    repeated work. Every silent failure this tool has had shows up here: hooks
    not wired, a hook wired but not `SessionEnd`, a model missing, sessions
    captured and never banked.
    """
    from .install import HOOK_EVENTS, installed_events

    config = Config(args.root)
    config.ensure_dirs()

    if args.settings:
        scopes = [("settings", Path(args.settings).expanduser())]
    else:
        scopes = [("project", Path.cwd() / ".claude" / "settings.json"),
                  ("user", Path.home() / ".claude" / "settings.json")]
    for label, path in scopes:
        wired = installed_events(path)
        missing = [e for e in HOOK_EVENTS if e not in wired]
        if not wired:
            flag = f"--{label}" if label in ("user", "project") else f"--settings {path}"
            print(f"hooks     {label:8} not wired — skillpp install {flag} --apply")
        elif missing:
            # Naming the missing ones matters: PostToolUse without SessionEnd
            # captures every step and banks none of it, and reads as working.
            print(f"hooks     {label:8} {', '.join(wired)}")
            print(f"          {'':8} MISSING {', '.join(missing)} — nothing will be banked")
        else:
            print(f"hooks     {label:8} all four wired")

    models, err = _available_models(config)
    if err:
        print(f"models    ollama   unreachable at {config.ollama_url}: {err}")
        print(f"          {'':8} without it nothing is judged, matched or named")
    else:
        print(f"models    ollama   reachable at {config.ollama_url}")
        for want in (config.local_model, config.embed_model):
            here = any(name == want or name.startswith(f"{want}:") for name in models)
            print(f"          {'':8} {want} {'✓' if here else '✗ not pulled'}")

    s = _waiting_sessions(config)
    pending = len(s["held"]) + len(s["waiting"])
    if pending or s["folding"]:
        parts = []
        if pending:
            parts.append(f"{pending} waiting to be banked")
        if s["folding"]:
            parts.append(f"{len(s['folding'])} folding now")
        print(f"sessions  {'':8} {', '.join(parts)}")
        if pending:
            print(f"          {'':8} run `skillpp fold-pending` to bank them now")
    else:
        print(f"sessions  {'':8} nothing waiting"
              + (f", {len(s['live'])} live" if s["live"] else ""))

    stats = Ledger(config).stats()
    print(f"ledger    {'':8} {stats['candidates']} candidate(s) "
          f"({stats['ready']} ready), {stats['promoted']} promoted"
          f"   {config.ledger_dir}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    config = Config(args.root)
    stats = Ledger(config).stats()
    print(f"ledger      {config.ledger_dir}")
    print(f"entries     {stats['total']}")
    print(f"  candidate {stats['candidates']}  (ready: {stats['ready']})")
    print(f"  promoted  {stats['promoted']}")
    print(f"  dismissed {stats['dismissed']}")
    print(f"size        {stats['bytes'] / 1024:.1f} KB")

    # Held sessions. `fold_session` refuses to bank a stream no model judged and
    # keeps the file instead, so a stack of these means skillpp has been running
    # offline and the work is waiting, not lost. Silence here would be the same
    # trap as a harness that scores green with the model down.
    # Only sessions `handle_session_end` stamped as held. A session still being
    # written has a file too, and counting it would report work lost from one
    # that is merely in flight.
    held = []
    for path in sorted(config.sessions_dir.glob("*.json")):
        try:
            if json.loads(path.read_text(encoding="utf-8")).get("held"):
                held.append(path)
        except (OSError, json.JSONDecodeError):
            continue
    if held:
        print(f"held        {len(held)} session(s) not banked, kept in "
              f"{config.sessions_dir}")
        print(f"            no local model answered ({config.local_model} at "
              f"{config.ollama_url}); the work is there, the candidates are not")
    return 0


# --------------------------------------------------------------------------
# promotion
# --------------------------------------------------------------------------

def cmd_scaffold(args: argparse.Namespace) -> int:
    config = Config(args.root)
    entry = Ledger(config).get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    answers = json.loads(args.answers) if args.answers else {}
    text = scaffold_skill(entry, args.name, args.description, answers,
                          args.tier, body=args.body,
                          limit=config.max_questions)
    # Said out loud, because the mode decides whether the caller is expected to
    # write a procedure — and a scaffold that quietly wrote one is how two real
    # drafts shipped as nothing but scaffold.
    if entry.turns and args.body != "full":
        print(f"scaffold: facts only — this candidate has {len(entry.turns)} "
              f"turn(s); write the procedure from them", file=sys.stderr)
    else:
        print("scaffold: full — no conversation was captured for this "
              "candidate, so its steps are the evidence", file=sys.stderr)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text)
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """Mark a candidate promoted. The SKILL.md itself is written by the agent."""
    from . import decisions
    config = Config(args.root)
    ledger = Ledger(config)
    entry = ledger.get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    skill_path = Path(args.skill_path).expanduser() if args.skill_path else None
    if skill_path and not skill_path.exists():
        print(f"Skill file does not exist: {skill_path}", file=sys.stderr)
        return 1
    # Logged before the status changes, so the recorded hint is the one that
    # was on screen when the decision was made.
    decisions.record(config, entry, decisions.PROMOTED)
    entry.status = STATUS_PROMOTED
    entry.skill_path = str(skill_path) if skill_path else ""
    if args.note:
        entry.notes = args.note
    ledger.save(entry)
    print(f"promoted {entry.id}" + (f" → {skill_path}" if skill_path else ""))
    return 0


def cmd_dismiss(args: argparse.Namespace) -> int:
    from . import decisions
    config = Config(args.root)
    ledger = Ledger(config)
    entry = ledger.get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    decisions.record(config, entry, decisions.DISMISSED, args.note or "")
    entry.status = STATUS_DISMISSED
    entry.parked_at_occurrences = entry.occurrences
    if args.note:
        entry.notes = args.note
    ledger.save(entry)
    print(f"dismissed {entry.id}")
    return 0


def cmd_expire(args: argparse.Namespace) -> int:
    config = Config(args.root)
    removed = Ledger(config).expire()
    print(f"expired {len(removed)} unapproved candidate(s) older than "
          f"{config.candidate_ttl_days} days")
    for entry_id in removed:
        print(f"  {entry_id}")
    return 0


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------

def cmd_lifecycle(args: argparse.Namespace) -> int:
    config = Config(args.root)
    skills_dir = Path(args.skills_dir).expanduser() if args.skills_dir else default_skills_dir()
    project_root = Path(args.project_root).expanduser() if args.project_root else Path.cwd()
    skills = scan(skills_dir, config, project_root)
    if not skills:
        print(f"No skills found under {skills_dir}")
        return 0
    print(f"{len(skills)} skill(s) · hot dir {skills_dir}\n")
    for skill in skills:
        marks = []
        if skill.is_stale:
            marks.append(f"STALE({len(skill.stale_refs)})")
        if skill.uses == 0:
            marks.append("never used")
        suffix = ("  " + " ".join(marks)) if marks else ""
        print(f"  [{skill.tier:8}] {skill.name:32} uses={skill.uses}{suffix}")
        if args.verbose and skill.stale_refs:
            for ref in skill.stale_refs:
                print(f"                 ↳ unresolved {ref}")
    return 0


def cmd_tier(args: argparse.Namespace) -> int:
    config = Config(args.root)
    skills_dir = Path(args.skills_dir).expanduser() if args.skills_dir else default_skills_dir()
    matches = [s for s in scan(skills_dir, config) if s.name == args.name]
    if not matches:
        print(f"No skill named '{args.name}'", file=sys.stderr)
        return 1
    dest = move_tier(matches[0], args.tier, skills_dir, config)
    print(f"{args.name}: {matches[0].tier} → {args.tier}  ({dest})")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Dependency check at pull time."""
    config = Config(args.root)
    if args.id:
        entry = Ledger(config).get(args.id)
        if not entry:
            print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
            return 1
        deps_cli, deps_mcp, label = entry.deps_cli, entry.deps_mcp, entry.id
    else:
        skills_dir = Path(args.skills_dir).expanduser() if args.skills_dir else default_skills_dir()
        matches = [s for s in scan(skills_dir, config) if s.name == args.name]
        if not matches:
            print(f"No skill named '{args.name}'", file=sys.stderr)
            return 1
        deps_cli, deps_mcp, label = matches[0].requires_cli, matches[0].requires_mcp, args.name

    result = check_dependencies(deps_cli, deps_mcp, Path.cwd())
    if result["ok"]:
        print(f"{label}: all dependencies present")
        return 0
    print(f"{label}: MISSING DEPENDENCIES")
    for dep in result["missing_cli"]:
        print(f"  cli  {dep}  (not on PATH)")
    for dep in result["missing_mcp"]:
        print(f"  mcp  {dep}  (server not configured)")
    print("\nThis skill will not run here. Install the missing dependencies "
          "rather than working around them.")
    return 2


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------

def cmd_bundle(args: argparse.Namespace) -> int:
    """Package skills as a distributable Claude Code plugin."""
    from .install import build_plugin_bundle

    config = Config(args.root)
    skills_dir = Path(args.skills_dir).expanduser() if args.skills_dir else default_skills_dir()
    found = scan(skills_dir, config)
    if args.name_filter:
        found = [s for s in found if s.name in args.name_filter]
    found = [s for s in found if s.tier == "hot" or args.include_cold]
    if not found:
        print(f"No skills to bundle under {skills_dir}", file=sys.stderr)
        return 1

    out = Path(args.out).expanduser()

    if args.format == "upload":
        from .install import build_upload_bundle, validate_for_upload
        failed = False
        for skill in found:
            problems = validate_for_upload(skill.path)
            if problems:
                failed = True
                print(f"✗ {skill.name}")
                for problem in problems:
                    print(f"    {problem}")
                continue
            archive = build_upload_bundle(skill.path, out)
            print(f"✓ {skill.name}  →  {archive}")
        if failed:
            print("\nFix the problems above and re-run. Upload via "
                  "Customize → Skills.", file=sys.stderr)
            return 1
        print("\nUpload via Customize → Skills. One zip per skill.")
        return 0

    commands = []
    if args.with_commands:
        from .install import COMMANDS, INTERACTIVE_COMMANDS
        commands = [COMMANDS / name for name in INTERACTIVE_COMMANDS]

    result = build_plugin_bundle(
        [s.path for s in found], out, args.plugin_name, args.description,
        args.plugin_version, commands)

    print(f"plugin   {args.plugin_name} v{args.plugin_version}")
    print(f"root     {result['root']}")
    print(f"skills   {', '.join(result['skills'])}")
    if result["commands"]:
        print(f"commands {', '.join(result['commands'])}")

    if args.zip:
        archive = shutil.make_archive(str(out), "zip", root_dir=str(out))
        print(f"archive  {archive}")
    return 0


def _settings_target(args: argparse.Namespace) -> tuple[Path, Path] | None:
    """(settings file, commands dir) for the chosen scope, or None if unchosen.

    There is deliberately no default. The old default was user-level and
    silent, which is how this repo ended up with hooks wired by hand into one
    project while `~/.claude/settings.json` had none for weeks — the command
    never said which file it was about to write.
    """
    if args.settings:
        path = Path(args.settings).expanduser()
        return path, path.parent / "commands"
    if args.user:
        home = Path.home() / ".claude"
        return home / "settings.json", home / "commands"
    if args.project is not None:
        root = Path(args.project).expanduser() if args.project else Path.cwd()
        return root / ".claude" / "settings.json", root / ".claude" / "commands"
    return None


def _usable_interpreter(python: str | None) -> str:
    """Empty if this interpreter can run skillpp, else why it cannot.

    Checked before writing, because a hook whose command cannot start fails
    silently: Claude Code runs it, it exits non-zero, and nothing is captured
    with nothing said. That is the failure this whole change exists to remove,
    so the installer must not reintroduce it.
    """
    import shutil as _shutil
    import subprocess

    name = python or "python3"
    if not python and not _shutil.which("python3"):
        return "no `python3` on PATH — pass --python /path/to/python3"
    try:
        out = subprocess.run(
            [name, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"cannot run {name}: {exc}"
    if out.returncode != 0:
        return f"{name} exited {out.returncode}: {out.stderr.strip()[:120]}"
    try:
        major, minor = (int(part) for part in out.stdout.strip().split("."))
    except ValueError:
        return f"{name} did not report a version: {out.stdout.strip()[:60]}"
    if (major, minor) < (3, 10):
        return f"{name} is {major}.{minor}; skillpp needs 3.10 or newer"
    return ""


def cmd_install(args: argparse.Namespace) -> int:
    from .install import (apply_settings, hook_command, install_command_files,
                          plan_removal, plan_settings, remove_command_files)

    target = _settings_target(args)
    if target is None:
        print("Choose where to install:\n"
              f"  --user              {Path.home() / '.claude' / 'settings.json'}"
              "   (every project)\n"
              f"  --project [DIR]     {Path.cwd() / '.claude' / 'settings.json'}"
              "   (this repo only)\n"
              "  --settings PATH     somewhere else\n\n"
              "Add --apply to write, or leave it off for a dry run.",
              file=sys.stderr)
        return 2
    settings_path, commands_dir = target

    try:
        if args.remove:
            merged, changes = plan_removal(settings_path)
        else:
            merged, changes = plan_settings(settings_path, args.python)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"settings file : {settings_path}")
    if not args.remove:
        print(f"hook command  : {hook_command(args.python)}")
    print("planned changes:")
    for change in changes:
        print(f"  - {change}")

    if not args.apply:
        print("\nDry run. Nothing was written.")
        if not args.remove:
            print("Re-run with --apply to install, or copy the hooks block below "
                  "into your settings manually:\n")
            print(json.dumps({"hooks": merged.get("hooks", {})}, indent=2))
        return 0

    settled = ("no change", "nothing to remove", "skillpp is not wired")
    if all(change.endswith("no change") or change.startswith(settled[1:])
           for change in changes):
        print("\nAlready in that state. Nothing written.")
        return 0

    if not args.remove:
        why = _usable_interpreter(args.python)
        if why:
            print(f"\nrefusing to write: {why}", file=sys.stderr)
            return 1

    backup = apply_settings(settings_path, merged)
    print(f"\nwrote {settings_path}" + (f" (backup: {backup})" if backup else ""))
    try:
        if args.remove:
            for path in remove_command_files(commands_dir):
                print(f"removed {path}")
            return 0
        for path in install_command_files(commands_dir):
            print(f"wrote {path}")
    except OSError as exc:
        print(f"could not update the slash commands: {exc}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skillpp",
        description="Skill Plus Plus — capture workflows passively, promote them deliberately.")
    parser.add_argument("--version", action="version", version=f"skillpp {__version__}")
    parser.add_argument("--root", help="ledger root (default ~/.claude/skillpp)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("hook", help="hook entry point (reads JSON on stdin)")
    p.add_argument("--event", help="override hook_event_name")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_hook)

    p = sub.add_parser("draft",
                       help="have your own agent write a draft SKILL.md for a "
                            "candidate; never installs it")
    p.add_argument("id")
    p.add_argument("--name", help="directory name for the draft")
    p.add_argument("--note",
                   help="what the agent should look out for while drafting; "
                        "handed to it with the candidate")
    p.add_argument("--apply", action="store_true",
                   help="actually invoke the agent; one model call")
    p.add_argument("--cwd", help="run the agent from here")
    p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("revise",
                       help="have your own agent change a draft SKILL.md as "
                            "instructed; never installs it")
    p.add_argument("id")
    p.add_argument("--instruction", required=True, help="what to change")
    p.add_argument("--apply", action="store_true",
                   help="actually invoke the agent; one model call")
    p.add_argument("--cwd", help="run the agent from here")
    p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=cmd_revise)

    p = sub.add_parser("name",
                       help="give a candidate a task-shaped title and a "
                            "description of when it applies")
    p.add_argument("id")
    p.add_argument("--title", help="what the task is, not what was typed")
    p.add_argument("--description",
                   help="one line on when this applies; max 200 chars")
    p.set_defaults(func=cmd_name)

    p = sub.add_parser("split",
                       help="split a candidate that holds two procedures")
    p.add_argument("id")
    p.add_argument("--at", type=int, required=True,
                   help="index of the first step of the second procedure")
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("merge",
                       help="merge candidates that are the same procedure "
                            "worded differently (needs Ollama)")
    p.add_argument("--apply", action="store_true",
                   help="actually fold them; the second entry is absorbed")
    p.add_argument("--floor", type=float,
                   help="cosine at or above which two entries fold "
                        "(default: SKILLPP_MATCH_FLOOR)")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("keep",
                       help="save the work so far as a candidate, without "
                            "ending the session")
    p.add_argument("--session-id", help="which session; defaults to the newest")
    p.set_defaults(func=cmd_keep)

    p = sub.add_parser("fold-session",
                       help="bank one session that has ended; spawned by the "
                            "SessionEnd hook")
    p.add_argument("session_id")
    p.add_argument("--transcript",
                   help="transcript path, if the session file has none")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_fold_session)

    p = sub.add_parser("fold-pending",
                       help="bank sessions that ended without being banked")
    p.add_argument("--exclude", help="a live session to leave alone")
    p.add_argument("--idle-hours", type=float, default=12.0,
                   help="treat a session untouched this long as ended")
    p.set_defaults(func=cmd_fold_pending)

    p = sub.add_parser("web", help="browse the ledger in a local page")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--skills-dir")
    p.add_argument("--no-browser", action="store_true",
                   help="do not open a browser window")
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("ignored",
                       help="list parked candidates and what has recurred since")
    p.add_argument("--threshold", type=int,
                   help="recurrences since parking before it is flagged")
    p.set_defaults(func=cmd_ignored)

    p = sub.add_parser("reconcile",
                       help="report promoted skills whose file is gone")
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("accuracy",
                       help="how often the ranker agreed with your own "
                            "promote/dismiss decisions")
    p.set_defaults(func=cmd_accuracy)

    p = sub.add_parser("reopen", help="put a parked or declined candidate back")
    p.add_argument("id")
    p.set_defaults(func=cmd_reopen)

    p = sub.add_parser("sift",
                       help="rank the review queue by how repeatable each "
                            "candidate looks (needs Ollama)")
    p.add_argument("id", nargs="*", help="only these candidates")
    p.add_argument("--park", action="store_true",
                   help="also set the one-off ones aside. Lossy: a local model "
                        "dropped about a third of real procedures in testing")
    p.add_argument("--model", help="local model to ask; defaults to "
                                   "SKILLPP_LOCAL_MODEL")
    p.set_defaults(func=cmd_sift)

    p = sub.add_parser("retitle",
                       help="name candidates that were banked without a model "
                            "(needs Ollama)")
    p.add_argument("id", nargs="*", help="only these candidates")
    p.add_argument("--all", action="store_true",
                   help="also rename commit subjects and names already written "
                        "by the model")
    p.set_defaults(func=cmd_retitle)

    p = sub.add_parser("review", help="list candidates ready for review")
    p.add_argument("--all", action="store_true", help="include below-threshold candidates")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("dictate", help="describe a workflow instead of performing it")
    p.add_argument("--text", help="the description (reads stdin if omitted)")
    p.add_argument("--title")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dictate)

    p = sub.add_parser("show", help="effect summary, evidence and open questions")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--draft", action="store_true",
                   help="with --json: the input a draft is written from")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("search", help="search the ledger of your own past work")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("doctor",
                       help="is skillpp wired, reachable and keeping up?")
    p.add_argument("--settings", help="check this settings file instead of "
                                      "the project and user ones")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("stats", help="ledger size and status counts")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("scaffold", help="generate a starting SKILL.md for a candidate")
    p.add_argument("id")
    p.add_argument("--name", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--answers", help="JSON object of answered questions")
    p.add_argument("--tier", default="provisional", choices=["provisional", "trusted"])
    p.add_argument("--out", help="write to this path instead of stdout")
    p.add_argument("--body", choices=["auto", "full", "facts"], default="auto",
                   help="whether the scaffold writes a procedure. auto: only "
                        "when the candidate has no captured conversation")
    p.set_defaults(func=cmd_scaffold)

    p = sub.add_parser("promote", help="mark a candidate promoted")
    p.add_argument("id")
    p.add_argument("--skill-path")
    p.add_argument("--note")
    p.set_defaults(func=cmd_promote)

    p = sub.add_parser("dismiss", help="dismiss a candidate")
    p.add_argument("id")
    p.add_argument("--note")
    p.set_defaults(func=cmd_dismiss)

    p = sub.add_parser("expire", help="delete unapproved candidates past their TTL")
    p.set_defaults(func=cmd_expire)

    p = sub.add_parser("lifecycle", help="inventory skills, tiers and staleness")
    p.add_argument("--skills-dir")
    p.add_argument("--project-root")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_lifecycle)

    p = sub.add_parser("tier", help="move a skill between hot/cold/archived")
    p.add_argument("name")
    p.add_argument("tier", choices=["hot", "cold", "archived"])
    p.add_argument("--skills-dir")
    p.set_defaults(func=cmd_tier)

    p = sub.add_parser("check", help="dependency check at pull time")
    p.add_argument("--name", help="skill name")
    p.add_argument("--id", help="ledger candidate id")
    p.add_argument("--skills-dir")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("bundle", help="package skills for distribution")
    p.add_argument("--out", required=True, help="output directory for the bundle")
    p.add_argument("--format", choices=["plugin", "upload"], default="plugin",
                   help="plugin: .claude-plugin/ + skills/ (team, Claude Code). "
                        "upload: one <name>.zip per skill (Customize → Skills)")
    p.add_argument("--plugin-name", default="my-skills")
    p.add_argument("--plugin-version", default="1.0.0")
    p.add_argument("--description", default="Skills captured with Skill Plus Plus.")
    p.add_argument("--name-filter", nargs="*", help="only these skill names")
    p.add_argument("--skills-dir")
    p.add_argument("--include-cold", action="store_true")
    p.add_argument("--with-commands", action="store_true",
                   help="include the skillpp slash commands")
    p.add_argument("--zip", action="store_true", help="also produce a .zip")
    p.set_defaults(func=cmd_bundle)

    p = sub.add_parser("install", help="wire Claude Code hooks (dry run by default)")
    p.add_argument("--apply", action="store_true", help="actually write settings.json")
    p.add_argument("--user", action="store_true",
                   help="wire ~/.claude/settings.json — every project")
    p.add_argument("--project", nargs="?", const="", metavar="DIR",
                   help="wire DIR/.claude/settings.json — this repo only "
                        "(default: the current directory)")
    p.add_argument("--settings", help="wire this settings file instead")
    p.add_argument("--remove", action="store_true",
                   help="take skillpp's hooks back out, leaving any others")
    p.add_argument("--python",
                   help="interpreter for the hook command (default: python3 from PATH)")
    p.set_defaults(func=cmd_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check" and not args.name and not args.id:
        print("check requires --name or --id", file=sys.stderr)
        return 1
    return args.func(args)
