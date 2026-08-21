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
from pathlib import Path

from . import __version__
from .capture import (fold_dictation, handle_prompt, handle_session_end,
                      handle_tool, log_error)
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
        elif event in ("SessionEnd", "Stop"):
            result = handle_session_end(config, payload)
            if args.verbose:
                print(json.dumps(result))
        elif args.verbose:
            print(json.dumps({"status": "ignored-event", "event": event}))
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all
        log_error(config, f"hook error: {type(exc).__name__}: {exc}")
    return 0


# --------------------------------------------------------------------------
# ledger inspection
# --------------------------------------------------------------------------

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
    prompt = f"/skillpp-draft {entry.id} {out_dir}"
    try:
        template = config.agent_command
        argv = [prompt if part == "{PROMPT}" else part.replace("{PROMPT}", prompt)
                for part in shlex.split(template)]
    except ValueError as exc:
        print(f"SKILLPP_AGENT is not a valid command: {exc}", file=sys.stderr)
        return 1

    import shutil
    found = shutil.which(argv[0])
    print(f"candidate  {entry.id}  x{entry.occurrences}  {entry.title[:60]}")
    print(f"draft dir  {out_dir}")
    print(f"agent      {' '.join(shlex.quote(a) for a in argv)}")
    # Said before the call rather than discovered during it: `claude` is often
    # not on PATH even where Claude Code is in use.
    print(f"resolves   {found or 'NO — not on PATH; set SKILLPP_AGENT'}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to spend one model call.")
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Default to the package root: the allowed-tools pattern names
        # `python3 bin/skillpp`, which only resolves from there.
        where = args.cwd or Path(__file__).resolve().parent.parent
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
                   SKILLPP_DRAFT_DIR=str(out_dir))
        proc = subprocess.run(argv, cwd=where, timeout=args.timeout,
                              capture_output=True, text=True, env=env,
                              # Without this the agent waits on a tty it will
                              # never get, and stalls before starting.
                              stdin=subprocess.DEVNULL)
        print((proc.stdout or "") + (proc.stderr or ""), end="")
    except FileNotFoundError:
        print(f"\nNo such agent: {argv[0]}. Set SKILLPP_AGENT to how yours is "
              f"invoked.", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print(f"\nThe agent did not finish within {args.timeout}s.",
              file=sys.stderr)
        return 1

    renamed = Ledger(config).get(entry.id)
    if renamed and (renamed.title != entry.title or renamed.description):
        print(f"\nnamed      {renamed.title}")
        if renamed.description:
            print(f"           {renamed.description}")

    written = sorted(out_dir.rglob("SKILL.md"))
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
    from .ledger import STATUS_CANDIDATE, STATUS_SPLIT, Entry, make_id
    from .normalize import signature

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
        sig = signature(steps)
        if not sig:
            print("Refusing: one half has no signature to match on.",
                  file=sys.stderr)
            return 1
        part = Entry(
            id=make_id(sig), signature=sig, status=STATUS_CANDIDATE,
            title=entry.title, steps=steps,
            # Provenance is shared: both halves were observed in the same
            # sessions, and occurrences count sessions.
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
    """Merge candidates that are the same procedure worded differently.

    Only pairs already in the near-miss band are considered — lexical
    similarity below the merge threshold but above the floor — so almost every
    comparison stays free and the model is asked where the cheap signal is
    genuinely ambiguous.

    Dry run unless ``--apply``, because folding is not symmetrical: the second
    candidate's evidence moves into the first and the second is gone.
    """
    from .ledger import STATUS_CANDIDATE
    from .similar import fold_into, near_misses, same_procedure

    config = Config(args.root)
    ledger = Ledger(config)
    entries = [e for e in ledger.all() if e.status == STATUS_CANDIDATE]
    pairs = near_misses(entries, floor=args.floor or config.near_miss_floor,
                        ceiling=config.similarity_threshold)
    if not pairs:
        print(f"No near-miss pairs between "
              f"{args.floor or config.near_miss_floor:.2f} and "
              f"{config.similarity_threshold:.2f}. Nothing a model could add.")
        return 0

    merges, seen = [], set()
    for a, b, lex in pairs:
        if a.id in seen or b.id in seen:
            continue          # one merge per entry per run, so folds cannot chain
        verdict, score, why = same_procedure(
            a, b, model=config.embed_model, host=config.ollama_url,
            floor=config.embed_floor)
        mark = {True: "same     ", False: "different", None: "no opinion"}[verdict]
        print(f"{mark} lexical {lex:.3f} · embedding {score:.3f}")
        print(f"          {a.id} {a.title[:44]}")
        print(f"          {b.id} {b.title[:44]}")
        print(f"          {why}")
        if verdict:
            merges.append((a, b))
            seen.update({a.id, b.id})

    if not merges:
        print("\nNothing to merge.")
        return 0
    if not args.apply:
        print(f"\nDry run. Re-run with --apply to fold {len(merges)} pair(s).")
        return 0
    for keep, drop in merges:
        fold_into(keep, drop)
        ledger.save(keep)
        ledger.delete(drop.id)
        print(f"\nfolded {drop.id} into {keep.id} — now seen "
              f"{keep.occurrences}x")
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
    rebuilds the same id and quietly undoes the decision. So their counts keep
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
    """Undo a sift verdict.

    The filter's judgement comes from a model, so being able to put an entry
    back is what makes parking it acceptable in the first place.
    """
    from .ledger import STATUS_CANDIDATE, STATUS_ONE_OFF

    config = Config(args.root)
    ledger = Ledger(config)
    for entry in ledger.all():
        if entry.id != args.id:
            continue
        if entry.status != STATUS_ONE_OFF:
            print(f"{entry.id} is {entry.status}, not parked by sift.")
            return 1
        entry.status = STATUS_CANDIDATE
        ledger.save(entry)
        # A reopen is a false drop caught in the act, and the most informative
        # label there is: a model parked something a person wanted back.
        from . import decisions
        decisions.record(config, entry, decisions.REOPENED,
                         "parked by sift, wanted back")
        print(f"reopened {entry.id} — {entry.title[:60]}")
        return 0
    print(f"No entry {args.id}.")
    return 1


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


def cmd_stats(args: argparse.Namespace) -> int:
    config = Config(args.root)
    stats = Ledger(config).stats()
    print(f"ledger      {config.ledger_dir}")
    print(f"entries     {stats['total']}")
    print(f"  candidate {stats['candidates']}  (ready: {stats['ready']})")
    print(f"  promoted  {stats['promoted']}")
    print(f"  dismissed {stats['dismissed']}")
    print(f"size        {stats['bytes'] / 1024:.1f} KB")
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
    text = scaffold_skill(entry, args.name, args.description, answers, args.tier)
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
        commands_dir = Path(__file__).resolve().parent.parent / "commands"
        commands = sorted(commands_dir.glob("*.md"))

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


def cmd_install(args: argparse.Namespace) -> int:
    from .install import apply_settings, hook_command, install_command_file, plan_settings

    settings_path = Path(args.settings).expanduser() if args.settings else (
        Path.home() / ".claude" / "settings.json")
    try:
        merged, changes = plan_settings(settings_path)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"settings file : {settings_path}")
    print(f"hook command  : {hook_command()}")
    print("planned changes:")
    for change in changes:
        print(f"  - {change}")

    if not args.apply:
        print("\nDry run. Nothing was written.")
        print("Re-run with --apply to install, or copy the hooks block below "
              "into your settings manually:\n")
        print(json.dumps({"hooks": merged.get("hooks", {})}, indent=2))
        return 0

    backup = apply_settings(settings_path, merged)
    print(f"\nwrote {settings_path}" + (f" (backup: {backup})" if backup else ""))
    commands_dir = Path(args.commands_dir).expanduser() if args.commands_dir else (
        Path.cwd() / ".claude" / "commands")
    try:
        dest = install_command_file(commands_dir)
        print(f"wrote {dest}")
    except OSError as exc:
        print(f"could not install slash command: {exc}", file=sys.stderr)
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
    p.add_argument("--apply", action="store_true",
                   help="actually invoke the agent; one model call")
    p.add_argument("--cwd", help="run the agent from here")
    p.add_argument("--timeout", type=int, default=900)
    p.set_defaults(func=cmd_draft)

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
                   help="lowest lexical similarity worth a model call")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("keep",
                       help="save the work so far as a candidate, without "
                            "ending the session")
    p.add_argument("--session-id", help="which session; defaults to the newest")
    p.set_defaults(func=cmd_keep)

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

    p = sub.add_parser("reopen", help="undo a sift verdict; put a candidate back")
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
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("search", help="search the ledger of your own past work")
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("stats", help="ledger size and status counts")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("scaffold", help="generate a starting SKILL.md for a candidate")
    p.add_argument("id")
    p.add_argument("--name", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--answers", help="JSON object of answered questions")
    p.add_argument("--tier", default="provisional", choices=["provisional", "trusted"])
    p.add_argument("--out", help="write to this path instead of stdout")
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
    p.add_argument("--settings")
    p.add_argument("--commands-dir")
    p.set_defaults(func=cmd_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "check" and not args.name and not args.id:
        print("check requires --name or --id", file=sys.stderr)
        return 1
    return args.func(args)
