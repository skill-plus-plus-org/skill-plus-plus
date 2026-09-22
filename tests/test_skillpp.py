"""Unit tests. Stdlib only: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skillpp.capture import (_fold_steps, fold_session, handle_prompt, handle_tool,
                             handle_session_end)
from skillpp.config import Config
from skillpp.ledger import STATUS_CANDIDATE, Entry, Ledger
from skillpp.lifecycle import check_staleness, parse_frontmatter, record_use, scan
from skillpp.normalize import normalize_command, parameterize, step_shape
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
_REAL_EMBED = None


def _shapes(steps):
    """What a workflow's steps did, arguments thrown away — for assertions."""
    return [step_shape(s) for s in steps]


def _sig(steps):
    """The steps as one string of shapes, consecutive repeats collapsed.

    What `normalize.signature` used to return. It no longer decides anything —
    matching is an embedding — but it is still a compact way for a test to say
    which steps a fold kept.
    """
    out = []
    for shape in _shapes(steps):
        if shape and (not out or out[-1] != shape):
            out.append(shape)
    return " | ".join(out)


def _stub_embed(text, **kw):
    """A deterministic stand-in for the embedding model.

    `matching.steps_text` renders a run as one numbered line per step,
    `N. Tool body`. This embeds only the *kind* of each step — a Bash command is
    reduced to `normalize_command` — as a bag of hashed tokens. So two runs of
    the same workflow with different arguments score 1.0, and different
    workflows score lower, which is the behaviour the suite was written against.
    It says nothing about how well a real embedding separates procedures;
    `tests/fixtures/sessions/recurrence.py` measures that.
    """
    import hashlib
    vector = [0.0] * 256
    for line in text.splitlines():
        # A conversation (`matching.turns_text`) embeds as a bag of its words,
        # so two runs that said different things are different; mapped to the
        # fallback below they would all be identical and everything would merge.
        if line.startswith(("User: ", "Agent: ")):
            for word in re.findall(r"[a-z0-9]+", line.split(": ", 1)[1].lower()):
                slot = int(hashlib.sha256(("word:" + word).encode()).hexdigest(), 16) % len(vector)
                vector[slot] += 1.0
            continue
        # A multi-line command continues on lines of its own; only a line that
        # opens with a step number and a tool name is a step.
        m = re.match(r"\d+\. (mcp__[\w-]+|[A-Z][A-Za-z]+) ?(.*)", line)
        if not m:
            continue
        tool, body = m.groups()
        if tool == "Bash":
            token = "bash:" + normalize_command(body)
        elif tool in ("Edit", "Write", "NotebookEdit"):
            token = f"{tool.lower()}:{Path(body).suffix}"
        elif tool.startswith("mcp__") or tool in ("Read", "Glob", "Grep"):
            token = tool.lower()
        else:
            token = f"{tool.lower()}:{body.strip().lower()}"
        slot = int(hashlib.sha256(token.encode()).hexdigest(), 16) % len(vector)
        vector[slot] += 1.0
    return vector if any(vector) else [1.0] + [0.0] * 255


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
    global _REAL_JUDGE, _REAL_DESCRIBE, _REAL_EMBED
    import skillpp.boundary as boundary
    import skillpp.matching as matching
    _REAL_JUDGE = boundary.judge_session
    _REAL_DESCRIBE = boundary.describe_in_session
    _REAL_EMBED = matching.embed
    boundary.judge_session = _marker_judge
    # Same reasoning for the embedding that decides "same procedure": the suite
    # must pass without Ollama.
    matching.embed = _stub_embed
    # Same reasoning for the describer, which runs on the same hot path and is
    # slower still — it writes a sentence where the judge writes one word.
    boundary.describe_in_session = (
        lambda config, session, step, reply="": "")
    # And for naming, which every fold that banks a new entry asks for. It was
    # the one call left real: with Ollama up the suite took eight minutes and
    # kept a 7.7 GB model resident afterwards; with it down, every fold waited
    # on a refused connection. A candidate the model never named keeps the
    # title capture gave it, which is what these tests assert against.
    global _REAL_NAME
    import skillpp.capture as capture
    _REAL_NAME = capture._name_from_model
    capture._name_from_model = lambda config, entry: None
    # And for `install`, which lists the Ollama models and pulls missing ones on
    # --apply. A test must never start a 10 GB download, nor depend on what the
    # machine running it has pulled: every model is present unless a test says
    # otherwise, and a pull that is not stubbed fails the test.
    global _REAL_MODELS, _REAL_PULL
    import skillpp.cli as cli
    _REAL_MODELS, _REAL_PULL = cli._available_models, cli._pull_model
    cli._available_models = lambda config: ([config.local_model, config.embed_model + ":latest"], "")

    def _no_pull(config, name):
        raise AssertionError(f"a test tried to pull {name}")
    cli._pull_model = _no_pull


def tearDownModule() -> None:
    import skillpp.boundary as boundary
    import skillpp.capture as capture
    import skillpp.matching as matching
    boundary.judge_session = _REAL_JUDGE
    boundary.describe_in_session = _REAL_DESCRIBE
    matching.embed = _REAL_EMBED
    capture._name_from_model = _REAL_NAME
    import skillpp.cli as cli
    cli._available_models, cli._pull_model = _REAL_MODELS, _REAL_PULL


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

    def test_arguments_do_not_change_step_shapes(self):
        a = _shapes([bash("pytest -k auth"), bash("git push")])
        b = _shapes([bash("pytest -k billing"), bash("git push")])
        self.assertEqual(a, b)

    def test_normalize_links_reads_every_link_of_a_chain(self):
        from skillpp.normalize import normalize_links
        self.assertEqual(normalize_links("cd /repo && git add -A && git commit -m x"),
                         ["cd", "git add", "git commit"])
        self.assertEqual(normalize_links(""), [])

    def test_the_chain_fix_does_not_move_normalize_command(self):
        """`step_shape` calls it, so any drift here moves every step shape."""
        self.assertEqual(normalize_command("cd /repo && git commit -m x"), "cd")
        self.assertEqual(normalize_command("git commit -m 'x'"), "git commit")
        self.assertEqual(normalize_command("npm run test -- --watch"), "npm run")

    def test_a_description_does_not_change_the_step_shape(self):
        """The guard on rendering.

        `step_shape` is compared across a candidate's runs to find the steps
        every run shares (`signals.recurring_steps`), so it is built to be
        stable. A `description` is the opposite: free text the agent rewrites
        every run — the same `./assemble.sh` was described "Assemble after tone
        pass" once and "Assemble and measure section 6" the next time. If that
        reached `step_shape`, two runs of one procedure would share no steps.
        """
        plain = [bash("pytest -k auth"), bash("git push")]
        described = [
            {"tool": "Bash", "input": {"command": "pytest -k auth",
                                       "description": "Assemble after tone pass"},
             "failed": False},
            {"tool": "Bash", "input": {"command": "git push",
                                       "description": "Ship it"}, "failed": False}]
        self.assertEqual(_shapes(plain), _shapes(described))

    def test_parameterize_paths(self):
        out = parameterize("/proj/app/main.py and /proj/app/x", "/proj/app")
        self.assertNotIn("/proj/app", out)
        self.assertIn("${PROJECT_PATH}", out)

    def test_parameterize_ids(self):
        self.assertIn("${ID}", parameterize("aws s3 ls bucket-1234567", None))


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

    def test_git_commands_that_discard_uncommitted_work_are_destructive(self):
        """A real run restored `cases.json` to HEAD and its draft said nothing."""
        from skillpp.signals import DESTRUCTIVE
        discards = ["git checkout HEAD -- backend/eval/cases.json && git diff --stat",
                    "git checkout -- .", "git checkout .", "git -C repo checkout main -- a.py",
                    "git restore cases.json", "git restore --staged --worktree a.py",
                    "git clean -fd", "git clean -xdf", "git stash clear", "git stash drop"]
        keeps = ["git checkout main", "git checkout -b feat/x", "git restore --staged a.py",
                 "git clean -n", "git status", "pg_restore -d app dump.sql"]
        for cmd in discards:
            self.assertTrue(DESTRUCTIVE.search(cmd), cmd)
        for cmd in keeps:
            self.assertFalse(DESTRUCTIVE.search(cmd), cmd)


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
                         ("add walkthrough-card case for Desk Booking", "commit"))

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
                         ("ship the thing", "prompt"))

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
        self.assertIn("read", _sig(entry.steps).split(" | "))

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
        self.assertNotIn("read", _sig(entry.steps).split(" | "))

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
        # Off by default now that nothing reads it; this is how it reads when on.
        self.config.describe_steps = True

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
        self.assertIn("## Open questions", text)
        self.assertIn("# Fact Check", text, "raw dictation is a poor heading")

    def test_answering_a_question_closes_its_gap(self):
        entry, _ = self.dictate(self.EXAMPLE)
        text = scaffold_skill(entry, "fact-check", "Verify claims.", answers={
            "when_to_use": "When the user pastes a claim.",
            "output_format": "Claim / Verdict / Sources",
            "sources": "two independent primary sources",
        })
        gaps = text.split("## Open questions")[-1] if "## Open questions" in text else ""
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
        self.assertNotIn("## Open questions", text)


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
        # `printf` and `wc` were asserted here until coreutils stopped being
        # declared; the keyword-stepping this test exists to guard is the
        # second assertion, which is unchanged.
        self.assertEqual(_cli_dependencies([bash(
            'cd s && for f in *.md; do printf x; npm run lint "$f"; done')]),
            {"npm"})
        self.assertEqual(_cli_dependencies([bash("for x in 1; do npm test; done")]),
                         {"npm"})

    def test_a_heredoc_body_is_not_shell(self):
        """`python3 - <<'EOF' … EOF` carries Python, whose `;` means nothing to
        a shell. Splitting on it declared `print('deps` as a requirement."""
        from skillpp.capture import _cli_dependencies
        self.assertEqual(_cli_dependencies([bash(
            "python3 - <<'EOF'\nprint('deps'); x = 1\nEOF")]), {"python3"})

    def test_a_truncated_heredoc_drops_the_rest(self):
        """`max_field_chars` cuts a long command at 2000 characters, so the
        terminator is usually gone — this is where `frontend` came from."""
        from skillpp.capture import _cli_dependencies
        self.assertEqual(_cli_dependencies([bash(
            "python3 - <<'EOF'\nr('frontend gets AGENT_URL'); print('py')")]),
            {"python3"})

    def test_code_in_a_quoted_argument_is_not_shell(self):
        """Nothing here knows what `-e` means; staying inside the quotes is
        enough. This declared `console.log('ok')\"` on a real skill."""
        from skillpp.capture import _cli_dependencies
        self.assertEqual(_cli_dependencies([bash(
            'node -e "require(\'pptxgenjs\'); console.log(\'ok\')" && npm test')]),
            {"node", "npm"})

    def test_quoted_operators_do_not_split(self):
        from skillpp.capture import _cli_dependencies
        self.assertEqual(_cli_dependencies([bash('git commit -m "a && b; c"')]),
                         {"git"})

    def test_coreutils_are_not_declared_dependencies(self):
        """The list is headed "if a requirement is missing, stop", and
        `check_dependencies` resolves with `shutil.which` — which never fails
        on `cp`. Declaring them is noise nothing can act on."""
        from skillpp.capture import _cli_dependencies
        self.assertEqual(_cli_dependencies([bash(
            "cp a b; rm c; grep x d; sed -i '' s/a/b/ e; pdftoppm f g")]),
            {"pdftoppm"})

    def test_stored_garbage_dependencies_never_reach_the_skill(self):
        """14 entries were banked before the parser was fixed and keep their
        junk on disk, so the filter has to run at read time too."""
        entry = Entry(id="abc", signature="s", title="build",
                      deps_cli=["')", "','const", "console.log('ok')\"", "cp",
                                "npm"],
                      steps=[bash("npm run build"), bash("npm test")])
        text = scaffold_skill(entry, "build", "Build.")
        fm = parse_frontmatter(text)
        self.assertEqual(fm["metadata"]["requires_cli"], ["npm"])
        self.assertIn("- `npm` on PATH", text)
        self.assertNotIn("')", text.split("## Requirements")[1].split("##")[0])

    def test_a_candidate_with_turns_gets_no_procedure(self):
        """Both of the first real drafts came back as pure scaffold — TODO
        comment and all — because the scaffold had already written a document
        that looked finished."""
        from skillpp.summary import WRITE_HERE
        entry = Entry(id="abc", signature="s", title="build a deck",
                      deps_cli=["node"],
                      steps=[bash("npm run build"), bash("rm -rf dist")],
                      turns=[{"prompt": "check the slides", "reply": "checked",
                              "used": []}])
        text = scaffold_skill(entry, "deck", "Build a deck.")
        self.assertIn(WRITE_HERE, text)
        self.assertNotIn("## Steps", text)
        self.assertNotIn("<!-- TODO", text)
        self.assertNotIn("## When to use", text)
        # A fact: stays.
        self.assertIn("## Requirements", text)
        # A judgement: handed to the agent in `show --json --draft` instead.
        # Pasted verbatim, it flagged a procedure deleting its own scratch
        # images and shipped the session's temp paths as a safety warning.
        self.assertNotIn("## Destructive operations", text)

    def test_a_destructive_command_is_one_line_in_full_mode(self):
        """A raw newline inside a bullet's backticks split the code span when
        the page rendered it, and `slide-*.jpg` came out as `slide-.jpg`."""
        entry = Entry(id="abc", signature="s", title="render",
                      steps=[bash('S=/tmp/x\ncd "$S" && rm -f slide-*.jpg')])
        section = scaffold_skill(entry, "r", "d").split(
            "## Destructive operations")[1].split("##")[0]
        bullets = [l for l in section.splitlines() if l.startswith("- ")]
        self.assertEqual(len(bullets), 1)
        self.assertIn("slide-*.jpg", bullets[0])
        self.assertIn("⏎", bullets[0])

    def test_the_frontmatter_is_the_same_in_both_modes(self):
        """`provenance`, `tier` and `occurrences` are read back by the ledger
        and the review page, so the agent must never have to write them."""
        entry = Entry(id="abc", signature="s", title="build a deck",
                      deps_cli=["node"], occurrences=3,
                      steps=[bash("npm run build")],
                      turns=[{"prompt": "go", "reply": "done", "used": []}])
        facts = parse_frontmatter(scaffold_skill(entry, "deck", "d"))
        full = parse_frontmatter(scaffold_skill(entry, "deck", "d", body="full"))
        self.assertEqual(facts, full)

    def test_the_mode_can_be_forced_either_way(self):
        with_turns = Entry(id="a", signature="s", title="t",
                           steps=[bash("npm test")],
                           turns=[{"prompt": "go", "reply": "ok", "used": []}])
        without = Entry(id="b", signature="s", title="t",
                        steps=[bash("npm test")])
        self.assertIn("## Steps", scaffold_skill(with_turns, "n", "d", body="full"))
        self.assertNotIn("## Steps", scaffold_skill(without, "n", "d", body="facts"))
        self.assertIn("## Steps", scaffold_skill(without, "n", "d"))

    def test_questions_are_capped_when_a_limit_is_given(self):
        entry = Entry(id="abc", signature="s", title="deploy",
                      steps=[bash("npm run build"), bash("./deploy.sh prod")])
        text = scaffold_skill(entry, "deploy", "Deploy.", limit=1)
        gaps = text.split("## Open questions")[-1]
        self.assertLessEqual(len([l for l in gaps.splitlines()
                                  if l.startswith("- ")]), 1)

    def test_unanswered_questions_become_known_gaps(self):
        entry = Entry(id="abc", signature="s", title="deploy",
                      steps=[bash("npm run build"), bash("./deploy.sh prod")])
        text = scaffold_skill(entry, "deploy", "Deploy.")
        self.assertIn("## Open questions", text)

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

    def test_open_questions_are_not_scanned_for_stale_references(self):
        """`_META_SECTIONS` skipped `## Known gaps`; the section was renamed,
        so without the new name a question mentioning a command reports it as
        a broken reference."""
        hot = self.root / "skills"
        path = self._write_skill(hot, "alpha", body=(
            "## Open questions\n\n- Does `definitely-not-a-real-binary-xyz` "
            "matter here?\n"))
        skill = {s.name: s for s in scan(hot, self.config, self.root)}["alpha"]
        self.assertEqual(skill.stale_refs, [], path.read_text())

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
        from skillpp.normalize import strip_scaffolding
        raw = [bash("cd /r && npm test 2>&1 | tail -5"), bash("cd /r && git commit -m x")]
        stripped = [bash(strip_scaffolding(s["input"]["command"])) for s in raw]
        self.assertEqual(_sig(raw), "bash:cd")
        self.assertNotEqual(_sig(raw), _sig(stripped))

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
        """`241955c7`, the session this was found on. A private one: it runs
        where `SKILLPP_FIXTURES` points at the set that holds it."""
        sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "sessions"))
        import score
        found = score.load("241955c7")
        if not found:
            self.skipTest("session 241955c7 is not in this set")
        doc = found[0]
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
                   if _sig(e.steps) == self.CLEAN_SIGNATURE]
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
        signatures = {_sig(e.steps) for e in Ledger(self.config).all()}
        self.assertIn(self.CLEAN_SIGNATURE, signatures)

    @unittest.expectedFailure
    def test_after_the_deploy_is_titled_after_the_deploy(self):
        self._fold_segmented(to_captured_session)
        deploy = next(e for e in Ledger(self.config).all()
                      if _sig(e.steps) == self.CLEAN_SIGNATURE)
        self.assertIn("deploy", deploy.title.lower())

    def test_the_clean_baseline_is_unaffected(self):
        """demo.sh's behaviour must not change: 3 occurrences, one entry."""
        entries = self._fold_segmented(clean_session_dict)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].occurrences, 3)
        self.assertEqual(_sig(entries[0].steps), self.CLEAN_SIGNATURE)

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
            shapes.add(_sig(steps))
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
        base = dict(root=self.config.root, id="cand1", name=None, note=None,
                    apply=False, cwd=None, timeout=900)
        base.update(kw)
        return self.argparse.Namespace(**base)

    def _spy(self, exit_code=0, writes=None, say=""):
        """Capture argv instead of running an agent."""
        import subprocess
        seen = {}

        def fake(argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw
            # Looked at now: the agent's home is removed once it returns.
            home = Path(kw["cwd"])
            seen["home"] = sorted(str(p.relative_to(home))
                                  for p in home.rglob("*") if p.is_file())
            # Where a real agent writes: the directory it was handed, which
            # is a workspace outside `~/.claude` — `Write` is denied under
            # there. `cmd_draft` moves the result into `drafts/<id>/`.
            workspace = Path(kw["env"]["SKILLPP_DRAFT_DIR"])
            for rel in (writes or []):
                path = workspace / rel.split("/", 1)[1]
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
        """The allowed-tools pattern is relative, so the cwd is load-bearing.
        It used to be this checkout, which an installed package does not have:
        the agent now runs from a home holding exactly what its prompt uses,
        removed afterwards."""
        from skillpp.cli import cmd_draft
        seen = self._spy()
        cmd_draft(self._args(apply=True))
        self.assertEqual(seen["home"], [".claude/commands/skillpp-draft.md", "bin/skillpp"])
        self.assertFalse(Path(seen["kw"]["cwd"]).exists())

    def test_the_agents_cli_is_the_skillpp_that_started_it(self):
        """Run from an unrelated directory with a plain `python3`, as the agent's
        Bash runs it: this package answers, whether installed or checked out."""
        import shutil
        import subprocess
        import skillpp
        from skillpp.cli import _agent_home
        home = _agent_home("cand1")
        self.addCleanup(lambda: shutil.rmtree(home, ignore_errors=True))
        out = subprocess.run(["python3", "bin/skillpp", "--version"], cwd=home,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(skillpp.__version__, out.stdout)
        from skillpp.install import COMMANDS
        self.assertEqual((home / ".claude/commands/skillpp-draft.md").read_bytes(),
                         (COMMANDS / "skillpp-draft.md").read_bytes())

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
        workspace = seen["kw"]["env"]["SKILLPP_DRAFT_DIR"]
        self.assertTrue(prompt.endswith(workspace))
        self.assertNotIn("$", prompt)
        # Never inside the ledger root: on a default install that is under
        # `~/.claude`, where the agent's `Write` and `Edit` are refused, and
        # every draft came back as the bare scaffold because of it.
        self.assertFalse(Path(workspace).is_relative_to(self.config.root))
        # SKILLPP_ROOT stays in the environment; Python reads it directly.
        self.assertEqual(seen["kw"]["env"]["SKILLPP_ROOT"],
                         str(self.config.root))

    def test_a_note_reaches_the_agent_after_the_two_arguments(self):
        """The developer's note is part of the prompt, not of `show`'s output:
        a tool's output is evidence, and this is an instruction."""
        from skillpp.cli import cmd_draft
        seen = self._spy()
        note = "- check every command against DEPLOY.md\n- leave the styling out"
        cmd_draft(self._args(apply=True, note=f"  {note}\n"))
        prompt = next(a for a in seen["argv"] if a.startswith("/skillpp-draft"))
        workspace = seen["kw"]["env"]["SKILLPP_DRAFT_DIR"]
        self.assertEqual(prompt.splitlines()[0], f"/skillpp-draft cand1 {workspace}",
                         "id and directory still come first, as the command reads them")
        self.assertTrue(prompt.endswith(note), "the note arrives whole, lines and all")

    def test_a_blank_note_leaves_the_prompt_as_it_was(self):
        from skillpp.cli import cmd_draft
        seen = self._spy()
        cmd_draft(self._args(apply=True, note="  \n "))
        prompt = next(a for a in seen["argv"] if a.startswith("/skillpp-draft"))
        workspace = seen["kw"]["env"]["SKILLPP_DRAFT_DIR"]
        self.assertEqual(prompt, f"/skillpp-draft cand1 {workspace}")

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


class TestReviseCommand(TempRoot):
    """`skillpp revise`: the agent changes an existing draft, in place, as told.

    No agent is launched: `subprocess.run` is replaced by a function that edits
    (or does not edit) the draft the way an agent would.
    """

    def setUp(self) -> None:
        super().setUp()
        import argparse
        self.argparse = argparse
        Ledger(self.config).save(Entry(id="cand1", title="add an eval case",
                                       steps=[{"tool": "Bash", "input": {"command": "x"}}]))
        self.skill = self.config.root / "drafts" / "cand1" / "SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("---\nname: x\ndescription: d\n---\n# Body\n")

    def _args(self, **kw):
        base = dict(root=self.config.root, id="cand1", instruction="add a trap",
                    apply=True, cwd=None, timeout=900)
        base.update(kw)
        return self.argparse.Namespace(**base)

    def _agent(self, edit=None, exit_code=0, say=""):
        import subprocess
        seen = {}

        def fake(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw
            if edit:
                # The working copy the prompt names, never the draft itself.
                edit(Path(kw["env"]["SKILLPP_DRAFT_DIR"]) / "SKILL.md")
            return subprocess.CompletedProcess(argv, exit_code, stdout=say, stderr="")
        real, subprocess.run = subprocess.run, fake
        self.addCleanup(lambda: setattr(subprocess, "run", real))
        return seen

    def test_a_dry_run_launches_nothing(self):
        from skillpp.cli import cmd_revise
        seen = self._agent()
        self.assertEqual(cmd_revise(self._args(apply=False)), 0)
        self.assertNotIn("argv", seen)

    def test_the_instruction_and_the_file_reach_the_agent_as_one_prompt(self):
        from skillpp.cli import cmd_revise
        seen = self._agent(edit=lambda p: p.write_text(p.read_text() + "more\n"))
        cmd_revise(self._args(instruction="also cover fact cases"))
        prompt = next(a for a in seen["argv"] if "Revise a draft skill" in a)
        self.assertIn("also cover fact cases", prompt)
        working_copy = Path(seen["kw"]["env"]["SKILLPP_DRAFT_DIR"]) / "SKILL.md"
        self.assertIn(str(working_copy), prompt)
        self.assertNotIn(str(self.skill), prompt,
                         "the agent was pointed at a path it may not edit")
        # And the change reached the real draft anyway, moved by skillpp.
        self.assertTrue(self.skill.read_text().endswith("more\n"))
        self.assertEqual(seen["kw"]["env"]["SKILLPP_INTERNAL"], "1")

    def test_a_revision_keeps_the_previous_version(self):
        from skillpp.cli import cmd_revise
        self._agent(edit=lambda p: p.write_text(p.read_text() + "## Traps\n"))
        self.assertEqual(cmd_revise(self._args()), 0)
        self.assertIn("## Traps", self.skill.read_text())
        kept = list((self.skill.parent / ".revisions").glob("SKILL.*.md"))
        self.assertEqual(len(kept), 1)
        self.assertNotIn("## Traps", kept[0].read_text())

    def test_no_change_is_not_a_revision(self):
        from skillpp.cli import cmd_revise
        self._agent()
        self.assertEqual(cmd_revise(self._args()), 1)

    def test_a_stated_decline_is_reported_and_leaves_the_draft(self):
        from skillpp.cli import cmd_revise
        self._agent(say="SKILLPP-DECLINE: that would make it a different skill\n")
        self.assertEqual(cmd_revise(self._args()), 1)
        self.assertEqual(self.skill.read_text(), "---\nname: x\ndescription: d\n---\n# Body\n")

    def test_there_must_be_a_draft_to_revise(self):
        import shutil as sh
        from skillpp.cli import cmd_revise
        sh.rmtree(self.skill.parent)
        seen = self._agent()
        self.assertEqual(cmd_revise(self._args()), 1)
        self.assertNotIn("argv", seen)


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


class TestOccurrencesCountEveryRecognition(TempRoot):
    """The count is how often a procedure was recognized, repeats inside one
    session included; `sessions` records where.

    This reverses an earlier rule that counted distinct sessions. The cost is
    known and accepted: one long session can reach the recurrence threshold on
    its own.
    """

    DEPLOY = ["docker build -t api .", "docker push api",
              "kubectl set image deploy/api api=api", "git commit -am deploy"]

    def _fold(self, session_id):
        _fold_steps(self.config, {"session_id": session_id, "cwd": "/w",
                                  "prompts": ["deploy the api"]},
                    [bash(c) for c in self.DEPLOY])

    def test_twice_in_one_session_counts_twice(self):
        self._fold("only-session")
        self._fold("only-session")
        (entry,) = list(Ledger(self.config).all())
        self.assertEqual(entry.occurrences, 2)
        self.assertEqual(entry.sessions, ["only-session"])

    def test_one_session_can_reach_the_threshold(self):
        for _ in range(3):
            self._fold("only-session")
        (entry,) = list(Ledger(self.config).all())
        self.assertTrue(entry.ready(self.config.recurrence_threshold))

    def test_three_sessions_still_reach_three(self):
        for sid in ("s1", "s2", "s3"):
            self._fold(sid)
        (entry,) = list(Ledger(self.config).all())
        self.assertEqual(entry.occurrences, 3)
        self.assertEqual(len(entry.sessions), 3)


class TestMatching(TempRoot):
    """`skillpp.matching`: same procedure, decided by an embedding.

    The embedder is stubbed with fixed vectors so these test the matcher's own
    rules — what it compares, what it caches, where the floor sits — without a
    model.
    """

    def setUp(self) -> None:
        super().setUp()
        import skillpp.matching as matching
        self.matching = matching
        self.calls: list[str] = []
        self.vectors: dict[str, list[float]] = {}
        real = matching.embed

        def fake(text, **kw):
            self.calls.append(text)
            for needle, vector in self.vectors.items():
                if needle in text:
                    return vector
            return [0.0, 0.0, 1.0]
        matching.embed = fake
        self.addCleanup(lambda: setattr(matching, "embed", real))
        self.config.match_floor = 0.9

    def _entry(self, eid, command, status="candidate"):
        entry = Entry(id=eid, signature="", title=eid, status=status,
                      intents=[f"do {command}"], steps=[bash(command)])
        Ledger(self.config).save(entry)
        return entry

    def test_what_is_embedded_is_the_numbered_steps_and_no_prompt(self):
        entry = Entry(id="a", signature="", title="a",
                      intents=["Use the adk-docs MCP tool first"],
                      steps=[{"tool": "Read", "input": {"file_path": "eval/cases.json"}},
                             bash("git status --short")])
        self.assertEqual(self.matching.entry_text(entry),
                         "1. Read eval/cases.json\n2. Bash git status --short")

    def test_a_long_run_is_cut_on_a_step_boundary_under_the_limit(self):
        steps = [bash("x" * 200) for _ in range(100)]
        text = self.matching.steps_text(steps)
        self.assertLessEqual(len(text), self.matching.TEXT_CHARS)
        last = text.splitlines()[-1]
        self.assertEqual(len(last.split(" ", 2)[2]), self.matching.STEP_CHARS,
                         "every kept step is whole, cut only at STEP_CHARS")

    def test_the_closest_entry_above_the_floor_is_the_match(self):
        self.vectors = {"regenerate": [1.0, 0.0, 0.0], "restart": [0.0, 1.0, 0.0]}
        a = self._entry("a", "regenerate evalset")
        b = self._entry("b", "restart servers")
        match = self.matching.find_same([bash("regenerate evalset")], [a, b],
                                        self.config)
        self.assertEqual(match[0].id, "a")
        self.assertAlmostEqual(match[1], 1.0)

    def test_nothing_above_the_floor_is_no_match(self):
        self.vectors = {"regenerate": [1.0, 0.0, 0.0], "restart": [0.6, 0.8, 0.0]}
        a = self._entry("a", "regenerate evalset")
        self.assertIsNone(self.matching.find_same(
            [bash("restart servers")], [a], self.config))

    def test_a_parked_entry_is_still_compared(self):
        """Otherwise its next occurrence would rebuild it and undo the parking."""
        self.vectors = {"regenerate": [1.0, 0.0, 0.0]}
        a = self._entry("a", "regenerate evalset", status="dismissed")
        match = self.matching.find_same([bash("regenerate evalset")], [a],
                                        self.config)
        self.assertEqual(match[0].id, "a")

    def test_an_entry_is_embedded_once_and_again_only_when_it_changes(self):
        self.vectors = {"regenerate": [1.0, 0.0, 0.0]}
        a = self._entry("a", "regenerate evalset")
        for _ in range(3):
            self.matching.find_same([bash("regenerate evalset")], [a], self.config)
        # Every call embeds the episode; only the entry's own embeds are counted.
        self.assertEqual(len(self.calls), 3 + 1, "entry cached after the first time")

        a.steps = [bash("regenerate evalset --force")]
        self.matching.find_same([bash("regenerate evalset")], [a], self.config)
        self.assertEqual(len(self.calls), 4 + 2, "a changed text is embedded again")

    def test_a_different_embedding_model_is_not_served_from_the_cache(self):
        self.vectors = {"regenerate": [1.0, 0.0, 0.0]}
        a = self._entry("a", "regenerate evalset")
        self.matching.find_same([bash("regenerate evalset")], [a], self.config)
        before = len(self.calls)
        self.config.embed_model = "some-other-embedder"
        self.matching.find_same([bash("regenerate evalset")], [a], self.config)
        self.assertEqual(len(self.calls) - before, 2, "episode and entry both re-embedded")

    def test_an_unreachable_model_raises_rather_than_guessing(self):
        from skillpp.local import LocalModelUnavailable
        a = self._entry("a", "regenerate evalset")

        def down(text, **kw):
            raise LocalModelUnavailable("down")
        self.matching.embed = down
        with self.assertRaises(LocalModelUnavailable):
            self.matching.find_same([bash("regenerate evalset")], [a], self.config)

    def test_no_entries_is_no_match_and_no_model_call(self):
        self.assertIsNone(self.matching.find_same([bash("x")], [], self.config))
        self.assertEqual(self.calls, [])


class TestCandidatesBelongToOneProject(TempRoot):
    """A candidate, and the skill made from it, belong to the repo the work was
    done in. The same steps in another repo are another candidate: matching
    used to compare every entry of every project, and a hit simply added the
    new folder to the old candidate."""

    WORK = [bash("npm run build"), bash("./deploy.sh staging")]

    def _repo(self, name):
        repo = self.root / name
        (repo / ".git").mkdir(parents=True)
        return repo

    def _fold(self, sid, cwd):
        return fold_session(self.config, {"session_id": sid, "cwd": str(cwd), "prompts": [],
                                          "steps": judged([dict(st) for st in self.WORK])})

    def test_a_project_is_the_repo_root_above_the_folder(self):
        from skillpp.capture import project_of
        repo = self._repo("app")
        (repo / "src" / "deep").mkdir(parents=True)
        self.assertEqual(project_of(str(repo / "src" / "deep")), str(repo))
        loose = self.root / "notes"
        loose.mkdir()
        self.assertEqual(project_of(str(loose)), str(loose))
        self.assertEqual(project_of(""), "")

    def test_the_same_procedure_in_two_repos_is_two_candidates(self):
        a, b = self._repo("a"), self._repo("b")
        first, second = self._fold("s1", a), self._fold("s2", b)
        self.assertEqual((first["status"], second["status"]), ("created", "created"))
        self.assertEqual(sorted(len(e.projects) for e in Ledger(self.config).all()), [1, 1])

    def test_the_same_procedure_in_one_repo_merges_even_from_a_subfolder(self):
        repo = self._repo("app")
        (repo / "web").mkdir()
        self._fold("s1", repo)
        second = self._fold("s2", repo / "web")
        self.assertEqual(second["status"], "merged")
        self.assertEqual(second["occurrences"], 2)

    def test_an_entry_from_before_the_rule_still_matches_in_each_of_its_projects(self):
        a, b = self._repo("a"), self._repo("b")
        first = self._fold("s1", a)
        led = Ledger(self.config)
        entry = led.get(first["id"])
        entry.projects.append(str(b))         # merged across repos by the old rule
        led.save(entry)
        self.assertEqual(self._fold("s2", b)["status"], "merged")

    def test_live_capture_records_the_folder_the_session_ran_in(self):
        """The hooks carry the folder; a candidate banked from them names it.
        (Sessions folded with no folder, as the scorers do, have none.)"""
        from skillpp.capture import _load_session
        repo = self._repo("app")
        handle_prompt(self.config, {"session_id": "live", "cwd": str(repo), "prompt": "deploy"})
        for st in self.WORK:
            handle_tool(self.config, {"session_id": "live", "cwd": str(repo),
                                      "tool_name": "Bash", "tool_input": st["input"]})
        session = _load_session(self.config, "live")
        for step in session["steps"]:
            if not is_prompt(step):
                step["end"] = False
        result = fold_session(self.config, session)
        self.assertEqual(Ledger(self.config).get(result["id"]).projects, [str(repo)])

    def test_merge_never_joins_candidates_from_two_projects(self):
        from skillpp.cli import main
        a, b = self._repo("a"), self._repo("b")
        self._fold("s1", a)
        self._fold("s2", b)
        with mock.patch("sys.stdout"):
            main(["--root", str(self.config.root), "merge", "--apply"])
        self.assertEqual(len(list(Ledger(self.config).all())), 2)


class TestCaptureMatchesByEmbedding(TempRoot):
    """What changed when capture stopped using a signature."""

    WORK = [bash("npm run build"), bash("./deploy.sh staging")]

    def _session(self, sid):
        return {"session_id": sid, "cwd": "", "prompts": [],
                "steps": judged([dict(st) for st in self.WORK])}

    def _down(self):
        import skillpp.matching as matching
        from skillpp.local import LocalModelUnavailable

        def refuse(*a, **k):
            raise LocalModelUnavailable("refused")
        real, matching.embed = matching.embed, refuse
        self.addCleanup(lambda: setattr(matching, "embed", real))

    def test_a_captured_session_with_no_embedding_model_is_held_whole(self):
        """Checked before anything is saved, so no half-folded session."""
        self._down()
        result = fold_session(self.config, self._session("s1"))
        self.assertEqual(result["status"], "offline")
        self.assertIn("embed", result["reason"])
        self.assertEqual(list(Ledger(self.config).all()), [])

    def test_a_forced_fold_with_no_model_banks_unmatched(self):
        """Forced, it is banked anyway, and marked for `skillpp merge`."""
        self._down()
        result = fold_session(self.config, self._session("s1"), force=True)
        self.assertEqual(result["status"], "created")
        self.assertTrue(Ledger(self.config).get(result["id"]).unmatched)

    def test_a_miss_makes_a_duplicate_never_an_overwrite(self):
        """Ids used to be the signature, so a miss on identical work rewrote the
        existing file — status, parking and counts included."""
        from skillpp.ledger import STATUS_DISMISSED
        first = fold_session(self.config, self._session("s1"))
        led = Ledger(self.config)
        entry = led.get(first["id"])
        entry.status = STATUS_DISMISSED
        led.save(entry)

        self.config.match_floor = 1.01          # nothing can match
        second = fold_session(self.config, self._session("s2"))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(Ledger(self.config).get(first["id"]).status, STATUS_DISMISSED)

    def test_ids_are_not_derived_from_the_work(self):
        a = fold_session(self.config, self._session("s1"))["id"]
        other = Config(self.root / "elsewhere")
        other.ensure_dirs()
        b = fold_session(other, self._session("s1"))["id"]
        self.assertNotEqual(a, b)


class TestMergeCommand(TempRoot):
    """`skillpp merge`: folding what is already in the ledger."""

    def _args(self, **kw):
        import argparse
        base = dict(root=self.config.root, apply=False, floor=None)
        base.update(kw)
        return argparse.Namespace(**base)

    def _save(self, eid, sid):
        Ledger(self.config).save(Entry(
            id=eid, title=eid, sessions=[sid], occurrences=1, intents=[eid],
            steps=[bash("npm run build"), bash("./deploy.sh staging")]))

    def test_a_dry_run_changes_nothing(self):
        from skillpp.cli import cmd_merge
        self._save("a", "s1")
        self._save("b", "s2")
        cmd_merge(self._args())
        self.assertEqual(len(list(Ledger(self.config).all())), 2)

    def test_apply_folds_and_records_the_fold(self):
        """The absorbed entry's file is gone; decisions.jsonl is its only record."""
        from skillpp import decisions
        from skillpp.cli import cmd_merge
        self._save("a", "s1")
        self._save("b", "s2")
        cmd_merge(self._args(apply=True))
        entries = list(Ledger(self.config).all())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].occurrences, 2)
        notes = [r["note"] for r in decisions.read(self.config)]
        self.assertTrue(any("absorbed" in n for n in notes))

    def test_nothing_below_the_floor_is_folded(self):
        from skillpp.cli import cmd_merge
        self._save("a", "s1")
        self._save("b", "s2")
        cmd_merge(self._args(apply=True, floor=1.01))
        self.assertEqual(len(list(Ledger(self.config).all())), 2)


class TestFoldInto(TempRoot):
    """Moving one entry's evidence into another, as `skillpp merge` does."""

    RELEASE = ["git checkout main", "npm test", "git push --follow-tags"]
    RELEASE_PYTEST = ["git checkout main", "pytest -q", "git push --follow-tags"]

    def _entry(self, eid, cmds, **kw):
        base = dict(id=eid, title="cut the release", intents=["cut the release"],
                    sessions=[eid], steps=[bash(c) for c in cmds])
        base.update(kw)
        return Entry(**base)

    def test_folding_adds_the_counts_and_unions_the_sessions(self):
        from skillpp.similar import fold_into
        a = self._entry("a", self.RELEASE, sessions=["s1"], occurrences=2)
        b = self._entry("b", self.RELEASE_PYTEST, sessions=["s1"], occurrences=1)
        fold_into(a, b)
        self.assertEqual(a.occurrences, 3)
        self.assertEqual(a.sessions, ["s1"])

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
    """The decision page: candidates seen often enough, Accept or Decline, then
    Create Skill for an accepted one.

    Every action runs a real `skillpp` command in a subprocess, so these run the
    CLI end to end. The agent `skillpp draft` launches is a stub script; no model
    and no real agent are involved.
    """

    def setUp(self) -> None:
        super().setUp()
        self.ledger = Ledger(self.config)

    def _save(self, eid, occurrences=3, **kw):
        entry = Entry(id=eid, title=f"work {eid}", occurrences=occurrences,
                      sessions=[f"s{n}" for n in range(occurrences)],
                      steps=[{"tool": "Bash", "input": {"command": "npm test"}}],
                      **kw)
        self.ledger.save(entry)
        return entry

    def _rows(self):
        from skillpp.web import collect_state
        return {r["id"]: r for r in collect_state(self.config)["rows"]}

    def _agent(self, body):
        """Point `skillpp draft` at a stub agent script."""
        import os
        import shlex
        script = self.root / "agent.py"
        script.write_text("import os, pathlib, sys\n" + body, encoding="utf-8")
        os.environ["SKILLPP_AGENT"] = (f"{shlex.quote(sys.executable)} "
                                       f"{shlex.quote(str(script))} {{PROMPT}}")
        self.addCleanup(lambda: os.environ.pop("SKILLPP_AGENT", None))

    def _wait_for_draft(self, eid):
        from skillpp import web
        job = web._jobs.get(eid)
        if job:
            job.join(timeout=60)

    def test_candidates_are_split_by_whether_they_were_seen_often_enough(self):
        from skillpp.ledger import STATUS_ONE_OFF
        from skillpp.web import collect_state
        self._save("ready", occurrences=3)
        self._save("twice", occurrences=2)
        self._save("often", occurrences=7)
        self._save("parked", occurrences=5, status=STATUS_ONE_OFF)
        rows = collect_state(self.config)["rows"]
        self.assertEqual([r["id"] for r in rows], ["often", "ready", "twice"],
                         "most-seen first; one-off parking is not listed")
        by_id = {r["id"]: r for r in rows}
        self.assertTrue(by_id["ready"]["ready"])
        self.assertEqual(by_id["ready"]["state"], "undecided")
        self.assertFalse(by_id["twice"]["ready"])
        self.assertEqual(by_id["twice"]["state"], "collecting")

    def test_a_candidate_below_the_threshold_cannot_be_decided(self):
        from skillpp.ledger import STATUS_CANDIDATE
        from skillpp.web import accept, decline
        self._save("twice", occurrences=2)
        self.assertFalse(accept(self.config, "twice")["ok"])
        self.assertFalse(decline(self.config, "twice")["ok"])
        self.assertEqual(Ledger(self.config).get("twice").status, STATUS_CANDIDATE)

    def test_decisions_stay_listed_whatever_the_count(self):
        from skillpp.ledger import STATUS_DISMISSED, STATUS_PROMOTED
        self._save("yes", occurrences=1, status=STATUS_PROMOTED)
        self._save("no", occurrences=2, status=STATUS_DISMISSED)
        rows = self._rows()
        self.assertEqual(rows["yes"]["state"], "accepted")
        self.assertEqual(rows["no"]["state"], "dismissed")

    def test_accept_promotes_and_records_the_decision(self):
        from skillpp.ledger import STATUS_PROMOTED
        from skillpp.web import accept
        self._save("a")
        self.assertTrue(accept(self.config, "a")["ok"])
        self.assertEqual(Ledger(self.config).get("a").status, STATUS_PROMOTED)
        self.assertIn('"promoted"', (self.config.root / "decisions.jsonl").read_text())
        self.assertEqual(self._rows()["a"]["state"], "accepted")
        self.assertFalse(accept(self.config, "a")["ok"], "decided once")

    def test_decline_dismisses_and_records_the_decision(self):
        from skillpp.ledger import STATUS_DISMISSED
        from skillpp.web import accept, decline
        self._save("d")
        self.assertTrue(decline(self.config, "d")["ok"])
        self.assertEqual(Ledger(self.config).get("d").status, STATUS_DISMISSED)
        self.assertIn('"dismissed"', (self.config.root / "decisions.jsonl").read_text())
        self.assertEqual(self._rows()["d"]["state"], "dismissed",
                         "not the agent's 'declined', which offers a retry")
        self.assertFalse(accept(self.config, "d")["ok"], "declined stays declined")

    def test_the_clock_counts_down_from_the_last_recognition(self):
        from datetime import datetime, timedelta, timezone
        from skillpp.ledger import STATUS_PROMOTED
        from skillpp.web import days_left
        now = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)
        ago = lambda d: (now - timedelta(days=d)).isoformat()
        entry = lambda d, **kw: Entry(id="x", occurrences=kw.pop("n", 1), last_seen=ago(d), **kw)
        self.assertEqual(days_left(self.config, entry(0), now), 14)
        self.assertEqual(days_left(self.config, entry(0.5), now), 14)
        self.assertEqual(days_left(self.config, entry(10), now), 4)
        self.assertEqual(days_left(self.config, entry(13.9), now), 1)
        self.assertEqual(days_left(self.config, entry(20), now), 0)
        self.assertIsNone(days_left(self.config, entry(30, n=3), now),
                          "a candidate at the threshold is kept for review")
        self.assertIsNone(days_left(self.config, entry(30, status=STATUS_PROMOTED), now))

    def test_closest_to_expiring_first_at_the_same_count_and_expired_last(self):
        from datetime import datetime, timedelta, timezone
        from skillpp.web import collect_state
        ago = lambda d: (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()
        self._save("fresh", occurrences=1, last_seen=ago(1))
        self._save("old", occurrences=1, last_seen=ago(12))
        self._save("gone", occurrences=1, last_seen=ago(30))
        self._save("more", occurrences=2, last_seen=ago(0))
        order = [r["id"] for r in collect_state(self.config)["rows"]]
        self.assertEqual(order, ["more", "old", "fresh", "gone"])

    def test_a_declined_candidate_can_be_reinstated(self):
        from skillpp.ledger import STATUS_CANDIDATE
        from skillpp.web import decline, reinstate
        self._save("d")
        self.assertFalse(reinstate(self.config, "d")["ok"], "not declined yet")
        decline(self.config, "d")
        self.assertTrue(reinstate(self.config, "d")["ok"])
        self.assertEqual(Ledger(self.config).get("d").status, STATUS_CANDIDATE)
        self.assertEqual(self._rows()["d"]["state"], "undecided")
        log = (self.config.root / "decisions.jsonl").read_text()
        self.assertIn("declined, reinstated", log)

    def test_create_skill_needs_an_accepted_candidate(self):
        from skillpp.web import create_skill
        self._save("u")
        self.assertFalse(create_skill(self.config, "u")["ok"])
        self.assertFalse((self.config.root / "drafts" / "u").exists())

    def test_create_skill_drafts_and_never_installs(self):
        from skillpp.web import accept, create_skill
        self._agent("d = pathlib.Path(os.environ['SKILLPP_DRAFT_DIR'])\n"
                    "d.mkdir(parents=True, exist_ok=True)\n"
                    "(d / 'SKILL.md').write_text('---\\nname: x\\n---\\n')\n")
        self._save("c")
        accept(self.config, "c")
        self.assertTrue(create_skill(self.config, "c")["ok"])
        self._wait_for_draft("c")
        row = self._rows()["c"]
        self.assertEqual(row["state"], "drafted")
        self.assertTrue(row["path"].endswith("drafts/c/SKILL.md"))
        self.assertEqual(Ledger(self.config).get("c").skill_path, "")

    def _agent_echoing_its_prompt(self):
        """A stub that writes the prompt it was handed into the draft, so a
        test reads exactly what the agent read."""
        self._agent("d = pathlib.Path(os.environ['SKILLPP_DRAFT_DIR'])\n"
                    "d.mkdir(parents=True, exist_ok=True)\n"
                    "(d / 'SKILL.md').write_text('---\\nname: x\\n---\\n' + sys.argv[1])\n")

    def _drafted_with(self, eid, note):
        from skillpp.web import accept, create_skill
        self._save(eid)
        accept(self.config, eid)
        self.assertTrue(create_skill(self.config, eid, note=note)["ok"])
        self._wait_for_draft(eid)
        self.assertEqual(self._rows()[eid]["state"], "drafted")
        return (self.config.root / "drafts" / eid / "SKILL.md").read_text()

    def test_a_note_goes_from_the_page_to_the_agent(self):
        self._agent_echoing_its_prompt()
        note = '- keep "--prod"; never staging\n- the slide styling is not the point'
        self.assertIn(note, self._drafted_with("n", note))

    def test_a_note_that_looks_like_an_option_still_arrives(self):
        """Handed over as `--note <text>`, argparse reads `--dry-run` as an
        option, refuses the whole run, and the row just says Failed."""
        self._agent_echoing_its_prompt()
        self.assertIn("\n--dry-run", self._drafted_with("o", "--dry-run"))

    def test_a_note_too_long_is_refused_before_anything_runs(self):
        from skillpp.web import MAX_NOTE, accept, create_skill
        self._save("l")
        accept(self.config, "l")
        self.assertFalse(create_skill(self.config, "l", note="x" * (MAX_NOTE + 1))["ok"])
        self.assertFalse((self.config.root / "drafts" / "l").exists())
        self.assertEqual(self._rows()["l"]["state"], "accepted")

    def test_a_failed_draft_says_why_and_can_be_retried(self):
        from skillpp.web import accept, create_skill
        self._agent("print('not logged in'); sys.exit(1)\n")
        self._save("f")
        accept(self.config, "f")
        create_skill(self.config, "f")
        self._wait_for_draft("f")
        row = self._rows()["f"]
        self.assertEqual(row["state"], "failed")
        self.assertIn("not logged in", row["message"])
        self.assertTrue(create_skill(self.config, "f")["ok"], "retry allowed")
        self._wait_for_draft("f")

    def test_one_draft_run_at_a_time(self):
        import time
        from skillpp.web import _write_status, accept, create_skill
        self._save("r")
        accept(self.config, "r")
        _write_status(self.config, "r", state="running", started=time.time())
        self.assertEqual(self._rows()["r"]["state"], "creating")
        self.assertFalse(create_skill(self.config, "r")["ok"])

    def test_a_run_from_a_server_that_restarted_fails_at_once(self):
        import time
        from skillpp.web import _write_status, accept, create_skill
        self._save("o")
        accept(self.config, "o")
        _write_status(self.config, "o", state="running", started=time.time(),
                      boot="an-earlier-server")
        row = self._rows()["o"]
        self.assertEqual(row["state"], "failed")
        self.assertIn("restarted", row["message"])
        self._agent("sys.exit(1)\n")
        self.assertTrue(create_skill(self.config, "o")["ok"], "retry allowed")
        self._wait_for_draft("o")

    def test_a_run_that_never_finished_reads_as_failed(self):
        from skillpp.web import _write_status, accept
        self._save("s")
        accept(self.config, "s")
        _write_status(self.config, "s", state="running", started=0)
        self.assertEqual(self._rows()["s"]["state"], "failed")

    def test_an_installed_skill_is_shown_as_installed(self):
        from skillpp.ledger import STATUS_PROMOTED
        skill = self.root / "skills" / "x" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: x\n---\n")
        self._save("i", status=STATUS_PROMOTED, skill_path=str(skill))
        self.assertEqual(self._rows()["i"]["state"], "installed")

    def test_loading_the_page_runs_nothing(self):
        import subprocess
        self._save("a")

        def refuse(*a, **k):
            raise AssertionError("loading the page ran a command")
        real, subprocess.run = subprocess.run, refuse
        self.addCleanup(lambda: setattr(subprocess, "run", real))
        self.assertIn("a", self._rows())

    def _drafted(self, eid, name="add-eval-case", extra=None):
        from skillpp.ledger import STATUS_PROMOTED
        from skillpp.web import _write_status
        self._save(eid, status=STATUS_PROMOTED)
        folder = self.config.root / "drafts" / eid
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: \"Use when adding a case.\"\n---\n# Body\n")
        for rel, text in (extra or {}).items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_text(text)
        _write_status(self.config, eid, state="ready")

    def test_a_finished_draft_is_listed_for_review(self):
        from skillpp.web import collect_state
        self._drafted("x", extra={"references/notes.md": "n"})
        self._save("u")                                    # undecided: no draft
        drafts = collect_state(self.config)["drafts"]
        self.assertEqual([d["id"] for d in drafts], ["x"])
        self.assertEqual(drafts[0]["name"], "add-eval-case")
        self.assertEqual(drafts[0]["description"], "Use when adding a case.")
        self.assertIn("# Body", drafts[0]["body"])
        self.assertEqual(drafts[0]["files"], ["SKILL.md", "references/notes.md"],
                         "status.json is the page's bookkeeping, not the skill")

    def test_drafts_are_listed_newest_first_with_when_they_were_written(self):
        """Told apart by when, not only by name: the draft just asked for is
        the one being looked for."""
        import os
        from skillpp.web import collect_state
        self._drafted("old", name="zz-older")
        self._drafted("new", name="aa-newer")
        os.utime(self.config.root / "drafts" / "old" / "SKILL.md", (1_000_000, 1_000_000))
        drafts = collect_state(self.config)["drafts"]
        self.assertEqual([d["id"] for d in drafts], ["new", "old"])
        self.assertEqual(drafts[1]["drafted_at"], "1970-01-12T13:46:40+00:00")

    def test_a_draft_downloads_as_a_folder_ready_for_the_skills_directory(self):
        import io, zipfile
        from skillpp.web import draft_zip
        self._drafted("x", extra={"references/notes.md": "n"})
        filename, data = draft_zip(self.config, "x")
        self.assertEqual(filename, "add-eval-case.zip")
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertEqual(sorted(names), ["add-eval-case/SKILL.md",
                                         "add-eval-case/references/notes.md"])

    def test_a_downloaded_draft_is_listed_as_downloaded_until_it_changes(self):
        import io, zipfile
        from skillpp.web import collect_state, draft_zip, record_download
        self._drafted("x")
        self.assertEqual(collect_state(self.config)["drafts"][0]["downloaded_at"], "")
        record_download(self.config, "x")
        self.assertNotEqual(collect_state(self.config)["drafts"][0]["downloaded_at"], "")
        _, data = draft_zip(self.config, "x")
        self.assertEqual(zipfile.ZipFile(io.BytesIO(data)).namelist(),
                         ["add-eval-case/SKILL.md"], "the record is not part of the skill")
        skill = self.config.root / "drafts" / "x" / "SKILL.md"
        skill.write_text(skill.read_text() + "\n## Traps\n")
        self.assertEqual(collect_state(self.config)["drafts"][0]["downloaded_at"], "",
                         "a revised draft is back to review")

    def test_only_a_finished_draft_of_a_known_entry_downloads(self):
        from skillpp.web import draft_zip
        self._save("u")
        self.assertIsNone(draft_zip(self.config, "u"))
        self.assertIsNone(draft_zip(self.config, "../../etc"))

    def test_an_unsafe_skill_name_falls_back_to_the_id(self):
        from skillpp.web import draft_zip
        self._drafted("x", name="../escape")
        filename, _ = draft_zip(self.config, "x")
        self.assertEqual(filename, "x.zip")

    @unittest.skipUnless(shutil.which("node"), "needs node to run the page script")
    def test_a_draft_renders_as_markdown_and_never_as_raw_html(self):
        """The SKILL.md is written by an agent, so it is untrusted text."""
        import json, subprocess
        from skillpp.web import PAGE
        script = PAGE[PAGE.index("<script>") + len("<script>"):PAGE.rindex("load();")]
        source = ("---\nname: x\nmetadata:\n  tier: \"provisional\"\n---\n"
                  "# Title\n\nA line\ncontinued with `body_contains`.\n\n"
                  "1. **Step** one\n   ```bash\n   git add a\n   ```\n2. Step two\n"
                  "   - nested\n\n<script>alert(1)</script> <img src=x onerror=y>\n")
        program = script + f"\nprocess.stdout.write(md({json.dumps(source)}));"
        html = subprocess.run(["node", "-e", program], capture_output=True,
                              text=True, check=True).stdout
        self.assertIn("<h1>Title</h1>", html)
        self.assertIn("<p>A line continued with <code>body_contains</code>.</p>", html)
        self.assertIn("<strong>Step</strong>", html)
        self.assertIn("<pre><code>git add a</code></pre>", html)
        self.assertIn("<th>metadata.tier</th><td>provisional</td>", html)
        self.assertEqual(html.count("<li>"), html.count("</li>"))
        self.assertEqual(html.count("<ol>"), html.count("</ol>"))
        self.assertEqual(html.count("<ul>"), html.count("</ul>"))
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)

    def test_revise_needs_a_draft_and_an_instruction(self):
        from skillpp.web import revise
        self._save("u")
        self.assertFalse(revise(self.config, "u", "change it")["ok"])
        self._drafted("x")
        self.assertFalse(revise(self.config, "x", "   ")["ok"])

    def test_revise_changes_the_draft_and_keeps_history_out_of_the_download(self):
        import io, zipfile
        from skillpp.web import collect_state, draft_zip, revise
        self._agent("d = pathlib.Path(os.environ['SKILLPP_DRAFT_DIR']) / 'SKILL.md'\n"
                    "d.write_text(d.read_text() + '## Traps\\n')\n")
        self._drafted("x")
        self.assertTrue(revise(self.config, "x", "add a traps section")["ok"])
        self._wait_for_draft("x")
        draft = collect_state(self.config)["drafts"][0]
        self.assertIn("## Traps", draft["body"])
        self.assertFalse(draft["revising"])
        self.assertEqual(draft["message"], "")
        self.assertEqual(draft["files"], ["SKILL.md"])
        _, data = draft_zip(self.config, "x")
        self.assertEqual(zipfile.ZipFile(io.BytesIO(data)).namelist(),
                         ["add-eval-case/SKILL.md"])

    def test_a_failed_revision_keeps_the_draft_and_says_why(self):
        from skillpp.web import collect_state, revise
        self._agent("print('not logged in'); sys.exit(1)\n")
        self._drafted("x")
        revise(self.config, "x", "add a traps section")
        self._wait_for_draft("x")
        draft = collect_state(self.config)["drafts"][0]
        self.assertIn("# Body", draft["body"])
        self.assertIn("failed", draft["message"])
        self.assertEqual(self._rows()["x"]["state"], "drafted")

    def test_a_revision_in_progress_is_shown_and_not_started_twice(self):
        import time
        from skillpp.web import _write_status, collect_state, revise
        self._drafted("x")
        _write_status(self.config, "x", state="revising", started=time.time())
        self.assertEqual(self._rows()["x"]["state"], "revising")
        self.assertTrue(collect_state(self.config)["drafts"][0]["revising"])
        self.assertFalse(revise(self.config, "x", "again")["ok"])
        _write_status(self.config, "x", state="revising", started=0)
        self.assertEqual(self._rows()["x"]["state"], "drafted")
        self.assertIn("did not finish", self._rows()["x"]["message"])

    QUESTIONS = ("# Body\n\n## Open questions\n\n- Is it always `x`, or can it\n"
                 "  also be `y`?\n2. Which runner?\n\n---\n\n_footer_\n")

    GAPS = ("# Body\n\n## Known gaps\n\n- Is it always `x`?\n\n---\n\n_f_\n")

    def test_the_scaffolds_old_heading_is_read_as_questions_too(self):
        """`## Known gaps` was the scaffold's name for the same section and
        nothing read it, so two real drafts carried four unanswered questions,
        showed no answer fields and downloaded freely."""
        from skillpp.web import draft_zip, split_open_questions
        questions, rest = split_open_questions(self.GAPS)
        self.assertEqual(questions, ["Is it always `x`?"])
        self.assertNotIn("Known gaps", rest)
        self._drafted("x", extra={"SKILL.md": self.GAPS})
        self.assertIsNone(draft_zip(self.config, "x"),
                          "a draft with unanswered gaps downloaded")

    def test_open_questions_are_read_out_of_the_draft(self):
        from skillpp.web import split_open_questions
        questions, rest = split_open_questions(self.QUESTIONS)
        self.assertEqual(questions, ["Is it always `x`, or can it also be `y`?",
                                     "Which runner?"])
        self.assertNotIn("Open questions", rest)
        self.assertIn("# Body", rest)
        self.assertIn("_footer_", rest, "the section ends at the rule")
        self.assertEqual(split_open_questions("# Body\n"), ([], "# Body\n"))

    def test_a_draft_with_open_questions_shows_them_and_will_not_download(self):
        from skillpp.web import collect_state, draft_zip
        self._drafted("x")
        skill = self.config.root / "drafts" / "x" / "SKILL.md"
        skill.write_text(skill.read_text() + "\n" + self.QUESTIONS)
        draft = collect_state(self.config)["drafts"][0]
        self.assertEqual(len(draft["questions"]), 2)
        self.assertNotIn("Open questions", draft["body"])
        self.assertIsNone(draft_zip(self.config, "x"))

    def test_answers_revise_the_draft_until_it_downloads(self):
        from skillpp.web import answer_questions, collect_state, draft_zip
        self._agent(
            "import re\n"
            "d = pathlib.Path(os.environ['SKILLPP_DRAFT_DIR']) / 'SKILL.md'\n"
            "prompt = sys.argv[-1]\n"
            "assert 'A: always x' in prompt, prompt\n"
            "t = re.sub(r'## Open questions[\\s\\S]*?(?=\\n---)', '', d.read_text())\n"
            "d.write_text(t.replace('# Body', '# Body\\n\\nIt is always x.'))\n")
        self._drafted("x")
        skill = self.config.root / "drafts" / "x" / "SKILL.md"
        skill.write_text(skill.read_text() + "\n" + self.QUESTIONS)
        self.assertFalse(answer_questions(self.config, "x", [
            {"question": "Which runner?", "answer": "  "}])["ok"], "nothing answered")
        self.assertTrue(answer_questions(self.config, "x", [
            {"question": "Is it always `x`, or can it also be `y`?",
             "answer": "always x"}])["ok"])
        self._wait_for_draft("x")
        draft = collect_state(self.config)["drafts"][0]
        self.assertEqual(draft["questions"], [], draft["message"])
        self.assertIn("It is always x.", draft["body"])
        self.assertIsNotNone(draft_zip(self.config, "x"))

    def test_the_outline_uses_the_agents_own_step_descriptions(self):
        from skillpp.web import step_outline
        steps = [
            {"tool": "Bash", "input": {"command": "./start.sh", "description": "Restart servers"}},
            {"tool": "Bash", "input": {"command": "./start.sh", "description": "Restart servers"}},
            {"tool": "Edit", "input": {"file_path": "/repo/eval/cases.json"}},
            {"tool": "mcp__adk-docs__fetch_docs", "input": {"url": "https://x"}},
            {"tool": "Bash", "input": {"command": "npm   test"}},
            {"tool": "Stated", "input": {"text": "send the summary"}},
            # Its input is scrubbed to nothing; what came back still says it all.
            {"tool": "AskUserQuestion", "input": {},
             "tool_returned": 'User has answered your questions: "Bundle or split?"="Split". Go on.'},
        ]
        self.assertEqual(step_outline(steps), ["Restart servers", "Edit cases.json",
                                               "adk-docs: fetch docs", "npm test",
                                               "send the summary",
                                               "Asked you: Bundle or split? → Split"])

    def test_a_summary_is_asked_on_demand_cached_and_redone_when_the_entry_grows(self):
        import skillpp.local as local
        from skillpp.web import summarise
        self._save("a")
        calls = []
        real = local.ask
        def fake(model, prompt, **kw):
            calls.append(model)
            # Two lines, the shape `candidate_summary.md` asks for, plus a
            # labelled block the model sometimes appends anyway.
            return ("Running a project's test suite\n"
                    "The developer ran the tests.\n\n**Concrete thing:** tests")
        local.ask = fake
        self.addCleanup(lambda: setattr(local, "ask", real))

        self.assertEqual(self._rows()["a"]["summary"], "", "loading asks no model")
        self.assertEqual(calls, [])
        self.assertEqual(summarise(self.config, "a")["summary"], "Ran the tests.")
        summarise(self.config, "a")
        self.assertEqual(calls, [self.config.local_model], "cached, asked once")
        self.assertEqual(self._rows()["a"]["summary"], "Ran the tests.")
        self.assertEqual(Ledger(self.config).get("a").description, "",
                         "never written into the skill description")

        entry = Ledger(self.config).get("a")
        entry.steps.append({"tool": "Bash", "input": {"command": "git commit -m x"}})
        Ledger(self.config).save(entry)
        self.assertEqual(self._rows()["a"]["summary"], "")
        summarise(self.config, "a")
        self.assertEqual(len(calls), 2)

    def test_a_row_says_where_the_pattern_was_recognized(self):
        """"Seen in" is one line per recognition, not per distinct session."""
        from skillpp.web import seen_runs
        entry = self._save("a", occurrences=2, seen=[
            {"session": "s1", "at": "2026-09-18T09:00:00+00:00"},
            {"session": "s1", "at": "2026-09-18T11:00:00+00:00"}])
        self.assertEqual(seen_runs(entry),
                         [{"session": "s1", "at": "2026-09-18T09:00:00+00:00"},
                          {"session": "s1", "at": "2026-09-18T11:00:00+00:00"}])
        self.assertEqual(self._rows()["a"]["seen"], seen_runs(entry))

    def test_an_entry_from_before_the_record_shows_ids_without_a_time(self):
        """`created` and `last_seen` only bound the range; printing either
        against every run would be inventing when the work happened."""
        from skillpp.web import seen_runs
        entry = self._save("old", occurrences=2)
        entry.seen = []
        self.assertEqual(seen_runs(entry),
                         [{"session": "s0", "at": ""}, {"session": "s1", "at": ""}])

    def test_a_transcript_is_only_read_for_something_shaped_like_a_session(self):
        """The id reaches `_find_transcript` as a glob, so the page cannot
        steer it at another directory."""
        from skillpp.web import transcript
        for bad in ("../../etc", "a/b", "*", ""):
            self.assertEqual(transcript(self.config, bad)["error"], "not a session id")
        missing = transcript(self.config, "nosuchsession0000")
        self.assertFalse(missing["ok"])
        self.assertIn("no transcript", missing["error"])

    def test_every_state_has_its_own_label_on_the_page(self):
        """A dismissed row once rendered as "Agent declined" with a retry
        button, because both states were called "declined"."""
        from skillpp.web import PAGE
        for state in ("collecting", "undecided", "accepted", "creating", "drafted", "revising",
                      "installed", "failed", "declined", "dismissed"):
            self.assertIn(f'"{state}"', PAGE)

    def test_the_page_carries_no_external_references(self):
        """Dependency-free on purpose, and offline by consequence."""
        from skillpp.web import PAGE
        for bad in ("http://", "https://", "//cdn.", "<script src", "<link"):
            self.assertNotIn(bad, PAGE)


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
        # A set pointed at and found empty is a mistake worth failing on; no
        # public sessions recorded yet is not.
        if not cls.docs and not os.environ.get("SKILLPP_FIXTURES"):
            raise unittest.SkipTest(
                "no recorded sessions in tests/fixtures/sessions; point "
                "SKILLPP_FIXTURES at a set to score one")

    def test_every_banked_episode_has_a_family(self):
        """`recurrence.py` refuses to score if labels and episodes disagree.

        A family label per banked episode is the ground truth for whether
        repeated work becomes one candidate. If segmentation changes how many
        episodes a session banks, the labels must be rewritten, not silently
        misaligned. How many each family should hold belongs to the set, so it
        lives beside the sessions, in `expected.json`.
        """
        import recurrence
        expected = self.score.expected().get("family_sizes")
        if not expected:
            self.skipTest("this set has no expected.json family_sizes")
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(Path(tmp) / "skillpp")
            config.ensure_dirs()
            rows = recurrence.fold_all(config)
        sizes = recurrence.evaluate(rows)["sizes"]
        for family, size in expected.items():
            self.assertEqual(sizes.get(family), size, family)

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


class TestFoldResumesAfterOutage(TempRoot):
    """An embedding outage mid-fold holds the session, and a retry resumes.

    Before this the exception escaped the hook: the file stayed unstamped, the
    episodes before the outage were already banked with nothing recording it,
    and folding the file again counted them a second time.
    """

    def setUp(self):
        super().setUp()
        self.calls = self._stub_judge(is_marker)
        self.down = False
        import skillpp.matching as matching
        from skillpp.local import LocalModelUnavailable

        def flaky(text, **kw):
            # Down only for the second episode, so the first is banked first.
            if self.down and "pytest" in text:
                raise LocalModelUnavailable("could not reach an embedding model")
            return _stub_embed(text)
        patcher = mock.patch.object(matching, "embed", flaky)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _capture(self, session_id="two"):
        from skillpp.capture import handle_prompt, handle_tool
        handle_prompt(self.config, {"session_id": session_id, "cwd": "/r",
                                    "prompt": "ship both"})
        for command in ("npm test", "git commit -m 'a'",
                        "pytest -q", "git commit -m 'b'"):
            handle_tool(self.config, {"session_id": session_id, "cwd": "/r",
                                      "tool_name": "Bash",
                                      "tool_input": {"command": command}})

    def _occurrences(self):
        return sorted((e.steps[0]["input"]["command"], e.occurrences, e.unmatched)
                      for e in Ledger(self.config).all())

    def test_a_worker_killed_after_one_episode_does_not_count_twice(self):
        """`folded` is what stops a retry re-banking. It used to reach disk only
        on the offline path, so a worker killed mid-fold lost it — and every
        recognition counts, so one crash could reach the threshold alone."""
        import skillpp.capture as capture
        from skillpp.capture import fold_session_now, handle_session_end

        self._capture()
        real = capture._fold_steps
        seen = []

        def die_after_one(config, session, steps, **kw):
            if seen:
                raise KeyboardInterrupt("worker killed")
            seen.append(steps)
            return real(config, session, steps, **kw)

        capture._fold_steps = die_after_one
        self.addCleanup(lambda: setattr(capture, "_fold_steps", real))
        with self.assertRaises(KeyboardInterrupt):
            handle_session_end(self.config, {"session_id": "two"})

        capture._fold_steps = real
        fold_session_now(self.config, "two")
        self.assertEqual(self._occurrences(),
                         [("npm test", 1, False), ("pytest -q", 1, False)])

    def test_an_outage_mid_fold_holds_the_session(self):
        from skillpp.capture import _session_file, handle_session_end
        self._capture()
        self.down = True
        result = handle_session_end(self.config, {"session_id": "two"})

        self.assertEqual(result["status"], "offline")
        self.assertIn("after 1 of 2 episodes", result["reason"])
        saved = json.loads(_session_file(self.config, "two").read_text(encoding="utf-8"))
        self.assertTrue(saved["held"]["at"])
        self.assertEqual(saved["folded"], [0])
        self.assertEqual(self._occurrences(), [("npm test", 1, False)])

    def test_a_retry_resumes_without_counting_twice(self):
        from skillpp.capture import _session_file, handle_session_end
        self._capture()
        self.down = True
        handle_session_end(self.config, {"session_id": "two"})

        self.down = False
        self.calls.clear()
        result = handle_session_end(self.config, {"session_id": "two"})

        self.assertNotEqual(result["status"], "offline")
        self.assertEqual(self._occurrences(),
                         [("npm test", 1, False), ("pytest -q", 1, False)])
        self.assertEqual(self.calls, [], "the judge was asked again on retry")
        self.assertFalse(_session_file(self.config, "two").exists())


class TestTurns(TempRoot):
    """A candidate keeps its run as a conversation: prompt, reply, what it used.

    The reply to a prompt was lost at capture — `_narration` only looks when a
    tool fires, so replies to consecutive prompts overwrote each other and a
    real run kept none of 4,900 characters. A draft written from the steps
    alone restated another skill's internals instead of the procedure.
    """

    def setUp(self):
        super().setUp()
        self.transcript = self.root / "transcript.jsonl"
        self.rows: list[dict] = []

    # -- a transcript written as the session goes ----------------------------
    def _user(self, text):
        self.rows.append({"type": "user", "message": {"role": "user", "content": text}})
        self._flush()

    def _agent(self, *blocks):
        self.rows.append({"type": "assistant", "message": {"content": list(blocks)}})
        self._flush()

    def _flush(self):
        self.transcript.write_text(
            "".join(json.dumps(r) + "\n" for r in self.rows), encoding="utf-8")

    def _prompt(self, text, *, written_first=True):
        from skillpp.capture import handle_prompt
        if written_first:
            self._user(text)
        handle_prompt(self.config, {"session_id": "s", "cwd": "/r", "prompt": text,
                                    "transcript_path": str(self.transcript)})
        if not written_first:
            self._user(text)

    def _tool(self, name, tool_input):
        from skillpp.capture import handle_tool
        self._agent({"type": "tool_use", "name": name, "input": tool_input})
        handle_tool(self.config, {"session_id": "s", "cwd": "/r", "tool_name": name,
                                  "tool_input": tool_input,
                                  "transcript_path": str(self.transcript)})

    def _markers(self):
        from skillpp.capture import _load_session
        return [st for st in _load_session(self.config, "s")["steps"] if is_prompt(st)]

    def _run(self):
        """Three prompts: a tool call after the first, none after the second."""
        self._prompt("propose the slides")
        self._agent({"type": "thinking", "thinking": "SECRET REASONING"},
                    {"type": "text", "text": "Reading the source."})
        self._tool("Bash", {"command": "npm test"})
        self._agent({"type": "text", "text": "Here are 9 slides."})
        self._prompt("check every claim and cut to 6")
        self._agent({"type": "text", "text": "All claims backed. Cut to 6."})
        self._user("<system-reminder>not typed by anyone</system-reminder>")
        self._user("[Image: original 2001x1125]")
        self._agent({"type": "text", "text": "Slide render looks fine."})
        self._prompt("build it", written_first=False)
        self._agent({"type": "text", "text": "Building with the pptx skill."})
        self._tool("Skill", {"skill": "anthropic-skills:pptx"})
        self._tool("mcp__notion__search", {"query": "house style"})
        self._tool("Skill", {"skill": "anthropic-skills:pptx"})

    def test_the_last_reply_is_attached_before_the_judge_runs(self):
        """The judge can be shown what the assistant said. Live it has to see
        what the benchmark sees, and the benchmark has every reply — so the
        session's last reply is attached first, as the keep path already did."""
        import skillpp.boundary as boundary
        from skillpp.capture import handle_session_end
        self._run()
        self._agent({"type": "text", "text": "Built the deck."})
        seen = {}

        def fake(config, session):
            markers = [s for s in session["steps"] if is_prompt(s)]
            seen["last_reply"] = markers[-1].get("reply")
            return 0
        real = boundary.judge_session
        boundary.judge_session = fake
        self.addCleanup(lambda: setattr(boundary, "judge_session", real))
        handle_session_end(self.config, {"session_id": "s",
                                         "transcript_path": str(self.transcript)})
        # Judged first, it had no reply at all.
        self.assertIsNotNone(seen["last_reply"])
        self.assertTrue(seen["last_reply"].endswith("Built the deck."))

    def test_each_prompt_gets_the_reply_that_followed_it(self):
        from skillpp.capture import _load_session, _save_session, _attach_reply
        self._run()
        first, second, third = self._markers()
        self.assertEqual(first["reply"], "Reading the source.\n\nHere are 9 slides.")
        # No tool call in between: the case that was lost.
        self.assertEqual(second["reply"],
                         "All claims backed. Cut to 6.\n\nSlide render looks fine.")
        self.assertNotIn("reply", third)
        session = _load_session(self.config, "s")
        _attach_reply(session, str(self.transcript))
        _save_session(self.config, session)
        self.assertEqual(self._markers()[2]["reply"], "Building with the pptx skill.")

    def test_thinking_is_not_the_reply(self):
        self._run()
        self.assertNotIn("SECRET REASONING", json.dumps(self._markers()))

    def test_a_long_reply_is_clipped_and_scrubbed(self):
        from skillpp.capture import _REPLY_CHARS
        token = "ghp_" + "a" * 36
        self._prompt("go")
        self._agent({"type": "text", "text": token + " " + "word " * 3000})
        self._prompt("next")
        reply = self._markers()[0]["reply"]
        self.assertNotIn(token, reply)
        self.assertLessEqual(len(reply), _REPLY_CHARS + 2)
        self.assertTrue(reply.endswith(" …"))

    def test_the_candidate_keeps_the_turns_and_what_each_used(self):
        from skillpp.capture import handle_session_end
        self._run()
        handle_session_end(self.config, {"session_id": "s",
                                         "transcript_path": str(self.transcript)})
        (entry,) = Ledger(self.config).all()
        self.assertEqual([t["prompt"] for t in entry.turns],
                         ["propose the slides", "check every claim and cut to 6", "build it"])
        self.assertEqual(entry.turns[2]["reply"], "Building with the pptx skill.")
        self.assertEqual([t["used"] for t in entry.turns],
                         [[], [], ["skill anthropic-skills:pptx", "mcp mcp__notion__search"]])

    def test_each_episode_holds_only_its_own_turns(self):
        from skillpp.capture import handle_session_end
        self._prompt("release the backend")
        self._agent({"type": "text", "text": "Releasing."})
        self._tool("Bash", {"command": "npm test"})
        self._tool("Bash", {"command": "git commit -m 'backend'"})
        self._prompt("now write the changelog")
        self._agent({"type": "text", "text": "Writing it."})
        self._tool("Write", {"file_path": "/r/CHANGELOG.md", "content": "x"})
        self._tool("Bash", {"command": "git commit -m 'changelog'"})
        handle_session_end(self.config, {"session_id": "s",
                                         "transcript_path": str(self.transcript)})
        turns = sorted([t["prompt"] for t in e.turns] for e in Ledger(self.config).all())
        self.assertEqual(turns, [["now write the changelog"], ["release the backend"]])

    def test_a_merge_keeps_the_first_runs_turns(self):
        from skillpp.capture import fold_session
        def run(session, reply):
            return {"session_id": session, "cwd": "/r", "steps": [
                {"tool": "UserPrompt", "input": {"text": "ship the release now"}, "reply": reply},
                {"tool": "Bash", "input": {"command": "npm test"}, "end": False},
                {"tool": "Bash", "input": {"command": "git commit -m x"}, "end": True}]}
        fold_session(self.config, run("s1", "done"))
        result = fold_session(self.config, run("s2", "done done"))
        self.assertEqual(result["status"], "merged")
        (entry,) = Ledger(self.config).all()
        self.assertEqual([t["reply"] for t in entry.turns], ["done"])

    def test_show_for_a_draft_gives_turns_not_steps(self):
        import io
        from contextlib import redirect_stdout
        from skillpp.cli import main
        Ledger(self.config).save(Entry(
            id="aaaaaaaaaaaa", title="t",
            steps=[{"tool": "Bash", "input": {"command": "npm test"}}],
            turns=[{"prompt": "p", "reply": "r", "used": []}]))
        Ledger(self.config).save(Entry(
            id="bbbbbbbbbbbb", title="old",
            steps=[{"tool": "Bash", "input": {"command": "npm test"}}]))

        def show(*args):
            out = io.StringIO()
            with redirect_stdout(out):
                main(["--root", str(self.config.root), "show", *args])
            return json.loads(out.getvalue())
        drafted = show("aaaaaaaaaaaa", "--json", "--draft")
        self.assertEqual(drafted["turns"], [{"prompt": "p", "reply": "r", "used": []}])
        self.assertNotIn("steps", drafted)
        self.assertNotIn("questions", drafted)
        self.assertIn("steps", show("aaaaaaaaaaaa", "--json"))
        self.assertEqual(show("bbbbbbbbbbbb", "--json", "--draft"),
                         show("bbbbbbbbbbbb", "--json"))


class TestFoldPending(TempRoot):
    """Sessions that ended without being banked are banked later.

    Nine of ten unbanked real sessions were desktop sessions whose `SessionEnd`
    fired while the local model did not answer, so they were held — and nothing
    ever retried a held session.
    """

    def _session(self, sid, *, held=False, hours_ago=0.0, ending_minutes_ago=None):
        from datetime import datetime, timedelta, timezone

        from skillpp.capture import _session_file
        steps = [{"tool": "UserPrompt", "input": {"text": f"ship {sid}"}},
                 {"tool": "Bash", "input": {"command": "npm test"}},
                 {"tool": "Bash", "input": {"command": "git commit -m x"}}]
        doc = {"session_id": sid, "cwd": "/r", "prompts": [], "steps": steps}
        if held:
            doc["held"] = {"at": "2026-09-07T13:02:00+00:00", "reason": "no verdicts"}
        if ending_minutes_ago is not None:
            stamped = (datetime.now(timezone.utc)
                       - timedelta(minutes=ending_minutes_ago))
            doc["ending"] = stamped.isoformat(timespec="seconds")
        path = _session_file(self.config, sid)
        path.write_text(json.dumps(doc), encoding="utf-8")
        stamp = time.time() - hours_ago * 3600
        os.utime(path, (stamp, stamp))
        return path

    def test_a_held_session_is_banked_once_the_model_answers(self):
        from skillpp.capture import fold_pending
        path = self._session("held1", held=True)
        (result,) = fold_pending(self.config)
        self.assertEqual(result["status"], "created")
        self.assertFalse(path.exists())
        self.assertEqual(len(list(Ledger(self.config).all())), 1)

    def test_a_held_session_stays_held_while_the_model_is_silent(self):
        from skillpp.capture import fold_pending
        self._stub_judge(None)
        path = self._session("held1", held=True)
        (result,) = fold_pending(self.config)
        self.assertEqual(result["status"], "offline")
        self.assertTrue(json.loads(path.read_text())["held"])
        self.assertEqual(list(Ledger(self.config).all()), [])

    def test_a_recent_session_is_live_and_an_old_one_has_ended(self):
        from skillpp.capture import fold_pending
        recent = self._session("recent", hours_ago=1)
        old = self._session("old", hours_ago=13)
        results = {r["session"]: r["status"] for r in fold_pending(self.config)}
        self.assertEqual(results, {"old": "created", "recent": "live"})
        self.assertTrue(recent.exists())
        self.assertFalse(old.exists())

    def test_the_starting_session_is_left_alone(self):
        from skillpp.capture import fold_pending
        path = self._session("starting", held=True)
        self.assertEqual(fold_pending(self.config, exclude="starting"), [])
        self.assertTrue(path.exists())

    def test_a_running_fold_is_not_joined_but_a_dead_one_is_ignored(self):
        from skillpp.capture import fold_pending
        self._session("held1", held=True)
        lock = self.config.root / "fold-pending.lock"
        lock.write_text("")
        self.assertEqual(fold_pending(self.config), [{"status": "locked"}])
        stamp = time.time() - 2 * 3600
        os.utime(lock, (stamp, stamp))
        self.assertEqual(fold_pending(self.config)[0]["status"], "created")
        self.assertFalse(lock.exists())

    def test_a_chat_with_no_tool_calls_is_not_held_as_offline(self):
        from skillpp.capture import _session_file, fold_pending
        path = _session_file(self.config, "chat")
        path.write_text(json.dumps({
            "session_id": "chat", "cwd": "/r", "prompts": [],
            "held": {"at": "2026-09-07T13:02:00+00:00", "reason": "no verdicts"},
            "steps": [{"tool": "UserPrompt", "input": {"text": "make it an I"}},
                      {"tool": "UserPrompt", "input": {"text": "add a closing"}}]}))
        (result,) = fold_pending(self.config)
        self.assertEqual(result["status"], "too-thin")
        self.assertFalse(path.exists())

    def test_a_killed_worker_is_retried_in_minutes_not_hours(self):
        """A hook killed mid-fold leaves no `held` stamp, so before `ending`
        existed it was indistinguishable from a live session and waited out the
        12-hour idle rule."""
        from skillpp.capture import fold_pending
        path = self._session("killed", ending_minutes_ago=5, hours_ago=0)
        (result,) = fold_pending(self.config)
        self.assertEqual(result["status"], "created")
        self.assertFalse(path.exists())
        self.assertEqual(len(list(Ledger(self.config).all())), 1)

    def test_a_session_ending_moments_ago_is_not_stolen(self):
        """The grace is what stops the sweep racing a worker that is still
        starting up."""
        from skillpp.capture import fold_pending
        self._session("fresh", ending_minutes_ago=0.1, hours_ago=0)
        (result,) = fold_pending(self.config)
        self.assertEqual(result["status"], "live")

    def test_a_worker_still_folding_is_left_alone(self):
        import socket
        from skillpp.capture import _lock_file, fold_pending
        path = self._session("busy", ending_minutes_ago=5, hours_ago=0)
        _lock_file(self.config, "busy").write_text(
            json.dumps({"pid": os.getpid(), "host": socket.gethostname()}),
            encoding="utf-8")
        (result,) = fold_pending(self.config)
        self.assertEqual(result["status"], "folding")
        self.assertTrue(path.exists())
        self.assertEqual(list(Ledger(self.config).all()), [])

    def test_the_sweep_folds_inline_rather_than_spawning(self):
        """Ten pending sessions must not become ten model calls at once: they
        serialise at Ollama anyway, and that is the failure being fixed."""
        from skillpp.capture import fold_pending
        self._session("one", held=True)
        with mock.patch("subprocess.Popen") as popen:
            fold_pending(self.config)
        popen.assert_not_called()
        self.assertEqual(len(list(Ledger(self.config).all())), 1)

    def test_an_orphan_lock_is_cleaned_up(self):
        """A SIGKILLed fold never runs the `finally`, and the session it banked
        is already gone — so nothing else would ever remove its lock."""
        import socket
        from skillpp.capture import _lock_file, fold_pending
        lock = _lock_file(self.config, "gone")
        lock.write_text(json.dumps({"pid": 999999,
                                    "host": socket.gethostname()}),
                        encoding="utf-8")
        fold_pending(self.config)
        self.assertFalse(lock.exists())

    def test_an_orphan_lock_from_another_machine_is_left_to_age(self):
        """A foreign pid says nothing about this box, so the age ceiling is the
        only safe way to decide — never `os.kill` on a number from elsewhere."""
        from skillpp.capture import _FOLD_LOCK_SECONDS, _lock_file, fold_pending
        lock = _lock_file(self.config, "elsewhere")
        lock.write_text(json.dumps({"pid": 999999, "host": "another-machine"}),
                        encoding="utf-8")
        fold_pending(self.config)
        self.assertTrue(lock.exists())
        stamp = time.time() - _FOLD_LOCK_SECONDS - 60
        os.utime(lock, (stamp, stamp))
        fold_pending(self.config)
        self.assertFalse(lock.exists())

    def test_session_start_spawns_the_fold_and_does_not_wait(self):
        import io
        from skillpp.cli import main
        real_stdin = sys.stdin
        sys.stdin = io.StringIO('{"session_id": "s1"}')
        self.addCleanup(lambda: setattr(sys, "stdin", real_stdin))
        with mock.patch("subprocess.Popen") as popen:
            code = main(["--root", str(self.config.root), "hook", "--event", "SessionStart"])
        self.assertEqual(code, 0)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-3:], ["fold-pending", "--exclude", "s1"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        popen.return_value.wait.assert_not_called()


class TestFrontmatterCharacters(unittest.TestCase):
    """A description with a dash or an accent survives the scaffold and the
    reader, instead of reaching the review page as `\\u2014`."""

    def test_an_escaped_description_reads_as_its_characters(self):
        text = '---\nname: x\ndescription: "Use when a \\u2014 b"\n---\n'
        self.assertEqual(parse_frontmatter(text)["description"], "Use when a \u2014 b")

    def test_the_scaffold_writes_characters_as_they_are(self):
        entry = Entry(id="e1", title="t", steps=[{"tool": "Bash", "input": {"command": "ls"}}])
        text = scaffold_skill(entry, name="x", description="Use when café \u2014 ok")
        self.assertIn('description: "Use when café \u2014 ok"', text)
        self.assertEqual(parse_frontmatter(text)["description"], "Use when café \u2014 ok")


class TestSessionCatalogue(unittest.TestCase):
    """The public sessions' ground truth, the kit that records them, and the
    builder that turns a recording into a fixture against that truth."""

    SESSIONS = Path(__file__).resolve().parent / "fixtures" / "sessions"

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(cls.SESSIONS))
        import from_transcript
        cls.ft = from_transcript
        cls.plans = from_transcript.catalogue()

    @staticmethod
    def _stream(*parts):
        """`"p:text"` is a prompt, anything else one work step."""
        from skillpp.segment import PROMPT_TOOL
        return [{"tool": PROMPT_TOOL, "input": {"text": p[2:]}} if p.startswith("p:")
                else {"tool": "Bash", "input": {"command": p}} for p in parts]

    def test_every_prompt_in_the_catalogue_is_in_the_kit_word_for_word(self):
        kit = (self.SESSIONS / "RECORDING.md").read_text(encoding="utf-8")
        for sid, plan in self.plans.items():
            for n, prompt in enumerate(plan["prompts"], 1):
                with self.subTest(session=sid, prompt=n):
                    self.assertIn(f"`{prompt['text']}`", kit)

    def test_the_catalogue_adds_up(self):
        """Every cut starts a task, and every task but look-only has a family."""
        for sid, plan in self.plans.items():
            with self.subTest(sid):
                n = len(plan["prompts"])
                self.assertTrue(all(1 < c <= n for c in plan["cuts"]), plan["cuts"])
                tasks = len(plan["cuts"]) + 1
                self.assertEqual(len(plan["families"]), 0 if "detect.look-only" in plan["checks"]
                                 else tasks)
                self.assertEqual(len(plan["subjects"]), len(plan["families"]))
                for c in plan["cuts"]:
                    self.assertEqual(plan["prompts"][c - 1]["role"], "switch")

    def test_a_cut_before_a_prompt_lands_after_the_last_step_before_it(self):
        steps = self._stream("p:one", "a", "b", "p:more", "c", "p:two", "d", "e")
        self.assertEqual(self.ft.boundary_after(steps, [3]), [3])

    def test_a_cut_with_no_work_before_it_is_refused(self):
        steps = self._stream("p:one", "p:two", "a")
        with self.assertRaises(SystemExit):
            self.ft.boundary_after(steps, [2])

    def test_an_answer_to_the_agent_does_not_shift_the_cut(self):
        """The developer typed a reply the catalogue did not plan: the cut
        still falls where the planned prompt starts the new task."""
        planned = [{"role": "explore", "text": "Look at the code first"},
                   {"role": "switch", "text": "Now write the release notes"}]
        steps = self._stream("p:Look at the code first", "a",
                             "p:yes, the second option", "b",
                             "p:Now write the release notes", "c", "d")
        which = self.ft.align(steps, planned)
        self.assertEqual(which, [1, "answer", 2])
        self.assertEqual(self.ft.boundary_after(steps, [2], which), [2])

    def test_a_planned_prompt_missing_from_the_recording_is_refused(self):
        planned = [{"role": "explore", "text": "Look at the code first"},
                   {"role": "switch", "text": "Now write the release notes"}]
        with self.assertRaises(SystemExit):
            self.ft.align(self._stream("p:Look at the code first", "a"), planned)

    def test_a_task_under_three_steps_is_flagged(self):
        steps = self._stream("p:one", "a", "b", "c", "p:two", "d")
        self.assertEqual(self.ft.thin_tasks(steps, [3]), [(2, 1)])

    def test_the_judge_is_scored_by_the_role_of_each_prompt(self):
        import score
        steps = self._stream("p:start", "a", "b", "p:fix it", "c", "p:other job", "d", "e")
        steps[2]["end"] = True          # a cut at the correction: false
        steps[4]["end"] = False         # no cut at the switch: missed
        doc = {"tag": "t", "steps": steps,
               "truth": {"roles": ["explore", "correct", "switch"], "boundary_after": [3]}}
        self.assertEqual(score.cuts_by_role(doc), {
            "correct": {"asked": 1, "false": 1, "cuts": 0, "missed": 0},
            "switch": {"asked": 1, "false": 0, "cuts": 1, "missed": 1}})

    def test_a_gap_shared_by_two_prompts_belongs_to_the_first(self):
        """A switch answered in words alone, then "create it": one gap, the switch's."""
        import score
        steps = self._stream("p:start", "a", "b", "p:new job, propose only", "p:create it", "c", "d")
        steps[2]["end"] = True
        doc = {"tag": "t", "steps": steps,
               "truth": {"roles": ["explore", "switch", "implement"], "boundary_after": [2]}}
        self.assertEqual(score.cuts_by_role(doc),
                         {"switch": {"asked": 1, "false": 0, "cuts": 1, "missed": 0}})

    def test_merged_pairs_are_counted_by_how_alike_the_runs_are(self):
        import recurrence
        rows = [{"family": "f", "entry": "e1", "kind": "code", "level": "identical", "subject": "x"},
                {"family": "f", "entry": "e1", "kind": "code", "level": "identical", "subject": "x"},
                {"family": "f", "entry": "e2", "kind": "code", "level": None, "subject": "y"}]
        levels = recurrence.evaluate(rows)["levels"]
        self.assertEqual(levels[("identical", "code")], {"should": 1, "merged": 1})
        self.assertEqual(levels[("different subject", "code")], {"should": 2, "merged": 0})

    @unittest.skipIf(shutil.which("git") is None, "needs git")
    def test_setup_gives_code_sessions_a_repo_committed_by_nobody_real(self):
        import subprocess
        setup = self.SESSIONS / "recording" / "setup.sh"
        with tempfile.TemporaryDirectory() as tmp:
            for sid in ("C-3", "P-2said"):
                subprocess.run(["sh", str(setup), sid, tmp], check=True,
                               capture_output=True)
            log = subprocess.run(["git", "-C", f"{tmp}/C-3", "log", "--format=%an <%ae>"],
                                 capture_output=True, text=True, check=True).stdout
            self.assertEqual(log.strip(), "Recorder <recorder@example.com>")
            self.assertEqual(sorted(os.listdir(f"{tmp}/P-2said")),
                             ["CHANGELOG.md", "meeting-1.md"])

    def test_the_seed_repo_lacks_every_feature_the_prompts_ask_for(self):
        """A feature prompt asking for what already works would measure
        nothing: `isalnum()` keeps `é`, so the seed must keep ASCII only."""
        seed = str(self.SESSIONS / "recording" / "seed-repo")
        sys.path.insert(0, seed)
        try:
            from textkit.slugify import slugify
            from textkit.titlecase import titlecase
            from textkit import wordfreq
        finally:
            sys.path.remove(seed)
            for name in [m for m in sys.modules if m == "textkit" or m.startswith("textkit.")]:
                del sys.modules[name]
        self.assertEqual(slugify("Café au lait"), "caf-au-lait")
        self.assertEqual(titlecase("don't stop the music"), "Don'T Stop The Music")
        for option in ("--top", "--min-length", "--json"):
            with self.subTest(option), self.assertRaises(SystemExit), \
                    mock.patch("sys.stderr"):
                wordfreq.main([option, "3", "f.txt"])


class TestDraftCheck(unittest.TestCase):
    """The criteria a drafted SKILL.md is held to, run on hand-written drafts,
    so the checker is known to fail what it should before a real draft runs."""

    GOOD = """---
name: talk-deck-from-docs
description: "Use when someone needs a short talk built from a document: outline first, approval, then the deck."
metadata:
  provenance: "ledger:abc123"
---

# talk-deck-from-docs

## When to use

Someone asks for slides drawn from a document they point at.

## Procedure

1. Read the source material the user names.
2. Propose an outline of the slides; keep to the length the user asks for, e.g. 6 slides.
3. Check every claim in the outline against the source and report what is not backed.
4. Wait for the user to approve the outline before building anything.
5. Build the deck as a .pptx file.
6. Open the file and check the slides match the approved outline.
"""
    LOG = "# 2026-09-22T08:00:00+00:00  exit 0  60.0s\n"

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(Path(__file__).resolve().parent / "benchmarks"))
        import draft_check
        cls.dc = draft_check
        data = draft_check.load_cases()
        cls.case, cls.hedge = data["cases"]["draft.procedure.deck"], data["hedge"]

    def _failed(self, text, log=None):
        rows = self.dc.evaluate(text, self.LOG if log is None else log, "abc123",
                                {"tag": "5d99e183", "steps": [{"tool_returned": "[main 1a2b3c4] x"}]},
                                self.case, self.hedge)
        return {r["id"] for r in rows if not r["ok"]}

    def test_a_good_draft_passes_every_criterion(self):
        self.assertEqual(self._failed(self.GOOD), set())

    def test_building_before_approval_fails(self):
        swapped = self.GOOD.replace(
            "4. Wait for the user to approve the outline before building anything.\n"
            "5. Build the deck as a .pptx file.",
            "4. Build the deck as a .pptx file.\n"
            "5. Wait for the user to approve it.")
        self.assertIn("order D4<=D5", self._failed(swapped))

    def test_a_setting_from_that_day_stated_as_a_rule_fails(self):
        rule = self.GOOD.replace("keep to the length the user asks for, e.g. 6 slides",
                                 "always use 6 slides")
        self.assertIn("settings", self._failed(rule))

    def test_anything_from_the_recording_fails(self):
        self.assertIn("G8", self._failed(self.GOOD + "\nSee ${HOME}/skillpp-recordings.\n"))
        self.assertIn("G8", self._failed(self.GOOD + "\nIt was commit 1a2b3c4.\n"))

    def test_pasted_shell_fails(self):
        block = "\n```bash\ncd x\npython3 a.py\npython3 b.py\ngit commit\n```\n"
        self.assertIn("G9", self._failed(self.GOOD + block))

    def test_the_run_name_in_the_description_fails(self):
        self.assertIn("meta", self._failed(self.GOOD.replace("from a document", "from the skillpp README")))

    def test_a_declined_or_failed_run_fails(self):
        self.assertIn("G1", self._failed(self.GOOD, log="# t  exit 1  1.0s\n"))
        self.assertIn("G1", self._failed(self.GOOD, log=self.LOG + "SKILLPP-DECLINE: one bug\n"))

    def test_the_bare_template_fails(self):
        self.assertIn("G5", self._failed(self.GOOD + "\n<!-- skillpp:write-the-procedure -->\n"))

    def test_steps_written_as_subheadings_are_read_as_steps(self):
        steps = self.GOOD.split("## Procedure")[1]
        as_headings = "\n".join(
            "### " + line if line[:2].strip(".").isdigit() else line for line in steps.splitlines())
        self.assertEqual(self._failed(self.GOOD.split("## Procedure")[0] + "## Procedure" + as_headings), set())

    def test_a_wrapped_question_with_its_mark_mid_item_passes(self):
        text = self.GOOD + ("\n## Open questions\n\n- Is six the default, or asked each time? The run\n"
                            "  used six without asking.\n")
        self.assertNotIn("G10", self._failed(text))

    def test_a_setting_asked_about_is_not_a_rule(self):
        text = self.GOOD + "\n## Open questions\n\n- Should it always be 6 slides?\n"
        self.assertNotIn("settings", self._failed(text))

    def test_a_negation_on_the_line_before_does_not_count_as_building(self):
        wrapped = self.GOOD.replace(
            "2. Propose an outline of the slides;",
            "2. Propose an outline of the slides. Do not\n   generate the deck yet;")
        self.assertEqual(self._failed(wrapped), set())

    def test_reader_is_not_read(self):
        spec = {"all": [r"\b(read|reads|reading|look at|inspect|explore)\b", r"\b(propose|assess)"]}
        self.assertIsNone(self.dc.find_concept(["1. Propose it so the reader sees why."], spec, {}))
        self.assertEqual(self.dc.find_concept(["1. Read the file and propose it."], spec, {}), 1)

    def test_the_judge_reply_is_read_from_its_json(self):
        reply = 'Here you go:\n[{"id": "D1", "answer": "yes", "quote": "x"}]\nDone.'
        self.assertEqual(self.dc.parse_judge(reply), [{"id": "D1", "answer": "yes", "quote": "x"}])
        self.assertEqual(self.dc.parse_judge("no json here"), [])

    def test_a_judged_yes_counts_only_with_a_quote_from_the_draft(self):
        answers = [{"id": "D4", "answer": "yes", "quote": "4. Wait for the user to approve the outline before building anything."},
                   {"id": "D5", "answer": "yes", "quote": "Build the slides at once without asking."},
                   {"id": "D6", "answer": "no", "quote": ""}]
        rows = {r["id"]: r for r in self.dc.judged_rows(self.GOOD, self.case, answers)}
        self.assertTrue(rows["D4"]["ok"])
        self.assertFalse(rows["D5"]["ok"], "an invented quote is not evidence")
        self.assertFalse(rows["D6"]["ok"])
        self.assertFalse(rows["D1"]["ok"], "an unanswered question fails")

    def test_a_quote_matches_despite_markdown_emphasis(self):
        text = self.GOOD.replace("5. Build the deck as a .pptx file.", "5. **Build** the deck as a `.pptx` file.")
        rows = self.dc.judged_rows(text, self.case,
                                   [{"id": "D5", "answer": "yes", "quote": "5. Build the deck as a .pptx file."}])
        self.assertTrue(next(r for r in rows if r["id"] == "D5")["ok"])

    def test_an_open_question_that_is_a_statement_fails(self):
        self.assertIn("G10", self._failed(self.GOOD + "\n## Open questions\n\n- The limit is unclear.\n"))


class TestNothingPrivateIsTracked(unittest.TestCase):
    """Everything tracked here is published. Real sessions once carried a
    colleague's name, an internal hostname and an account id into the repo;
    `scripts/leak_guard.py` checks the generic shapes of those on every run.
    The terms specific to one person's work are checked by the same script
    with a denylist kept outside the repo."""

    def test_no_home_path_address_id_or_credential_is_tracked(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import leak_guard
        self.assertEqual(leak_guard.scan_tree([]), [])

    def test_the_guard_sees_what_it_is_for(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import re
        import leak_guard
        # Assembled here, so this file does not trip the scan it tests.
        hits = leak_guard.scan_text("x", "\n".join([
            "cd /" + "Users/jane/work", "mail j.doe" + "@employer.de",
            "id " + "-".join(["12345678", "90ab", "cdef", "1234", "567890abcdef"]),
            "sk-" + "ant-api03-abcdefghijklmnopqrstuvwxyz",
            "the Mergecommand ran", "see Internal_Project docs"]),
            [re.compile(r"(?<![A-Za-z0-9])internal(?![A-Za-z0-9])", re.I),
             re.compile(r"(?<![A-Za-z0-9])command(?![A-Za-z0-9])", re.I)])
        kinds = [hit.split(": ")[1] for hit in hits]
        self.assertEqual(kinds, ["home path", "email", "uuid", "secret", "denylist"])

    def test_a_recorded_session_cannot_keep_half_a_home_path(self):
        """The fixture builder cut each field to 2,000 characters before
        templating `$HOME`, so a path crossing the cut kept the home folder and
        the first letter of the account name, and passed the check."""
        sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "sessions"))
        import from_transcript
        home = os.path.expanduser("~")
        uid = "-".join(["12345678", "90ab", "cdef", "1234", "567890abcdef"])
        row = {"text": "x" * 1990 + home + "/proj/a.py, account " + uid}
        text = from_transcript._template(row)["text"]
        self.assertNotIn(os.path.basename(home), text[:2000])
        self.assertTrue(text[:2000].endswith("${HOME}/pr"))
        self.assertNotIn(uid, text)


class TestShippedCommands(unittest.TestCase):
    """The slash commands ship inside the package, so an installed copy has
    them. `install` used to copy `/skillpp-review` only, and `--remove` left
    even that one behind."""

    def test_install_copies_the_commands_a_developer_types(self):
        from skillpp.install import install_command_files
        with tempfile.TemporaryDirectory() as tmp:
            written = install_command_files(Path(tmp) / "commands")
            self.assertEqual(sorted(p.name for p in written),
                             ["skillpp-new.md", "skillpp-review.md"])
            self.assertTrue(all(p.read_text(encoding="utf-8").strip() for p in written))

    def test_remove_keeps_a_command_the_developer_edited(self):
        from skillpp.install import install_command_files, remove_command_files
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "commands"
            install_command_files(target)
            (target / "skillpp-new.md").write_text("mine now", encoding="utf-8")
            removed = remove_command_files(target)
            self.assertEqual(sorted(p.name for p in removed), ["skillpp-review.md"])
            self.assertEqual((target / "skillpp-new.md").read_text(), "mine now")

    def test_an_installed_package_hooks_through_its_console_script(self):
        """A PYTHONPATH into a venv's site-packages names the Python version and
        breaks with the next upgrade; the console script's path does not."""
        from skillpp.install import MARKER, hook_command
        with tempfile.TemporaryDirectory() as site:
            installed = hook_command(package_root=Path(site),
                                     script="/home/dev/.local/bin/skillpp")
            self.assertEqual(installed, "/home/dev/.local/bin/skillpp hook")
            spaced = hook_command(package_root=Path(site),
                                  script="/Users/dev/My Tools/skillpp")
            self.assertIn("-m skillpp hook", spaced)
            self.assertTrue(all(MARKER in c for c in (installed, spaced)))


class TestInstallModels(TempRoot):
    """`install` brings the two local models too, so a new user runs one
    command instead of learning Ollama's first. Ollama itself is an app with its
    own installer: missing, it is explained, and the hooks go in anyway."""

    def setUp(self):
        super().setUp()
        import skillpp.cli as cli
        self.cli = cli
        self.project = self.root / "proj"
        (self.project / ".claude").mkdir(parents=True)
        self.pulled = []
        self._stub(models=[self.config.local_model], err="")
        pull = lambda config, name: self.pulled.append(name) or ""
        saved = cli._pull_model
        cli._pull_model = pull
        self.addCleanup(lambda: setattr(cli, "_pull_model", saved))

    def _stub(self, models, err):
        saved = self.cli._available_models
        self.cli._available_models = lambda config: (models, err)
        self.addCleanup(lambda: setattr(self.cli, "_available_models", saved))

    def _run(self, *argv):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("sys.stderr"):
            code = self.cli.main(["--root", str(self.config.root), "install",
                                  "--project", str(self.project), *argv])
        return code, out.getvalue()

    def test_a_dry_run_names_the_missing_model_and_downloads_nothing(self):
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("nomic-embed-text", out)
        self.assertIn("--apply downloads it", out)
        self.assertEqual(self.pulled, [])

    def test_apply_pulls_only_what_is_missing_after_the_hooks(self):
        code, out = self._run("--apply")
        self.assertEqual(code, 0)
        self.assertEqual(self.pulled, [self.config.embed_model])
        self.assertLess(out.index("wrote"), out.index("models (Ollama"))

    def test_hooks_already_in_place_still_bring_the_models(self):
        self._run("--apply", "--no-models")
        self.assertEqual(self.pulled, [])
        self._run("--apply")
        self.assertEqual(self.pulled, [self.config.embed_model])

    def test_without_ollama_the_hooks_go_in_and_it_says_how_to_get_it(self):
        self._stub(models=[], err="connection refused")
        code, out = self._run("--apply")
        self.assertEqual(code, 0)
        self.assertIn("Ollama is not running", out)
        self.assertEqual(self.pulled, [])
        self.assertTrue((self.project / ".claude" / "settings.json").exists())

    def test_removing_leaves_the_models_alone(self):
        self._run("--apply")
        self.pulled.clear()
        code, out = self._run("--remove", "--apply")
        self.assertEqual(code, 0)
        self.assertNotIn("models (Ollama", out)

    def test_a_pull_reports_progress_and_errors_from_the_stream(self):
        import io
        from contextlib import redirect_stdout
        from skillpp import cli
        real = _REAL_PULL
        lines = [b'{"status":"pulling","total":100,"completed":50}\n',
                 b'{"status":"success"}\n']
        with mock.patch("urllib.request.urlopen") as urlopen, redirect_stdout(io.StringIO()) as out:
            urlopen.return_value.__enter__.return_value = lines
            self.assertEqual(real(self.config, "m"), "")
        self.assertIn(" 50%", out.getvalue())
        with mock.patch("urllib.request.urlopen") as urlopen, redirect_stdout(io.StringIO()):
            urlopen.return_value.__enter__.return_value = [b'{"error":"pull model manifest: not found"}\n']
            self.assertIn("not found", real(self.config, "m"))


class TestInstallScopes(TempRoot):
    """Where the hooks go is said out loud, and they come back out cleanly.

    The default used to be user-level and silent, which is how this repo ran
    for weeks with hooks wired by hand into one project and none in
    `~/.claude/settings.json` — the command never named the file it wrote.
    """

    def setUp(self):
        super().setUp()
        self.project = self.root / "proj"
        (self.project / ".claude").mkdir(parents=True)
        self.settings = self.project / ".claude" / "settings.json"

    def _run(self, *argv):
        from skillpp.cli import main
        return main(["--root", str(self.config.root), "install", *argv])

    def _events(self):
        from skillpp.install import installed_events
        return installed_events(self.settings)

    def test_no_scope_writes_nothing_and_says_so(self):
        self.assertEqual(self._run("--apply"), 2)

    def test_a_project_install_touches_only_that_project(self):
        home_before = (Path.home() / ".claude" / "settings.json")
        stamp = home_before.stat().st_mtime if home_before.exists() else None
        self.assertEqual(self._run("--project", str(self.project), "--apply"), 0)
        self.assertEqual(len(self._events()), 4)
        if stamp is not None:
            self.assertEqual(home_before.stat().st_mtime, stamp,
                             "wrote the user's settings during a project install")

    def test_a_second_apply_changes_nothing(self):
        self._run("--project", str(self.project), "--apply")
        before = self.settings.read_text()
        self._run("--project", str(self.project), "--apply")
        self.assertEqual(self.settings.read_text(), before)

    def test_remove_leaves_a_foreign_hook_in_place(self):
        self.settings.write_text(json.dumps({
            "theme": "dark",
            "hooks": {"PostToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": "echo mine"}]}]},
        }), encoding="utf-8")
        self._run("--project", str(self.project), "--apply")
        self.assertEqual(len(self._events()), 4)

        self.assertEqual(self._run("--project", str(self.project),
                                   "--remove", "--apply"), 0)
        self.assertEqual(self._events(), [])
        left = json.loads(self.settings.read_text())
        self.assertEqual(left["theme"], "dark")
        self.assertIn("echo mine", json.dumps(left["hooks"]["PostToolUse"]))

    def test_the_hook_command_pins_no_interpreter(self):
        """It used to write `/opt/homebrew/opt/python@3.14/bin/python3.14`,
        which stops existing at the next upgrade and means nothing on anyone
        else's machine. skillpp is stdlib-only, so any python3 runs it."""
        from skillpp.install import hook_command
        command = hook_command()
        self.assertIn("python3 -m skillpp hook", command)
        self.assertNotIn(sys.executable, command)
        self.assertIn("-m skillpp hook", hook_command("/usr/bin/python3"))

    def test_installed_events_reads_the_marker(self):
        from skillpp.install import installed_events
        self.assertEqual(installed_events(self.settings), [])
        self.settings.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "*", "hooks": [{"type": "command",
                                        "command": "echo mine"}]}]}}),
            encoding="utf-8")
        self.assertEqual(installed_events(self.settings), [])
        self.assertEqual(installed_events(self.root / "nope.json"), [])


class TestDoctor(TempRoot):
    """One command that answers "is skillpp actually running?"."""

    def setUp(self) -> None:
        super().setUp()
        # Doctor asks Ollama which models it holds. Answered here, so the result
        # does not depend on whether this machine runs Ollama.
        import skillpp.cli as cli
        real = cli._available_models
        cli._available_models = lambda config: ([], "not reachable (stubbed)")
        self.addCleanup(lambda: setattr(cli, "_available_models", real))

    def _run(self, *argv):
        import io
        from skillpp.cli import main
        out = io.StringIO()
        real = sys.stdout
        sys.stdout = out
        try:
            code = main(["--root", str(self.config.root), "doctor", *argv])
        finally:
            sys.stdout = real
        return code, out.getvalue()

    def test_it_names_the_missing_events(self):
        """PostToolUse without SessionEnd captures every step and banks none of
        it, which reads as working."""
        from skillpp.install import desired_hooks
        settings = self.root / "settings.json"
        hooks = desired_hooks()
        del hooks["SessionEnd"]
        settings.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        _, out = self._run("--settings", str(settings))
        self.assertIn("MISSING SessionEnd", out)
        self.assertIn("nothing will be banked", out)

    def test_it_says_when_nothing_is_wired(self):
        _, out = self._run("--settings", str(self.root / "absent.json"))
        self.assertIn("not wired", out)

    def test_it_counts_sessions_waiting(self):
        from skillpp.capture import _session_file
        for sid, doc in (("held1", {"held": {"at": "x", "reason": "y"}}),
                         ("live1", {})):
            _session_file(self.config, sid).write_text(
                json.dumps({"session_id": sid, "prompts": [], "steps": [],
                            **doc}), encoding="utf-8")
        _, out = self._run("--settings", str(self.root / "absent.json"))
        self.assertIn("1 waiting to be banked", out)


class TestScaffoldCommand(TempRoot):
    """`skillpp scaffold` had no test at all — `--out`, the mode it picks, and
    the missing-entry path were all unexercised."""

    def _entry(self, eid, *, turns=None):
        entry = Entry(id=eid, signature="s", title=f"work {eid}",
                      deps_cli=["npm"], steps=[bash("npm run build")],
                      turns=turns or [])
        Ledger(self.config).save(entry)
        return entry

    def _run(self, *argv):
        import io
        from skillpp.cli import main
        out, err = io.StringIO(), io.StringIO()
        real_out, real_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = main(["--root", str(self.config.root), "scaffold", *argv])
        finally:
            sys.stdout, sys.stderr = real_out, real_err
        return code, out.getvalue(), err.getvalue()

    def test_a_conversational_candidate_is_scaffolded_facts_only(self):
        self._entry("a", turns=[{"prompt": "go", "reply": "done", "used": []}])
        path = self.root / "SKILL.md"
        code, out, err = self._run("a", "--name", "n", "--description", "d",
                                   "--out", str(path))
        self.assertEqual(code, 0)
        text = path.read_text()
        self.assertNotIn("## Steps", text)
        self.assertIn("skillpp:write-the-procedure", text)
        self.assertIn("facts only", err)
        self.assertIn(str(path), out)

    def test_a_candidate_without_turns_keeps_its_steps(self):
        self._entry("b")
        path = self.root / "b.md"
        _, _, err = self._run("b", "--name", "n", "--description", "d",
                              "--out", str(path))
        self.assertIn("## Steps", path.read_text())
        self.assertIn("full", err)

    def test_the_mode_can_be_overridden(self):
        self._entry("c", turns=[{"prompt": "go", "reply": "ok", "used": []}])
        path = self.root / "c.md"
        self._run("c", "--name", "n", "--description", "d", "--body", "full",
                  "--out", str(path))
        self.assertIn("## Steps", path.read_text())

    def test_an_unknown_candidate_writes_nothing(self):
        code, _, err = self._run("nope", "--name", "n")
        self.assertEqual(code, 1)
        self.assertIn("No ledger entry", err)


class TestDraftInput(TempRoot):
    """What the drafting agent is handed by `show --json --draft`."""

    def _show(self, eid):
        import io
        from skillpp.cli import main
        out, real = io.StringIO(), sys.stdout
        sys.stdout = out
        try:
            main(["--root", str(self.config.root), "show", eid, "--json", "--draft"])
        finally:
            sys.stdout = real
        return json.loads(out.getvalue())

    def test_the_agent_is_told_what_the_run_deleted(self):
        """It cannot see tool calls, and whether a deletion matters is its
        call to make — so the commands reach it as input, never as output."""
        Ledger(self.config).save(Entry(
            id="d1", signature="s", title="render a deck",
            steps=[bash("node build.js"), bash("rm -f slide-*.jpg")],
            turns=[{"prompt": "build it", "reply": "built", "used": []}]))
        shown = self._show("d1")
        self.assertEqual(shown["destructive"], ["rm -f slide-*.jpg"])
        self.assertNotIn("steps", shown)

    def test_the_agent_is_not_handed_garbage_dependencies(self):
        Ledger(self.config).save(Entry(
            id="d2", signature="s", title="t", deps_cli=["')", "cp", "npm"],
            steps=[bash("npm test")],
            turns=[{"prompt": "go", "reply": "ok", "used": []}]))
        self.assertEqual(self._show("d2")["deps_cli"], ["npm"])


class TestFoldWithoutMatching(TempRoot):
    """Detection measured on its own. Scoring a session folds it, and folding
    ran matching: two episodes cut from one session that looked alike merged
    back into one entry, so the count read as if the judge had never cut."""

    def test_matching_off_banks_every_episode_and_never_embeds(self):
        import skillpp.matching as matching
        from skillpp.capture import fold_session

        def forbidden(*a, **k):
            raise AssertionError("the embedding model was asked")
        real = matching.embed
        matching.embed = forbidden
        self.addCleanup(lambda: setattr(matching, "embed", real))
        self.config.match_candidates = False
        steps = [{"tool": "UserPrompt", "input": {"text": "ship it"}},
                 {"tool": "Bash", "input": {"command": "npm test"}, "end": False},
                 {"tool": "Bash", "input": {"command": "git commit -m a"}, "end": True},
                 {"tool": "UserPrompt", "input": {"text": "ship it again"}},
                 {"tool": "Bash", "input": {"command": "npm test"}, "end": False},
                 {"tool": "Bash", "input": {"command": "git commit -m b"}, "end": True}]
        result = fold_session(self.config, {"session_id": "s", "cwd": "/r",
                                            "prompts": [], "steps": steps})
        self.assertNotEqual(result["status"], "offline",
                            "off by choice is not an outage")
        entries = list(Ledger(self.config).all())
        self.assertEqual(len(entries), 2, "two identical episodes stay two")
        self.assertTrue(all(e.unmatched for e in entries))


class TestJudgeInput(unittest.TestCase):
    """What the boundary judge is shown, and that the defaults show exactly
    what was measured. Each optional slot exists to be measured one at a time
    (`tests/benchmarks/judge_replay.py`); none may move production."""

    # Every judge prompt over a set of recorded sessions, rendered before the
    # slots existed: each session's steps set to "not a boundary", every gap
    # asked in order, the stub answering "no" so no span ever resets. How many
    # there are and their digest belong to the set, so they live in its
    # `expected.json` under "judge_prompts".

    def setUp(self):
        import skillpp.boundary as boundary
        self.boundary = boundary
        saved = {k: getattr(boundary, k) for k in (
            "REPLY_BEFORE_CHARS", "REPLY_AFTER_CHARS", "STEP_OUTPUT_CHARS",
            "JUDGE_THINKS", "PRIOR_STEPS", "NEXT_STEPS", "SHOW_NEXT",
            "NEXT_LABEL", "ask")}
        self.addCleanup(lambda: [setattr(boundary, k, v) for k, v in saved.items()])

    def _prompts(self):
        import hashlib
        sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "sessions"))
        import score
        seen = []
        self.boundary.ask = lambda model, prompt, **kw: (seen.append(prompt), "no")[1]
        for doc in score.load():
            steps = doc["steps"]
            for step in steps:
                if not is_prompt(step):
                    step["end"] = False
            for index, said, follow in self.boundary.gaps(steps):
                self.boundary.judge_gap(steps, index, said, follow,
                                        model="m", host="h")
        return seen, hashlib.sha256("\x00".join(seen).encode()).hexdigest()

    def test_the_defaults_render_every_measured_prompt_unchanged(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "sessions"))
        import score
        pinned = score.expected().get("judge_prompts")
        if not pinned:
            self.skipTest("this set has no expected.json judge_prompts")
        seen, digest = self._prompts()
        self.assertEqual(len(seen), pinned["count"])
        self.assertEqual(digest, pinned["sha256"])

    def _gap(self, **slots):
        b = self.boundary
        for name, value in slots.items():
            setattr(b, name, value)
        steps = [
            {"tool": "UserPrompt", "input": {"text": "find the blank cards"},
             "reply": "Looked through both files. Where do you suspect the blanks are?"},
            {"tool": "Bash", "input": {"command": "grep -n card book.py"},
             "tool_returned": "12: card walkthrough"},
            {"tool": "UserPrompt", "input": {"text": "Separate job: add a case"},
             "reply": "Added atlas_card_staged to cases.json."},
            {"tool": "Bash", "input": {"command": "npm test"}},
        ]
        (index, said, follow), = b.gaps(steps)
        goal, prior = b.window(steps[:index])
        return b.build_prompt(goal, prior, steps[index], b.said_text(said),
                              [b.render_step(s) for s in follow],
                              b.gap_extras(steps, index, said))

    def test_each_slot_renders_in_its_place(self):
        text = self._gap(REPLY_BEFORE_CHARS=400, REPLY_AFTER_CHARS=400,
                         STEP_OUTPUT_CHARS=400)
        order = [text.index(s) for s in (
            "Now they ran", "It returned:", "12: card walkthrough",
            "After that, the assistant told them:", "Where do you suspect",
            "Then they say:", "Separate job", "The assistant answered:",
            "Added atlas_card_staged", "What they do next:")]
        self.assertEqual(order, sorted(order))

    def test_an_off_slot_adds_nothing(self):
        text = self._gap()
        for label in ("It returned:", "assistant told them", "assistant answered"):
            self.assertNotIn(label, text)

    def test_the_reply_before_is_its_tail_and_after_is_its_head(self):
        """The hand-off is at the end of what was said; how the new instruction
        was read is at the start of the answer to it."""
        text = self._gap(REPLY_BEFORE_CHARS=30, REPLY_AFTER_CHARS=30)
        before = text.split("assistant told them:\n\n    ")[1].split("\n")[0]
        after = text.split("assistant answered:\n\n    ")[1].split("\n")[0]
        # The end of what was said, cut at a word, never mid-word.
        self.assertEqual(before, "… you suspect the blanks are?")
        self.assertNotIn("Looked through", text)
        # The start of the answer, cut the same way.
        self.assertEqual(after, "Added atlas_card_staged to …")

    def test_the_next_steps_section_can_be_removed_or_relabelled(self):
        """With thinking on, the model judged the steps under "What they do
        next" instead of the instruction; both settings exist to measure that."""
        shown = self._gap()
        self.assertIn("What they do next:\n    ran `npm test`\n\nIs that", shown)
        removed = self._gap(SHOW_NEXT=False)
        self.assertNotIn("What they do next", removed)
        self.assertNotIn("npm test", removed)
        self.assertIn("Separate job: add a case\n\nIs that a new job", removed)
        relabelled = self._gap(SHOW_NEXT=True,
                               NEXT_LABEL="In answer to that, the assistant then:")
        self.assertIn("In answer to that, the assistant then:\n    ran `npm test`",
                      relabelled)

    def test_thinking_runs_at_one_context_size(self):
        """Sized per prompt, every thinking call reloaded the model."""
        b = self.boundary
        b.JUDGE_THINKS = True
        sent = {}
        b.ask = lambda model, prompt, **kw: (sent.update(kw), "no")[1]
        b.judge({"tool": "Bash", "input": {"command": "x"}}, model="m", host="h")
        self.assertEqual(sent["num_ctx"], b._THINK_CTX)

    def test_full_shows_everything(self):
        b = self.boundary
        self.assertEqual(b._head("a b c d", b.FULL), "a b c d")
        self.assertEqual(b._tail("a b c d", b.FULL), "a b c d")

    def test_a_reply_is_never_read_as_a_placeholder(self):
        """Replies carry braces and JSON; filling slots one after another would
        substitute `{NEXT}` inside a reply as if it were the template's."""
        b = self.boundary
        b.REPLY_BEFORE_CHARS = 400
        steps = [
            {"tool": "UserPrompt", "input": {"text": "go"},
             "reply": 'Wrote {"key": 1} and the literal {NEXT} marker.'},
            {"tool": "Bash", "input": {"command": "a"}},
            {"tool": "UserPrompt", "input": {"text": "more"}},
            {"tool": "Bash", "input": {"command": "b"}},
        ]
        (index, said, follow), = b.gaps(steps)
        goal, prior = b.window(steps[:index])
        text = b.build_prompt(goal, prior, steps[index], b.said_text(said),
                              [b.render_step(s) for s in follow],
                              b.gap_extras(steps, index, said))
        self.assertIn("the literal {NEXT} marker", text)

    def test_thinking_gets_room_and_time_and_a_clean_answer(self):
        b = self.boundary
        b.JUDGE_THINKS = True
        sent = {}

        def fake(model, prompt, **kw):
            sent.update(kw)
            return "yes"
        b.ask = fake
        step = {"tool": "Bash", "input": {"command": "npm test"}}
        self.assertTrue(b.judge(step, model="m", host="h"))
        self.assertIs(sent["think"], True)
        self.assertGreaterEqual(sent["timeout"], 180)
        self.assertGreater(sent["num_ctx"], 4096)
        # Reasoning leaked into the answer is no verdict, not a guessed one.
        b.ask = lambda model, prompt, **kw: "Well, no — on reflection yes it is"
        meta: dict = {}
        self.assertIsNone(b.judge(step, model="m", host="h", meta=meta))
        self.assertTrue(meta["unclean"])

    def test_the_default_judge_never_thinks(self):
        b = self.boundary
        sent = {}
        b.ask = lambda model, prompt, **kw: (sent.update(kw), "no")[1]
        b.judge({"tool": "Bash", "input": {"command": "x"}}, model="m", host="h")
        self.assertIs(sent["think"], False)
        self.assertNotIn("reserve", sent)


class TestWebRefusesOtherPages(TempRoot):
    """The review page binds to loopback, which keeps other machines out but
    not other pages in the same browser. A POST starts `claude -p` with Write
    and Edit, and the page used to accept one from anywhere: its own calls sent
    `text/plain`, a body any site may send cross-site without asking."""

    def setUp(self) -> None:
        super().setUp()
        import threading
        from skillpp.web import serve
        self.httpd = serve(self.config, port=0, open_browser=False)
        self.port = self.httpd.server_address[1]
        worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def _ask(self, method, path, body=None, **headers):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()

    def _json_post(self, **headers):
        return self._ask("POST", "/api/accept", json.dumps({"id": "nope"}),
                         **{"Content-Type": "application/json", **headers})

    def test_its_own_page_and_local_tools_get_through(self):
        status, body = self._json_post(Origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["error"], "no such entry")
        self.assertEqual(self._json_post()[0], 200, "curl sends no Origin")
        self.assertEqual(self._ask("GET", "/api/state")[0], 200)

    def test_a_body_another_site_could_send_is_refused(self):
        status, _ = self._ask("POST", "/api/accept", json.dumps({"id": "nope"}),
                              **{"Content-Type": "text/plain;charset=UTF-8"})
        self.assertEqual(status, 415)

    def test_a_post_from_another_origin_is_refused(self):
        self.assertEqual(self._json_post(Origin="https://example.com")[0], 403)
        self.assertEqual(self._json_post(Origin="null")[0], 403)

    def test_a_rebound_host_is_refused_even_for_reading(self):
        status, _ = self._ask("GET", "/api/state", Host=f"attacker.example:{self.port}")
        self.assertEqual(status, 403)

    def test_the_page_sends_json(self):
        from skillpp.web import PAGE
        self.assertIn('headers: {"Content-Type": "application/json"}', PAGE)
        self.assertNotIn('method:"POST", body:', PAGE)


class TestStepGroups(unittest.TestCase):
    """A candidate's steps under the request each one served, as the page shows
    them. The flat list read as thirty-odd tool calls with nothing to say which
    request they belonged to, most of them only looking around, and its cut at
    thirty hid the commit."""

    def _entry(self, turns, steps, projects=()):
        return Entry(id="g", signature="s", title="t", projects=list(projects),
                     turns=[{"prompt": p, "reply": r, "used": []} for p, r in turns],
                     steps=steps)

    @staticmethod
    def _step(serves, tool="Bash", closing="", **kw):
        return {"tool": tool, "input": kw, "serves": serves, "closing_note": closing}

    def _three_requests(self):
        # The episode starts at the session's third prompt, so `serves` runs 3-5.
        return self._entry(
            [("fetch the ADK docs first", "Fetched the docs: an eval case has an id and a query."),
             ("add the case", "Added case_x to cases.json after its sibling. JSON valid."),
             ("commit", "Committed as abc123. Working tree clean.")],
            [self._step(3, "mcp__adk-docs__fetch_docs",
                        closing="Fetched the docs: an eval case has an id"),
             self._step(4, "Read", file_path="/r/eval/cases.json"),
             self._step(4, command="grep -n expect_card /r/eval/cases.json",
                        description="Find card patterns"),
             self._step(4, "Edit", file_path="/r/eval/cases.json"),
             self._step(4, command="python3 -c 'import json'", description="Validate JSON",
                        closing="Added case_x to cases.json after its sibling."),
             self._step(5, command="git commit -m case", description="Commit")])

    def test_steps_sit_under_the_request_they_served(self):
        from skillpp.web import step_groups
        groups = step_groups(self._three_requests())
        self.assertEqual([g["request"] for g in groups],
                         ["fetch the ADK docs first", "add the case", "commit"])
        self.assertEqual([g["tools"] for g in groups], [1, 4, 1])
        add = groups[1]
        self.assertEqual([(line["kind"], line["text"]) for line in add["lines"]],
                         [("look", "Read cases.json · Find card patterns"),
                          ("do", "Edit cases.json"), ("do", "Validate JSON")])
        self.assertEqual([d["text"] for d in add["digest"]], ["Edit cases.json", "Validate JSON"])
        self.assertEqual(add["looks"], 2)
        self.assertEqual(groups[0]["lines"][0]["text"], "adk-docs: fetch docs")

    def test_steps_that_cannot_be_placed_for_certain_keep_the_flat_list(self):
        """A list shifted under the wrong requests reads worse than a flat one."""
        from skillpp.web import step_groups
        turns = [("a", "Looked it up and found the answer."), ("b", "Changed the file as asked.")]
        unanchored = self._entry(turns, [self._step(1), self._step(2)])
        self.assertIsNone(step_groups(unanchored))
        disagreeing = self._entry(turns, [
            self._step(1, closing="Looked it up and found the answer."),
            self._step(1, closing="Changed the file as asked.")])
        self.assertIsNone(step_groups(disagreeing))
        unnumbered = self._entry(turns, [{"tool": "Bash", "input": {},
                                          "closing_note": "Looked it up and found the answer."}])
        self.assertIsNone(step_groups(unnumbered))

    def test_one_request_needs_no_anchor(self):
        from skillpp.web import step_groups
        groups = step_groups(self._entry([("do it", "done")], [self._step(7), self._step(7)]))
        self.assertEqual([g["tools"] for g in groups], [2])

    def test_a_path_in_the_agents_words_still_anchors(self):
        """Replies are stored with paths parameterised; the notes are not."""
        from pathlib import Path
        from skillpp.web import step_groups
        home = str(Path.home())
        entry = self._entry(
            [("look", "Nothing to change here."),
             ("write it", "Wrote ${HOME}/proj/cases.json and ran the check.")],
            [self._step(3, "Write", file_path=f"{home}/proj/cases.json",
                        closing=f"Wrote {home}/proj/cases.json and ran the check.")])
        groups = step_groups(entry)
        self.assertIsNotNone(groups)
        self.assertEqual([g["tools"] for g in groups], [0, 1])

    def test_a_question_says_what_was_asked_and_answered(self):
        from skillpp.web import step_groups
        ask = {"tool": "AskUserQuestion", "input": {}, "serves": 1,
               "tool_returned": 'User has answered your questions: '
                                '"Bundle or split?"="Revert reformat". Go on.'}
        group = step_groups(self._entry([("commit", "ok")], [ask]))[0]
        self.assertEqual(group["lines"], [{"kind": "ask", "text": "Bundle or split? → Revert reformat",
                                           "warn": False, "steps": 1}])
        self.assertEqual(group["digest"], [{"text": "asked you", "warn": False}])

    def test_back_to_back_file_changes_count_together_only_when_closed(self):
        from skillpp.web import step_groups
        writes = [self._step(1, "Write", file_path=f"/a/{n}.md") for n in ("one", "two", "three")]
        edits = [self._step(1, "Edit", file_path="/a/LOG.md") for _ in range(3)]
        group = step_groups(self._entry([("write", "ok")], writes + edits))[0]
        self.assertEqual([d["text"] for d in group["digest"]], ["Write 3 files", "Edit LOG.md ×3"])
        self.assertEqual([line["text"] for line in group["lines"]],
                         ["Write one.md", "Write two.md", "Write three.md", "Edit LOG.md ×3"])

    def test_a_failed_change_shows_a_failed_lookup_only_looked(self):
        from skillpp.web import step_groups
        group = step_groups(self._entry([("go", "ok")], [
            {**self._step(1, "Write", file_path="/a/x.md"), "failed": True},
            {**self._step(1, command="grep -n nothing x.md"), "failed": True}]))[0]
        self.assertEqual([line["kind"] for line in group["lines"]], ["fail", "look"])
        self.assertEqual((group["failed"], group["looks"]), (1, 1))

    def test_a_destructive_command_is_marked_by_the_drafts_own_test(self):
        from skillpp.web import step_groups
        group = step_groups(self._entry([("clean", "ok")], [
            self._step(1, command="rm -f slide-*.jpg", description="Clear old previews")]))[0]
        self.assertTrue(group["lines"][0]["warn"])
        self.assertEqual(group["digest"], [{"text": "Clear old previews", "warn": True}])


class TestReviewPageRendering(unittest.TestCase):
    """The page's markdown renderer, executed — it is JavaScript, so asserting
    on its source would prove nothing about what a reviewer sees."""

    def _inline(self, source):
        import re
        import shutil
        import subprocess
        from skillpp.web import PAGE
        if not shutil.which("node"):
            self.skipTest("node is not installed")
        esc = re.search(r"const esc = .*?;\n", PAGE, re.S)
        inline = re.search(r"function mdInline\(raw\)\{.*?\n\}", PAGE, re.S)
        script = (esc.group(0) + "\n" + inline.group(0) +
                  f"\nprocess.stdout.write(mdInline({json.dumps(source)}));")
        return subprocess.run(["node", "-e", script], capture_output=True,
                              text=True, check=True).stdout

    def test_a_cut_code_span_loses_no_characters(self):
        """A real draft showed `rm -f slide-*.jpg` as `rm -f slide-.jpg`."""
        out = self._inline("- `cd x && rm -f slide-*.jpg && ls slide-*.jpg")
        self.assertEqual(out.count("slide-*.jpg"), 2)
        self.assertNotIn("<em>", out)

    def test_code_and_emphasis_still_render(self):
        out = self._inline("run `rm -f a*.jpg` then *look*")
        self.assertIn("<code>rm -f a*.jpg</code>", out)
        self.assertIn("<em>look</em>", out)


class TestSessionEndIsAsync(TempRoot):
    """`SessionEnd` stamps and hands off; the fold happens in its own process.

    The judge and the embeddings used to run inside the hook. Measured on the
    live fixtures that is 45 judge calls over 21 sessions and 13s for the worst
    one *warm* — but a cold call can reach `boundary.DEFAULT_TIMEOUT`, the hook
    budget is about a minute, and quitting the app gives less. Nine real
    desktop sessions were held that way.
    """

    def _capture(self, sid="s1"):
        from skillpp.capture import handle_prompt, handle_tool
        handle_prompt(self.config, {"session_id": sid, "cwd": "/r",
                                    "prompt": "ship it"})
        handle_tool(self.config, {"session_id": sid, "cwd": "/r",
                                  "tool_name": "Bash",
                                  "tool_input": {"command": "npm test"}})
        handle_tool(self.config, {"session_id": sid, "cwd": "/r",
                                  "tool_name": "Bash",
                                  "tool_input": {"command": "git commit -m x"}})

    def _hook(self, payload, event="SessionEnd"):
        import io
        from skillpp.cli import main
        real_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        self.addCleanup(lambda: setattr(sys, "stdin", real_stdin))
        with mock.patch("subprocess.Popen") as popen:
            code = main(["--root", str(self.config.root), "hook",
                         "--event", event])
        return code, popen

    def test_session_end_stamps_and_spawns_instead_of_folding(self):
        from skillpp.capture import _session_file
        self._capture()
        self.judged.clear()
        code, popen = self._hook({"session_id": "s1"})

        self.assertEqual(code, 0)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-2:], ["fold-session", "s1"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        popen.return_value.wait.assert_not_called()
        # The thing being bought: no model was asked anything in the hook.
        self.assertEqual(self.judged, [], "the judge ran inside the hook")
        self.assertEqual(list(Ledger(self.config).all()), [], "banked in the hook")
        doc = json.loads(_session_file(self.config, "s1").read_text())
        self.assertIn("ending", doc)

    def test_the_stamp_keeps_the_transcript_for_the_worker(self):
        from skillpp.capture import _session_file
        self._capture()
        self._hook({"session_id": "s1", "transcript_path": "/tmp/t.jsonl"})
        doc = json.loads(_session_file(self.config, "s1").read_text())
        self.assertEqual(doc["transcript"], "/tmp/t.jsonl")

    def test_an_unknown_session_spawns_nothing(self):
        code, popen = self._hook({"session_id": "never-seen"})
        self.assertEqual(code, 0)
        popen.assert_not_called()

    def test_stop_is_not_a_session_end(self):
        """`Stop` fires every turn. Folding there cuts one procedure into
        per-turn fragments, and the worker would unlink the session file while
        capture is still appending to it."""
        from skillpp.capture import _session_file
        self._capture()
        code, popen = self._hook({"session_id": "s1"}, event="Stop")
        self.assertEqual(code, 0)
        popen.assert_not_called()
        self.assertTrue(_session_file(self.config, "s1").exists())

    def test_the_worker_banks_what_the_hook_stamped(self):
        from skillpp.capture import _lock_file, _session_file
        from skillpp.cli import main
        self._capture()
        self._hook({"session_id": "s1"})

        code = main(["--root", str(self.config.root), "fold-session", "s1"])
        self.assertEqual(code, 0)
        self.assertEqual(len(list(Ledger(self.config).all())), 1)
        self.assertFalse(_session_file(self.config, "s1").exists())
        self.assertFalse(_lock_file(self.config, "s1").exists(), "lock left behind")

    def test_the_worker_logs_its_outcome(self):
        """Its stdio is DEVNULL, so the log is the only place it can answer
        "why was my session not banked?"."""
        from skillpp.cli import main
        self._capture()
        self._hook({"session_id": "s1"})
        main(["--root", str(self.config.root), "fold-session", "s1"])
        self.assertIn("fold-session s1", self.config.log_file.read_text())


class TestFoldLock(TempRoot):
    """One session is folded by one process at a time.

    A live `SessionEnd` fold and a manual one once ran together and pushed an
    entry's `occurrences` to 2. `occurrences` counts every recognition and
    cannot be un-incremented without hand-editing the ledger, so every
    ambiguous lock is read as *alive*: waiting costs a delay, folding twice
    costs the count.
    """

    def _session(self, sid="s1"):
        from skillpp.capture import _session_file
        steps = [{"tool": "UserPrompt", "input": {"text": "ship it"}},
                 {"tool": "Bash", "input": {"command": "npm test"}},
                 {"tool": "Bash", "input": {"command": "git commit -m x"}}]
        path = _session_file(self.config, sid)
        path.write_text(json.dumps({"session_id": sid, "cwd": "/r",
                                    "prompts": [], "steps": steps}),
                        encoding="utf-8")
        return path

    def _lock(self, sid="s1", **held):
        import socket
        from skillpp.capture import _lock_file
        path = _lock_file(self.config, sid)
        payload = {"pid": os.getpid(), "host": socket.gethostname(),
                   "what": "fold-session", **held}
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_a_second_worker_leaves_the_session_alone(self):
        from skillpp.capture import fold_session_now
        path = self._session()
        lock = self._lock()
        self.assertEqual(fold_session_now(self.config, "s1"),
                         {"status": "folding", "session": "s1"})
        self.assertEqual(list(Ledger(self.config).all()), [])
        self.assertTrue(path.exists())
        self.assertTrue(lock.exists(), "the loser deleted the winner's lock")

    def test_a_dead_workers_lock_is_broken(self):
        from skillpp.capture import fold_session_now
        self._session()
        lock = self._lock(pid=999999)
        self.assertEqual(fold_session_now(self.config, "s1")["status"], "created")
        self.assertFalse(lock.exists())

    def test_a_lock_from_another_machine_is_aged_out_not_pid_checked(self):
        from skillpp.capture import _FOLD_LOCK_SECONDS, fold_session_now
        self._session()
        lock = self._lock(pid=999999, host="somewhere-else")
        self.assertEqual(fold_session_now(self.config, "s1")["status"], "folding")
        stamp = time.time() - _FOLD_LOCK_SECONDS - 60
        os.utime(lock, (stamp, stamp))
        self.assertEqual(fold_session_now(self.config, "s1")["status"], "created")

    def test_a_corrupt_lock_falls_back_to_age(self):
        from skillpp.capture import _FOLD_LOCK_SECONDS, _lock_file, fold_session_now
        self._session()
        lock = _lock_file(self.config, "s1")
        lock.write_text("not json", encoding="utf-8")
        self.assertEqual(fold_session_now(self.config, "s1")["status"], "folding")
        stamp = time.time() - _FOLD_LOCK_SECONDS - 60
        os.utime(lock, (stamp, stamp))
        self.assertEqual(fold_session_now(self.config, "s1")["status"], "created")

    def test_the_lock_is_released_when_the_fold_raises(self):
        import skillpp.capture as capture
        from skillpp.capture import _lock_file, fold_session_now
        self._session()
        real = capture.handle_session_end

        def boom(*a, **k):
            raise RuntimeError("nope")
        capture.handle_session_end = boom
        self.addCleanup(lambda: setattr(capture, "handle_session_end", real))
        with self.assertRaises(RuntimeError):
            fold_session_now(self.config, "s1")
        self.assertFalse(_lock_file(self.config, "s1").exists())



class TestTranscriptExtract(TempRoot):
    """`from_transcript.extract` builds fixtures the way live capture records."""

    def _extract(self, rows):
        sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "sessions"))
        from from_transcript import extract
        path = self.root / "t.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return extract(path)

    def test_an_image_result_is_not_a_prompt_and_a_prompt_keeps_its_reply(self):
        steps = self._extract([
            {"type": "user", "message": {"content": "propose the slides"}},
            {"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "hidden"},
                {"type": "text", "text": "Here are 9 slides."},
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": "ls"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a.md"}]}},
            {"type": "user", "message": {"content": "[Image: original 2001x1125]"}},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Render looks fine."}]}},
        ])
        prompts = [st for st in steps if is_prompt(st)]
        self.assertEqual([p["input"]["text"] for p in prompts], ["propose the slides"])
        self.assertEqual(prompts[0]["reply"], "Here are 9 slides.\n\nRender looks fine.")

    def test_the_account_name_is_templated(self):
        user = Path.home().name
        steps = self._extract([
            {"type": "user", "message": {"content": "list it"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": f"ls /private/tmp/claude-501/-Users-{user.replace('.', '-')}-x"}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": f"-rw-r--r-- 1 {user} staff 10 notes.md"}]}},
        ])
        self.assertNotIn(user, json.dumps(steps))

    def test_the_judge_waits_long_enough_to_answer(self):
        import skillpp.boundary as boundary
        self.assertEqual(boundary.DEFAULT_TIMEOUT, 30.0)


class TestConversationMatching(TempRoot):
    """A run with replies matches on its conversation; one without, on steps."""

    STEPS = [{"tool": "Bash", "input": {"command": "npm test"}},
             {"tool": "Bash", "input": {"command": "git commit -m x"}}]

    def _entry(self, eid, turns=None):
        entry = Entry(id=eid, title=eid, steps=list(self.STEPS), turns=turns or [])
        Ledger(self.config).save(entry)
        return entry

    def test_the_text_is_the_conversation_when_there_is_one(self):
        from skillpp.matching import entry_text, has_conversation, turns_text
        turns = [{"prompt": "propose the slides", "reply": "Here are 9.", "used": []}]
        self.assertEqual(turns_text(turns), "User: propose the slides\nAgent: Here are 9.")
        self.assertTrue(has_conversation(turns))
        self.assertFalse(has_conversation([{"prompt": "p", "reply": "", "used": []}]))
        from skillpp.matching import conversation_text
        entry = self._entry("aaaaaaaaaaaa", turns)
        self.assertEqual(entry_text(entry), conversation_text(turns, entry.steps))
        self.assertIn("npm test", entry_text(self._entry("bbbbbbbbbbbb")))

    def test_the_material_is_kept_out_of_the_text(self):
        """File names and the body of a reply say what a run was *about*."""
        from skillpp.matching import REPLY_HEAD, turns_text
        text = turns_text([{"prompt": "turn ARTICLE.md into slides for the team",
                            "reply": "Reading ARTICLE.md. " + "slide bullet " * 200,
                            "used": []}])
        self.assertIn("turn <file> into slides", text)
        self.assertIn("Agent: Reading <file>.", text)
        self.assertNotIn("ARTICLE.md", text)
        self.assertLessEqual(len(text.split("Agent: ")[1]), REPLY_HEAD)

    def test_what_did_the_work_and_what_came_out_of_it(self):
        """Two lines the conversation does not carry, each measured on its own."""
        from skillpp.matching import conversation_text, deliverable_text
        turns = [{"prompt": "build the deck", "reply": "Building it.",
                  "used": ["skill anthropic-skills:pptx"]}]
        steps = [{"tool": "Write", "input": {"file_path": "/s/build.js"}},
                 {"tool": "Bash", "input": {"command": "node build.js && ls talk.pptx"}},
                 {"tool": "SendUserFile", "input": {}}]
        text = conversation_text(turns, steps)
        self.assertIn("Used: skill anthropic-skills:pptx", text)
        self.assertIn("Produced: .js, .pptx; handed the file over", text)
        self.assertEqual(deliverable_text([]), "Produced: nothing")

    def test_like_is_only_compared_with_like(self):
        from skillpp.matching import find_same
        talk = [{"prompt": "propose the slides", "reply": "Here are 9 slides.", "used": []}]
        steps_only = self._entry("aaaaaaaaaaaa")
        with_talk = self._entry("bbbbbbbbbbbb", talk)
        entries = [steps_only, with_talk]
        self.assertIs(find_same(self.STEPS, entries, self.config, turns=talk)[0], with_talk)
        self.assertIs(find_same(self.STEPS, entries, self.config)[0], steps_only)
        self.assertIsNone(find_same(self.STEPS, [steps_only], self.config, turns=talk))

    def test_a_conversation_clears_its_own_floor(self):
        from skillpp.matching import find_same
        near = [{"prompt": "propose the slides", "reply": "Here are 9 slides.", "used": []}]
        far = [{"prompt": "restart the backend server", "reply": "Restarted on 8000.", "used": []}]
        entry = self._entry("aaaaaaaaaaaa", near)
        self.assertIsNotNone(find_same(self.STEPS, [entry], self.config, turns=near))
        self.assertIsNone(find_same(self.STEPS, [entry], self.config, turns=far))
        self.config.match_floor_turns = 1.01
        self.assertIsNone(find_same(self.STEPS, [entry], self.config, turns=near))

    def test_merge_never_pairs_a_conversation_with_steps(self):
        import argparse, io
        from contextlib import redirect_stdout
        from skillpp.cli import cmd_merge
        talk = [{"prompt": "npm test", "reply": "git commit", "used": []}]
        self._entry("aaaaaaaaaaaa")
        self._entry("bbbbbbbbbbbb", talk)
        out = io.StringIO()
        with redirect_stdout(out):
            cmd_merge(argparse.Namespace(root=self.config.root, apply=False, floor=0.0))
        self.assertIn("Nothing to merge", out.getvalue())


class TestEmbedEndpoint(TempRoot):
    """`local.embed` truncates at the model's limit instead of failing.

    The legacy endpoint answered an overlong text with HTTP 500, which read as
    "model unreachable" and crashed every later fold in the project.
    """

    def setUp(self):
        super().setUp()
        import skillpp.local as local
        self.local = local
        local._CONTEXT_LENGTHS.clear()
        self.requests = []

    def _serve(self, embed_reply, show_reply=None, show_fails=False):
        import io
        import urllib.error

        def fake_urlopen(request, timeout=None):
            self.requests.append((request.full_url,
                                  json.loads(request.data.decode("utf-8"))))
            if request.full_url.endswith("/api/show"):
                if show_fails:
                    raise urllib.error.URLError("down")
                body = show_reply
            else:
                body = embed_reply
            return io.BytesIO(json.dumps(body).encode("utf-8"))
        return mock.patch("urllib.request.urlopen", fake_urlopen)

    def test_asks_the_embed_endpoint_to_truncate(self):
        with self._serve({"embeddings": [[0.5, 0.25]], "prompt_eval_count": 12}):
            vector = self.local.embed("1. Bash git status", model="m", host="http://h")
        self.assertEqual(vector, [0.5, 0.25])
        url, body = self.requests[0]
        self.assertEqual(url, "http://h/api/embed")
        self.assertEqual(body, {"model": "m", "input": "1. Bash git status",
                                "truncate": True})

    def test_reports_a_text_cut_at_the_context_length(self):
        seen = []
        show = {"model_info": {"nomic-bert.context_length": 2048}}
        with self._serve({"embeddings": [[1.0]], "prompt_eval_count": 2048}, show):
            self.local.embed("x", model="m", host="http://h", on_truncate=seen.append)
        self.assertEqual(seen, [2048])

    def test_a_text_that_fits_is_not_reported(self):
        seen = []
        show = {"model_info": {"nomic-bert.context_length": 2048}}
        with self._serve({"embeddings": [[1.0]], "prompt_eval_count": 900}, show):
            self.local.embed("x", model="m", host="http://h", on_truncate=seen.append)
        self.assertEqual(seen, [])

    def test_an_unknown_context_length_still_embeds(self):
        seen = []
        with self._serve({"embeddings": [[1.0]], "prompt_eval_count": 2048},
                         show_fails=True):
            vector = self.local.embed("x", model="m", host="http://h",
                                      on_truncate=seen.append)
        self.assertEqual((vector, seen), ([1.0], []))

    def test_no_callback_asks_nothing_about_the_model(self):
        with self._serve({"embeddings": [[1.0]], "prompt_eval_count": 2048}):
            self.local.embed("x", model="m", host="http://h")
        self.assertEqual([u for u, _ in self.requests], ["http://h/api/embed"])

    def test_unreachable_is_still_unavailable(self):
        import urllib.error

        def down(request, timeout=None):
            raise urllib.error.URLError("refused")
        with mock.patch("urllib.request.urlopen", down):
            with self.assertRaises(self.local.LocalModelUnavailable):
                self.local.embed("x", model="m", host="http://h")

    def test_find_same_logs_a_truncated_match(self):
        import skillpp.matching as matching
        from skillpp.ledger import Entry
        entry = Entry(id="longentry123", title="restart the servers",
                      signature="", steps=[{"tool": "Bash",
                                            "input": {"command": "./start.sh"}}])

        def cut(text, on_truncate=None, **kw):
            if on_truncate:
                on_truncate(2048)
            return _stub_embed(text)
        with mock.patch.object(matching, "embed", cut):
            matching.find_same(entry.steps, [entry], self.config)
        log = self.config.log_file.read_text(encoding="utf-8")
        self.assertIn("embedding truncated at 2048 tokens: new episode", log)
        self.assertIn("embedding truncated at 2048 tokens: entry longentry123", log)


class TestPromotedSkillsStayMatchable(TempRoot):
    """A promoted skill used to become invisible the moment it was promoted.

    Lexical matching at capture never reached its 0.85 against one — over 5,995
    real pairs the highest was 0.814 — and the embedding pass filtered to
    candidates. So the count froze at promotion and every later run of the same
    work opened a fresh proposal for something a skill already did. Embedding
    matching compares every status.
    """

    DEPLOY = [bash("npm run build"), bash("./deploy.sh staging")]

    def _skill(self):
        from skillpp.ledger import STATUS_PROMOTED
        skill = Entry(id="skill1", title="deploy", status=STATUS_PROMOTED,
                      sessions=["s1"], occurrences=1, intents=["deploy"],
                      steps=list(self.DEPLOY))
        Ledger(self.config).save(skill)
        return skill

    def test_a_new_run_of_a_skill_counts_toward_the_skill(self):
        from skillpp.ledger import STATUS_PROMOTED
        self._skill()
        result = fold_session(self.config, {
            "session_id": "s2", "cwd": "", "prompts": [],
            "steps": judged([dict(st) for st in self.DEPLOY])})
        self.assertEqual(result["status"], "merged")
        led = Ledger(self.config)
        self.assertEqual(led.get("skill1").status, STATUS_PROMOTED)
        self.assertEqual(led.get("skill1").occurrences, 2, "the skill was used again")
        self.assertEqual(len(list(led.all())), 1, "no fresh proposal for it")

    def test_merge_covers_a_candidate_a_skill_already_does(self):
        """Reinforce, never delete: the skill exists, and this says it is used."""
        import argparse
        from skillpp.cli import cmd_merge
        from skillpp.ledger import STATUS_COVERED, STATUS_PROMOTED
        self._skill()
        # Banked before matching could see it, as an unmatched keep would be.
        Ledger(self.config).save(Entry(
            id="cand1", title="deploy again", sessions=["s2"], occurrences=1,
            intents=["deploy again"], steps=list(self.DEPLOY), unmatched=True))
        cmd_merge(argparse.Namespace(root=self.config.root, apply=True, floor=None))

        led = Ledger(self.config)
        self.assertEqual(led.get("skill1").status, STATUS_PROMOTED)
        self.assertEqual(led.get("skill1").occurrences, 2)
        covered = led.get("cand1")
        self.assertIsNotNone(covered, "the candidate is evidence, not rubbish")
        self.assertEqual(covered.status, STATUS_COVERED)
        self.assertNotIn("cand1", [c.id for c in led.candidates()],
                         "work a skill already does is not a proposal")
