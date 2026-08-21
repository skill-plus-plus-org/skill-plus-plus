"""Unit tests. Stdlib only: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillpp.capture import (_fold_steps, fold_session, handle_prompt, handle_tool,
                             handle_session_end)
from skillpp.config import Config
from skillpp.ledger import Entry, Ledger, make_id
from skillpp.lifecycle import check_staleness, parse_frontmatter, record_use, scan
from skillpp.normalize import normalize_command, parameterize, signature
from skillpp.recurrence import find_match, similarity
from skillpp.sanitize import scrub
from skillpp.segment import segment
from skillpp.signals import detect, effects
from skillpp.summary import check_dependencies, scaffold_skill

from fixtures.messy_session import (EXPECTED_OCCURRENCES, LEAKED_TOKEN,
                                    clean_session_dict, to_captured_session,
                                    to_session_dict)


def bash(command: str, failed: bool = False) -> dict:
    return {"tool": "Bash", "input": {"command": command}, "failed": failed}


class TempRoot(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = Config(self.root / "skillpp")
        self.config.ensure_dirs()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class TestSanitize(unittest.TestCase):
    def test_redacts_known_token_shapes(self):
        cases = {
            "ghp_" + "a" * 36: "github-token",
            "AKIAIOSFODNN7EXAMPLE": "aws-access-key",
            "sk-ant-" + "x" * 40: "anthropic-key",
            "xoxb-123456789012-abcdefghijkl": "slack-token",
        }
        for secret, label in cases.items():
            out = scrub(f"export TOKEN={secret}")
            self.assertIn("[REDACTED", out, secret)
            self.assertNotIn(secret, out)

    def test_redacts_assignment_values_but_keeps_key(self):
        out = scrub('curl -H "api_key: hunter2supersecret"')
        self.assertIn("api_key", out)
        self.assertNotIn("hunter2supersecret", out)

    def test_redacts_connection_string_and_email(self):
        out = scrub("psql postgres://user:pw@db.example.com/app  # ping ops@corp.com")
        self.assertNotIn("db.example.com/app", out)
        self.assertNotIn("ops@corp.com", out)

    def test_preserves_ordinary_commands(self):
        cmd = "git commit -m 'fix the parser' && npm run test"
        self.assertEqual(scrub(cmd), cmd)

    def test_git_sha_is_not_treated_as_secret(self):
        sha = "a" * 40
        self.assertEqual(scrub(f"git checkout {sha}"), f"git checkout {sha}")

    def test_scrubbing_is_stable_for_signatures(self):
        a = scrub("deploy --token=" + "A1b2" * 12)
        b = scrub("deploy --token=" + "Z9y8" * 12)
        self.assertEqual(a, b, "same shape must scrub identically or dedup breaks")


class TestNormalize(unittest.TestCase):
    def test_normalize_command_keeps_subcommand(self):
        self.assertEqual(normalize_command("git commit -m 'x'"), "git commit")
        self.assertEqual(normalize_command("npm run test -- --watch"), "npm run")
        self.assertEqual(normalize_command("pytest -k auth"), "pytest")
        self.assertEqual(normalize_command("./scripts/deploy.sh prod"), "deploy.sh")
        self.assertEqual(normalize_command("FOO=1 terraform apply"), "terraform apply")

    def test_arguments_do_not_change_signature(self):
        a = signature([bash("pytest -k auth"), bash("git push")])
        b = signature([bash("pytest -k billing"), bash("git push")])
        self.assertEqual(a, b)

    def test_consecutive_duplicates_collapse(self):
        self.assertEqual(
            signature([bash("pytest"), bash("pytest"), bash("git push")]),
            signature([bash("pytest"), bash("git push")]))

    def test_parameterize_paths(self):
        out = parameterize("/proj/app/main.py and /proj/app/x", "/proj/app")
        self.assertNotIn("/proj/app", out)
        self.assertIn("${PROJECT_PATH}", out)

    def test_parameterize_ids(self):
        self.assertIn("${ID}", parameterize("aws s3 ls bucket-1234567", None))


class TestRecurrence(unittest.TestCase):
    def test_identical_signatures_match(self):
        self.assertEqual(similarity("a | b", "a | b"), 1.0)

    def test_unrelated_signatures_do_not_match(self):
        self.assertLess(similarity("bash:git commit | bash:git push",
                                   "bash:docker build | bash:kubectl apply"), 0.5)

    def test_one_extra_step_still_matches(self):
        a = "bash:npm run | bash:git add | bash:git commit | bash:git push"
        b = "bash:npm run | bash:git add | bash:git commit | bash:git push | bash:gh pr"
        self.assertGreater(similarity(a, b), 0.85)

    def test_find_match_respects_threshold(self):
        entries = [Entry(id="x", signature="bash:git commit | bash:git push")]
        self.assertIsNotNone(find_match("bash:git commit | bash:git push", entries, 0.85))
        self.assertIsNone(find_match("bash:terraform apply", entries, 0.85))


class TestLedger(TempRoot):
    def test_roundtrip_preserves_everything(self):
        entry = Entry(id="abc123", signature="bash:pytest", title="run the tests",
                      occurrences=3, steps=[bash("pytest -x")],
                      deps_cli=["pytest"], intents=["run tests before pushing"])
        ledger = Ledger(self.config)
        ledger.save(entry)
        loaded = ledger.get("abc123")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.occurrences, 3)
        self.assertEqual(loaded.steps, entry.steps)
        self.assertEqual(loaded.deps_cli, ["pytest"])

    def test_entry_file_is_human_readable(self):
        ledger = Ledger(self.config)
        ledger.save(Entry(id="abc123", signature="s", title="deploy staging",
                          steps=[bash("./deploy.sh")]))
        text = ledger.path_for("abc123").read_text()
        self.assertIn("# deploy staging", text)
        self.assertIn("`./deploy.sh`", text)

    def test_prefix_lookup(self):
        ledger = Ledger(self.config)
        ledger.save(Entry(id="deadbeef1234", signature="s"))
        self.assertIsNotNone(ledger.get("deadbe"))

    def test_expire_removes_only_stale_unapproved(self):
        ledger = Ledger(self.config)
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        ledger.save(Entry(id="old1", signature="a", last_seen=old, occurrences=1))
        ledger.save(Entry(id="ready", signature="b", last_seen=old, occurrences=5))
        ledger.save(Entry(id="kept", signature="c", last_seen=old, occurrences=1,
                          status="promoted"))
        removed = ledger.expire()
        self.assertEqual(removed, ["old1"])
        self.assertIsNotNone(ledger.get("ready"), "pending proposals survive")
        self.assertIsNotNone(ledger.get("kept"), "approved entries are never deleted")

    def test_search_finds_by_intent(self):
        ledger = Ledger(self.config)
        ledger.save(Entry(id="m1", signature="a", title="migration rollback",
                          intents=["roll back the failed migration"]))
        results = ledger.search("rollback migration")
        self.assertTrue(results)
        self.assertEqual(results[0][1].id, "m1")


class TestSignals(unittest.TestCase):
    def test_failure_then_retry_question(self):
        entry = Entry(id="x", signature="s", steps=[
            bash("terraform apply", failed=True),
            bash("terraform apply -lock=false"),
        ])
        kinds = [q.kind for q in detect(entry)]
        self.assertIn("failure_retry", kinds)

    def test_divergence_question_across_variants(self):
        entry = Entry(id="x", signature="s",
                      steps=[bash("./deploy.sh staging")],
                      variants=[[bash("./deploy.sh staging")],
                                [bash("./deploy.sh prod")]])
        kinds = [q.kind for q in detect(entry)]
        self.assertIn("divergence", kinds)

    def test_off_trace_ending_question(self):
        entry = Entry(id="x", signature="s", steps=[
            bash("npm run build"), bash("./deploy.sh prod")])
        kinds = [q.kind for q in detect(entry)]
        self.assertIn("off_trace_ending", kinds)

    def test_no_question_when_verification_present(self):
        entry = Entry(id="x", signature="s", steps=[
            bash("./deploy.sh prod"), bash("curl -f https://app/health")])
        kinds = [q.kind for q in detect(entry)]
        self.assertNotIn("off_trace_ending", kinds)

    def test_retry_suppresses_duplicate_divergence_question(self):
        """One command, one question: a retry already explains the variance."""
        entry = Entry(id="x", signature="s", steps=[
            bash("terraform apply", failed=True),
            bash("terraform apply -lock=false"),
        ], variants=[[bash("terraform apply")],
                     [bash("terraform apply -lock=false")]])
        kinds = [q.kind for q in detect(entry)]
        self.assertEqual(kinds.count("failure_retry"), 1)
        self.assertNotIn("divergence", kinds)

    def test_shell_builtins_are_not_stale_references(self):
        from skillpp.lifecycle import _referenced
        _, programs = _referenced("1. `export TOKEN=x`\n2. `terraform apply`\n")
        self.assertNotIn("export", programs)
        self.assertIn("terraform", programs)

    def test_clean_candidate_asks_nothing(self):
        entry = Entry(id="x", signature="s", steps=[
            bash("npm run lint"), bash("npm run test")])
        self.assertEqual(detect(entry), [])

    def test_effects_flag_destructive_and_writes(self):
        eff = effects([bash("rm -rf build"), bash("echo hi > out.txt"),
                       {"tool": "Write", "input": {"file_path": "src/a.py"}}])
        self.assertTrue(eff["destructive"])
        self.assertIn("out.txt", eff["writes"])
        self.assertIn("src/a.py", eff["writes"])


class TestCapture(TempRoot):
    def _session(self, commands, prompts=("do the thing",), sid="s1"):
        return {"session_id": sid, "cwd": "/proj", "prompts": list(prompts),
                "steps": [bash(c) for c in commands]}

    def test_thin_session_is_ignored(self):
        result = fold_session(self.config, self._session(["ls"]))
        self.assertEqual(result["status"], "too-thin")

    def test_recurrence_threshold_gates_readiness(self):
        cmds = ["npm run build", "./deploy.sh staging", "curl -f https://app/health"]
        first = fold_session(self.config, self._session(cmds, sid="s1"))
        self.assertEqual(first["status"], "created")
        self.assertFalse(first["ready"])

        second = fold_session(self.config, self._session(cmds, sid="s2"))
        self.assertEqual(second["status"], "merged")
        self.assertFalse(second["ready"])

        third = fold_session(self.config, self._session(cmds, sid="s3"))
        self.assertTrue(third["ready"], "3rd occurrence should surface a proposal")
        self.assertEqual(third["occurrences"], 3)
        self.assertEqual(len(list(Ledger(self.config).all())), 1, "must not duplicate")

    def test_secrets_never_reach_the_ledger(self):
        payload = {
            "session_id": "s9", "cwd": "/proj", "tool_name": "Bash",
            "tool_input": {"command": "curl -H 'Authorization: Bearer "
                                      + "A1b2C3d4" * 6 + "' https://api.example.com"},
            "tool_response": {"exit_code": 0},
        }
        handle_tool(self.config, payload)
        handle_tool(self.config, {**payload, "tool_input": {"command": "git push"}})
        handle_session_end(self.config, {"session_id": "s9"})
        text = "".join(p.read_text() for p in self.config.ledger_dir.glob("*.md"))
        self.assertNotIn("A1b2C3d4A1b2", text)
        self.assertIn("REDACTED", text)

    def test_intent_is_captured_from_prompts(self):
        handle_prompt(self.config, {"session_id": "s2", "cwd": "/p",
                                    "prompt": "roll back the bad migration"})
        for cmd in ("psql -c 'begin'", "./rollback.sh", "psql -c 'commit'"):
            handle_tool(self.config, {"session_id": "s2", "cwd": "/p",
                                      "tool_name": "Bash",
                                      "tool_input": {"command": cmd},
                                      "tool_response": {"exit_code": 0}})
        handle_session_end(self.config, {"session_id": "s2"})
        entries = list(Ledger(self.config).all())
        self.assertEqual(len(entries), 1)
        self.assertIn("roll back the bad migration", entries[0].intents)

    def test_session_buffer_is_deleted_after_fold(self):
        for cmd in ("npm ci", "npm test"):
            handle_tool(self.config, {"session_id": "s3", "tool_name": "Bash",
                                      "tool_input": {"command": cmd},
                                      "tool_response": {}})
        handle_session_end(self.config, {"session_id": "s3"})
        self.assertEqual(list(self.config.sessions_dir.glob("*.json")), [])

    def test_cli_dependencies_are_recorded(self):
        fold_session(self.config, self._session(
            ["gh pr create", "terraform apply", "cd /tmp"]))
        entry = list(Ledger(self.config).all())[0]
        self.assertIn("gh", entry.deps_cli)
        self.assertIn("terraform", entry.deps_cli)
        self.assertNotIn("cd", entry.deps_cli)

    def test_repo_scripts_are_not_path_dependencies(self):
        """./scripts/deploy.sh is a file in the repo, not a missing binary."""
        fold_session(self.config, self._session(
            ["npm run build", "./scripts/deploy.sh prod"]))
        entry = list(Ledger(self.config).all())[0]
        self.assertIn("npm", entry.deps_cli)
        self.assertNotIn("deploy.sh", entry.deps_cli)


class TestDictation(TempRoot):
    """The user's own example: 'I give you information, you search online
    about the facts -> give me in this format'."""

    EXAMPLE = ("I give you information, you search online about the facts "
               "-> give me in this format")

    def dictate(self, text, title=""):
        from skillpp.capture import fold_dictation
        result = fold_dictation(self.config, text, title)
        return Ledger(self.config).get(result["id"]), result

    def test_parses_into_ordered_steps(self):
        entry, _ = self.dictate(self.EXAMPLE)
        steps = [s["input"]["text"] for s in entry.steps]
        self.assertEqual(len(steps), 3)
        self.assertIn("search online", steps[1])

    def test_bypasses_recurrence_threshold(self):
        entry, _ = self.dictate(self.EXAMPLE)
        self.assertEqual(entry.source, "dictated")
        self.assertEqual(entry.occurrences, 1)
        self.assertTrue(entry.ready(self.config.recurrence_threshold),
                        "an explicit request is not noise")
        self.assertIn(entry.id, [e.id for e in Ledger(self.config).candidates(True)])

    def test_detects_format_referred_to_but_never_given(self):
        entry, _ = self.dictate(self.EXAMPLE)
        kinds = [q.kind for q in detect(entry)]
        self.assertIn("dangling_format", kinds)
        self.assertEqual(kinds[0], "dangling_format", "highest-value gap first")

    def test_detects_unconstrained_sources(self):
        entry, _ = self.dictate(self.EXAMPLE)
        self.assertIn("vague_sources", [q.kind for q in detect(entry)])

    def test_question_cap_still_applies(self):
        from skillpp.summary import questions_for
        entry, _ = self.dictate(self.EXAMPLE)
        self.assertGreater(len(detect(entry)), 3)
        self.assertEqual(len(questions_for(entry, self.config)), 3)

    def test_complete_description_asks_nothing(self):
        entry, _ = self.dictate(
            "Whenever I paste a claim, verify it against at least two primary "
            "sources. If sources conflict, say so instead of picking one. Return:\n"
            "- Claim\n- Verdict\n- Sources")
        self.assertEqual(detect(entry), [])

    def test_format_bullets_are_not_mistaken_for_steps(self):
        entry, _ = self.dictate(
            "Whenever I paste a claim, verify it against two primary sources. "
            "If it fails, say so. Return:\n- Claim\n- Verdict")
        steps = [s["input"]["text"] for s in entry.steps]
        self.assertIn("verify it against two primary sources", steps[0])
        self.assertTrue(steps[-1].startswith("Output format:"))

    def test_filled_in_example_is_not_labelled_a_format(self):
        """'it should look like: …' introduces one week's content, not a spec."""
        entry, _ = self.dictate(
            "I send a weekly update to my manager. It should look like:\n"
            "1. Researched AI tools\n2. Created documentation")
        last = entry.steps[-1]["input"]["text"]
        self.assertTrue(last.startswith("Example output:"), last)

    def test_explicit_format_keeps_the_format_label(self):
        entry, _ = self.dictate(
            "Whenever I paste a claim, verify it against two primary sources. "
            "If it fails say so. Return in this format:\n- Claim\n- Verdict")
        last = entry.steps[-1]["input"]["text"]
        self.assertTrue(last.startswith("Output format:"), last)

    def test_bullets_alone_are_the_procedure(self):
        entry, _ = self.dictate("1. run the linter\n2. fix what it finds\n3. open a PR")
        steps = [s["input"]["text"] for s in entry.steps]
        self.assertEqual(steps, ["run the linter", "fix what it finds", "open a PR"])

    def test_repeat_dictation_merges(self):
        first, _ = self.dictate(self.EXAMPLE)
        _, result = self.dictate(self.EXAMPLE)
        self.assertEqual(result["status"], "merged")
        self.assertEqual(len(list(Ledger(self.config).all())), 1)

    def test_secrets_in_dictation_are_scrubbed(self):
        entry, _ = self.dictate("call the API with token ghp_" + "b" * 36 + " then log it")
        self.assertNotIn("ghp_bbbb", json.dumps(entry.steps))

    def test_scaffold_works_for_dictated_entries(self):
        entry, _ = self.dictate(self.EXAMPLE)
        text = scaffold_skill(entry, "fact-check", "Verify claims against sources.",
                              answers={"when_to_use": "When the user pastes a claim."})
        fm = parse_frontmatter(text)
        self.assertEqual(fm["name"], "fact-check")
        self.assertIn("search online", text)
        self.assertIn("## Known gaps", text)
        self.assertIn("# Fact Check", text, "raw dictation is a poor heading")

    def test_answering_a_question_closes_its_gap(self):
        entry, _ = self.dictate(self.EXAMPLE)
        text = scaffold_skill(entry, "fact-check", "Verify claims.", answers={
            "when_to_use": "When the user pastes a claim.",
            "output_format": "Claim / Verdict / Sources",
            "sources": "two independent primary sources",
        })
        gaps = text.split("## Known gaps")[-1] if "## Known gaps" in text else ""
        self.assertNotIn("refer to a format", gaps, "answered gap must not reappear")
        self.assertNotIn("When should this fire", gaps)
        self.assertNotIn("good enough source", gaps)
        self.assertIn("nothing to report", gaps, "unanswered gap stays open")

    def test_answering_everything_removes_the_gaps_section(self):
        entry, _ = self.dictate(self.EXAMPLE)
        text = scaffold_skill(entry, "fact-check", "Verify claims.", answers={
            "when_to_use": "When a claim is pasted.",
            "output_format": "table",
            "sources": "two primary",
            "failure": "say what could not be verified",
        })
        self.assertNotIn("## Known gaps", text)


class TestScaffold(unittest.TestCase):
    def test_scaffold_has_valid_frontmatter_and_deps(self):
        entry = Entry(id="abc", signature="s", title="deploy to staging",
                      occurrences=3, steps=[bash("./deploy.sh staging")],
                      deps_cli=["gh"], deps_mcp=["mcp__github__create_pr"])
        text = scaffold_skill(entry, "deploy-staging", "Deploy to staging.")
        fm = parse_frontmatter(text)
        self.assertEqual(fm["name"], "deploy-staging")
        self.assertEqual(fm["metadata"]["requires_cli"], ["gh"])
        self.assertEqual(fm["metadata"]["tier"], "provisional")
        self.assertIn("provenance", fm["metadata"])

    def test_unanswered_questions_become_known_gaps(self):
        entry = Entry(id="abc", signature="s", title="deploy",
                      steps=[bash("npm run build"), bash("./deploy.sh prod")])
        text = scaffold_skill(entry, "deploy", "Deploy.")
        self.assertIn("## Known gaps", text)

    def test_destructive_steps_are_called_out(self):
        entry = Entry(id="abc", signature="s", title="reset",
                      steps=[bash("rm -rf dist"), bash("npm run build")])
        self.assertIn("## Destructive operations", scaffold_skill(entry, "reset"))


class TestDependencyCheck(unittest.TestCase):
    def test_missing_cli_is_reported(self):
        result = check_dependencies(["definitely-not-a-real-binary-xyz"], [])
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing_cli"], ["definitely-not-a-real-binary-xyz"])

    def test_present_cli_passes(self):
        self.assertTrue(check_dependencies(["python3"], [])["ok"])

    def test_missing_mcp_server_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = check_dependencies([], ["mcp__github__create_pr"], Path(tmp))
        self.assertFalse(result["ok"])
        self.assertIn("mcp__github__create_pr", result["missing_mcp"])

    def test_configured_mcp_server_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".mcp.json").write_text(
                json.dumps({"mcpServers": {"github": {"command": "x"}}}))
            result = check_dependencies([], ["mcp__github__create_pr"], Path(tmp))
        self.assertTrue(result["ok"])


class TestLifecycle(TempRoot):
    def _write_skill(self, directory: Path, name: str, body: str = "") -> Path:
        skill_dir = directory / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = skill_dir / "SKILL.md"
        path.write_text(
            f"---\nname: {name}\ndescription: \"test\"\nmetadata:\n"
            f"  requires_cli: [\"git\"]\n  tier: \"provisional\"\n---\n\n{body}",
            encoding="utf-8")
        return path

    def test_scan_reports_tiers_and_usage(self):
        hot = self.root / "skills"
        self._write_skill(hot, "alpha")
        self._write_skill(self.config.cold_dir, "beta")
        record_use(self.config, "alpha")
        record_use(self.config, "alpha")
        skills = {s.name: s for s in scan(hot, self.config, self.root)}
        self.assertEqual(skills["alpha"].tier, "hot")
        self.assertEqual(skills["alpha"].uses, 2)
        self.assertEqual(skills["beta"].tier, "cold")
        self.assertEqual(skills["beta"].uses, 0)

    def test_demotion_moves_files_and_never_deletes(self):
        hot = self.root / "skills"
        self._write_skill(hot, "alpha")
        skill = [s for s in scan(hot, self.config, self.root) if s.name == "alpha"][0]
        from skillpp.lifecycle import move_tier
        dest = move_tier(skill, "cold", hot, self.config)
        self.assertTrue((dest / "SKILL.md").exists())
        self.assertFalse((hot / "alpha").exists())
        self.assertEqual(
            [s.tier for s in scan(hot, self.config, self.root) if s.name == "alpha"],
            ["cold"])

    def test_staleness_detects_missing_reference(self):
        hot = self.root / "skills"
        path = self._write_skill(hot, "gamma", "Run `./scripts/gone.sh` to deploy.\n")
        self.assertTrue(any("gone.sh" in ref for ref in check_staleness(path, self.root)))

    def test_staleness_ignores_existing_reference(self):
        hot = self.root / "skills"
        (self.root / "scripts").mkdir(parents=True, exist_ok=True)
        (self.root / "scripts" / "here.sh").write_text("#!/bin/sh\n")
        path = self._write_skill(hot, "delta", "Run `./scripts/here.sh` to deploy.\n")
        self.assertEqual(
            [r for r in check_staleness(path, self.root) if "here.sh" in r], [])


class TestInstall(unittest.TestCase):
    def test_hook_command_does_not_redirect_the_ledger_into_the_repo(self):
        from skillpp.install import hook_command
        cmd = hook_command(python="/usr/bin/python3", package_root=Path("/repo"))
        self.assertIn('PYTHONPATH="/repo"', cmd)
        self.assertNotIn("--root", cmd)

    def test_plan_preserves_existing_hooks(self):
        from skillpp.install import plan_settings
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps({
                "model": "opus",
                "hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": "existing.sh"}]}]}}))
            merged, changes = plan_settings(path)
            on_disk = json.loads(path.read_text())
        self.assertEqual(merged["model"], "opus", "unrelated settings survive")
        self.assertIn("existing.sh", json.dumps(merged["hooks"]["PostToolUse"]))
        self.assertEqual(len(merged["hooks"]["PostToolUse"]), 2)
        self.assertEqual(len(on_disk["hooks"]["PostToolUse"]), 1, "planning must not write")

    def test_bundle_matches_the_plugin_layout_desktop_uses(self):
        from skillpp.install import build_plugin_bundle
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "skills" / "alpha"
            src.mkdir(parents=True)
            (src / "SKILL.md").write_text("---\nname: alpha\n---\nbody\n")
            out = Path(tmp) / "bundle"
            result = build_plugin_bundle([src / "SKILL.md"], out, "my-skills",
                                         "test bundle")
            manifest = json.loads((out / ".claude-plugin" / "plugin.json").read_text())
            self.assertEqual(manifest["name"], "my-skills")
            self.assertEqual(manifest["version"], "1.0.0")
            self.assertTrue((out / "skills" / "alpha" / "SKILL.md").exists())
            self.assertEqual(result["skills"], ["alpha"])

    def _skill(self, tmp, name, description):
        d = Path(tmp) / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f'---\nname: {name}\ndescription: "{description}"\n---\nbody\n')
        return d / "SKILL.md"

    def test_upload_zip_has_the_skill_folder_as_root(self):
        import zipfile
        from skillpp.install import build_upload_bundle
        with tempfile.TemporaryDirectory() as tmp:
            md = self._skill(tmp, "alpha", "does a thing")
            archive = build_upload_bundle(md, Path(tmp) / "out")
            names = zipfile.ZipFile(archive).namelist()
        self.assertIn("alpha/SKILL.md", names)
        self.assertNotIn("SKILL.md", names, "files must not sit at archive root")

    def test_upload_validation_catches_overlong_description(self):
        from skillpp.install import validate_for_upload, UPLOAD_DESCRIPTION_MAX
        with tempfile.TemporaryDirectory() as tmp:
            md = self._skill(tmp, "alpha", "x" * (UPLOAD_DESCRIPTION_MAX + 20))
            problems = validate_for_upload(md)
        self.assertTrue(problems)
        self.assertIn("trim by 20", problems[0])

    def test_upload_validation_passes_a_good_skill(self):
        from skillpp.install import validate_for_upload
        with tempfile.TemporaryDirectory() as tmp:
            md = self._skill(tmp, "alpha", "Use when the user asks for a thing.")
            self.assertEqual(validate_for_upload(md), [])

    def test_plan_is_idempotent(self):
        from skillpp.install import plan_settings
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            merged, _ = plan_settings(path)
            path.write_text(json.dumps(merged))
            _, changes = plan_settings(path)
        self.assertTrue(all("no change" in c for c in changes), changes)


class TestHookRobustness(TempRoot):
    def test_hook_never_raises_on_garbage(self):
        from skillpp.cli import main
        import io
        bad_payloads = ['not json', '{}', '{"tool_name": 123}', '[]', '']
        for payload in bad_payloads:
            stdin = sys.stdin
            sys.stdin = io.StringIO(payload)
            try:
                code = main(["--root", str(self.config.root), "hook",
                             "--event", "PostToolUse"])
            finally:
                sys.stdin = stdin
            self.assertEqual(code, 0, f"hook must exit 0 for payload {payload!r}")


class TestSegmentBoundaries(unittest.TestCase):
    """Where a task ends. Unit-level, no ledger involved."""

    def _prompt(self, text="do a thing"):
        return {"tool": "UserPrompt", "input": {"text": text}, "failed": False}

    def test_a_commit_ends_a_task(self):
        episodes = segment([bash("npm test"), bash("git commit -m 'x'"),
                            bash("npm outdated"), bash("npm view pkg")])
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0].ended_by, "marker")

    def test_inspection_commands_do_not_end_a_task(self):
        """git status / diff / add look like completions and are not."""
        for decoy in ("git status", "git diff", "git add -A", "git stash"):
            episodes = segment([bash("npm test"), bash(decoy), bash("npm run build")])
            self.assertEqual(len(episodes), 1, f"{decoy} must not cut")

    def test_a_rejected_commit_does_not_end_a_task(self):
        """A pre-commit hook rejection means the task is still in progress."""
        episodes = segment([
            bash("edit something"), bash("git add -A"),
            bash("git commit -m 'x'", failed=True),   # rejected
            bash("npm run lint -- --fix"),
            bash("git commit -m 'x'"),                # the real end
        ])
        self.assertEqual(len(episodes), 1)
        last = episodes[0].steps[-1]
        self.assertFalse(last.get("failed"))

    def test_a_boundary_is_ignored_below_the_minimum_size(self):
        """One step is not a workflow, so a lone marker must not strand it.

        Guards test_cli_dependencies_are_recorded: `gh pr create` first in the
        list would otherwise become a one-step episode and lose its dep.
        """
        episodes = segment([bash("gh pr create"), bash("terraform apply"),
                            bash("cd /tmp")])
        self.assertEqual(len(episodes), 1)

    def test_a_new_prompt_ends_the_preceding_work(self):
        episodes = segment([self._prompt("task one"), bash("npm test"),
                            bash("./deploy.sh staging"),
                            self._prompt("task two"), bash("npm outdated"),
                            bash("npm view pkg")])
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0].ended_by, "prompt")

    def test_a_mid_task_prompt_does_not_cut(self):
        """"continue" arriving before any work must not shred the episode."""
        episodes = segment([self._prompt("start"), bash("npm test"),
                            self._prompt("continue"), bash("npm run build")])
        self.assertEqual(len(episodes), 1)

    def test_trailing_work_with_nothing_to_show_is_flagged(self):
        episodes = segment([bash("npm test"), bash("git commit -m 'x'"),
                            bash("kubectl logs api"), bash("kubectl top pods")])
        self.assertEqual(len(episodes), 2)
        self.assertFalse(episodes[0].flagged, "ended in a commit")
        self.assertTrue(episodes[1].flagged, "no marker, ended only at session end")

    def test_a_single_episode_session_that_did_something_is_not_flagged(self):
        """A session that did one thing needs no artifact to be believable.

        The rule this protects: a marker is not required. Deployments end in
        `./scripts/deploy.sh`, productivity work ends in an MCP call, and
        neither is a regex the segmenter knows.
        """
        episodes = segment([bash("kubectl logs api"),
                            bash("kubectl scale deploy/api --replicas=3")])
        self.assertEqual(len(episodes), 1)
        self.assertFalse(episodes[0].flagged)

    def test_a_session_that_only_looked_around_is_flagged(self):
        """Deliberately the opposite of what this file asserted before.

        The old rule exempted every single-episode session from flagging, so a
        whole session of reading banked one candidate titled after the question
        that started it. Two benchmark cases exist for exactly that shape. A
        session that only looked at things did not "do one thing" — it looked
        around, and there is no method in it however it ended.
        """
        episodes = segment([bash("kubectl logs api"), bash("kubectl top pods")])
        self.assertEqual(len(episodes), 1)
        self.assertTrue(episodes[0].flagged)


class TestSegmentBeforeAfter(TempRoot):
    """The regression this exists to prevent, measured both ways.

    ``_fold_steps`` on a whole session is the original behaviour — still a live
    code path, since single-episode sessions take it — so "before" and "after"
    are both runnable here rather than one of them being history.
    """

    SESSIONS = ("s1", "s2", "s3")
    CLEAN_SIGNATURE = ("bash:npm run | bash:export | bash:terraform apply "
                       "| bash:deploy.sh")

    def _fold_whole(self, maker):
        """BEFORE: fingerprint each session as a single unit."""
        for name in self.SESSIONS:
            session = maker(name)
            _fold_steps(self.config, session, session["steps"])
        return list(Ledger(self.config).all())

    def _fold_segmented(self, maker):
        """AFTER: cut into episodes first."""
        for name in self.SESSIONS:
            fold_session(self.config, maker(name))
        return list(Ledger(self.config).all())

    def test_before_the_repeated_workflow_is_invisible(self):
        entries = self._fold_whole(to_session_dict)
        self.assertEqual(len(entries), 3, "one orphan entry per session")
        for entry in entries:
            self.assertEqual(entry.occurrences, 1)
            self.assertFalse(entry.ready(self.config.recurrence_threshold))

    def test_before_the_sessions_score_far_below_the_threshold(self):
        entries = self._fold_whole(to_session_dict)
        scores = [similarity(a.signature, b.signature)
                  for i, a in enumerate(entries) for b in entries[i + 1:]]
        self.assertTrue(scores)
        for score in scores:
            self.assertLess(score, self.config.similarity_threshold)
        # Not a near miss: loosening the threshold this far would merge
        # genuinely unrelated work.
        self.assertLess(max(scores), 0.6)

    def test_before_every_title_names_the_pollution(self):
        entries = self._fold_whole(to_session_dict)
        self.assertFalse(any("deploy" in e.title.lower() for e in entries),
                         "three deploy sessions, none titled after the deploy")

    def test_after_the_repeated_workflow_reaches_the_threshold(self):
        self._fold_segmented(to_captured_session)
        deploys = [e for e in Ledger(self.config).all()
                   if e.signature == self.CLEAN_SIGNATURE]
        self.assertEqual(len(deploys), 1, "the deploy must be one entry, not three")
        self.assertEqual(deploys[0].occurrences, EXPECTED_OCCURRENCES)
        self.assertTrue(deploys[0].ready(self.config.recurrence_threshold))

    def test_after_the_signature_matches_the_unpolluted_baseline(self):
        """Pollution must leave no trace in the recovered workflow."""
        self._fold_segmented(to_captured_session)
        signatures = {e.signature for e in Ledger(self.config).all()}
        self.assertIn(self.CLEAN_SIGNATURE, signatures)

    def test_after_the_deploy_is_titled_after_the_deploy(self):
        self._fold_segmented(to_captured_session)
        deploy = next(e for e in Ledger(self.config).all()
                      if e.signature == self.CLEAN_SIGNATURE)
        self.assertIn("deploy", deploy.title.lower())

    def test_the_clean_baseline_is_unaffected(self):
        """demo.sh's behaviour must not change: 3 occurrences, one entry."""
        entries = self._fold_segmented(clean_session_dict)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].occurrences, 3)
        self.assertEqual(entries[0].signature, self.CLEAN_SIGNATURE)

    def test_secrets_survive_segmentation(self):
        """Scrubbing happens upstream, but the step lists are now sliced."""
        for name in self.SESSIONS:
            session = to_captured_session(name)
            for step in session["steps"]:
                command = (step.get("input") or {}).get("command", "")
                if command:
                    step["input"]["command"] = scrub(command)
            fold_session(self.config, session)
        text = "".join(p.read_text() for p in self.config.ledger_dir.glob("*.md"))
        self.assertNotIn(LEAKED_TOKEN, text)
        self.assertIn("REDACTED", text)


class TestFixtureIntegrity(unittest.TestCase):
    """Keeps the fixture from rotting into an easy case."""

    def test_the_deploy_workflow_is_identical_across_sessions(self):
        """The controlled variable must stay controlled."""
        shapes = set()
        for name in ("s1", "s2", "s3"):
            steps = [s for s in to_session_dict(name)["steps"]
                     if str((s.get("input") or {}).get("command", ""))
                     .startswith(("npm run build", "export DEPLOY", "terraform",
                                  "./scripts/deploy.sh"))]
            shapes.add(signature(steps))
        self.assertEqual(len(shapes), 1, "the deploy must recur unchanged")

    def test_the_decoys_are_still_present(self):
        commands = {str((s.get("input") or {}).get("command", ""))
                    for name in ("s1", "s2", "s3")
                    for s in to_session_dict(name)["steps"]}
        self.assertTrue(any(c.startswith("git status") for c in commands))
        self.assertTrue(any(c.startswith("git add") for c in commands))
        self.assertTrue(any(c.startswith("git diff") for c in commands))

    def test_a_rejected_commit_is_still_present(self):
        rejected = [s for s in to_session_dict("s3")["steps"]
                    if s.get("failed")
                    and "git commit" in str((s.get("input") or {}).get("command", ""))]
        self.assertTrue(rejected, "s3's rejected commit is what proves a failed "
                                  "marker does not end a task")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestEpisodeFilter(TempRoot):
    """The one step that can discard, so every case here is about restraint.

    No model is contacted: ``ask`` is replaced per test. What is being pinned is
    the wiring and the fail-safe direction, not the model's judgement — that is
    measured separately and cannot be asserted in a unit test.
    """

    def _entry(self, **kw) -> Entry:
        base = dict(id="e1", signature="sig", title="cut the release",
                    intents=["cut the 2.4 release"],
                    steps=[{"tool": "Bash", "input": {"command": "npm test"}},
                           {"tool": "Bash", "input": {"command": "git tag v2.4"}}])
        base.update(kw)
        return Entry(**base)

    def _answer(self, reply):
        """Point episode.ask at a canned reply, or an exception to raise."""
        import skillpp.episode as ep

        def fake(model, prompt, *, host=None, timeout=None):
            if isinstance(reply, Exception):
                raise reply
            return reply
        self._real, ep.ask = ep.ask, fake
        self.addCleanup(lambda: setattr(ep, "ask", self._real))

    def test_yes_keeps_and_no_drops(self):
        from skillpp.episode import is_reusable
        self._answer("yes")
        self.assertIs(is_reusable(self._entry())[0], True)

    def test_no_is_a_drop(self):
        from skillpp.episode import is_reusable
        self._answer("no")
        self.assertIs(is_reusable(self._entry())[0], False)

    def test_a_verbose_answer_still_parses(self):
        """One-word instructions are advisory; a model that explains still counts."""
        from skillpp.episode import is_reusable
        self._answer("**No** — this was one particular bug.")
        self.assertIs(is_reusable(self._entry())[0], False)

    def test_unreachable_model_keeps_the_episode(self):
        """The expensive error is dropping real work, so absence means keep."""
        from skillpp.episode import is_reusable
        from skillpp.local import LocalModelUnavailable
        self._answer(LocalModelUnavailable("connection refused"))
        verdict, why = is_reusable(self._entry())
        self.assertIsNone(verdict)
        self.assertIn("keeping", why)

    def test_an_unclear_answer_keeps_the_episode(self):
        from skillpp.episode import is_reusable
        self._answer("it depends on what you mean by reusable")
        verdict, why = is_reusable(self._entry())
        self.assertIsNone(verdict)
        self.assertIn("keeping", why)

    def test_render_keeps_the_whole_command(self):
        """Crisp, not lossy: fingerprinting hides that tests ran at all."""
        from skillpp.episode import render_step
        line = render_step({"tool": "Bash",
                            "input": {"command": "python3 -m unittest discover"}})
        self.assertIn("unittest discover", line)

    def test_render_marks_a_failed_step(self):
        from skillpp.episode import render_step
        self.assertTrue(render_step(
            {"tool": "Bash", "input": {"command": "npm test"},
             "failed": True}).startswith("!"))

    def test_render_truncates_a_giant_step(self):
        from skillpp.episode import render_step
        line = render_step({"tool": "Bash", "input": {"command": "x" * 5000}})
        self.assertLess(len(line), 260)

    def test_a_parked_entry_is_not_ready_and_not_a_candidate(self):
        """Why a new status rather than a flag: `ready` already gates on it."""
        from skillpp.ledger import STATUS_ONE_OFF
        entry = self._entry(occurrences=9, status=STATUS_ONE_OFF)
        ledger = Ledger(self.config)
        ledger.save(entry)
        self.assertFalse(entry.ready(3))
        self.assertEqual(list(ledger.candidates(ready_only=False)), [])

    def test_parking_survives_a_round_trip(self):
        from skillpp.ledger import STATUS_ONE_OFF
        ledger = Ledger(self.config)
        ledger.save(self._entry(status=STATUS_ONE_OFF))
        self.assertEqual([e.status for e in ledger.all()], [STATUS_ONE_OFF])


class TestSiftRanking(TempRoot):
    """Ranking, not gating — and the rules that outrank the model."""

    def setUp(self) -> None:
        super().setUp()
        import argparse
        import skillpp.episode as ep
        self.argparse = argparse
        ep.ask, self._real = (lambda *a, **k: "no"), ep.ask
        self.addCleanup(lambda: setattr(ep, "ask", self._real))
        self.ledger = Ledger(self.config)

    def _save(self, **kw):
        base = dict(id="one", signature="s1",
                    title="find out why the export 500s",
                    intents=["find out why the export 500s"],
                    steps=[{"tool": "Bash", "input": {"command": "grep -rn x ."}},
                           {"tool": "Bash", "input": {"command": "git commit -am f"}}])
        base.update(kw)
        entry = Entry(**base)
        self.ledger.save(entry)
        return entry

    def _args(self, **kw):
        base = dict(root=self.config.root, id=[], park=False, model=None)
        base.update(kw)
        return self.argparse.Namespace(**base)

    def _get(self, eid="one"):
        return next(e for e in Ledger(self.config).all() if e.id == eid)

    def test_ranking_annotates_without_gating(self):
        from skillpp.cli import cmd_sift
        from skillpp.ledger import STATUS_CANDIDATE
        self._save()
        cmd_sift(self._args())
        entry = self._get()
        self.assertEqual(entry.hint, "one-off")
        self.assertEqual(entry.status, STATUS_CANDIDATE)

    def test_recurrence_outranks_the_model(self):
        """Twice is behavioural evidence; the model said no and does not win."""
        from skillpp.cli import cmd_sift
        self._save(occurrences=2)
        cmd_sift(self._args())
        self.assertEqual(self._get().hint, "method")

    def test_a_recurring_entry_is_never_parked(self):
        from skillpp.cli import cmd_sift
        from skillpp.ledger import STATUS_CANDIDATE
        self._save(occurrences=3)
        cmd_sift(self._args(park=True))
        self.assertEqual(self._get().status, STATUS_CANDIDATE)

    def test_parking_is_opt_in(self):
        from skillpp.cli import cmd_sift
        from skillpp.ledger import STATUS_ONE_OFF
        self._save()
        cmd_sift(self._args(park=True))
        self.assertEqual(self._get().status, STATUS_ONE_OFF)

    def test_an_unreachable_model_leaves_no_hint(self):
        import skillpp.episode as ep
        from skillpp.cli import cmd_sift
        from skillpp.local import LocalModelUnavailable

        def boom(*a, **k):
            raise LocalModelUnavailable("refused")
        ep.ask = boom
        self._save()
        cmd_sift(self._args(park=True))
        entry = self._get()
        self.assertEqual(entry.hint, "")
        self.assertEqual(entry.status, "candidate")

    def test_review_lists_repeatable_first(self):
        from skillpp.episode import rank_key
        entries = [Entry(id="c", signature="s", hint="one-off"),
                   Entry(id="a", signature="s", hint="method"),
                   Entry(id="b", signature="s", hint="")]
        self.assertEqual([e.id for e in sorted(entries, key=rank_key)],
                         ["a", "b", "c"])

    def test_a_hint_survives_a_round_trip(self):
        self._save(hint="method")
        self.assertEqual(self._get().hint, "method")

    def test_reopen_still_undoes_a_parking(self):
        from skillpp.cli import cmd_reopen, cmd_sift
        from skillpp.ledger import STATUS_CANDIDATE
        self._save()
        cmd_sift(self._args(park=True))
        cmd_reopen(self.argparse.Namespace(root=self.config.root, id="one"))
        self.assertEqual(self._get().status, STATUS_CANDIDATE)


class TestDraftCommand(TempRoot):
    """Claude writes the body; nothing installs it.

    No agent is launched: `subprocess.run` is replaced. What is pinned is the
    command that would be built and the promise that promotion stays human.
    """

    def setUp(self) -> None:
        super().setUp()
        import argparse
        self.argparse = argparse
        self.ledger = Ledger(self.config)
        self.ledger.save(Entry(
            id="cand1", signature="s", title="Weekly manager update email",
            intents=["draft the weekly update"],
            steps=[{"tool": "Bash", "input": {"command": "git log --since=7.days"}},
                   {"tool": "Write", "input": {"file_path": "/tmp/update.md"}}]))

    def _args(self, **kw):
        base = dict(root=self.config.root, id="cand1", name=None, apply=False,
                    cwd=None, timeout=900)
        base.update(kw)
        return self.argparse.Namespace(**base)

    def _spy(self, exit_code=0, writes=None, say=""):
        """Capture argv instead of running an agent."""
        import subprocess
        seen = {}

        def fake(argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw
            for rel in (writes or []):
                path = self.config.root / "drafts" / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("---\nname: x\n---\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, exit_code,
                                               stdout=say, stderr="")
        real = subprocess.run
        subprocess.run = fake
        self.addCleanup(lambda: setattr(subprocess, "run", real))
        return seen

    def test_a_dry_run_launches_nothing(self):
        from skillpp.cli import cmd_draft
        seen = self._spy()
        self.assertEqual(cmd_draft(self._args()), 0)
        self.assertNotIn("argv", seen)

    def test_the_prompt_is_one_argument_with_the_id(self):
        """A space inside --allowed-tools once split it into two broken args."""
        from skillpp.cli import cmd_draft
        seen = self._spy()
        cmd_draft(self._args(apply=True))
        argv = seen["argv"]
        # One argv element carrying id and destination, both literal.
        prompt = next(a for a in argv if a.startswith("/skillpp-draft"))
        self.assertIn("cand1", prompt)
        # `skillpp` is not on PATH; the CLI is invoked as `python3 bin/skillpp`,
        # so a pattern naming the bare binary would allow nothing.
        self.assertIn("Bash(python3 bin/skillpp *),Read,Write,Edit", argv)

    def test_the_agent_runs_where_the_cli_resolves(self):
        """The allowed-tools pattern is relative, so the cwd is load-bearing."""
        from skillpp.cli import cmd_draft
        seen = self._spy()
        cmd_draft(self._args(apply=True))
        self.assertTrue((Path(seen["kw"]["cwd"]) / "bin" / "skillpp").exists())

    def test_the_agent_is_configurable(self):
        """A command template, so no vendor and no API key are baked in."""
        import os
        from skillpp.cli import cmd_draft
        seen = self._spy()
        os.environ["SKILLPP_AGENT"] = "my-agent --go {PROMPT}"
        self.addCleanup(lambda: os.environ.pop("SKILLPP_AGENT", None))
        cmd_draft(self._args(apply=True))
        self.assertEqual(seen["argv"][:2], ["my-agent", "--go"])

    def test_the_agent_is_told_where_to_write(self):
        """The prompt once said `<draft-dir>` with nothing substituting it, so a
        good draft was written to the agent's own scratchpad and reported as no
        draft at all."""
        from skillpp.cli import cmd_draft
        seen = self._spy()
        cmd_draft(self._args(apply=True))
        # A literal in the prompt, not an environment variable: a sandboxed
        # Bash call containing `$VAR` is rejected as "Contains expansion".
        prompt = next(a for a in seen["argv"] if a.startswith("/skillpp-draft"))
        self.assertTrue(prompt.endswith("drafts/cand1"))
        self.assertNotIn("$", prompt)
        # SKILLPP_ROOT stays in the environment; Python reads it directly.
        self.assertEqual(seen["kw"]["env"]["SKILLPP_ROOT"],
                         str(self.config.root))

    def test_drafting_never_promotes(self):
        from skillpp.cli import cmd_draft
        from skillpp.ledger import STATUS_CANDIDATE
        self._spy(writes=["cand1/SKILL.md"])
        cmd_draft(self._args(apply=True))
        entry = Ledger(self.config).get("cand1")
        self.assertEqual(entry.status, STATUS_CANDIDATE)
        self.assertEqual(entry.skill_path, "")

    def test_a_draft_lands_outside_the_skills_directory(self):
        from skillpp.cli import cmd_draft
        self._spy(writes=["cand1/SKILL.md"])
        cmd_draft(self._args(apply=True))
        drafted = list((self.config.root / "drafts").rglob("SKILL.md"))
        self.assertEqual(len(drafted), 1)
        self.assertNotIn("skills", drafted[0].parts[:-2])

    def test_a_stated_decline_is_not_an_error(self):
        """The prompt tells the agent to say so when there is no procedure."""
        from skillpp.cli import cmd_draft
        self._spy(writes=[], exit_code=0,
                  say="SKILLPP-DECLINE: one particular bug, not a method\n")
        self.assertEqual(cmd_draft(self._args(apply=True)), 0)

    def test_silence_is_not_a_decline(self):
        """The defect the first live run exposed.

        A blocked tool, a denied permission and a considered "nothing here" all
        write no file and can all exit 0. Inferring a judgement from the absence
        of a file made a blocked agent look like a working filter, so a decline
        now has to be stated.
        """
        from skillpp.cli import cmd_draft
        self._spy(writes=[], exit_code=0,
                  say="I could not read the candidate, so I am stopping.\n")
        self.assertEqual(cmd_draft(self._args(apply=True)), 1)

    def test_a_failed_agent_is_not_read_as_a_judgement(self):
        """An unauthenticated CLI exits 1 and writes nothing — same shape as a
        decline, and the opposite meaning. Reporting it as "nothing here" is how
        an unattended run hides a dead agent."""
        from skillpp.cli import cmd_draft
        self._spy(writes=[], exit_code=1)
        self.assertEqual(cmd_draft(self._args(apply=True)), 1)

    def test_a_missing_agent_reports_how_to_fix_it(self):
        import subprocess
        from skillpp.cli import cmd_draft

        def boom(*a, **k):
            raise FileNotFoundError("claude")
        real, subprocess.run = subprocess.run, boom
        self.addCleanup(lambda: setattr(subprocess, "run", real))
        self.assertEqual(cmd_draft(self._args(apply=True)), 1)


class TestLeadingExplorationTrim(unittest.TestCase):
    """Ported from the capture branch — the one thing that design got right.

    It earns its place twice: shorter episodes, and episodes a reader judges
    differently. `secrets` was dropped as "one particular job" with six greps in
    front of it and kept as a method without them.
    """

    def bash(self, command, **kw):
        return {"tool": "Bash", "input": {"command": command}, **kw}

    def prompt(self, text):
        return {"tool": "UserPrompt", "input": {"prompt": text}}

    def test_drops_the_greps_that_found_the_bug(self):
        from skillpp.segment import trim_leading_exploration
        steps = [self.bash("grep -rn timeout src/"), self.bash("cat src/api.py"),
                 self.bash("sed -i s/5/30/ src/api.py"),
                 self.bash("git commit -am fix")]
        kept, cut = trim_leading_exploration(steps)
        self.assertEqual(cut, 2)
        self.assertEqual(len(kept), 2)

    def test_keeps_reads_that_are_real_steps(self):
        """"check the logs, then restart" is a two-step recipe, not exploration."""
        from skillpp.segment import trim_leading_exploration
        steps = [self.bash("systemctl restart api"),
                 self.bash("journalctl -u api -n 50")]
        kept, cut = trim_leading_exploration(steps)
        self.assertEqual(cut, 0)
        self.assertEqual(len(kept), 2)

    def test_never_trims_below_two_substantive_steps(self):
        """Otherwise an exploration-only episode becomes a recipe of its tail."""
        from skillpp.segment import trim_leading_exploration
        steps = [self.bash("grep -rn x ."), self.bash("grep -rn y ."),
                 self.bash("./deploy.sh")]
        kept, cut = trim_leading_exploration(steps)
        self.assertEqual(cut, 0)
        self.assertEqual(len(kept), 3)

    def test_a_prompt_sentinel_survives_the_trim(self):
        """The episode is titled from it; losing it names the episode after a command."""
        from skillpp.segment import is_prompt, trim_leading_exploration
        steps = [self.prompt("push today's metrics like last week"),
                 self.bash("grep -rn metrics src/"), self.bash("grep -rn m2 src/"),
                 self.bash("curl -X POST https://dash/api/metrics"),
                 self.bash("curl -s https://dash/api/metrics/latest")]
        kept, cut = trim_leading_exploration(steps)
        self.assertEqual(cut, 2)
        self.assertTrue(is_prompt(kept[0]))

    def test_an_all_reads_episode_is_left_alone(self):
        from skillpp.segment import trim_leading_exploration
        steps = [self.bash("git status"), self.bash("git diff"), self.bash("ls -la")]
        kept, cut = trim_leading_exploration(steps)
        self.assertEqual((len(kept), cut), (3, 0))

    def test_a_write_is_never_read_only(self):
        from skillpp.segment import is_read_only
        self.assertFalse(is_read_only({"tool": "Write",
                                       "input": {"file_path": "/tmp/x"}}))

    def test_an_anchored_read_is_not_fooled_by_a_substring(self):
        from skillpp.segment import is_read_only
        self.assertTrue(is_read_only(self.bash("grep -rn x .")))
        self.assertFalse(is_read_only(self.bash("vim $(grep -l x .)")))

    def test_segment_records_what_it_trimmed(self):
        from skillpp.segment import segment
        episodes = segment([self.prompt("fix the export"),
                            self.bash("grep -rn export src/"),
                            self.bash("cat src/export.py"),
                            self.bash("sed -i s/a/b/ src/export.py"),
                            self.bash("git commit -am fix")])
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].trimmed, 2)


class TestCaptureDoesNotObserveItself(TempRoot):
    """Two failure modes found by installing the branch and using it.

    Neither came from a fixture. Both were in the live ledger within an hour.
    """

    def test_an_internal_run_is_not_captured(self):
        """`draft` spawns an agent, whose session was banked as a candidate.

        Two runs produced two junk entries titled `/skillpp-draft <id>`, holding
        the draft agent's own `find` and `ls`. Automation observing itself is a
        feedback loop: using the tool manufactures work for the tool.
        """
        import os
        from skillpp.cli import cmd_hook
        import argparse, io, sys as _sys

        os.environ["SKILLPP_INTERNAL"] = "1"
        self.addCleanup(lambda: os.environ.pop("SKILLPP_INTERNAL", None))
        real, _sys.stdin = _sys.stdin, io.StringIO(json.dumps(
            {"session_id": "auto", "cwd": "/p", "prompt": "invisible"}))
        self.addCleanup(lambda: setattr(_sys, "stdin", real))
        cmd_hook(argparse.Namespace(root=self.config.root,
                                    event="UserPromptSubmit", verbose=False))
        self.assertFalse((self.config.sessions_dir / "auto.json").exists())

    def test_a_harness_envelope_is_not_stated_intent(self):
        """A candidate was titled `<task-notification>`.

        The harness injects envelopes into the prompt stream. They are not
        something a developer typed, and a skill named after one never fires.
        """
        from skillpp.capture import handle_prompt
        handle_prompt(self.config, {"session_id": "e", "cwd": "/p",
                                    "prompt": "<task-notification>\ndone"})
        handle_prompt(self.config, {"session_id": "e", "cwd": "/p",
                                    "prompt": "<system-reminder>ignore me"})
        handle_prompt(self.config, {"session_id": "e", "cwd": "/p",
                                    "prompt": "the real request"})
        session = json.loads(
            (self.config.sessions_dir / "e.json").read_text())
        self.assertEqual(session["prompts"], ["the real request"])


class TestBenchmarkSegmentation(unittest.TestCase):
    """The free half of the benchmark, run as a test.

    Segmentation costs nothing and is deterministic, so there is no reason for
    it to live only in a benchmark anyone has to remember to run. Ranking stays
    out of the suite because it needs a local model.

    A wrong episode count is the one error nothing downstream recovers: merged
    episodes hide procedures inside each other, split ones destroy the
    recurrence count, and an episode that should not exist becomes a candidate
    titled after whatever question started it.
    """

    # Cases whose ground truth is correct and whose fix is downstream of
    # segmentation: `draft` splits one, `merge` folds the other. Both are
    # verified separately. Listed rather than silently excluded, so improving
    # the segmenter shows up as a test that needs updating.
    DOWNSTREAM = {"needs-split", "needs-merge"}

    def _score(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmarks"))
        from benchmarks.run import score
        from benchmarks.cases import CASES
        return score(CASES, use_model=False)

    def test_every_case_segments_as_expected(self):
        """Excluding the two whose fix is a later stage, not the segmenter."""
        wrong = [f"{r['name']}: expected {r['expected_episodes']}, "
                 f"got {r['got_episodes']}"
                 for r in self._score()["rows"]
                 if not r["segmentation"]
                 and not self.DOWNSTREAM & set(r["tags"])]
        self.assertEqual(wrong, [], "\n" + "\n".join(wrong))

    def test_the_known_gaps_are_still_exactly_the_known_gaps(self):
        """If segmentation starts handling one of these, this test says so.

        A suite that silently tolerates a documented gap cannot tell you when
        the gap closes, and a stale exclusion is how a benchmark quietly stops
        measuring.
        """
        failing = {r["name"] for r in self._score()["rows"]
                   if not r["segmentation"]}
        expected = {"deploy-then-status-email",
                    "the-same-release-different-runner"}
        self.assertEqual(failing, expected)

    def test_recurrence_is_measured_at_all(self):
        """Occurrences count sessions, so a single-session corpus cannot see the
        promotion gate. It could not, for 17 cases."""
        result = self._score()
        self.assertIsNotNone(result["recurrence"])
        self.assertGreaterEqual(result["recurrence"]["of"], 2)

    def test_the_corpus_covers_both_kinds(self):
        """A programming-only corpus would miss everything MCP-shaped."""
        sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmarks"))
        from benchmarks.cases import by_kind
        self.assertGreaterEqual(len(by_kind("programming")), 6)
        self.assertGreaterEqual(len(by_kind("productivity")), 5)

    def test_the_corpus_has_negative_cases(self):
        """Without sessions that should bank nothing, a detector that banks
        everything scores perfectly."""
        sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmarks"))
        from benchmarks.cases import CASES
        self.assertGreaterEqual(sum(1 for c in CASES if c.episodes == 0), 2)
        self.assertGreaterEqual(sum(1 for c in CASES if c.methods == 0), 3)


class TestEmbeddingMatch(TempRoot):
    """The one shape lexical similarity cannot see.

    No model is contacted: `embed` is replaced. What is pinned is the band, the
    arithmetic and the fail-safe — the embedding's own accuracy is measured
    separately and cannot be asserted here.
    """

    def _entry(self, eid, cmds, intent="cut the release", **kw):
        base = dict(id=eid, signature="", title=intent, intents=[intent],
                    sessions=[eid],
                    steps=[{"tool": "Bash", "input": {"command": c}} for c in cmds])
        base.update(kw)
        entry = Entry(**base)
        from skillpp.normalize import signature as sig
        entry.signature = sig(entry.steps)
        return entry

    def _vectors(self, mapping):
        """Point `embed` at canned vectors keyed by a substring of its input."""
        import skillpp.similar as sim

        def fake(text, **kw):
            for needle, vector in mapping.items():
                if needle in text:
                    return vector
            return [0.0, 0.0, 1.0]
        real, sim.embed = sim.embed, fake
        self.addCleanup(lambda: setattr(sim, "embed", real))

    RELEASE = ["git checkout main", "git pull --ff-only", "npm test",
               "npm version 2.4.0", "git tag -s v2.4.0 -m rel",
               "git push --follow-tags"]
    RELEASE_PYTEST = ["git checkout main", "git pull --ff-only", "pytest -q",
                      "npm version 2.5.0", "git tag -s v2.5.0 -m rel",
                      "git push --follow-tags"]

    def test_the_band_holds_only_the_ambiguous_pairs(self):
        """Identical procedures match lexically and never reach a model."""
        from skillpp.similar import near_misses
        a = self._entry("a", self.RELEASE)
        b = self._entry("b", self.RELEASE)
        self.assertEqual(near_misses([a, b], floor=0.70, ceiling=0.85), [])

    def test_a_substituted_step_lands_in_the_band(self):
        from skillpp.similar import near_misses
        pairs = near_misses([self._entry("a", self.RELEASE),
                             self._entry("b", self.RELEASE_PYTEST)],
                            floor=0.70, ceiling=0.85)
        self.assertEqual(len(pairs), 1)

    def test_a_high_embedding_reads_as_the_same_procedure(self):
        from skillpp.similar import same_procedure
        self._vectors({"npm test": [1.0, 0.0, 0.0], "pytest": [0.98, 0.2, 0.0]})
        verdict, score, _ = same_procedure(
            self._entry("a", self.RELEASE), self._entry("b", self.RELEASE_PYTEST),
            model="x", floor=0.80)
        self.assertIs(verdict, True)
        self.assertGreater(score, 0.8)

    def test_a_low_embedding_leaves_them_apart(self):
        from skillpp.similar import same_procedure
        self._vectors({"npm test": [1.0, 0.0, 0.0], "pytest": [0.0, 1.0, 0.0]})
        verdict, _, _ = same_procedure(
            self._entry("a", self.RELEASE), self._entry("b", self.RELEASE_PYTEST),
            model="x", floor=0.80)
        self.assertIs(verdict, False)

    def test_an_unreachable_model_never_merges(self):
        """Folding is irreversible — the second entry's evidence moves and it is
        gone — so a failed call must leave both alone."""
        import skillpp.similar as sim
        from skillpp.local import LocalModelUnavailable
        from skillpp.similar import same_procedure

        def boom(text, **kw):
            raise LocalModelUnavailable("refused")
        real, sim.embed = sim.embed, boom
        self.addCleanup(lambda: setattr(sim, "embed", real))
        verdict, _, why = same_procedure(
            self._entry("a", self.RELEASE), self._entry("b", self.RELEASE_PYTEST),
            model="x")
        self.assertIsNone(verdict)
        self.assertIn("leaving both", why)

    def test_occurrences_count_sessions_not_sightings(self):
        """The correction this project already had to make once: two sightings
        inside one session are one occurrence."""
        from skillpp.similar import fold_into
        a = self._entry("a", self.RELEASE, sessions=["s1"])
        b = self._entry("b", self.RELEASE_PYTEST, sessions=["s1"])
        fold_into(a, b)
        self.assertEqual(a.occurrences, 1)

        c = self._entry("c", self.RELEASE, sessions=["s1"])
        d = self._entry("d", self.RELEASE_PYTEST, sessions=["s2"])
        fold_into(c, d)
        self.assertEqual(c.occurrences, 2)

    def test_folding_keeps_the_other_body_as_a_variant(self):
        from skillpp.similar import fold_into
        a = self._entry("a", self.RELEASE, sessions=["s1"])
        b = self._entry("b", self.RELEASE_PYTEST, sessions=["s2"])
        fold_into(a, b)
        self.assertEqual(len(a.variants), 1)

    def test_cosine_is_safe_on_degenerate_input(self):
        from skillpp.local import cosine
        self.assertEqual(cosine([], [1.0]), 0.0)
        self.assertEqual(cosine([0.0, 0.0], [0.0, 0.0]), 0.0)
        self.assertEqual(cosine([1.0, 2.0], [1.0]), 0.0)
        self.assertAlmostEqual(cosine([1.0, 0.0], [1.0, 0.0]), 1.0)


class TestNaming(TempRoot):
    """Naming is the half capture cannot do.

    A candidate arrives titled with whatever the developer typed, because code
    can only reuse a string it observed. Measured against a frontier reader:
    it produced `draining-app-replicas-to-clear-a-migration-lock` where this
    branch had `the staging migration is stuck, get it green`.
    """

    def setUp(self) -> None:
        super().setUp()
        import argparse
        self.argparse = argparse
        Ledger(self.config).save(Entry(
            id="cand", signature="s",
            title="the staging migration is stuck, get it green",
            steps=[{"tool": "Bash", "input": {"command": "npm run migrate"}},
                   {"tool": "Bash", "input": {"command": "kubectl scale deploy/api --replicas=0"}}]))

    def _args(self, **kw):
        base = dict(root=self.config.root, id="cand", title=None, description=None)
        base.update(kw)
        return self.argparse.Namespace(**base)

    def _get(self):
        return Ledger(self.config).get("cand")

    def test_a_title_replaces_the_developers_words(self):
        from skillpp.cli import cmd_name
        self.assertEqual(cmd_name(self._args(
            title="drain replicas to clear a migration lock")), 0)
        self.assertEqual(self._get().title,
                         "drain replicas to clear a migration lock")

    def test_a_description_is_recorded_and_round_trips(self):
        from skillpp.cli import cmd_name
        cmd_name(self._args(description="Use when a migration is blocked by a "
                                        "lock held by running replicas."))
        self.assertIn("blocked by a lock", self._get().description)

    def test_a_description_over_the_frontmatter_limit_is_refused(self):
        """200 characters is the skill frontmatter limit. A description that
        will not fit cannot become a skill, so refusing here beats discovering
        it at promotion."""
        from skillpp.cli import cmd_name
        self.assertEqual(cmd_name(self._args(description="x" * 201)), 1)
        self.assertEqual(self._get().description, "")

    def test_a_title_long_enough_to_be_a_session_summary_is_refused(self):
        from skillpp.cli import cmd_name
        self.assertEqual(cmd_name(self._args(title="x" * 81)), 1)

    def test_naming_nothing_is_an_error_not_a_silent_pass(self):
        from skillpp.cli import cmd_name
        self.assertEqual(cmd_name(self._args()), 1)

    def test_naming_does_not_promote_or_change_status(self):
        from skillpp.cli import cmd_name
        from skillpp.ledger import STATUS_CANDIDATE
        cmd_name(self._args(title="drain replicas first"))
        entry = self._get()
        self.assertEqual(entry.status, STATUS_CANDIDATE)
        self.assertEqual(entry.skill_path, "")

    def test_the_description_reaches_the_written_file(self):
        """A human reading the ledger entry should see it without --json."""
        from skillpp.cli import cmd_name
        cmd_name(self._args(description="Use when a migration is lock-blocked."))
        text = Ledger(self.config).path_for("cand").read_text()
        self.assertIn("description: Use when a migration is lock-blocked.", text)


class TestSplit(TempRoot):
    """The expensive stage correcting the cheap one.

    Code cuts only at markers and prompt boundaries. A single request that did
    two things, neither finishing recognisably, arrives as one candidate — case
    C in the benchmark. A local model can find that boundary when told one
    exists but cannot tell whether one does (3 of 5), so this is reached from
    `draft`, where a frontier reader is already looking.
    """

    def setUp(self) -> None:
        super().setUp()
        import argparse
        self.argparse = argparse
        self.ledger = Ledger(self.config)
        self.ledger.save(Entry(
            id="both", signature="sig", title="roll out api then smoke-test",
            intents=["roll out api then smoke-test staging"], sessions=["s1"],
            steps=[{"tool": "Bash", "input": {"command": c}} for c in (
                "helm upgrade api charts/api --wait",
                "kubectl rollout status deploy/api",
                "./scripts/smoke.sh staging",
                "curl -s https://staging/health")]))

    def _split(self, at):
        from skillpp.cli import cmd_split
        return cmd_split(self.argparse.Namespace(
            root=self.config.root, id="both", at=at))

    def test_a_split_produces_two_candidates(self):
        from skillpp.ledger import STATUS_CANDIDATE
        self.assertEqual(self._split(2), 0)
        cands = [e for e in Ledger(self.config).all()
                 if e.status == STATUS_CANDIDATE]
        self.assertEqual(sorted(len(e.steps) for e in cands), [2, 2])

    def test_the_original_is_kept_not_deleted(self):
        """The verdict came from a model and the steps are the only evidence."""
        from skillpp.ledger import STATUS_SPLIT
        self._split(2)
        original = Ledger(self.config).get("both")
        self.assertEqual(original.status, STATUS_SPLIT)
        self.assertEqual(len(original.steps), 4)
        self.assertIn("split at step 2", original.notes)

    def test_a_split_that_would_strand_a_step_is_refused(self):
        """One step is not a procedure, so cutting there loses it."""
        from skillpp.ledger import STATUS_CANDIDATE
        self.assertEqual(self._split(1), 1)
        self.assertEqual(Ledger(self.config).get("both").status,
                         STATUS_CANDIDATE)

    def test_a_split_at_the_end_is_refused(self):
        self.assertEqual(self._split(4), 1)

    def test_both_halves_inherit_the_provenance(self):
        """Occurrences count sessions, and both halves were seen in the same one."""
        from skillpp.ledger import STATUS_CANDIDATE
        self._split(2)
        for e in Ledger(self.config).all():
            if e.status == STATUS_CANDIDATE:
                self.assertEqual(e.sessions, ["s1"])

    def test_a_split_entry_is_not_offered_for_review(self):
        self._split(2)
        ids = {e.id for e in Ledger(self.config).candidates(ready_only=False)}
        self.assertNotIn("both", ids)


class TestDecisionLog(TempRoot):
    """Ground truth that nobody has to maintain.

    The benchmark is 17 hand-written cases whose truth was authored by whoever
    wrote the detector — internal consistency, and it hid a defect until real
    work was scored. This accumulates labels from decisions that actually
    happened, which is the idea `truth.py` on the pattern-detection branch
    exists to serve.
    """

    def setUp(self) -> None:
        super().setUp()
        import argparse
        self.argparse = argparse
        self.ledger = Ledger(self.config)

    def _entry(self, eid, hint="", **kw):
        entry = Entry(id=eid, signature="s", title=f"work {eid}", hint=hint, **kw)
        self.ledger.save(entry)
        return entry

    def test_a_promote_records_a_method_label(self):
        from skillpp import decisions
        decisions.record(self.config, self._entry("a", hint="method"),
                         decisions.PROMOTED)
        rows = decisions.read(self.config)
        self.assertEqual(rows[0]["decision"], "promoted")
        self.assertEqual(rows[0]["hint"], "method")

    def test_the_hint_is_captured_alongside_the_decision(self):
        """Without the pair, the line is history rather than a measurement."""
        from skillpp import decisions
        decisions.record(self.config, self._entry("a", hint="one-off"),
                         decisions.PROMOTED)
        score = decisions.score(self.config)
        self.assertEqual(score["disagreed"], 1)
        self.assertEqual(score["misses"][0]["hint"], "one-off")

    def test_agreement_is_counted_both_ways(self):
        from skillpp import decisions
        decisions.record(self.config, self._entry("a", hint="method"),
                         decisions.PROMOTED)
        decisions.record(self.config, self._entry("b", hint="one-off"),
                         decisions.DISMISSED)
        score = decisions.score(self.config)
        self.assertEqual((score["agreed"], score["disagreed"]), (2, 0))

    def test_an_unranked_decision_is_not_scored(self):
        """Deciding before sift ran says nothing about sift."""
        from skillpp import decisions
        decisions.record(self.config, self._entry("a"), decisions.PROMOTED)
        score = decisions.score(self.config)
        self.assertEqual(score["unranked"], 1)
        self.assertEqual(score["scored"], 0)

    def test_the_latest_decision_wins_per_candidate(self):
        """Parked, reopened, then promoted is one judgement with a history."""
        from skillpp import decisions
        entry = self._entry("a", hint="one-off")
        decisions.record(self.config, entry, decisions.PARKED)
        decisions.record(self.config, entry, decisions.REOPENED)
        decisions.record(self.config, entry, decisions.PROMOTED)
        score = decisions.score(self.config)
        self.assertEqual(score["judged"], 1)
        self.assertEqual(score["disagreed"], 1)

    def test_parking_is_never_truth(self):
        """It is the model's own act, so scoring it would grade its own homework."""
        from skillpp import decisions
        decisions.record(self.config, self._entry("a", hint="one-off"),
                         decisions.PARKED)
        self.assertEqual(decisions.score(self.config)["judged"], 0)

    def test_a_reopen_counts_as_a_method(self):
        from skillpp import decisions
        decisions.record(self.config, self._entry("a", hint="one-off"),
                         decisions.REOPENED)
        self.assertEqual(decisions.score(self.config)["disagreed"], 1)

    def test_an_unwritable_log_does_not_break_the_command(self):
        """Losing a label must never fail a promote.

        A directory where the file belongs makes the open fail without any
        permission juggling, which would otherwise outlive the test.
        """
        from skillpp import decisions
        entry = self._entry("a", hint="method")
        self.config.decisions_file.mkdir(parents=True, exist_ok=True)
        decisions.record(self.config, entry, decisions.PROMOTED)  # must not raise
        self.assertEqual(decisions.read(self.config), [])

    def test_a_corrupt_line_is_skipped_not_fatal(self):
        from skillpp import decisions
        decisions.record(self.config, self._entry("a", hint="method"),
                         decisions.PROMOTED)
        with self.config.decisions_file.open("a") as fh:
            fh.write("{not json\n")
        self.assertEqual(len(decisions.read(self.config)), 1)
