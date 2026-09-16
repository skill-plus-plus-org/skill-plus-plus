"""Unit tests. Stdlib only: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillpp.capture import (_fold_steps, fold_session, handle_prompt, handle_tool,
                             handle_session_end, note_pending_check)
from skillpp.config import Config
from skillpp.ledger import STATUS_CANDIDATE, Entry, Ledger, make_id
from skillpp.lifecycle import check_staleness, parse_frontmatter, record_use, scan
from skillpp.normalize import normalize_command, parameterize, signature
from skillpp.recurrence import find_match, similarity
from skillpp.sanitize import scrub
from skillpp.segment import is_marker, is_prompt
from skillpp.segment import segment as _real_segment
from skillpp.signals import detect, effects
from skillpp.summary import check_dependencies, scaffold_skill

from fixtures.messy_session import (EXPECTED_OCCURRENCES, LEAKED_TOKEN,
                                    clean_session_dict, to_captured_session,
                                    to_session_dict)


_REAL_JUDGE = None
_REAL_DESCRIBE = None


def setUpModule() -> None:
    """Silence the boundary judge for the whole suite.

    `capture.handle_tool` asks a local model whether each step ended a task.
    Left real, the suite needs Ollama running and pays ~0.7s per captured step —
    measured, `TestBenchmarkSegmentation` alone went from 0.4s to **371s**, and
    a machine without Ollama would sit through a five-second timeout per step
    instead.

    Module scope rather than `TempRoot`, because the classes that route through
    capture are not all `TempRoot` subclasses — `TestBenchmarkSegmentation` is a
    plain `TestCase` and is exactly the one that hurt.

    The stub stands in for a model that is *reachable*, answering every step
    from the completion-marker vocabulary. It used to answer `None`, meaning "no
    opinion" — which was fine while `None` made `segment` fall back to that same
    vocabulary. Now `None` means offline and nothing is banked, so a `None` stub
    would test the offline path in 53 places that mean to test capture.

    The vocabulary is a good test double precisely because it is no longer a
    product: deterministic, free, and it reproduces the boundaries these tests
    were written against. A test of the real judge opts in with
    `TempRoot._stub_judge`; a test of the offline path builds a session with no
    verdicts on purpose.
    """
    global _REAL_JUDGE, _REAL_DESCRIBE
    import skillpp.boundary as boundary
    _REAL_JUDGE = boundary.judge_session
    _REAL_DESCRIBE = boundary.describe_in_session
    boundary.judge_session = _marker_judge
    # Same reasoning for the describer, which runs on the same hot path and is
    # slower still — it writes a sentence where the judge writes one word.
    boundary.describe_in_session = (
        lambda config, session, step, reply="": "")


def tearDownModule() -> None:
    import skillpp.boundary as boundary
    boundary.judge_session = _REAL_JUDGE
    boundary.describe_in_session = _REAL_DESCRIBE


def _marker_judge(config, session, verdict=is_marker):
    """Stand in for `boundary.judge_session` without a model.

    The real one asks a question per prompt gap. This answers per step from
    *verdict*, defaulting to the completion-marker vocabulary — deterministic,
    free, and it reproduces the boundaries this suite was written against.

    Every substantive step gets a key, because `segment.was_judged` is what
    separates a judged session from an offline one. `verdict=None` means the
    model is unreachable: no keys, and the session is offline.
    """
    found = 0
    for step in session.get("steps", []):
        if is_prompt(step):
            continue
        answer = verdict(step) if callable(verdict) else verdict
        if answer is None:
            step.pop("end", None)
            continue
        step["end"] = bool(answer)
        found += bool(answer)
    return found


def bash(command: str, failed: bool = False) -> dict:
    return {"tool": "Bash", "input": {"command": command}, "failed": failed}


def judged(steps: list[dict]) -> list[dict]:
    """Stamp a hand-built session with the verdicts a live capture would carry.

    `segment` banks nothing from a stream no model judged, so a session assembled
    as literal dicts — rather than driven through `handle_tool` — reaches the
    fold with no verdicts and is treated as offline. The marker vocabulary is the
    stand-in, for the same reason it is in `setUpModule`.
    """
    for step in steps:
        if not is_prompt(step):
            step["end"] = is_marker(step)
    return steps


def segment(steps, *args, **kwargs):
    """`skillpp.segment.segment`, on a stream that carries verdicts.

    Boundaries come from the judge now; a stream nothing judged banks nothing at
    all, which is the offline path and not what these tests are about. Stamping
    the marker vocabulary in reproduces the boundaries they were written against
    without needing Ollama.

    Tests of the offline path call `_real_segment` directly, with steps that
    carry no verdicts on purpose — see `TestOfflineWithoutAModel`.
    """
    return _real_segment(judged([dict(s) for s in steps]), *args, **kwargs)


class TempRoot(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = Config(self.root / "skillpp")
        self.config.ensure_dirs()
        self.judged = self._stub_judge(is_marker)

    def _stub_judge(self, verdict):
        """Answer the boundary judge without a model, recording what it saw.

        `handle_tool` asks a local model whether each step ended a task. Left
        real, the suite needs Ollama running and pays ~0.7s per captured step —
        so a test of the ledger becomes a test of the model, and a machine
        without Ollama sees hundreds of five-second timeouts instead of results.

        The default is `is_marker`, standing in for a model that is *reachable*
        and answers every step. It used to be `None`, "no opinion", which was
        fine while `None` made `segment` fall back to the same vocabulary. Now
        `None` means offline and nothing is banked, so a `None` default would
        quietly turn every ledger test into a test of the offline path.

        Pass `True`/`False`, or a callable taking the step, to script a
        different judge. Pass `None` deliberately to test being offline.
        """
        import skillpp.boundary as boundary
        real = boundary.judge_session
        calls: list[dict] = []

        def fake(config, session):
            calls.extend(s for s in session.get("steps", []) if not is_prompt(s))
            return _marker_judge(config, session, verdict)

        boundary.judge_session = fake
        self.addCleanup(lambda: setattr(boundary, "judge_session", real))
        return calls

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

    def test_an_ssh_remote_is_not_an_email_address(self):
        """`git@github.com` names nobody — it is the same string for every user
        alive. Redacting it destroyed the clone step of every onboarding trace."""
        for cmd in ("git clone git@github.com:acme/api.git",
                    "ssh git@gitlab.internal:team/repo.git",
                    "git remote add origin git@bitbucket.org:team/svc.git"):
            self.assertEqual(scrub(cmd), cmd)

    def test_a_real_address_is_still_redacted(self):
        self.assertIn("[REDACTED:email]", scrub("contact me at dev@example.com"))
        self.assertNotIn("jane.doe", scrub("email: jane.doe@acme.co, cc ops@acme.co"))

    def test_a_long_path_is_not_mistaken_for_a_secret(self):
        """`/` is in the token alphabet for base64's sake, so any path past 40
        chars scored as high-entropy. A real entry read
        `cat ${HOME}/.[REDACTED:high-entropy].md`, with the path destroyed."""
        path = "/Users/dev/.claude/projects/-Users-dev-ai-projects-skill-plus/notes.md"
        self.assertEqual(scrub(f"cat {path}"), f"cat {path}")

    def test_a_long_descriptive_filename_is_not_a_secret(self):
        """Found by looking at document work rather than the command-heavy
        sessions this heuristic was tuned against. The first guard only split
        on `/`, so a long hyphenated filename still scored as high-entropy."""
        for name in ("2026-02-19-article-draft-guarantees-in-code-v3.md",
                     "Reference2-heading-density-vs-word-count-analysis.md",
                     "/tmp/2026-02-19-article-draft-guarantees-in-code-v3.md"):
            self.assertEqual(scrub(name), name)

    def test_a_blob_with_separators_in_it_is_still_a_secret(self):
        """Segment *length* is not the discriminator — a base64 blob splits into
        short pieces too. Its pieces do not look like words."""
        blob = "OObjTXEYQHXlFd4PJUMsX7f3/OTb2ysM791a7dgirmuKjqhrdV9Xhl8yW7M"
        self.assertIn("[REDACTED:high-entropy]", scrub(f"TOKEN {blob}"))

    def test_a_base64_blob_containing_a_slash_is_still_caught(self):
        """The path guard must not open a hole: judge the longest segment."""
        blob = "aGVsbG8/d29ybGQrc2VjcmV0LzEyMzQ1Njc4OTBhYmNkZWZnaGlqa2xtbg=="
        self.assertIn("[REDACTED:high-entropy]", scrub(f"SECRET {blob}"))

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

    def test_normalize_links_reads_every_link_of_a_chain(self):
        from skillpp.normalize import normalize_links
        self.assertEqual(normalize_links("cd /repo && git add -A && git commit -m x"),
                         ["cd", "git add", "git commit"])
        self.assertEqual(normalize_links(""), [])

    def test_the_chain_fix_does_not_move_normalize_command(self):
        """`step_shape` calls it, so any drift here moves every signature."""
        self.assertEqual(normalize_command("cd /repo && git commit -m x"), "cd")
        self.assertEqual(normalize_command("git commit -m 'x'"), "git commit")
        self.assertEqual(normalize_command("npm run test -- --watch"), "npm run")

    def test_a_description_does_not_change_the_signature(self):
        """The guard on rendering.

        `signature` is the entry id and the recurrence match key, and it is
        built to be maximally stable. A `description` is the opposite: free
        text the agent rewrites every run — the same `./assemble.sh` was
        described "Assemble after tone pass" once and "Assemble and measure
        section 6" the next time. If that reached `step_shape`, two runs of one
        procedure would fingerprint differently and never reach the threshold.
        """
        plain = [bash("pytest -k auth"), bash("git push")]
        described = [
            {"tool": "Bash", "input": {"command": "pytest -k auth",
                                       "description": "Assemble after tone pass"},
             "failed": False},
            {"tool": "Bash", "input": {"command": "git push",
                                       "description": "Ship it"}, "failed": False}]
        self.assertEqual(signature(plain), signature(described))

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

    def test_effects_carry_descriptions_without_reshaping_commands(self):
        """Additive: every other key keeps its type, so scaffold_skill is safe."""
        eff = effects([
            {"tool": "Bash", "input": {"command": "rm -rf build",
                                       "description": "Clear the stale build"}},
            bash("npm test")])
        self.assertEqual(eff["describes"]["rm -rf build"], "Clear the stale build")
        self.assertNotIn("npm test", eff["describes"])
        self.assertIsInstance(eff["commands"], list)
        self.assertIn("rm -rf build", eff["destructive"])

    def test_effects_keep_the_first_description_for_a_repeated_command(self):
        """Must agree with the deduped command list, which keeps the first."""
        eff = effects([
            {"tool": "Bash", "input": {"command": "./assemble.sh",
                                       "description": "Assemble after tone pass"}},
            {"tool": "Bash", "input": {"command": "./assemble.sh",
                                       "description": "Assemble and measure"}}])
        self.assertEqual(eff["commands"], ["./assemble.sh"])
        self.assertEqual(eff["describes"]["./assemble.sh"], "Assemble after tone pass")

    def test_effects_flag_destructive_and_writes(self):
        eff = effects([bash("rm -rf build"), bash("echo hi > out.txt"),
                       {"tool": "Write", "input": {"file_path": "src/a.py"}}])
        self.assertTrue(eff["destructive"])
        self.assertIn("out.txt", eff["writes"])
        self.assertIn("src/a.py", eff["writes"])


class TestCapture(TempRoot):
    def _session(self, commands, prompts=("do the thing",), sid="s1"):
        return {"session_id": sid, "cwd": "/proj", "prompts": list(prompts),
                "steps": judged([bash(c) for c in commands])}

    def test_thin_session_is_ignored(self):
        result = fold_session(self.config, self._session(["ls"]))
        self.assertEqual(result["status"], "too-thin")

    def test_the_title_comes_from_the_commit_not_the_prompt(self):
        """A commit is written after the work and says what it accomplished.

        The opening prompt is written before and says what was wrong. On the
        session this was measured against, the prompt produced the entry title
        "Looks good — commit".
        """
        from skillpp.capture import _subject_of, _title_for
        heredoc = ("git add -A && git commit -m \"$(cat <<'EOF'\n"
                   "test(eval): add walkthrough-card case for Desk Booking\n\n"
                   "Sibling to desk_booking_local.\nEOF\n)\"")
        steps = [bash(heredoc)]
        self.assertEqual(_subject_of(steps),
                         "add walkthrough-card case for Desk Booking")
        self.assertEqual(_title_for(["Looks good — commit"], steps),
                         "add walkthrough-card case for Desk Booking")

    def test_commit_subject_parsing_falls_through_rather_than_guessing(self):
        """Unrecognised forms keep the old behaviour instead of inventing one."""
        from skillpp.capture import _subject_of, _title_for
        self.assertEqual(_subject_of([bash("git commit -m 'fix: guard empty cart'")]),
                         "guard empty cart")
        self.assertEqual(_subject_of([bash('git commit -m "feat(api): add retry"')]),
                         "add retry")
        for nothing in ("git commit", "npm test"):
            self.assertEqual(_subject_of([bash(nothing)]), "")
        # A rejected commit is not a completed task and must not name the entry.
        self.assertEqual(_subject_of([bash("git commit -m 'nope'", failed=True)]), "")
        self.assertEqual(_title_for(["ship the thing"], [bash("npm test")]),
                         "ship the thing")

    def test_the_sift_is_shown_every_stated_intent(self):
        """The verification step lives at the end of a task, where the cap was.

        Capped at three, the model was handed "did you call MCP for this?" and
        never saw "confirm TOPICS is still the single source of truth".
        """
        from skillpp.episode import render
        entry = Entry(id="a", signature="s", title="t",
                      steps=[bash("npm test")],
                      intents=["add an eval case",
                               "did you call MCP for this?",
                               "use the adk-docs MCP tool",
                               "regenerate the evalset",
                               "check the docstring in book.py",
                               "confirm TOPICS is still the single source of truth"])
        ask, _ = render(entry)
        self.assertIn("adk-docs", ask)
        self.assertIn("single source of truth", ask)

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

    def test_a_captured_read_keeps_the_path_the_write_rule_needs(self):
        """End to end, because the two halves lived apart and never met.

        `_substantive`'s read-feeds-a-write rule compares a `Read`'s
        `file_path` against a following write's. `_KEEP_INPUT` did not keep the
        field, so in production that comparison was always against `None` and
        the rule could not fire once — while `tests/benchmarks/boundaries.py`
        added the path when replaying a transcript, so every fixture built from
        one exercised a rule the pipeline never ran. Measured on a real session:
        four reads captured, none kept; the same session from its transcript,
        three kept.

        Asserting on the signature rather than on `_KEEP_INPUT` keeps this
        honest — it fails if either half regresses.
        """
        handle_prompt(self.config, {"session_id": "rw", "cwd": "/p",
                                    "prompt": "fix the auth bug"})
        script = [("Bash", {"command": "npm test"}),
                  ("Read", {"file_path": "/p/auth.py"}),
                  ("Edit", {"file_path": "/p/auth.py"}),
                  ("Bash", {"command": "git commit -m fix"})]
        for tool, payload in script:
            handle_tool(self.config, {"session_id": "rw", "cwd": "/p",
                                      "tool_name": tool, "tool_input": payload,
                                      "tool_response": {"exit_code": 0}})
        handle_session_end(self.config, {"session_id": "rw"})
        entry = list(Ledger(self.config).all())[0]
        self.assertIn("read", entry.signature.split(" | "))

    def test_a_captured_read_that_leads_nowhere_is_still_dropped(self):
        """The other half of the rule. Keeping the path must not keep the
        exploration the rule exists to discard."""
        handle_prompt(self.config, {"session_id": "ro", "cwd": "/p",
                                    "prompt": "fix the auth bug"})
        script = [("Bash", {"command": "npm test"}),
                  ("Read", {"file_path": "/p/somewhere_else.py"}),
                  ("Edit", {"file_path": "/p/auth.py"}),
                  ("Bash", {"command": "git commit -m fix"})]
        for tool, payload in script:
            handle_tool(self.config, {"session_id": "ro", "cwd": "/p",
                                      "tool_name": tool, "tool_input": payload,
                                      "tool_response": {"exit_code": 0}})
        handle_session_end(self.config, {"session_id": "ro"})
        entry = list(Ledger(self.config).all())[0]
        self.assertNotIn("read", entry.signature.split(" | "))

    def test_the_describer_sees_more_reply_than_the_step_stores(self):
        """The describer must not read the reply back off the step.

        `tool_returned` is cut to storage size. An earlier version of the
        describer read it from there, which capped the model below the budget
        that was measured to help it and made widening that budget a no-op —
        `boundary._REPLY_CHARS` is 1200 and the stored field is 400. The
        difference only shows on a reply longer than the storage cap, which is
        37% of real tool calls.
        """
        import skillpp.boundary as boundary
        seen = {}

        def spy(config, session, step, reply=""):
            seen["reply"] = reply
            seen["stored"] = step.get("tool_returned", "")
            return "described"

        real = boundary.describe_in_session
        boundary.describe_in_session = spy
        self.addCleanup(lambda: setattr(boundary, "describe_in_session", real))

        handle_tool(self.config, {
            "session_id": "big", "cwd": "/p", "tool_name": "Bash",
            "tool_input": {"command": "grep -rn thing ."},
            "tool_response": {"stdout": "match " * 900}})

        self.assertGreater(len(seen["reply"]), boundary._REPLY_CHARS)
        self.assertLessEqual(len(seen["stored"]), 400)
        self.assertGreater(len(seen["reply"]), len(seen["stored"]))

    def test_the_reply_reaches_the_prompt_not_just_the_call(self):
        """`describe_in_session` once accepted `reply` and dropped it.

        The parameter was added and never forwarded, so `describe` fell back to
        the stored 400 characters and widening the budget did nothing. Stubbing
        `describe_in_session` cannot catch that — it is the callee that lost the
        argument — so this asserts on the prompt `describe` actually builds.
        """
        import skillpp.boundary as boundary
        seen = {}

        def spy_ask(model, prompt, **kw):
            seen["prompt"] = prompt
            return "described"

        real = boundary.ask
        boundary.ask = spy_ask
        self.addCleanup(lambda: setattr(boundary, "ask", real))

        needle = "UNIQUE-MARKER-DEEP-IN-THE-REPLY"
        _REAL_DESCRIBE(
            self.config,
            {"steps": [{"tool": "UserPrompt", "input": {"text": "find it"},
                        "failed": False}]},
            {"tool": "Bash", "input": {"command": "grep -rn thing ."},
             "tool_returned": "x" * 400},
            reply="filler " * 60 + needle)
        self.assertIn(needle, seen["prompt"])

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


class TestEnvelopePrefixes(TempRoot):
    """Both of these reached the ledger as candidate titles in a real replay."""

    def _prompts(self, text):
        handle_prompt(self.config, {"session_id": "s", "prompt": text})
        from skillpp.capture import _load_session
        return _load_session(self.config, "s")["prompts"]

    def test_command_message_is_not_a_prompt(self):
        self.assertEqual(
            self._prompts("<command-message>caveman:caveman</command-message>"), [])

    def test_attach_marker_is_not_a_prompt(self):
        self.assertEqual(self._prompts("<!-- attach -->"), [])

    def test_real_work_still_lands(self):
        self.assertEqual(self._prompts("fix the staging migration"),
                         ["fix the staging migration"])


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

    def test_the_skill_keeps_only_what_recurred(self):
        """An episode is one occurrence wrapped in that day's particulars. On a
        real entry seen 11x the method was 3 steps and the episode held 14."""
        from skillpp.signals import recurring_steps
        core = [bash("./review.sh"), bash("./assemble.sh --force"),
                bash("wc -w sections/*.md")]
        entry = Entry(id="abc", signature="s", title="article loop",
                      occurrences=3, steps=core + [bash("mv 11-close.md 12-close.md")],
                      variants=[core + [bash("mv 11-close.md 12-close.md")],
                                core + [bash("git commit -am wip")],
                                core])
        kept = recurring_steps(entry)
        self.assertEqual([s["input"]["command"] for s in kept],
                         [s["input"]["command"] for s in core])
        self.assertNotIn("mv 11-close.md", scaffold_skill(entry, "x", "y"))

    def test_one_occurrence_keeps_every_step(self):
        """Nothing to compare, so nothing is evidence of being incidental."""
        from skillpp.signals import recurring_steps
        steps = [bash("npm ci"), bash("npm test")]
        entry = Entry(id="abc", signature="s", steps=steps, variants=[steps])
        self.assertEqual(len(recurring_steps(entry)), 2)

    def test_a_disagreeing_set_of_variants_keeps_the_episode(self):
        """An empty intersection means the occurrences agree on nothing, and
        the honest answer is the whole episode rather than a stub."""
        from skillpp.signals import recurring_steps
        entry = Entry(id="abc", signature="s",
                      steps=[bash("npm ci"), bash("npm test")],
                      variants=[[bash("npm ci")], [bash("cargo build")]])
        self.assertEqual(len(recurring_steps(entry)), 2)

    def test_shell_keywords_are_not_dependencies(self):
        """A real skill declared `requires_cli: ["\\", "do", "done", "for"]`."""
        from skillpp.capture import _cli_dependencies
        deps = _cli_dependencies([bash(
            'cd s && for f in *.md; do printf x; wc -w < "$f"; done')])
        self.assertEqual(deps, {"printf", "wc"})
        self.assertEqual(_cli_dependencies([bash("for x in 1; do npm test; done")]),
                         {"npm"})

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
        # "judged", not "marker": the boundary is a verdict now. The marker
        # vocabulary reaches this test as the stand-in judge in `judged()`, not
        # as a rule inside `segment`.
        self.assertEqual(episodes[0].ended_by, "judged")

    def _mcp(self, name):
        return {"tool": f"mcp__{name}", "input": {}, "failed": False}

    def test_toolsearch_and_asking_are_lookups_not_work(self):
        """Both loaded a schema or asked a question; neither produced anything.

        Their absence kept the doc-fetch episode from folding into its commit.
        """
        from skillpp.segment import is_read_only
        for tool in ("ToolSearch", "AskUserQuestion"):
            self.assertTrue(is_read_only({"tool": tool, "input": {}}), tool)

    def test_a_six_turn_session_with_one_task_is_one_episode(self):
        """The real session's shape: six turns, one task, one commit at the end.

        Each mid-task instruction did more than one tool call, which was all the
        step-count guard asked for, so the prompt rule cut this five times and
        banked five fragments plus the commit, none of them the procedure.
        `_absorb_before_commit` existed to sweep those fragments back together.

        Both are gone. Prompts do not cut, so there are no fragments to repair,
        and the session is one episode because nothing claimed it ended until
        the commit.
        """
        steps = [self._prompt("add an eval case for the desk-booking card"),
                 {"tool": "ToolSearch", "input": {}, "failed": False},
                 self._mcp("adk-docs__fetch_docs"),
                 self._prompt("now write the case"),
                 {"tool": "Edit", "input": {"file_path": "eval/cases.json"}, "failed": False},
                 bash("python3 -c 'json.load(...)'"),
                 self._prompt("regenerate the evalset from that"),
                 bash("python3 eval/generate_evalset.py"),
                 self._prompt("check the docstring in book.py first"),
                 bash("grep -n TOPICS book.py"),
                 self._prompt("looks good — commit"),
                 bash("git diff --stat"),
                 bash("git commit -m 'test(eval): add desk-booking case'")]
        episodes = segment(steps)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].ended_by, "judged")

    def test_the_doc_fetch_survives_into_the_episode(self):
        """The step the procedure exists for. It was banked as its own orphan.

        A captured skill that says "add an eval case" without "read the
        framework docs first" is a different and worse procedure.
        """
        steps = [self._prompt("add an eval case"),
                 self._mcp("adk-docs__fetch_docs"),
                 {"tool": "Edit", "input": {"file_path": "eval/cases.json"}, "failed": False},
                 self._prompt("commit it"),
                 bash("git commit -m 'add case'")]
        kept = [s for e in segment(steps) for s in e.steps]
        self.assertTrue(any(s.get("tool", "").startswith("mcp__adk-docs")
                            for s in kept))

    def test_two_committed_tasks_stay_separate(self):
        """Absorb must only claim work that never concluded on its own."""
        steps = [self._prompt("fix the bug"), bash("python3 -c 'edit'"),
                 bash("git commit -m 'fix'"),
                 self._prompt("now bump the CI image"), bash("python3 -c 'edit ci'"),
                 bash("git commit -m 'ci'")]
        self.assertEqual(len(segment(steps)), 2)

    def test_work_after_a_verdict_keeps_its_own_boundary(self):
        """Trailing work is its own episode — the ending preceded it.

        This used to assert that `_absorb_before_commit` spared the trailing
        investigation. That pass is gone, and the two episodes now come from the
        verdict on the commit alone, with nothing re-merging them afterwards.
        """
        steps = [self._prompt("fix the bug"), bash("python3 -c 'edit'"),
                 bash("git commit -m 'fix'"),
                 self._prompt("why is prod slow?"), bash("kubectl get pods"),
                 bash("kubectl logs api")]
        episodes = segment(steps)
        self.assertEqual(len(episodes), 2)
        self.assertEqual(episodes[0].ended_by, "judged")
        self.assertEqual(episodes[1].ended_by, "session-end")

    def test_a_commit_made_with_git_dash_c_is_still_a_commit(self):
        """`git -C <path> commit` — how an agent commits without cd-ing first.

        Found on a real session that committed and was recorded as if it never
        had: `-C` is a flag, the subcommand scan stops at the first flag, so the
        command fingerprinted as a bare `git` and matched no marker.
        """
        from skillpp.segment import is_marker
        for form in ("git -C /repo commit -m x",
                     "git -c user.email=a@b.com commit -m x",
                     "git --git-dir=/r/.git commit -m x"):
            self.assertTrue(is_marker(bash(form)), form)

    def test_inspecting_with_git_dash_c_is_still_read_only(self):
        """The same blind spot, on the other side: 76 real calls counted as work."""
        from skillpp.segment import is_read_only
        for form in ("git -C /repo status", "git --no-pager log", "git -C /r diff"):
            self.assertTrue(is_read_only(bash(form)), form)

    def test_flags_after_a_subcommand_still_end_the_scan(self):
        """The guard: this is what keeps two runs of one test suite alike."""
        from skillpp.normalize import normalize_command
        self.assertEqual(normalize_command("pytest -k auth"), "pytest")
        self.assertEqual(normalize_command("pytest -k billing"), "pytest")
        self.assertEqual(normalize_command("npm run test -- --watch"), "npm run")

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

    def test_a_new_prompt_no_longer_ends_the_preceding_work(self):
        """Deliberately the opposite of what this file asserted before.

        A new prompt used to close the preceding episode once two substantive
        steps had happened. It was a proxy for "the last thing must have
        finished", and a poor one: it cuts on a step count, which measures that
        work happened, not that a goal ended. Measured on the live sessions it
        severed a deliverable from the investigation that produced it twice
        (`5c7b0f81`, `fb505861`) and a doc retrieval from the comparison it fed
        (`95b6bde7`).

        Only a verdict cuts now. Nothing here says a task ended, so this is one
        episode.
        """
        episodes = segment([self._prompt("task one"), bash("npm test"),
                            bash("./deploy.sh staging"),
                            self._prompt("task two"), bash("npm outdated"),
                            bash("npm view pkg")])
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].ended_by, "session-end")

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

    def test_a_session_that_only_looked_around_is_kept(self):
        """Deliberately the opposite of what this file asserted before.

        Reading was flagged as containing no method, on the strength of one
        hand-authored benchmark case. A real session, `95b6bde7`, then lost the
        MCP retrieval its procedure exists for to that rule. Looking things up
        in the right order is a method; noise is filtered by the recurrence
        threshold, which a one-off never reaches, rather than by guessing from
        the tool names that nothing happened.
        """
        episodes = segment([bash("kubectl logs api"), bash("kubectl top pods")])
        self.assertEqual(len(episodes), 1)
        self.assertFalse(episodes[0].flagged)


def _lb(command, failed=False):
    return {"tool": "Bash", "input": {"command": command}, "failed": failed}


def _pr(text):
    return {"tool": "UserPrompt", "input": {"text": text}, "failed": False}


class TestEpisodeSampler(unittest.TestCase):
    """The instrument that measures the pipeline, not the pipeline."""

    def _mod(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmarks"))
        import boundaries
        return boundaries

    def test_the_age_filter_excludes_recent_transcripts(self):
        """Fourteen of the fifteen transcripts every threshold was tuned
        against are under two weeks old, so the gap is what keeps the yardstick
        away from the material that shaped it."""
        import os, time
        b = self._mod()
        pool = b.eligible_transcripts(older_than_days=14)
        cutoff = time.time() - 14 * 86400
        for t in pool:
            self.assertLess(os.path.getmtime(t["path"]), cutoff, t["tag"])

    def test_sessions_are_split_on_bash_share_not_mcp_share(self):
        """MCP share files the Stitch design sessions as command-heavy — they
        sit at 8% — which would rebuild the dev-heavy sample this avoids."""
        b = self._mod()
        self.assertEqual(b.COMMAND_LEANING_AT, 0.55)
        for t in b.eligible_transcripts(older_than_days=14):
            expected = "command" if t["bash_share"] >= 0.55 else "document"
            self.assertEqual(t["kind"], expected, t["tag"])

    def test_a_draft_is_never_a_verdict(self):
        b = self._mod()
        labels = b.draft_labels({"steps": [bash("npm ci"), bash("npm test")],
                                 "evidence": "Committed as 0728ac0, tree clean."})
        self.assertIsNone(labels["one_task"]["verdict"])
        self.assertIsNone(labels["method"]["verdict"])
        self.assertIn(labels["confidence"], ("high", "low"))

    def test_scoring_refuses_a_batch_of_unadjudicated_drafts(self):
        """Scoring a draft would be scoring my own guess and calling it truth."""
        import json, tempfile
        b = self._mod()
        path = Path(tempfile.mkdtemp()) / "batch.json"
        path.write_text(json.dumps({"episodes": [
            {"id": "x-1", "split": "tuning", "steps": ["$ npm ci"], "evidence": "",
             "labels": {"one_task": {"draft": True, "verdict": None},
                        "method": {"draft": True, "verdict": None},
                        "confidence": "low", "why": ""}}]}))
        with self.assertRaises(SystemExit) as caught:
            b.load_batch(str(path))
        self.assertIn("draft", str(caught.exception))

    def test_an_adjudicated_batch_loads(self):
        import json, tempfile
        b = self._mod()
        path = Path(tempfile.mkdtemp()) / "batch.json"
        path.write_text(json.dumps({"episodes": [
            {"id": "x-1", "split": "tuning", "steps": ["$ npm ci"], "evidence": "",
             "labels": {"one_task": {"draft": True, "verdict": True},
                        "method": {"draft": True, "verdict": False},
                        "confidence": "high", "why": ""}}]}))
        self.assertEqual(len(b.load_batch(str(path))["episodes"]), 1)


class TestReadThatFeedsAWrite(unittest.TestCase):
    """`_NOISE_TOOLS` dropped every `Read`. Measured over 621 real ones, 38.5%
    are immediately followed by a write to the same file and only 18.2% sit in
    a run of reads — so the rule discarded twice as much procedure input as
    exploration, and the step it discarded named the file being operated on."""

    def _keep(self, steps):
        from skillpp.capture import _substantive
        return [s["tool"] for s in _substantive(steps)]

    def _read(self, path):
        return {"tool": "Read", "input": {"file_path": path}, "failed": False}

    def _edit(self, path):
        return {"tool": "Edit", "input": {"file_path": path}, "failed": False}

    def test_a_read_that_feeds_an_edit_of_the_same_file_survives(self):
        self.assertEqual(self._keep([self._read("/a.py"), self._edit("/a.py")]),
                         ["Read", "Edit"])

    def test_a_read_of_a_different_file_is_still_dropped(self):
        self.assertEqual(self._keep([self._read("/a.py"), self._edit("/b.py")]),
                         ["Edit"])

    def test_a_run_of_reads_is_still_dropped(self):
        self.assertEqual(
            self._keep([self._read("/a.py"), self._read("/b.py"), bash("npm test")]),
            ["Bash"])

    def test_the_other_noise_tools_are_untouched(self):
        for tool in ("Grep", "Glob", "TodoWrite", "Task", "WebFetch", "WebSearch"):
            self.assertEqual(
                self._keep([{"tool": tool, "input": {}, "failed": False},
                            bash("npm test")]), ["Bash"], tool)

    def test_a_read_with_no_path_is_dropped(self):
        self.assertEqual(self._keep([{"tool": "Read", "input": {}, "failed": False},
                                     bash("npm test")]), ["Bash"])


class TestReadsAndRetrievals(unittest.TestCase):
    """What counts as looking, and what the trimmer is allowed to drop."""

    def test_the_read_tools_are_read_only(self):
        """Their absence meant the pure-exploration flag could never fire on an
        episode containing a `Read` — which is most of them."""
        from skillpp.segment import is_read_only
        for tool in ("Read", "Glob", "Grep", "WebFetch", "WebSearch"):
            self.assertTrue(is_read_only({"tool": tool, "input": {}}), tool)
        self.assertFalse(is_read_only({"tool": "Write", "input": {}}))
        self.assertFalse(is_read_only({"tool": "Edit", "input": {}}))

    def test_an_mcp_retrieval_still_counts_as_looking(self):
        """`is_read_only` is unchanged for MCP: the flagging rule needs it."""
        from skillpp.segment import is_read_only
        self.assertTrue(is_read_only({"tool": "mcp__Drive__search_files", "input": {}}))
        self.assertFalse(is_read_only({"tool": "mcp__Slack__post_message", "input": {}}))

    def test_the_trimmer_keeps_a_leading_mcp_retrieval(self):
        """Fetching the material is step one of the method, not the search that
        found it. Trimming left `meeting-prep` as two steps of five."""
        from skillpp.segment import trim_leading_exploration
        steps = [{"tool": "mcp__Calendar__get_event", "input": {}, "failed": False},
                 {"tool": "mcp__Drive__search_files", "input": {}, "failed": False},
                 {"tool": "Write", "input": {"file_path": "/tmp/a.md"}, "failed": False},
                 {"tool": "mcp__Slack__post_message", "input": {}, "failed": False}]
        kept, cut = trim_leading_exploration(steps, 2)
        self.assertEqual(cut, 0)
        self.assertEqual(len(kept), 4)

    def test_the_trimmer_still_drops_leading_greps(self):
        from skillpp.segment import trim_leading_exploration
        steps = [bash("grep -rn export src/"), bash("grep -rn timeout src/"),
                 {"tool": "Edit", "input": {"file_path": "/a.py"}, "failed": False},
                 bash("pytest")]
        kept, cut = trim_leading_exploration(steps, 2)
        self.assertEqual(cut, 2)
        self.assertEqual([s["tool"] for s in kept], ["Edit", "Bash"])

    def test_a_read_only_run_is_absorbed_into_the_work_it_precedes(self):
        """`95b6bde7`: retrieval, then a prompt, then the work it informs.

        The retrieval used to land in its own episode, which was flagged for
        being all reads and dropped — so the session banked the comparison with
        none of the material it compared.
        """
        steps = [{"tool": "UserPrompt", "input": {"text": "look up the docs"}, "failed": False},
                 {"tool": "mcp__adk-docs__list_doc_sources", "input": {}, "failed": False},
                 {"tool": "mcp__adk-docs__fetch_docs", "input": {"url": "a"}, "failed": False},
                 {"tool": "mcp__adk-docs__fetch_docs", "input": {"url": "b"}, "failed": False},
                 {"tool": "UserPrompt", "input": {"text": "now compare"}, "failed": False},
                 {"tool": "Edit", "input": {"file_path": "/a.py"}, "failed": False},
                 bash("pytest")]
        episodes = segment(steps, 2)
        self.assertEqual(len(episodes), 1)
        self.assertFalse(episodes[0].flagged)
        tools = [s["tool"] for s in episodes[0].steps]
        self.assertIn("mcp__adk-docs__list_doc_sources", tools)
        self.assertEqual(tools.count("mcp__adk-docs__fetch_docs"), 2)

    def test_reading_that_ends_a_session_is_still_flagged(self):
        """Absorbing is forward only. Nothing follows this, so it concluded

        nothing — the trailing rule, which the absorb pass does not replace.
        """
        steps = [{"tool": "UserPrompt", "input": {"text": "ship it"}, "failed": False},
                 {"tool": "Edit", "input": {"file_path": "/a.py"}, "failed": False},
                 bash("git commit -m 'x'"),
                 {"tool": "UserPrompt", "input": {"text": "catch me up"}, "failed": False},
                 {"tool": "mcp__Drive__search_files", "input": {}, "failed": False},
                 {"tool": "Read", "input": {"file_path": "/tmp/a.md"}, "failed": False},
                 {"tool": "mcp__Slack__search_messages", "input": {}, "failed": False}]
        episodes = segment(steps, 2)
        self.assertTrue(episodes[-1].flagged)


class TestStripScaffolding(unittest.TestCase):
    """Inputs here are real commands, sampled from transcripts.

    Deliberately not taken from the benchmark corpus: the strip rules were
    calibrated against 4,853 real Bash calls, and testing them against fixtures
    written by the same hand that wrote the rules measures nothing.
    """

    def _strip(self, cmd):
        from skillpp.normalize import strip_scaffolding
        return strip_scaffolding(cmd)

    def test_leading_cd_and_ampersands(self):
        self.assertEqual(
            self._strip("cd /Users/dev/ai_projects/skill-plus-plus && sed -n '1,40p' skillpp/cli.py"),
            "sed -n '1,40p' skillpp/cli.py")

    def test_bare_cd_on_its_own_line(self):
        """17% of real commands, and the newline hides it from the `&&` form."""
        self.assertEqual(self._strip("cd /repo\ngit status --short"),
                         "git status --short")

    def test_echo_markers_and_pagers_and_redirects(self):
        self.assertEqual(
            self._strip('python3 -m unittest discover -s tests -q 2>&1 | tail -3'),
            "python3 -m unittest discover -s tests -q")
        self.assertEqual(self._strip('echo "=== hooks ===" ; grep -n hook cli.py'),
                         "grep -n hook cli.py")

    def test_leading_variable_and_dev_null_and_or_true(self):
        self.assertEqual(self._strip('S=/tmp/cc.txt; grep -oE "agent" "$S" | sort -u | head -6'),
                         'grep -oE "agent" "$S" | sort -u')
        self.assertEqual(self._strip("./examples/demo.sh >/dev/null 2>&1"),
                         "./examples/demo.sh")
        self.assertEqual(self._strip("npm ci || true"), "npm ci")

    def test_a_clean_command_is_untouched(self):
        for cmd in ("kubectl scale deploy/api --replicas=0 -n staging",
                    "git commit -am 'fix: guard empty cart'",
                    "pytest tests/test_auth.py"):
            self.assertEqual(self._strip(cmd), cmd)

    def test_a_command_that_is_all_scaffolding_survives(self):
        """Better to show noise than to show nothing."""
        self.assertTrue(self._strip('cd /repo && echo "hi"'))

    def test_stripping_never_reaches_a_signature(self):
        """The guard. Stripping *would* move fingerprints — `cd /r && npm test`
        shapes as `bash:cd` raw and `bash:npm test` stripped — so `step_shape`
        must keep reading the raw command and this must stay rendering-only."""
        from skillpp.normalize import signature, strip_scaffolding
        raw = [bash("cd /r && npm test 2>&1 | tail -5"), bash("cd /r && git commit -m x")]
        stripped = [bash(strip_scaffolding(s["input"]["command"])) for s in raw]
        self.assertEqual(signature(raw), "bash:cd")
        self.assertNotEqual(signature(raw), signature(stripped))

    def test_describe_step_still_emits_a_runnable_command(self):
        """`render_step` feeds a model; `describe_step` writes the SKILL.md a
        person runs. A stripped command there would not work."""
        from skillpp.ledger import describe_step
        step = {"tool": "Bash", "failed": False,
                "input": {"command": "cd /repo && npm ci 2>&1 | tail -25",
                          "description": "Install pinned dependencies"}}
        self.assertIn("cd /repo", describe_step(step))
        self.assertIn("tail -25", describe_step(step))

    def test_render_step_shows_the_stripped_command(self):
        from skillpp.episode import render_step
        step = {"tool": "Bash", "failed": False,
                "input": {"command": 'echo "=== npm ===" \ncd /Users/dev/acme && npm ci 2>&1 | tail -25',
                          "description": "Install pinned dependencies"}}
        out = render_step(step)
        self.assertIn("Install pinned dependencies", out)
        self.assertIn("npm ci", out)
        self.assertNotIn("tail -25", out)
        self.assertNotIn("=== npm ===", out)


class TestCorpusRealism(unittest.TestCase):
    """The benchmark corpus must keep the shape of real work.

    Measured over 15 real transcripts: 92% of Bash commands are chained, 52%
    are multiline, 19% carry a heredoc, median length 275 chars. The corpus was
    at 2% / 0% / 0% / 22 — which is why it scored 95% recall while the same
    pipeline showed no discrimination at all on real sessions, and why it could
    not see that `is_read_only` misses every chained read.

    Floors rather than exact targets, so the corpus can grow without this
    becoming noise. Free: no model, no fixtures.
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmarks"))
        from cases import CASES
        cls.cmds = [row[1] for c in CASES
                    for script in (c.script, c.follow or [])
                    for row in script if row[0] == "Bash"]

    def _share(self, predicate):
        return sum(1 for c in self.cmds if predicate(c)) / len(self.cmds)

    def test_commands_are_chained_like_real_ones(self):
        share = self._share(lambda c: "&&" in c or ";" in c or "|" in c)
        self.assertGreaterEqual(share, 0.85, f"only {share:.0%} chained; real is 92%")

    def test_commands_are_multiline_like_real_ones(self):
        share = self._share(lambda c: "\n" in c)
        self.assertGreaterEqual(share, 0.40, f"only {share:.0%} multiline; real is 52%")

    def test_some_commands_carry_a_heredoc(self):
        """Floor well under the real 19% on purpose.

        A heredoc *is* the command in 95% of real cases, so only commands that
        genuinely read one get one — here that is `git commit -F-` and `psql`.
        This corpus is git/npm/kubectl where real work is python3-heavy, and an
        earlier pass that forced the real rate did it by bolting an unrelated
        file-inspecting heredoc onto whatever command was already there. That
        made `git clone` arrive trailed by 250 characters of unrelated Python
        and cost a benchmark case. Inflating the number is worse than missing it.
        """
        share = self._share(lambda c: "<<" in c)
        self.assertGreaterEqual(share, 0.04, f"only {share:.0%} heredoc")

    def test_commands_are_long_like_real_ones(self):
        """Also under the real 275. Real commands are long because their paths
        and pipelines are long; padding these to match would be measuring the
        padding."""
        median = sorted(len(c) for c in self.cmds)[len(self.cmds) // 2]
        self.assertGreaterEqual(median, 100, f"median {median} chars; real is 275")


class TestChainedMarkers(unittest.TestCase):
    """17 real commits produced 0 markers, because only the head was read."""

    def test_a_chained_commit_is_a_marker(self):
        from skillpp.segment import is_marker
        self.assertTrue(is_marker(_lb("cd /repo && git add -A && git commit -m x")))

    def test_a_heredoc_commit_is_a_marker(self):
        from skillpp.segment import is_marker
        self.assertTrue(is_marker(_lb(
            "git add -A backend/ && git commit -q -F- <<'EOF' && git log --oneline -1")))

    def test_a_failed_chained_commit_is_not_a_marker(self):
        """A commit a pre-commit hook rejected means the task is still running."""
        from skillpp.segment import is_marker
        self.assertFalse(is_marker(
            _lb("cd /repo && git commit -m x", failed=True)))

    def test_a_chain_of_reads_is_not_a_marker(self):
        from skillpp.segment import is_marker
        self.assertFalse(is_marker(_lb("cd /repo && ls -la && cat README.md")))


class TestNarrationIsAttributedToTheRightStep(TempRoot):
    """A task's completion report belongs to the task that finished.

    `_narration` collected everything said between two tool calls and gave it
    all to the second one. When a prompt fell in that gap, the first half was
    the *previous* task's conclusion and it was filed under the next task's
    first step. Measured on `241955c7`, three times in one 24-step session.
    """

    def _transcript(self, rows):
        path = self.root / "transcript.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
        return {"transcript_path": str(path)}

    def _assistant(self, *blocks):
        return {"type": "assistant", "message": {"content": list(blocks)}}

    def _text(self, body):
        return {"type": "text", "text": body}

    def _call(self, name="Bash"):
        return {"type": "tool_use", "name": name, "input": {}}

    def _prompt_row(self, body):
        return {"type": "user", "message": {"content": body}}

    def test_text_between_two_calls_leads_in_to_the_second(self):
        from skillpp.capture import _narration
        payload = self._transcript([
            self._assistant(self._call()),
            self._assistant(self._text("now check the other file"), self._call()),
        ])
        lead_in, closes_previous = _narration(payload)
        self.assertEqual(lead_in, "now check the other file")
        self.assertEqual(closes_previous, "")

    def test_a_prompt_in_the_gap_sends_the_first_half_backwards(self):
        """The shape this fixes: report, new prompt, next task's first call."""
        from skillpp.capture import _narration
        payload = self._transcript([
            self._assistant(self._call()),
            self._assistant(self._text("Scan done. All 4 resolve to keys.")),
            self._prompt_row("Separate job: add the missing case"),
            self._assistant(self._text("starting on that now"), self._call()),
        ])
        lead_in, closes_previous = _narration(payload)
        self.assertEqual(closes_previous, "Scan done. All 4 resolve to keys.")
        self.assertEqual(lead_in, "starting on that now")

    def test_the_report_lands_on_the_step_it_describes(self):
        """End to end through the hook, not just the parser."""
        from skillpp.capture import handle_prompt, handle_tool
        self.config.describe_steps = False
        handle_prompt(self.config, {"session_id": "s", "cwd": "/r",
                                    "prompt": "work out which ones"})
        rows = [self._assistant(self._call())]
        handle_tool(self.config, {"session_id": "s", "cwd": "/r",
                                  "tool_name": "Read",
                                  "tool_input": {"file_path": "/book.py"},
                                  **self._transcript(rows)})
        rows += [self._assistant(self._text("Scan done. All 4 resolve.")),
                 self._prompt_row("Separate job: add the case")]
        handle_prompt(self.config, {"session_id": "s", "cwd": "/r",
                                    "prompt": "Separate job: add the case"})
        rows += [self._assistant(self._call())]
        handle_tool(self.config, {"session_id": "s", "cwd": "/r",
                                  "tool_name": "Bash",
                                  "tool_input": {"command": "grep -n atlas"},
                                  **self._transcript(rows)})

        from skillpp.capture import _load_session
        steps = [s for s in _load_session(self.config, "s")["steps"]
                 if not is_prompt(s)]
        self.assertEqual(steps[0]["tool"], "Read")
        self.assertTrue(steps[0]["closing_note"].startswith("Scan done"),
                        "the report belongs to the step that finished the task")
        self.assertNotIn("Scan done", steps[1].get("assistant_note", ""))

    def test_a_missing_transcript_costs_the_note_and_nothing_else(self):
        from skillpp.capture import _narration
        self.assertEqual(_narration({}), ("", ""))
        self.assertEqual(_narration({"transcript_path": "/no/such/file"}), ("", ""))

    def test_the_live_session_carries_its_investigation_report(self):
        """`241955c7`, the session this was found on."""
        import glob
        doc = json.loads(Path(glob.glob(
            "tests/fixtures/sessions/241955c7*.json")[0]).read_text())
        work = [s for s in doc["steps"] if not is_prompt(s)]
        last_of_task_one = work[5]           # the Read that ends the investigation
        self.assertEqual(last_of_task_one["tool"], "Read")
        self.assertTrue(last_of_task_one["closing_note"].startswith("Scan done"))
        self.assertNotIn("Scan done", work[6].get("assistant_note", ""))


class TestOfflineWithoutAModel(TempRoot):
    """No verdicts, no candidates — and no lost work.

    `segment` used to fall back to a vocabulary when nothing judged the steps:
    cut at every new prompt following two substantive steps, and at every git
    completion verb. Measured across the eleven live sessions that fallback
    scored 7/11, against 9/11 for making no cuts at all and 10/11 for the judge.
    It is not a degraded mode of the judge, it is a different and worse product.
    """

    def _unjudged(self):
        return {"session_id": "off", "cwd": "/proj", "prompts": ["do it"],
                "steps": [bash("npm test"), bash("git commit -m 'x'"),
                          bash("npm outdated"), bash("npm view pkg")]}

    def test_segment_cuts_nothing_without_verdicts(self):
        self.assertEqual(_real_segment(self._unjudged()["steps"]), [])

    def test_the_same_steps_segment_once_judged(self):
        """The steps are not the problem — the missing verdicts are."""
        self.assertEqual(len(segment(self._unjudged()["steps"])), 2)

    def test_folding_reports_offline_rather_than_an_empty_success(self):
        result = fold_session(self.config, self._unjudged())
        self.assertEqual(result["status"], "offline")
        self.assertEqual(Ledger(self.config).stats()["total"], 0)

    def test_force_still_banks_for_a_person_who_asked(self):
        """`force` is someone saying "save this", not a detector guessing."""
        result = fold_session(self.config, self._unjudged(), force=True)
        self.assertNotEqual(result["status"], "offline")

    def test_the_session_file_survives_being_offline(self):
        """Being offline costs the candidate, never the record.

        The file is the only copy of the work, and `handle_session_end` unlinked
        it unconditionally before this. Stamped `held` so `skillpp stats` can
        tell it apart from a session still being written.
        """
        from skillpp.capture import (_session_file, handle_prompt,
                                     handle_session_end, handle_tool)
        self._stub_judge(None)                  # the model is not reachable
        handle_prompt(self.config, {"session_id": "off", "cwd": "/r",
                                    "prompt": "cut the release"})
        for command in ("npm test", "git commit -m 'x'"):
            handle_tool(self.config, {"session_id": "off", "cwd": "/r",
                                      "tool_name": "Bash",
                                      "tool_input": {"command": command}})
        result = handle_session_end(self.config, {"session_id": "off"})

        self.assertEqual(result["status"], "offline")
        path = _session_file(self.config, "off")
        self.assertTrue(path.exists(), "the only copy of the work was deleted")
        held = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(held["held"]["at"])
        self.assertEqual(len(held["steps"]), 3)

    def test_a_live_session_is_not_reported_as_held(self):
        """A session still being written has a file too."""
        from skillpp.capture import handle_prompt
        handle_prompt(self.config, {"session_id": "live", "cwd": "/r",
                                    "prompt": "still going"})
        files = list(self.config.sessions_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn(
            "held", json.loads(files[0].read_text(encoding="utf-8")))


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

    @unittest.expectedFailure
    def test_after_the_repeated_workflow_reaches_the_threshold(self):
        """Known failure, and left visible. The cause has moved twice.

        `s2` is "ship the api build" — which completes at
        `./scripts/deploy.sh staging`, not at a commit — followed by an
        unrelated "add the release notes" that does commit. So the clean deploy
        signature appears in two sessions instead of three and never reaches the
        threshold.

        It used to fail because `_absorb_before_commit` folded the deploy into
        the commit that followed. That pass is gone — it never fired once across
        the eleven live sessions and no test depended on it. Now it fails
        because nothing separates them at all: `./deploy.sh` is not a completion
        marker, only a verdict can say a deploy finished, and this suite's
        stand-in judge is the marker vocabulary, which by design cannot.

        Same open shape as the live session `241955c7` and the `two-chores-*`
        cases. Marked expected rather than deleted, and rather than teaching the
        stand-in to recognise `deploy.sh` — that would be tuning the double
        until the test passes. This fixture is hand-authored, and no real
        captured session has yet shown a task completing without a commit and
        being followed by one.
        """
        self._fold_segmented(to_captured_session)
        deploys = [e for e in Ledger(self.config).all()
                   if e.signature == self.CLEAN_SIGNATURE]
        self.assertEqual(len(deploys), 1, "the deploy must be one entry, not three")
        self.assertEqual(deploys[0].occurrences, EXPECTED_OCCURRENCES)
        self.assertTrue(deploys[0].ready(self.config.recurrence_threshold))

    # Both of the following need a verdict that does not exist yet.
    #
    # The deploy ends in `./deploy.sh`, which is not a completion marker — no
    # git verb, no artifact tool. The prompt rule used to close the episode
    # there, and it was deleted for closing episodes wrongly on three live
    # sessions. With nothing cutting, the deploy merges into whatever follows
    # and `CLEAN_SIGNATURE` never appears.
    #
    # `_absorb_before_commit` recorded this shape as a known cost before it was
    # deleted — "a task that completes without committing, a deploy ending in
    # ./scripts/deploy.sh, is absorbed into whatever commits next". It is now
    # not merely absorbed but never separated. Same open problem as the live
    # session `241955c7` and the `two-chores-*` cases: only a model can say a
    # deploy finished, and this suite's stand-in judge is the marker vocabulary,
    # which by design cannot.
    #
    # Marked expected rather than deleted, and rather than teaching the stand-in
    # to recognise `deploy.sh` — that would be tuning the double until the test
    # passes, which measures nothing. This fixture is hand-authored and no real
    # captured session has yet shown the shape.

    @unittest.expectedFailure
    def test_after_the_signature_matches_the_unpolluted_baseline(self):
        """Pollution must leave no trace in the recovered workflow."""
        self._fold_segmented(to_captured_session)
        signatures = {e.signature for e in Ledger(self.config).all()}
        self.assertIn(self.CLEAN_SIGNATURE, signatures)

    @unittest.expectedFailure
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

    def test_render_no_longer_truncates_a_giant_step(self):
        """The cap used to guillotine heredocs, which is where the meaning is."""
        from skillpp.episode import render_step
        line = render_step({"tool": "Bash", "input": {"command": "x" * 5000}})
        self.assertIn("x" * 5000, line)
        self.assertNotIn("…", line)

    def test_render_collapses_whitespace_in_a_multiline_command(self):
        from skillpp.episode import render_step
        line = render_step({"tool": "Bash",
                            "input": {"command": "python3 - <<PY\nimport os\nPY"}})
        self.assertIn("python3 - <<PY import os PY", line)
        self.assertNotIn("\n", line)

    def test_render_leads_with_the_agents_description(self):
        """87% of captured Bash calls carry one; it is the purpose signal."""
        from skillpp.episode import render_step
        line = render_step({"tool": "Bash", "input": {
            "command": "uvx --from mcpdoc mcpdoc --help",
            "description": "Test if mcpdoc installs via uvx"}})
        first, second = line.split("\n")
        self.assertEqual(first, "$ Test if mcpdoc installs via uvx")
        self.assertIn("uvx --from mcpdoc", second)

    def test_render_omits_the_description_cleanly_when_absent(self):
        """6.8% of episodes have none. No placeholder, no stray blank line."""
        from skillpp.episode import render_step
        line = render_step({"tool": "Bash", "input": {"command": "npm test"}})
        self.assertEqual(line, "$ npm test")

    def test_render_marks_a_failed_step_that_has_a_description(self):
        """The `!` must survive the two-line path, or failure goes invisible."""
        from skillpp.episode import render_step
        line = render_step({"tool": "Bash", "failed": True, "input": {
            "command": "kubectl scale deploy/api --replicas=0",
            "description": "Drain replicas holding the lock"}})
        self.assertTrue(line.startswith("! Drain replicas"))

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

    # Cases the corpus fails because a *pipeline* rule is broken, not because
    # the ground truth is wrong. Both appeared the moment the corpus commands
    # were given the shape real ones have: `is_read_only` is anchored at the
    # start of a command, so `cd /repo && grep …` does not match it and pure
    # exploration stops being recognised as exploration. At the real 92%
    # chaining rate that rule is effectively dead. Deliberately left broken here
    # so the fix, when it lands, is attributable to the fix.
    # `a-procedure-then-a-dead-end` left this set when the prompt rule was
    # deleted: it was two episodes only because a prompt cut them, and the
    # chaining bug then swallowed the second. One episode is the right answer.
    CHAINED_READS = {"investigation-that-goes-nowhere"}

    def _score(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmarks"))
        from benchmarks.run import score
        from benchmarks.cases import CASES
        return score(CASES, use_model=False)

    # Two unrelated chores in one sitting, no marker between them. The prompt
    # rule used to cut here and was deleted for cutting wrongly on three live
    # sessions; nothing observable separates these halves, so only a verdict
    # can. Same open problem as `241955c7`.
    NEEDS_A_VERDICT = {"two-chores-one-sitting", "two-chores-then-nothing"}

    def test_every_case_segments_as_expected(self):
        """Excluding the two whose fix is a later stage, not the segmenter."""
        wrong = [f"{r['name']}: expected {r['expected_episodes']}, "
                 f"got {r['got_episodes']}"
                 for r in self._score()["rows"]
                 if not r["segmentation"]
                 and not self.DOWNSTREAM & set(r["tags"])
                 and r["name"] not in self.CHAINED_READS
                 and r["name"] not in self.NEEDS_A_VERDICT]
        self.assertEqual(wrong, [], "\n" + "\n".join(wrong))

    def test_the_known_gaps_are_still_exactly_the_known_gaps(self):
        """If segmentation starts handling one of these, this test says so.

        A suite that silently tolerates a documented gap cannot tell you when
        the gap closes, and a stale exclusion is how a benchmark quietly stops
        measuring.
        """
        failing = {r["name"] for r in self._score()["rows"]
                   if not r["segmentation"]}
        # Both release cases bank two entries where the truth is one, for the
        # same reason and at two different distances: lexical similarity cannot
        # see through a swapped tool. `different-runner` lands at 0.786, inside
        # the band `skillpp merge` already looks at; `two-steps-different`
        # lands at 0.531, under the floor of every band a live command can
        # afford, which is why the queued pass exists. Neither is a segmenter
        # bug — segmentation cut both sessions correctly.
        #
        # The other two are the `is_read_only` chaining bug described on
        # `CHAINED_READS` — a pipeline defect the corpus could not see until its
        # commands were shaped like real ones.
        #
        # `two-chores-*` joined when the prompt rule was deleted. Both are two
        # unrelated chores in one sitting with no completion marker between
        # them, so nothing observable separates the halves and only a verdict
        # can. That is the same open problem as the live session `241955c7`,
        # whose eighth prompt begins "Separate job:" and which E4B still reads
        # as one task across all 24 steps. Recorded here rather than repaired by
        # bringing the prompt rule back: it cut correctly on these two and
        # wrongly on three live sessions.
        expected = {"deploy-then-status-email",
                    "the-same-release-different-runner",
                    "the-same-release-two-steps-different",
                    "two-chores-one-sitting",
                    "two-chores-then-nothing"} | self.CHAINED_READS
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
        everything scores perfectly.

        Was two `episodes == 0` cases; `reading-around` moved to one when the
        all-read-only flag was deleted, because reading is no longer taken as
        evidence that nothing happened. Only one shape banks nothing now — work
        that concluded nothing *and* ended the session — so one is what the
        corpus can honestly hold, and raising it back would mean inventing a
        case rather than recording one.

        The property this guards is unweakened: five cases still expect a banked
        episode to yield no method, which is what a bank-everything detector
        fails on.
        """
        sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmarks"))
        from benchmarks.cases import CASES
        self.assertGreaterEqual(sum(1 for c in CASES if c.episodes == 0), 1)
        self.assertGreaterEqual(sum(1 for c in CASES if c.methods == 0), 3)


class TestOccurrencesCountSessionsInCaptureToo(TempRoot):
    """The rule `fold_into` states, enforced on the path that actually banks.

    `test_occurrences_count_sessions_not_sightings` pinned this for the merge
    path and the capture path kept incrementing per fold. Segmentation is where
    that diverges: one session becomes several episodes, several of them match
    the same entry, and each bump landed on a counter documented as counting
    *distinct sessions*. Found on 76 real sessions — 25 of 467 entries claimed
    more occurrences than sessions, worst x154 against 17.
    """

    DEPLOY = ["docker build -t api .", "docker push api",
              "kubectl set image deploy/api api=api", "git commit -am deploy"]

    def _run(self, session_id, rounds=1):
        from skillpp.capture import (handle_prompt, handle_session_end,
                                     handle_tool)
        for n in range(rounds):
            handle_prompt(self.config, {"session_id": session_id, "cwd": "/w",
                                        "prompt": f"deploy the api ({n})"})
            for cmd in self.DEPLOY:
                handle_tool(self.config, {"session_id": session_id, "cwd": "/w",
                                          "tool_name": "Bash",
                                          "tool_input": {"command": cmd}})
        return handle_session_end(self.config, {"session_id": session_id})

    def test_twice_in_one_session_is_one_occurrence(self):
        self._run("only-session", rounds=2)
        for entry in Ledger(self.config).all():
            self.assertEqual(entry.occurrences, len(entry.sessions))
            self.assertEqual(entry.occurrences, 1)

    def test_the_threshold_cannot_be_reached_inside_one_session(self):
        """The whole point of counting sessions rather than sightings."""
        self._run("only-session", rounds=5)
        for entry in Ledger(self.config).all():
            self.assertFalse(entry.ready(self.config.recurrence_threshold),
                             "one sitting cleared the recurrence threshold")

    def test_the_same_work_in_three_sessions_still_reaches_three(self):
        """The fix must not cost real recurrence, only the inflated kind."""
        for sid in ("s1", "s2", "s3"):
            self._run(sid)
        top = max(e.occurrences for e in Ledger(self.config).all())
        self.assertEqual(top, 3)

    def test_occurrences_never_exceed_distinct_sessions(self):
        """The invariant, stated as an invariant, over a mixed history."""
        self._run("s1", rounds=3)
        self._run("s2")
        self._run("s3", rounds=2)
        for entry in Ledger(self.config).all():
            self.assertLessEqual(entry.occurrences, len(entry.sessions),
                                 f"{entry.title[:40]} claims more occurrences "
                                 f"than sessions")


class TestTheQueuedNearMissPass(TempRoot):
    """The same check as `skillpp merge`, moved off the SessionEnd path.

    No model is contacted and no process is spawned: `embed` and
    `subprocess.Popen` are both replaced. What is pinned is that SessionEnd
    decides nothing, that SessionStart never waits, that the wider floor reaches
    the pairs the live command cannot, and that nothing folds without `--apply`.
    """

    # 0.531 lexically once parameterised — under `near_miss_floor`, over
    # `queued_near_miss_floor`. The whole point of the pass in one pair.
    RELEASE = ["git checkout main", "git pull --ff-only", "npm test",
               "npm version 2.4.0", "git tag -s v2.4.0 -m rel",
               "git push --follow-tags"]
    RELEASE_FAR = ["git checkout main", "git fetch --all", "pytest -q",
                   "npm version 2.5.0", "git tag -s v2.5.0 -m rel",
                   "git push --follow-tags"]

    def _bank(self, sid, prompt, cmds):
        from skillpp.capture import handle_prompt, handle_session_end, handle_tool
        handle_prompt(self.config, {"session_id": sid, "cwd": "/w",
                                    "prompt": prompt})
        for cmd in cmds:
            handle_tool(self.config, {"session_id": sid, "cwd": "/w",
                                      "tool_name": "Bash",
                                      "tool_input": {"command": cmd}})
        return handle_session_end(self.config, {"session_id": sid})

    def _two_releases(self):
        self._bank("s1", "cut the 2.4 release", self.RELEASE)
        self._bank("s2", "cut the 2.5 release", self.RELEASE_FAR)
        return list(Ledger(self.config).all())

    def _same_shape(self):
        """Every embedding identical, so cosine is 1.0 for any pair."""
        import skillpp.similar as sim
        real, sim.embed = sim.embed, lambda text, **kw: [1.0, 0.0, 0.0]
        self.addCleanup(lambda: setattr(sim, "embed", real))

    def _no_model(self):
        import skillpp.similar as sim
        from skillpp.local import LocalModelUnavailable

        def boom(text, **kw):
            raise LocalModelUnavailable("no daemon")
        real, sim.embed = sim.embed, boom
        self.addCleanup(lambda: setattr(sim, "embed", real))

    def _spy_popen(self):
        import subprocess
        calls = []

        class Fake:
            pid = 4242

            def wait(self, *a, **kw):
                raise AssertionError("SessionStart waited on the background pass")

            def communicate(self, *a, **kw):
                raise AssertionError("SessionStart waited on the background pass")

        def fake(argv, **kw):
            calls.append((argv, kw))
            return Fake()
        real, subprocess.Popen = subprocess.Popen, fake
        self.addCleanup(lambda: setattr(subprocess, "Popen", real))
        return calls

    # -- SessionEnd decides nothing -------------------------------------------

    def test_session_end_queues_the_entry_and_asks_no_model(self):
        self._no_model()   # any embedding call here fails the test loudly
        self._bank("s1", "cut the 2.4 release", self.RELEASE)
        rows = [json.loads(l) for l in
                self.config.pending_checks_file.read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["entry_id"])
        self.assertEqual(rows[0]["session_id"], "s1")

    def test_every_touched_entry_is_queued_including_a_merged_one(self):
        """A merged entry can still be a near-miss against a third."""
        self._bank("s1", "cut the 2.4 release", self.RELEASE)
        self._bank("s2", "cut the 2.5 release", self.RELEASE)   # merges lexically
        rows = [json.loads(l) for l in
                self.config.pending_checks_file.read_text().splitlines() if l.strip()]
        self.assertEqual(len(rows), 2, "a lexical merge was not queued")
        self.assertEqual({r["reason"] for r in rows}, {"created", "merged"})

    # -- SessionStart spawns and returns --------------------------------------

    def test_an_empty_queue_spawns_nothing(self):
        from skillpp.similar import maybe_spawn_background_check
        calls = self._spy_popen()
        self.assertEqual(maybe_spawn_background_check(self.config),
                         {"status": "empty"})
        self.assertEqual(calls, [])

    def test_a_queued_entry_spawns_one_detached_process_and_does_not_wait(self):
        from skillpp.similar import maybe_spawn_background_check
        self._bank("s1", "cut the 2.4 release", self.RELEASE)
        calls = self._spy_popen()
        result = maybe_spawn_background_check(self.config)
        self.assertEqual(result["status"], "spawned")
        self.assertEqual(len(calls), 1)
        argv, kw = calls[0]
        self.assertIn("background-merge-check", argv)
        self.assertIn(str(self.config.root), argv)
        self.assertTrue(kw["start_new_session"], "child stayed in the hook's group")
        # Fake.wait/communicate raise, so reaching here proves neither was called.

    def test_the_session_start_hook_prints_nothing_without_verbose(self):
        import io
        self._bank("s1", "cut the 2.4 release", self.RELEASE)
        self._spy_popen()
        from skillpp.cli import main
        out, err = io.StringIO(), io.StringIO()
        stdin, sys.stdin = sys.stdin, io.StringIO(json.dumps({"session_id": "x"}))
        real_out, sys.stdout = sys.stdout, out
        try:
            code = main(["--root", str(self.config.root), "hook",
                         "--event", "SessionStart"])
        finally:
            sys.stdin, sys.stdout = stdin, real_out
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "", "a session start printed to context")

    # -- the wider floor reaches what the live one cannot ---------------------

    def test_the_live_floor_misses_this_pair_and_the_queued_floor_does_not(self):
        from skillpp.similar import near_misses
        entries = self._two_releases()
        self.assertEqual(len(entries), 2, "these must not merge lexically")
        live = near_misses(entries, floor=self.config.near_miss_floor,
                           ceiling=self.config.similarity_threshold)
        queued = near_misses(entries, floor=self.config.queued_near_miss_floor,
                             ceiling=self.config.similarity_threshold)
        self.assertEqual(live, [], "`merge`'s own floor changed")
        self.assertEqual(len(queued), 1)
        self.assertLess(queued[0][2], self.config.near_miss_floor)

    def test_the_queued_pass_folds_and_drains_the_queue(self):
        from skillpp.similar import run_background_check
        self._two_releases()
        self._same_shape()
        result = run_background_check(self.config)
        self.assertEqual(result["merged"], 1)
        self.assertFalse(result["timed_out"])
        self.assertFalse(result["model_unreachable"])
        self.assertEqual(self.config.pending_checks_file.read_text(), "")

    def test_the_queued_pass_folds_by_itself(self):
        """No second command. The pass that finds it is the pass that folds it."""
        from skillpp.similar import run_background_check
        self._two_releases()
        self._same_shape()
        run_background_check(self.config)
        entries = list(Ledger(self.config).all())
        self.assertEqual(len(entries), 1)
        # Union, not sum: two sightings in one session are still one occurrence.
        self.assertEqual(entries[0].occurrences, 2)
        self.assertEqual(len(entries[0].sessions), 2)
        # The loser's evidence survives on the winner, which is what makes a
        # wrong fold visible at review rather than silent.
        self.assertEqual(len(entries[0].intents), 2)

    def test_an_auto_fold_is_recorded_in_decisions(self):
        """Once the dropped file is gone, this line is the only record of it."""
        from skillpp import decisions
        from skillpp.similar import AUTO_MERGED, run_background_check
        self._two_releases()
        dropped = {e.id for e in Ledger(self.config).all()}
        self._same_shape()
        run_background_check(self.config)
        rows = [r for r in decisions.read(self.config)
                if r["decision"] == AUTO_MERGED]
        self.assertEqual(len(rows), 1)
        survivor = list(Ledger(self.config).all())[0].id
        gone = (dropped - {survivor}).pop()
        self.assertIn(gone, rows[0]["note"])

    def test_an_auto_fold_does_not_count_as_a_ranker_judgement(self):
        """`skillpp accuracy` scores hint-against-person. A fold is neither."""
        from skillpp import decisions
        from skillpp.similar import run_background_check
        self._two_releases()
        self._same_shape()
        run_background_check(self.config)
        self.assertEqual(decisions.score(self.config)["scored"], 0)

    def test_one_merge_per_entry_per_pass(self):
        """Folds must not chain: a folded entry is gone and cannot absorb again."""
        from skillpp.similar import run_background_check
        self._bank("s1", "cut the 2.4 release", self.RELEASE)
        self._bank("s2", "cut the 2.5 release", self.RELEASE_FAR)
        self._bank("s3", "cut the 2.6 release",
                   ["git switch main", "git fetch --all", "pytest -q",
                    "npm version 2.6.0", "git tag -s v2.6.0 -m rel",
                    "git push --follow-tags"])
        before = len(list(Ledger(self.config).all()))
        self.assertGreaterEqual(before, 3, "fixture did not bank three entries")
        self._same_shape()   # every pair reads as the same procedure
        result = run_background_check(self.config)
        after = len(list(Ledger(self.config).all()))
        self.assertEqual(result["merged"], before - after)
        self.assertLess(result["merged"], before,
                        "folded every entry into nothing")

    # -- reading it, and the --apply gate ------------------------------------

    def test_an_entry_a_person_decided_is_never_folded(self):
        """Only candidates are compared, so a dismissal is not undone by a fold."""
        from skillpp.ledger import STATUS_DISMISSED
        from skillpp.similar import run_background_check
        self._two_releases()
        ledger = Ledger(self.config)
        victim = list(ledger.all())[0]
        victim.status = STATUS_DISMISSED
        ledger.save(victim)
        self._same_shape()
        result = run_background_check(self.config)
        self.assertEqual(result["merged"], 0)
        self.assertEqual(len(list(Ledger(self.config).all())), 2,
                         "folded an entry a person had already decided")

    # -- fail-safe -----------------------------------------------------------

    def test_an_unreachable_model_leaves_the_queue_and_the_ledger_alone(self):
        from skillpp.similar import run_background_check
        self._two_releases()
        before = self.config.pending_checks_file.read_text()
        self._no_model()
        result = run_background_check(self.config)
        self.assertTrue(result["model_unreachable"])
        self.assertEqual(result["merged"], 0)
        self.assertEqual(self.config.pending_checks_file.read_text(), before,
                         "a dead model drained the queue")
        self.assertEqual(len(list(Ledger(self.config).all())), 2)

    def test_a_timeout_keeps_the_whole_backlog(self):
        from skillpp.similar import run_background_check
        self._two_releases()
        before = self.config.pending_checks_file.read_text()
        self._same_shape()
        result = run_background_check(self.config, timeout=-1)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["pairs_checked"], 0)
        self.assertEqual(self.config.pending_checks_file.read_text(), before,
                         "a partial pass dropped what it never reached")

    def test_a_corrupt_queue_line_is_skipped_not_fatal(self):
        from skillpp.similar import run_background_check
        self._two_releases()
        with self.config.pending_checks_file.open("a") as fh:
            fh.write("{not json at all\n\n")
        self._same_shape()
        self.assertEqual(run_background_check(self.config)["merged"], 1)


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


class TestKeep(TempRoot):
    """Saving work without ending the session.

    `SessionEnd` is otherwise the only thing that folds, so there was no way to
    say "that thing I just did is worth keeping" while still working. That is a
    larger gap than it sounds: recurrence is the automatic route to a candidate
    and it has never fired on real work.
    """

    def _work(self, sid="live", cmds=(), prompt="fail over staging"):
        from skillpp.capture import handle_prompt, handle_tool
        handle_prompt(self.config, {"session_id": sid, "cwd": "/r",
                                    "prompt": prompt})
        for c in cmds:
            handle_tool(self.config, {"session_id": sid, "cwd": "/r",
                                      "tool_name": "Bash",
                                      "tool_input": {"command": c}})

    def test_it_banks_the_work_so_far(self):
        from skillpp.capture import keep_current
        self._work(cmds=("./scripts/failover.sh staging", "curl -sI https://staging"))
        result = keep_current(self.config)
        self.assertEqual(result.get("status"), "created")
        self.assertEqual(len(list(Ledger(self.config).all())), 1)

    def test_the_buffer_is_cleared_so_nothing_folds_twice(self):
        from skillpp.capture import keep_current
        self._work(cmds=("./scripts/failover.sh staging", "curl -sI https://staging"))
        keep_current(self.config)
        self.assertEqual(list(self.config.sessions_dir.glob("*.json")), [])

    def test_what_is_kept_is_marked_as_kept(self):
        """Provenance matters: this was asked for, not inferred."""
        from skillpp.capture import keep_current
        self._work(cmds=("./scripts/failover.sh staging", "curl -sI https://staging"))
        keep_current(self.config)
        self.assertEqual([e.source for e in Ledger(self.config).all()], ["kept"])

    def test_an_explicit_keep_overrides_the_guards(self):
        """A guard exists to stop a detector banking noise, not to overrule a
        person who has read the work and asked for it."""
        from skillpp.capture import keep_current
        # Read-only throughout: at SessionEnd this is discarded as exploration.
        self._work(cmds=("git log --oneline -5", "git diff", "cat README.md"))
        self.assertEqual(keep_current(self.config).get("status"), "created")

    def test_no_session_is_reported_not_guessed(self):
        from skillpp.capture import keep_current
        self.assertEqual(keep_current(self.config)["status"], "no-session")

    def test_an_empty_buffer_is_not_a_candidate(self):
        from skillpp.capture import keep_current
        from skillpp.capture import handle_prompt
        handle_prompt(self.config, {"session_id": "live", "cwd": "/r",
                                    "prompt": "thinking about it"})
        self.assertEqual(keep_current(self.config)["status"], "nothing-yet")


class TestReconcile(TempRoot):
    """Reporting drift, and never acting on it."""

    def _promoted(self, eid, path):
        from skillpp.ledger import STATUS_PROMOTED
        entry = Entry(id=eid, signature="s", title=f"skill {eid}",
                      status=STATUS_PROMOTED, skill_path=str(path))
        Ledger(self.config).save(entry)
        return entry

    def test_a_live_skill_is_not_drift(self):
        from skillpp.lifecycle import reconcile
        alive = self.root / "SKILL.md"
        alive.write_text("---\nname: x\n---\n")
        self._promoted("a", alive)
        result = reconcile(Ledger(self.config), self.config)
        self.assertEqual((result["live"], result["missing"]), (1, []))

    def test_a_deleted_skill_is_reported(self):
        from skillpp.lifecycle import reconcile
        self._promoted("a", self.root / "gone" / "SKILL.md")
        result = reconcile(Ledger(self.config), self.config)
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(result["missing"][0]["id"], "a")

    def test_reporting_never_changes_status(self):
        """Reopening would second-guess a deletion that was almost certainly
        deliberate, and re-propose the same workflow on every decline."""
        from skillpp.ledger import STATUS_PROMOTED
        from skillpp.lifecycle import reconcile
        self._promoted("a", self.root / "gone" / "SKILL.md")
        reconcile(Ledger(self.config), self.config)
        self.assertEqual(Ledger(self.config).get("a").status, STATUS_PROMOTED)

    def test_a_promotion_with_no_path_recorded_is_drift_too(self):
        from skillpp.lifecycle import reconcile
        self._promoted("a", "")
        result = reconcile(Ledger(self.config), self.config)
        self.assertEqual(result["missing"][0]["skill_path"], "(never recorded)")


class TestParkingSticks(TempRoot):
    """A decision must survive the work happening again.

    Ids derive from the signature, so an entry that recurrence refuses to match
    is rebuilt with the same id on the next occurrence and overwritten — the
    decision silently undone. Measured before the fix: dismiss, do the work
    again, and it was a candidate once more.
    """

    CMDS = ["npm test", "git tag -s v2.4.0 -m rel", "git push --follow-tags"]

    def _session(self, sid):
        from skillpp.capture import (handle_prompt, handle_session_end,
                                     handle_tool)
        handle_prompt(self.config, {"session_id": sid, "cwd": "/r",
                                    "prompt": "cut the release"})
        for c in self.CMDS:
            handle_tool(self.config, {"session_id": sid, "cwd": "/r",
                                      "tool_name": "Bash",
                                      "tool_input": {"command": c}})
        handle_session_end(self.config, {"session_id": sid})

    def _park(self, status="dismissed"):
        led = Ledger(self.config)
        entry = next(led.all())
        entry.status = status
        entry.parked_at_occurrences = entry.occurrences
        led.save(entry)
        return entry.id

    def test_a_dismissal_survives_the_work_happening_again(self):
        self._session("s1")
        eid = self._park()
        self._session("s2")
        self.assertEqual(Ledger(self.config).get(eid).status, "dismissed")

    def test_a_parked_entry_is_matched_not_rebuilt(self):
        """One entry, not two — the same signature must not make a second."""
        self._session("s1")
        self._park()
        self._session("s2")
        self.assertEqual(len(list(Ledger(self.config).all())), 1)

    def test_recurrences_after_parking_are_counted(self):
        self._session("s1")
        eid = self._park()
        self._session("s2")
        self._session("s3")
        entry = Ledger(self.config).get(eid)
        self.assertEqual(entry.recurrences_since_parked(), 2)
        self.assertTrue(entry.parking_looks_wrong(2))

    def test_a_parked_entry_never_becomes_ready(self):
        """The count moves; the entry stays out of review. `ready` gates on
        status, which is what makes matching it safe."""
        self._session("s1")
        eid = self._park()
        for sid in ("s2", "s3", "s4", "s5"):
            self._session(sid)
        entry = Ledger(self.config).get(eid)
        self.assertGreaterEqual(entry.occurrences, 4)
        self.assertFalse(entry.ready(3))

    def test_an_unparked_entry_reports_no_deviation(self):
        self._session("s1")
        self.assertEqual(next(Ledger(self.config).all())
                         .recurrences_since_parked(), 0)

    def test_a_sift_parking_sticks_the_same_way(self):
        self._session("s1")
        eid = self._park(status="one-off")
        self._session("s2")
        self.assertEqual(Ledger(self.config).get(eid).status, "one-off")


class TestWeb(TempRoot):
    """A page that shows the current ledger, not the one it was written for.

    The previous version predates `hint`, `description`, `parked_at_occurrences`
    and the one-off/split statuses, so it would have rendered parked entries as
    live ones and no ranking at all. A stale view that looks authoritative is
    worse than no view, which is why this was rewritten rather than ported.
    """

    def setUp(self) -> None:
        super().setUp()
        self.skills = self.root / "skills"
        self.skills.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(self.config)

    def _step(self, c):
        return {"tool": "Bash", "input": {"command": c}}

    def test_the_state_separates_parked_from_candidates(self):
        from skillpp.ledger import STATUS_DISMISSED
        from skillpp.web import collect_state
        self.ledger.save(Entry(id="a", signature="s1", title="live one",
                               steps=[self._step("npm test")]))
        self.ledger.save(Entry(id="b", signature="s2", title="parked one",
                               status=STATUS_DISMISSED,
                               steps=[self._step("ls")]))
        state = collect_state(self.config, self.skills)
        self.assertEqual([e["id"] for e in state["candidates"]], ["a"])
        self.assertEqual([e["id"] for e in state["parked"]], ["b"])

    def test_review_rows_show_what_a_reader_needs_to_judge(self):
        from skillpp.web import review_rows
        self.ledger.save(Entry(id="a", signature="npm test", title="restart again",
                               intents=["restart again", "still broken"],
                               steps=[self._step("npm test")]))
        self.ledger.save(Entry(id="b", signature="npm test|git commit",
                               title="fix: the thing", intents=["please fix it"],
                               steps=[self._step("npm test"),
                                      self._step("git commit -m 'fix: the thing'")]))
        self.ledger.save(Entry(id="c", signature="x", title="restart again",
                               intents=["restart again"],
                               steps=[self._step("git commit -m 'later'")]))
        rows = {r["id"]: r for r in review_rows(self.config)}
        self.assertTrue(rows["a"]["title_from_prompt"])
        self.assertFalse(rows["b"]["title_from_prompt"], "titled from its commit")
        self.assertTrue(rows["c"]["title_from_prompt"],
                        "a later commit does not change where the title came from")
        self.assertEqual(rows["a"]["intents"], ["restart again", "still broken"])
        self.assertEqual(rows["a"]["closest"]["id"], "b")

    def test_a_review_tag_is_saved_cleared_and_validated(self):
        from skillpp.web import load_review, review_rows, save_review
        self.ledger.save(Entry(id="a", signature="s", title="t",
                               steps=[self._step("npm test")]))
        self.assertTrue(save_review(self.config, "a", "fragment")["ok"])
        self.assertEqual(load_review(self.config), {"a": "fragment"})
        self.assertEqual(review_rows(self.config)[0]["tag"], "fragment")
        self.assertFalse(save_review(self.config, "a", "brilliant")["ok"])
        self.assertFalse(save_review(self.config, "nope", "good")["ok"])
        save_review(self.config, "a", "")
        self.assertEqual(load_review(self.config), {})

    def test_a_summary_is_cached_and_redone_when_the_entry_grows(self):
        import skillpp.local as local
        from skillpp.web import review_rows, summarise
        self.ledger.save(Entry(id="a", signature="s", title="option 1",
                               intents=["option 1"],
                               steps=[self._step("npm test")]))
        calls = []
        real = local.ask
        def fake(model, prompt, **kw):
            calls.append(prompt)
            return "The developer ran the tests.\n\n**Concrete thing:** tests"
        local.ask = fake
        self.addCleanup(lambda: setattr(local, "ask", real))

        out = summarise(self.config, "a")
        self.assertEqual(out["summary"], "Ran the tests.",
                         "first line only, no leading 'The developer'")
        summarise(self.config, "a")
        self.assertEqual(len(calls), 1, "cached, not asked twice")
        self.assertEqual(review_rows(self.config)[0]["summary"], "Ran the tests.")

        entry = self.ledger.get("a")
        entry.steps.append(self._step("git commit -m x"))
        self.ledger.save(entry)
        self.assertEqual(review_rows(self.config)[0]["summary"], "",
                         "a grown entry is not described by the old sentence")
        summarise(self.config, "a")
        self.assertEqual(len(calls), 2)

    def test_a_summary_never_fills_the_skill_description(self):
        """`description` decides whether a promoted skill loads."""
        import skillpp.local as local
        from skillpp.web import summarise
        self.ledger.save(Entry(id="a", signature="s", title="t",
                               steps=[self._step("npm test")]))
        real = local.ask
        local.ask = lambda *a, **k: "Ran the tests."
        self.addCleanup(lambda: setattr(local, "ask", real))
        summarise(self.config, "a")
        self.assertEqual(Ledger(self.config).get("a").description, "")

    def test_review_tags_never_touch_the_ledger(self):
        """Judging the output must not steer it."""
        from skillpp.web import save_review
        self.ledger.save(Entry(id="a", signature="s", title="t",
                               steps=[self._step("npm test")]))
        before = self.ledger.get("a").status
        save_review(self.config, "a", "duplicate")
        self.assertEqual(Ledger(self.config).get("a").status, before)

    def test_the_ranking_and_description_reach_the_page(self):
        """Both are new since the old UI, and both decide what a reader does."""
        from skillpp.web import collect_state
        self.ledger.save(Entry(id="a", signature="s", title="t", hint="method",
                               description="When a migration is lock-blocked.",
                               steps=[self._step("npm test")]))
        row = collect_state(self.config, self.skills)["candidates"][0]
        self.assertEqual(row["hint"], "method")
        self.assertIn("lock-blocked", row["description"])

    def test_deviation_is_visible_on_a_parked_entry(self):
        from skillpp.ledger import STATUS_DISMISSED
        from skillpp.web import collect_state
        self.ledger.save(Entry(id="a", signature="s", title="t",
                               status=STATUS_DISMISSED, occurrences=4,
                               parked_at_occurrences=1,
                               steps=[self._step("npm test")]))
        row = collect_state(self.config, self.skills)["parked"][0]
        self.assertEqual(row["since_parked"], 3)
        self.assertTrue(row["parking_looks_wrong"])

    def test_a_traversal_name_is_refused_not_sanitised(self):
        from skillpp.web import _skill_path
        for name in ("../../etc/passwd", "..", "a/../../b", "", "x" * 80):
            self.assertIsNone(_skill_path(self.skills, self.config, name), name)

    def test_saving_a_skill_without_a_description_is_refused(self):
        """It is the only thing read when deciding whether to load a skill."""
        from skillpp.web import save_skill
        path = self.skills / "x" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: x\ndescription: original\n---\nbody\n")
        result = save_skill(path, "---\nname: x\n---\nbody\n")
        self.assertFalse(result["ok"])
        self.assertIn("description", result["error"])
        self.assertIn("original", path.read_text())

    def test_an_over_long_description_is_refused(self):
        from skillpp.web import save_skill
        path = self.skills / "x" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: x\ndescription: fine\n---\n")
        long = "y" * 201
        result = save_skill(path, f"---\nname: x\ndescription: {long}\n---\n")
        self.assertFalse(result["ok"])
        self.assertIn("200", result["error"])

    def test_a_valid_save_keeps_the_previous_version(self):
        from skillpp.web import save_skill
        path = self.skills / "x" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: x\ndescription: before\n---\n")
        result = save_skill(path, "---\nname: x\ndescription: after\n---\n")
        self.assertTrue(result["ok"])
        self.assertIn("after", path.read_text())
        self.assertTrue(list(path.parent.glob("*.bak-*")))

    def test_reopen_is_the_only_status_change_the_page_makes(self):
        from skillpp.ledger import STATUS_CANDIDATE, STATUS_DISMISSED
        from skillpp.web import reopen
        self.ledger.save(Entry(id="a", signature="s", title="t",
                               status=STATUS_DISMISSED))
        self.assertTrue(reopen(self.config, "a")["ok"])
        self.assertEqual(Ledger(self.config).get("a").status, STATUS_CANDIDATE)
        # not applicable to anything that was not parked
        self.assertFalse(reopen(self.config, "a")["ok"])

    def test_the_page_carries_no_external_references(self):
        """Dependency-free on purpose, and offline by consequence."""
        from skillpp.web import PAGE
        for bad in ("http://", "https://cdn", "//cdn.", "<script src"):
            self.assertNotIn(bad, PAGE.replace("http://127.0.0.1", ""))


class TestLiveSessions(unittest.TestCase):
    """Real captured sessions, scored against hand-written ground truth.

    `tests/benchmarks/cases.py` was authored by whoever was also writing the
    detector, which makes it a test of internal consistency. These are real
    work, and each already caught something that corpus could not: one banked
    six fragments for one task, the other committed with `git -C <path> commit`
    and was recorded as if it never committed at all.

    See `tests/fixtures/sessions/README.md`.
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "sessions"))
        import score
        cls.score = score
        cls.docs = score.load()

    def test_there_are_live_sessions_to_score(self):
        """A silently empty directory would make every test below vacuous."""
        self.assertGreaterEqual(len(self.docs), 2)

    def _scored(self):
        """Sessions the pipeline is expected to get right.

        A fixture carrying `expected_fail` records a gap that is known and
        unfixed; asserting against it would only restate the gap. It is checked
        separately, by `test_the_known_gaps_are_still_gaps`.
        """
        return [d for d in self.docs if not d.get("expected_fail")]

    def test_every_live_session_segments_to_its_ground_truth(self):
        for doc in self._scored():
            with self.subTest(doc["tag"]):
                row = self.score.check(doc)
                self.assertTrue(row["episodes"]["ok"],
                                f"{doc['name']}: {row['episodes']}")

    def test_the_known_gaps_are_still_gaps(self):
        """Fails when a recorded gap closes — which is the point.

        A gap that quietly starts passing is a fix nobody noticed, and the
        fixture's ground truth and `expected_fail` note then both need
        rewriting. Better to be told.
        """
        for doc in self.docs:
            if not doc.get("expected_fail"):
                continue
            with self.subTest(doc["tag"]):
                row = self.score.check(doc)
                ok = all(row[k]["ok"] for k in ("episodes", "title", "kept", "markers"))
                self.assertFalse(
                    ok, f"{doc['name']} now passes — remove `expected_fail` "
                        f"and update the fixture: {doc['expected_fail'][:90]}")

    def test_a_commitless_session_is_still_captured(self):
        """No commit must not mean no capture. It fragments today, but the
        work is banked — losing it entirely would be a different, worse bug."""
        for doc in self.docs:
            if doc["truth"].get("markers") != 0:
                continue
            with self.subTest(doc["tag"]):
                row = self.score.check(doc)
                self.assertEqual(row["markers"]["got"], 0)
                self.assertGreaterEqual(row["episodes"]["got"], 1,
                                        "commitless work must still bank something")

    def test_every_live_session_is_titled_after_the_work(self):
        for doc in self._scored():
            with self.subTest(doc["tag"]):
                row = self.score.check(doc)
                self.assertTrue(row["title"]["ok"],
                                f"{doc['name']}: got {row['title']['got']!r}")

    def test_the_steps_the_procedure_exists_for_survive(self):
        """The assertion that matters. An episode can be the right size and the
        right name and still have lost the documentation lookup that makes the
        procedure worth repeating."""
        for doc in self._scored():
            with self.subTest(doc["tag"]):
                row = self.score.check(doc)
                self.assertTrue(row["kept"]["ok"],
                                f"{doc['name']}: missing {row['kept']['missing']}")

    def test_no_live_session_carries_a_home_path_or_a_secret(self):
        """These are committed files. An earlier fixture attempt shipped a
        token-shaped string into the repo."""
        secret = re.compile(r"ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,}"
                            r"|(?:TOKEN|SECRET|PASSWORD|API_KEY)\s*=\s*\S+")
        for doc in self.docs:
            with self.subTest(doc["tag"]):
                blob = json.dumps(doc["steps"])
                self.assertNotIn(str(Path.home()), blob)
                self.assertIsNone(secret.search(blob))


class TestPromotedSkillsStayMatchable(TempRoot):
    """A promoted skill used to become invisible the moment it was promoted.

    Capture's `find_match` does see promoted entries, but decides lexically at
    0.85, and measured over 5,995 real pairs nothing reaches 0.85 — the highest
    is 0.814. The embedding pass then excluded them outright by filtering to
    candidates. So the count froze at promotion and every later run of the same
    work opened a fresh proposal for something a skill already did.
    """

    def _entry(self, eid, sig, title, status=STATUS_CANDIDATE, session="s1"):
        from skillpp.ledger import Entry
        return Entry(id=eid, signature=sig, title=title, status=status,
                     sessions=[session], occurrences=1,
                     steps=[bash("npm run build"), bash("./deploy.sh staging")],
                     intents=[title])

    def test_a_promoted_skill_is_compared_against(self):
        from skillpp.ledger import STATUS_PROMOTED
        from skillpp.similar import near_misses
        skill = self._entry("a", "bash:npm run | bash:deploy.sh", "deploy",
                            status=STATUS_PROMOTED)
        cand = self._entry("b", "bash:npm run | bash:deploy.sh | bash:curl",
                           "deploy again", session="s2")
        pairs = near_misses([skill, cand], floor=0.0, ceiling=1.0)
        self.assertEqual(len(pairs), 1, "a promoted skill must be comparable")

    def test_matching_a_skill_reinforces_it_and_covers_the_candidate(self):
        """Reinforce, never delete: the skill exists, and this says it is used."""
        from skillpp.ledger import STATUS_COVERED, STATUS_PROMOTED
        from skillpp.similar import run_background_check
        from skillpp import similar
        # Signatures that land inside the near-miss band, as the real pair did
        # at 0.583: identical ones score 1.0 and fall outside it entirely.
        skill = self._entry("aaaa", "bash:npm run | bash:deploy.sh | bash:curl",
                            "deploy", status=STATUS_PROMOTED)
        cand = self._entry("bbbb", "bash:npm run | bash:deploy.sh | bash:kubectl",
                           "deploy again", session="s2")
        led = Ledger(self.config)
        for e in (skill, cand):
            led.save(e)
        note_pending_check(self.config, "bbbb", "s2", "created")

        real = similar.cosine
        similar.cosine = lambda a, b: 0.99          # stand in for the model
        similar.embed = lambda *a, **k: [0.0]
        try:
            run_background_check(self.config)
        finally:
            similar.cosine = real

        led = Ledger(self.config)  # re-read from disk
        self.assertEqual(led.get("aaaa").status, STATUS_PROMOTED)
        self.assertEqual(led.get("aaaa").occurrences, 2, "the skill was used again")
        covered = led.get("bbbb")
        self.assertIsNotNone(covered, "the candidate is evidence, not rubbish")
        self.assertEqual(covered.status, STATUS_COVERED)
        self.assertNotIn("bbbb", [c.id for c in led.candidates()],
                         "work a skill already does is not a proposal")


class TestThreeRealRunsRecur(TempRoot):
    """Three real sessions of one procedure, replayed into one ledger.

    The first thing in this project to reach the recurrence threshold from real
    work rather than a fixture. Each session added the same kind of eval case to
    the same repository against a different topic — desk booking, timesheet
    timesheet, absence — so what they share is the method and what they do not
    is that day's particulars.

    Deliberately not run against a model: `run_background_check` needs an
    embedding, so this pins the deterministic half — that all three fold, that
    the queued pass has pairs to consider, and that nothing regresses the
    segmentation those three depend on.
    """

    def _sessions(self):
        path = Path(__file__).resolve().parent / "fixtures" / "sessions"
        for f in sorted(path.glob("*.json")):
            yield json.loads(f.read_text(encoding="utf-8"))

    def test_each_run_folds_to_one_bankable_entry(self):
        """A fragmented run cannot recur: three shapes never match each other."""
        for doc in self._sessions():
            with self.subTest(doc["tag"]):
                result = fold_session(self.config, {
                    "session_id": doc["tag"], "cwd": "/w", "prompts": [],
                    "steps": doc["steps"]})
                banked = [e for e in result.get("episodes", [])
                          if e.get("status") in ("created", "matched")]
                self.assertGreaterEqual(len(banked), 1, doc["name"])

    def test_the_three_runs_are_near_misses_of_each_other(self):
        """They do not match lexically — 0.583 on the pair measured — which is
        why the embedding pass exists. What this pins is that they land inside
        its band rather than below it, where nothing would ever look at them."""
        from skillpp.similar import near_misses
        for doc in self._sessions():
            fold_session(self.config, {"session_id": doc["tag"], "cwd": "/w",
                                       "prompts": [], "steps": doc["steps"]})
        entries = list(Ledger(self.config).all())
        self.assertGreaterEqual(len(entries), 3, "each run banks its own entry")
        pairs = near_misses(entries, floor=self.config.queued_near_miss_floor,
                            ceiling=self.config.similarity_threshold)
        self.assertTrue(pairs, "the three runs must reach the embedding at all")
