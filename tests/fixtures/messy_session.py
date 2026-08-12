"""The three ``examples/demo.sh`` sessions, polluted with unrelated work.

The demo's three sessions each contain exactly one task — the deploy — in one
order with nothing else around it. All three therefore produce an identical
4-token signature, merge at similarity 1.0, and cross the recurrence threshold
on the third run. That is the clean baseline.

This fixture keeps **the same deploy workflow, in the same order, three
times** — so the workflow still genuinely recurs 3× — and surrounds each
occurrence with different unrelated tasks. The deploy is the only controlled
variable; the pollution is what changes.

Expected consequence without segmentation: three signatures that no longer
resemble each other, so the deploy workflow that occurred three times is
recorded as three unrelated one-offs and never reaches the threshold. With
segmentation, the deploy episode should be recovered from each session and
merge back to ``occurrences: 3``, matching the clean baseline.

The pollution supplies decoy completion markers for free — each polluted task
ends in its own ``git commit``, so a segmenter cannot simply cut on "contains
git commit" and call the deploy done.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from skillpp.segment import COMPLETION_MARKERS, PROMPT_TOOL  # noqa: F401

BASE = 1_760_000_000.0  # arbitrary epoch anchor; only deltas matter

# COMPLETION_MARKERS is imported from the implementation rather than restated
# here. An earlier draft kept a second copy, which agreed with the real set only
# by coincidence and could drift silently in either direction.

# Look like completions, are not. Present in the pollution below.
DECOYS = ("git status", "git diff", "git add", "failed git commit", "git stash")

# The leaked credential from demo.sh, kept verbatim: it must still be scrubbed
# once the deploy is buried in unrelated work.
LEAKED_TOKEN = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _t(minutes: float) -> float:
    return round(BASE + minutes * 60, 1)


def _bash(command: str, minutes: float, failed: bool = False) -> dict:
    return {"tool": "Bash", "input": {"command": command},
            "failed": failed, "t": _t(minutes)}


def _edit(path: str, minutes: float) -> dict:
    return {"tool": "Edit", "input": {"file_path": path}, "failed": False,
            "t": _t(minutes)}


def _write(path: str, minutes: float) -> dict:
    return {"tool": "Write", "input": {"file_path": path}, "failed": False,
            "t": _t(minutes)}


def _read(path: str, minutes: float) -> dict:
    """Noise — dropped by _NOISE_TOOLS before a signature is built."""
    return {"tool": "Read", "input": {"file_path": path}, "failed": False,
            "t": _t(minutes)}


# --------------------------------------------------------------------------
# The controlled variable: demo.sh's deploy workflow, unchanged.
# --------------------------------------------------------------------------

def deploy_steps(target: str, at: float) -> list[dict]:
    """The five steps demo.sh fires, in order, offset to start at *at*."""
    return [
        _bash("npm run build", at),
        _bash(f"export DEPLOY_TOKEN={LEAKED_TOKEN}", at + 2),
        _bash("terraform apply", at + 4, failed=True),   # fails...
        _bash("terraform apply -lock=false", at + 6),    # ...then succeeds
        _bash(f"./scripts/deploy.sh {target}", at + 9),
    ]


DEPLOY_PROMPTS = {
    "s1": "deploy the api to staging",
    "s2": "ship the api build",
    "s3": "deploy the api to prod",
}


# --------------------------------------------------------------------------
# The pollution: different unrelated tasks around each deploy.
# --------------------------------------------------------------------------

def _s1_events() -> list[dict]:
    """Deploy sandwiched between a docs fix and a dependency bump."""
    return [
        # unrelated task A — ends in its own commit (decoy marker)
        {"kind": "prompt", "text": "fix the broken link in the README", "t": _t(0)},
        {"kind": "step", **_read("/proj/api/README.md", 1)},
        {"kind": "step", **_bash("git status", 2)},                    # decoy
        {"kind": "step", **_edit("/proj/api/README.md", 3)},
        {"kind": "step", **_bash("git commit -m 'fix readme link'", 5)},

        # the deploy — the workflow under test
        {"kind": "prompt", "text": DEPLOY_PROMPTS["s1"], "t": _t(8)},
        *[{"kind": "step", **s} for s in deploy_steps("staging", 10)],

        # unrelated task B — no marker, trails off
        {"kind": "prompt", "text": "is the sdk dependency out of date?", "t": _t(24)},
        {"kind": "step", **_bash("npm outdated", 25)},
        {"kind": "step", **_read("/proj/api/package.json", 27)},
        {"kind": "step", **_bash("npm view @acme/sdk versions", 29)},
    ]


def _s2_events() -> list[dict]:
    """Deploy preceded by a flaky-test hunt, followed by a changelog commit."""
    return [
        # unrelated task A — long, ends in a commit
        {"kind": "prompt", "text": "the clock test keeps failing in ci", "t": _t(0)},
        {"kind": "step", **_bash("npm test -- test_clock", 2, failed=True)},
        {"kind": "step", **_read("/proj/api/tests/test_clock.py", 4)},
        {"kind": "step", **_bash("git diff HEAD~3 -- tests/", 6)},     # decoy
        {"kind": "step", **_edit("/proj/api/tests/test_clock.py", 9)},
        {"kind": "step", **_bash("npm test -- test_clock", 11)},       # red -> green
        {"kind": "step", **_bash("git add tests/", 12)},               # decoy
        {"kind": "step", **_bash("git commit -m 'deflake clock test'", 13)},

        # the deploy — same five steps, different surroundings
        {"kind": "prompt", "text": DEPLOY_PROMPTS["s2"], "t": _t(16)},
        *[{"kind": "step", **s} for s in deploy_steps("staging", 18)],

        # unrelated task B — short, ends in a commit
        {"kind": "prompt", "text": "add the release notes", "t": _t(32)},
        {"kind": "step", **_write("/proj/api/CHANGELOG.md", 34)},
        {"kind": "step", **_bash("git commit -m 'changelog for 2.4.0'", 36)},
    ]


def _s3_events() -> list[dict]:
    """Deploy buried mid-session: a rejected commit before, an open-ended
    investigation after that never produces anything."""
    return [
        # unrelated task A — the commit is REJECTED, so the task is not over
        {"kind": "prompt", "text": "tidy up the config loader", "t": _t(0)},
        {"kind": "step", **_edit("/proj/api/config.py", 2)},
        {"kind": "step", **_bash("git add -A", 4)},                    # decoy
        {"kind": "step", **_bash("git commit -m 'tidy config'", 5, failed=True)},
        {"kind": "step", **_bash("npm run lint -- --fix", 6)},
        {"kind": "step", **_bash("git commit -m 'tidy config'", 8)},

        # discussion only — no tool calls at all
        {"kind": "prompt", "text": "should the loader read env before file?", "t": _t(11)},
        {"kind": "prompt", "text": "ok leave it as is", "t": _t(14)},

        # the deploy — third and final occurrence
        {"kind": "prompt", "text": DEPLOY_PROMPTS["s3"], "t": _t(17)},
        *[{"kind": "step", **s} for s in deploy_steps("prod", 19)],

        # unrelated task B — forty minutes, no marker, no artifact
        {"kind": "prompt", "text": "prod latency looks off, dig in", "t": _t(33)},
        {"kind": "step", **_bash("kubectl get pods -n prod", 34)},
        {"kind": "step", **_bash("kubectl logs api-7d9f -n prod --tail=200", 37)},
        {"kind": "step", **_bash("curl -w '%{time_total}' https://api.internal/health", 41)},
        {"kind": "step", **_bash("psql -c 'explain analyze select * from sessions'", 46)},
        {"kind": "step", **_bash("kubectl top pods -n prod", 52)},
        {"kind": "step", **_bash("kubectl describe pod api-7d9f -n prod", 58)},
        {"kind": "step", **_bash("kubectl logs api-7d9f -n prod --since=2h", 64)},
        {"kind": "prompt", "text": "nothing obvious, leave it", "t": _t(70)},
    ]


SESSIONS = {"s1": _s1_events, "s2": _s2_events, "s3": _s3_events}


# --------------------------------------------------------------------------
# What a segmenter should recover from each polluted session.
# --------------------------------------------------------------------------

EXPECTED_EPISODES = {
    "s1": [
        {"label": "fix readme link", "ends_at": "git commit -m 'fix readme link'",
         "emit": True},
        {"label": "deploy the api", "ends_at": "./scripts/deploy.sh staging",
         "emit": True, "is_the_workflow_under_test": True},
        {"label": "check sdk dependency freshness", "ends_at": None,
         "emit": False, "flag": True,
         "why": "no marker, no artifact — a question, not a workflow"},
    ],
    "s2": [
        {"label": "deflake clock test", "ends_at": "git commit -m 'deflake clock test'",
         "emit": True},
        {"label": "deploy the api", "ends_at": "./scripts/deploy.sh staging",
         "emit": True, "is_the_workflow_under_test": True},
        {"label": "add release notes", "ends_at": "git commit -m 'changelog for 2.4.0'",
         "emit": True},
    ],
    "s3": [
        {"label": "tidy the config loader", "ends_at": "git commit -m 'tidy config'",
         "emit": True,
         "why": "the FIRST commit was rejected and must not end the episode"},
        {"label": "discussion about loader precedence", "ends_at": None,
         "emit": False,
         "why": "no tool calls; the existing len(substantive) < 2 gate covers it"},
        {"label": "deploy the api", "ends_at": "./scripts/deploy.sh prod",
         "emit": True, "is_the_workflow_under_test": True},
        {"label": "investigate prod latency", "ends_at": None,
         "emit": False, "flag": True,
         "why": "seven steps over forty minutes with nothing to show. Emitting "
                "it recreates the mega-candidate segmentation exists to prevent"},
    ],
}

# The point of the whole fixture: this episode occurs once per session, so it
# must end at occurrences == 3, exactly as it does in the clean demo.
WORKFLOW_UNDER_TEST = "deploy the api"
EXPECTED_OCCURRENCES = 3


def to_session_dict(name: str, cwd: str = "/proj/api") -> dict:
    """One polluted session in the **pre-segmentation** buffer shape.

    Prompts go only to ``session["prompts"]``, so the interleaving of what was
    asked and what ran is lost — which is exactly how ``handle_prompt`` behaved
    before the segmenter, and how any session buffered by older code still
    looks. Feed this to ``capture._fold_steps`` to reproduce the original
    whole-session behaviour, or to ``fold_session`` to see what segmentation
    can manage with markers alone.
    """
    events = SESSIONS[name]()
    return {
        "session_id": name,
        "cwd": cwd,
        "prompts": [e["text"] for e in events if e["kind"] == "prompt"],
        "steps": [{k: v for k, v in e.items() if k != "kind"}
                  for e in events if e["kind"] == "step"],
    }


def to_captured_session(name: str, cwd: str = "/proj/api") -> dict:
    """The same session in the **post-segmentation** buffer shape.

    Mirrors what ``capture.handle_prompt`` now writes: each prompt lands in
    ``session["prompts"]`` *and* as a ``UserPrompt`` sentinel in the ordered
    step stream, so position alone records which prompt preceded which work.
    No clock involved.
    """
    events = SESSIONS[name]()
    steps: list[dict] = []
    for event in events:
        if event["kind"] == "prompt":
            steps.append({"tool": PROMPT_TOOL, "input": {"text": event["text"]},
                          "failed": False, "t": event["t"]})
        else:
            steps.append({k: v for k, v in event.items() if k != "kind"})
    return {
        "session_id": name,
        "cwd": cwd,
        "prompts": [e["text"] for e in events if e["kind"] == "prompt"],
        "steps": steps,
    }


def clean_session_dict(name: str, cwd: str = "/proj/api") -> dict:
    """The unpolluted demo.sh equivalent, for A/B comparison."""
    target = "prod" if name == "s3" else "staging"
    return {
        "session_id": f"{name}-clean",
        "cwd": cwd,
        "prompts": [DEPLOY_PROMPTS[name]],
        "steps": deploy_steps(target, 0),
    }
