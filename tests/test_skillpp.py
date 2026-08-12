"""Unit tests. Stdlib only: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillpp.capture import fold_session, handle_prompt, handle_tool, handle_session_end
from skillpp.config import Config
from skillpp.ledger import Entry, Ledger, make_id
from skillpp.lifecycle import check_staleness, parse_frontmatter, record_use, scan
from skillpp.normalize import normalize_command, parameterize, signature
from skillpp.recurrence import find_match, similarity
from skillpp.sanitize import scrub
from skillpp.signals import detect, effects
from skillpp.summary import check_dependencies, scaffold_skill


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
