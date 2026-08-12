"""Command line interface.

Division of labour: this CLI does everything deterministic. The
``/skillpp-review`` slash command drives an agent through the parts that need
judgement — reading the proposal, resolving what the repo can answer, asking
the developer at most three questions, and writing the final prose.
"""

from __future__ import annotations

import argparse
import json
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

def cmd_review(args: argparse.Namespace) -> int:
    config = Config(args.root)
    ledger = Ledger(config)
    entries = ledger.candidates(ready_only=not args.all)
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
    entry.status = STATUS_PROMOTED
    entry.skill_path = str(skill_path) if skill_path else ""
    if args.note:
        entry.notes = args.note
    ledger.save(entry)
    print(f"promoted {entry.id}" + (f" → {skill_path}" if skill_path else ""))
    return 0


def cmd_dismiss(args: argparse.Namespace) -> int:
    config = Config(args.root)
    ledger = Ledger(config)
    entry = ledger.get(args.id)
    if not entry:
        print(f"No ledger entry matching '{args.id}'", file=sys.stderr)
        return 1
    entry.status = STATUS_DISMISSED
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
