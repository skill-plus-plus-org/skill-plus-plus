"""A benchmark corpus of realistic sessions, with ground truth.

Every earlier measurement here came from fixtures written by whoever was also
writing the detector, which makes them a test of internal consistency more than
of anything else. These are written the other way round: from what the work
actually looks like, in two domains, with the answer decided before the pipeline
was run against them.

Two kinds, because they exercise different halves:

* **programming** — Bash-heavy, closing markers the segmenter recognises
* **productivity** — MCP calls, `Write`, and email or document work, where no
  `git commit` ever arrives and the only boundary is the next request

The ground truth is deliberately coarse. `episodes` is how many candidates a
correct run banks, and `methods` is how many of those a person would follow
again for a different case. Anything finer would be encoding the current
implementation's opinions as truth.

A case marked ``methods=0`` with ``episodes=1`` is not a failure to detect — it
is real work that happened once and should be ranked low, not discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def P(text):                      # a developer request
    return ("prompt", text, False)


def B(command, failed=False, note=""):    # a shell command
    # `note` is the agent's own `description` — the field Claude Code's Bash
    # tool carries and `capture` keeps. Real sessions have it on 87% of calls
    # and `render_step` puts it above the command, so a corpus without it
    # measures a rendering the pipeline no longer produces.
    return ("Bash", command, failed, note)


def W(path):                      # writing a file
    return ("Write", path, False)


def E(path):                      # editing a file
    return ("Edit", path, False)


def M(tool, **kw):                # an MCP call
    return (f"mcp__{tool}", kw, False)


def R(path):                      # a read — noise, never a step
    return ("Read", path, False)


@dataclass
class Case:
    name: str
    kind: str                     # "programming" | "productivity"
    why: str                      # why the ground truth is what it is
    script: list
    episodes: int                 # candidates a correct run banks
    methods: int                  # of those, how many are reusable procedures
    tags: list = field(default_factory=list)
    # A second session, played into the same ledger after the first. Recurrence
    # and merging are invisible within one session by definition — occurrences
    # count sessions — so the corpus could not see either until this existed.
    follow: list = None
    # Highest occurrence count a correct run reaches. Only meaningful with
    # `follow`, and the whole promotion gate rests on it.
    occurrences: int = 1


CASES = [
    # ---------------------------------------------------------------- programming
    Case(
        "release-a-service", "programming",
        "The shape stays and the version changes. Textbook procedure.",
        [P("cut the 2.4 release"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git checkout main\necho "=== exit $? ==="', note="Start from a clean main"), B('cd /Users/dev/ai_projects/acme-platform && git pull --ff-only 2>&1 | tail -15', note="Fast-forward to origin"), B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Verify green before tagging"),
         B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm version 2.4.0\necho "=== exit $? ==="', note="Bump the package version"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && git tag -s v2.4.0 -m \'Release 2.4.0\'\necho "=== exit $? ==="', note="Sign the release tag"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git push --follow-tags\necho "=== exit $? ==="', note="Publish the tag to origin")],
        episodes=1, methods=1, tags=["marker", "recurring-shape"]),

    Case(
        "rotate-a-credential", "programming",
        "Quarterly, per credential. Rare but high value — the case frequency "
        "alone would never surface.",
        [P("rotate the staging database password"),
         B('echo "=== openssl ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && openssl rand -base64 32\necho "=== exit $? ==="', note="Generate a fresh password"),
         B('cd /Users/dev/ai_projects/acme-platform && kubectl create secret generic db-staging --from-literal=pw=<v> --dry-run=client -o yaml | kubectl apply -f - 2>&1 | tail -15', note="Replace the staging DB secret"),
         B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-973; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && kubectl rollout restart deploy/api -n staging; echo "artifacts in $SP"', note="Restart the API to pick up the secret"),
         B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-973; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && kubectl rollout status deploy/api -n staging; echo "artifacts in $SP"', note="Wait for the rollout to finish"),
         B("cd /Users/dev/ai_projects/acme-platform && psql -h staging <<'SQL'\nselect count(*) from schema_migrations;\nselect max(version) from schema_migrations;\nSQL", note="Confirm the new credential connects")],
        episodes=1, methods=1, tags=["no-commit", "rare"]),

    Case(
        "onboard-a-repository", "programming",
        "Done per repo, per machine, per new joiner.",
        [P("get this repo running locally"),
         B('cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git clone git@github.com:acme/api.git', note="Clone the repository"), B('echo "=== cp ===" \ncd /Users/dev/ai_projects/acme-platform && cp .env.example .env 2>&1 | tail -25\necho "=== exit $? ==="', note="Seed local env from the example"),
         B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && npm ci\necho "=== exit $? ==="', note="Install pinned dependencies"), B('echo "=== docker ===" \ncd /Users/dev/ai_projects/acme-platform && docker compose up -d postgres 2>&1 | tail -25\necho "=== exit $? ==="', note="Bring up the local database"),
         B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && npm run migrate\necho "=== exit $? ==="', note="Apply the schema"), B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Confirm the checkout builds green")],
        episodes=1, methods=1, tags=["no-commit"]),

    Case(
        "migration-with-a-lock", "programming",
        "The workaround is the knowledge: scale to zero, then migrate. Worth "
        "keeping even though it started as one failure.",
        [P("the staging migration is stuck, get it green"),
         B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && npm run migrate\necho "=== exit $? ==="', failed=True, note="Run the migration"),
         B('cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && kubectl scale deploy/api --replicas=0 -n staging', note="Drain replicas holding the lock"),
         B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && npm run migrate\necho "=== exit $? ==="', note="Retry the migration with the lock free"),
         B('cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && kubectl scale deploy/api --replicas=3 -n staging', note="Scale the API back up"),
         B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Confirm the service is healthy")],
        episodes=1, methods=1, tags=["failure-then-fix"]),

    Case(
        "explore-then-one-line-fix", "programming",
        "Eight greps and a one-line change. Real work, done once — banked, "
        "trimmed, and ranked low.",
        [P("the export endpoint 500s intermittently, find out why"),
         B('echo "=== grep ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && grep -rn export src/\necho "=== exit $? ==="', note="Find where export is implemented"), B('echo "=== grep ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && grep -rn timeout src/api/\necho "=== exit $? ==="', note="Look for a timeout in the export path"),
         B('echo "=== cat ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && cat src/api/export.py\necho "=== exit $? ==="', note="Read the export handler"), B('echo "=== grep ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && grep -rn pool src/db/\necho "=== exit $? ==="', note="Check the pool settings the export uses"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git log --oneline -20 src/api/export.py\necho "=== exit $? ==="', note="See what changed in export recently"),
         E("src/api/export.py"), B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Confirm the fix passes"), B("cd /Users/dev/ai_projects/acme-platform/services/api && git commit -a -F- <<'MSG'\nraise export timeout\n\nVerified green before landing; see the run log.\nMSG", note="Commit the timeout change")],
        episodes=1, methods=0, tags=["exploration", "one-off"]),

    Case(
        "investigation-that-goes-nowhere", "programming",
        "Six reads and a shrug. Nothing was done, so nothing is a procedure.",
        [P("the nightly job got slower this week, any idea why"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git log --since=7.days --oneline\necho "=== exit $? ==="', note="See what landed in the last week"), B('echo "=== cat ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && cat .github/workflows/nightly.yml\necho "=== exit $? ==="', note="Read the nightly workflow definition"),
         B('echo "=== grep ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && grep -rn timeout .github/\necho "=== exit $? ==="', note="Look for a timeout in CI config"), B('echo "=== ls ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && ls -la logs/\necho "=== exit $? ==="', note="See what logs exist"),
         B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-325; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && tail -100 logs/nightly.log; echo "artifacts in $SP"', note="Read the tail of the nightly log")],
        episodes=0, methods=0, tags=["nothing-here"]),

    Case(
        "two-tasks-one-sitting", "programming",
        "A release and an unrelated CI bump. Two procedures, not one session.",
        [P("cut the 2.4 release tag"),
         B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Verify green before tagging"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && git tag -s v2.4.0 -m rel\necho "=== exit $? ==="', note="Tag the release"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git push --follow-tags\necho "=== exit $? ==="', note="Publish the tag to origin"),
         P("now bump CI to node 22"),
         E(".github/workflows/ci.yml"), B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Check the suite still passes"),
         B("cd /Users/dev/ai_projects/acme-platform/services/api && git commit -a -F- <<'MSG'\nci: node 22\n\nVerified green before landing; see the run log.\nMSG", note="Commit the CI node bump"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git push\necho "=== exit $? ==="', note="Push the CI change")],
        episodes=2, methods=2, tags=["boundary"]),

    Case(
        "hotfix-one-specific-bug", "programming",
        "A null check for one crash. Nobody follows these steps again.",
        [P("users report a crash on empty carts"),
         B('echo "=== grep ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && grep -rn \'cart.items\' src/\necho "=== exit $? ==="', note="Find where cart items are read"), E("src/cart.py"),
         B('cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && pytest tests/test_cart.py', note="Reproduce the empty-cart failure"), B("cd /Users/dev/ai_projects/acme-platform/services/api && git commit -a -F- <<'MSG'\nfix: guard empty cart\n\nVerified green before landing; see the run log.\nMSG", note="Commit the empty-cart guard")],
        episodes=1, methods=0, tags=["one-off"]),

    Case(
        "framework-eval-with-adr-check", "programming",
        "A hybrid shape not otherwise in this corpus: an MCP doc fetch opens "
        "the work instead of trailing it, and the marker at the end is still "
        "a plain `git commit`. `trim_leading_exploration` is written to never "
        "cut an MCP retrieval regardless of position, specifically so a "
        "fetch-then-build procedure like this one is not mistaken for "
        "exploration and stripped off the front. `follow` uses a different "
        "ADK function and a different eval file so recurrence has to survive "
        "on shape, not on repeated literals.",
        [P("add a Google ADK eval for the retry-policy tool call"),
         M("adk-docs__fetch_docs", url="https://google.github.io/adk-docs/evaluate/"),
         W("/Users/dev/ai_projects/acme-platform/backend/acme_agent/evals/retry_policy.evalset.json"),
         R("/Users/dev/ai_projects/acme-platform/backend/acme_agent/docs/evals.md"),
         E("/Users/dev/ai_projects/acme-platform/backend/acme_agent/docs/evals.md"),
         B('cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && pytest tests/test_adr_retry_policy.py',
           note="Confirm the ADR's claim still holds against the new eval"),
         B("cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git commit -a -F- <<'MSG'\n"
           "feat: add retry-policy eval\n\nDocs and ADR verified against the framework; see the run log.\nMSG",
           note="Commit the new eval")],
        follow=[P("add a Google ADK eval for the circuit-breaker tool call"),
                M("adk-docs__fetch_docs", url="https://google.github.io/adk-docs/evaluate/"),
                W("/Users/dev/ai_projects/acme-platform/backend/acme_agent/evals/circuit_breaker.evalset.json"),
                R("/Users/dev/ai_projects/acme-platform/backend/acme_agent/docs/evals.md"),
                E("/Users/dev/ai_projects/acme-platform/backend/acme_agent/docs/evals.md"),
                B('cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && pytest tests/test_adr_circuit_breaker.py',
                  note="Confirm the ADR's claim still holds against the new eval"),
                B("cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git commit -a -F- <<'MSG'\n"
                  "feat: add circuit-breaker eval\n\nDocs and ADR verified against the framework; see the run log.\nMSG",
                  note="Commit the new eval")],
        episodes=1, methods=1, occurrences=2,
        tags=["mcp", "marker", "hybrid", "recurrence"]),

    # --------------------------------------------------------------- productivity
    Case(
        "weekly-status-email", "productivity",
        "Every Friday, same shape, different week. No shell marker ever fires.",
        [P("draft my weekly update for Ludwig"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git log --author=dev --since=7.days --oneline\necho "=== exit $? ==="', note="Collect this week's commits for the update"),
         M("Gmail__search_messages", query="from:ludwig newer_than:7d"),
         W("/tmp/weekly-update.md"),
         M("Gmail__create_draft", to="ludwig@example.com", subject="Weekly update")],
        episodes=1, methods=1, tags=["mcp", "no-commit", "recurring-shape"]),

    Case(
        "expense-report", "productivity",
        "Monthly, same steps, different receipts.",
        [P("do my expenses for March"),
         M("Drive__search_files", query="receipt March"),
         B('echo "=== ls ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && ls ~/Documents/receipts/2026-03/\necho "=== exit $? ==="', note="List this month's receipts"),
         W("/tmp/expenses-2026-03.csv"),
         M("Sheets__append_rows", spreadsheet="Expenses 2026", range="March!A1")],
        episodes=1, methods=1, tags=["mcp", "no-commit"]),

    Case(
        "triage-the-support-queue", "productivity",
        "A daily routine: read, classify, route, reply.",
        [P("go through the support inbox"),
         M("Gmail__search_messages", query="label:support is:unread"),
         M("Linear__create_issue", team="SUP", title="CSV import fails"),
         M("Gmail__send_message", to="customer@example.com"),
         M("Gmail__modify_labels", add="triaged")],
        episodes=1, methods=1, tags=["mcp", "recurring-shape"]),

    Case(
        "meeting-prep", "productivity",
        "Before every review: pull the docs, build the agenda, share it.",
        [P("prep me for the quarterly review with the platform team"),
         M("Calendar__get_event", title="Quarterly review"),
         M("Drive__search_files", query="platform roadmap Q1"),
         R("/tmp/roadmap.md"),
         W("/tmp/agenda.md"),
         M("Slack__post_message", channel="#platform", text="agenda attached")],
        episodes=1, methods=1, tags=["mcp", "reads"]),

    Case(
        "answer-one-question", "productivity",
        "A colleague asked one thing and got one answer. Not a procedure.",
        [P("what did we decide about the retry cap last month?"),
         M("Slack__search_messages", query="retry cap"),
         M("Slack__post_message", channel="#eng", text="we settled on 3")],
        episodes=1, methods=0, tags=["mcp", "one-off"]),

    Case(
        "reading-around", "productivity",
        "Read four documents, decided nothing, wrote nothing. Banks one now.",
        [P("catch me up on the pricing discussion"),
         M("Drive__search_files", query="pricing"),
         R("/tmp/pricing-v1.md"), R("/tmp/pricing-v2.md"),
         M("Slack__search_messages", query="pricing")],
        # Expected 0 until the all-read-only flag was deleted. This case was
        # that flag's only justification, and a real session — `95b6bde7` —
        # lost the MCP retrieval its procedure exists for to it. Banking this
        # is the accepted cost: a row left in the ledger to review rather than
        # a silent discard, and a one-off never reaches the recurrence
        # threshold anyway.
        episodes=1, methods=0, tags=["nothing-here", "mcp"]),
]


# Multi-task sessions. Added after the first cross-branch run, where a detector
# doing no segmentation at all scored 11 of 14 — because 13 of the 14 cases held
# one task, and banking exactly one entry is right by construction there. A
# corpus that cannot separate "segments correctly" from "never segments" is not
# measuring segmentation.
CASES += [
    Case(
        "three-tasks-one-morning", "programming",
        "A release, a dependency bump and a hotfix. Three procedures a "
        "session-level detector reports as one.",
        [P("cut the 2.4 release tag"),
         B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Verify green before tagging"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && git tag -s v2.4.0 -m rel\necho "=== exit $? ==="', note="Tag the release"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git push --follow-tags\necho "=== exit $? ==="', note="Publish the tag to origin"),
         P("now bump lodash to 4.17.21"),
         E("package.json"), B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && npm ci\necho "=== exit $? ==="', note="Reinstall after the dependency bump"), B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Confirm the bump breaks nothing"),
         B("cd /Users/dev/ai_projects/acme-platform/services/api && git commit -a -F- <<'MSG'\nchore: bump lodash\n\nVerified green before landing; see the run log.\nMSG", note="Commit the lodash bump"),
         P("and the login redirect is broken on staging"),
         B('echo "=== grep ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && grep -rn redirect src/auth/\necho "=== exit $? ==="', note="Find the auth redirect logic"), E("src/auth/login.py"),
         B('cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && pytest tests/test_auth.py', note="Reproduce the login redirect failure"), B("cd /Users/dev/ai_projects/acme-platform/services/api && git commit -a -F- <<'MSG'\nfix: login redirect\n\nVerified green before landing; see the run log.\nMSG", note="Commit the redirect fix")],
        episodes=3, methods=1, tags=["boundary", "multi"]),

    Case(
        "two-chores-one-sitting", "productivity",
        "Friday: the update, then the expenses. Unrelated, both recurring.",
        [P("draft my weekly update"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git log --author=dev --since=7.days --oneline\necho "=== exit $? ==="', note="Collect this week's commits for the update"),
         W("/tmp/weekly.md"),
         M("Gmail__create_draft", to="ludwig@example.com"),
         P("also do March expenses while we are here"),
         M("Drive__search_files", query="receipt March"),
         W("/tmp/expenses.csv"),
         M("Sheets__append_rows", spreadsheet="Expenses 2026")],
        episodes=2, methods=2, tags=["boundary", "multi", "mcp"]),

    Case(
        "a-procedure-then-a-dead-end", "programming",
        "Real work, then an investigation that concludes nothing. One should "
        "be banked and the other should not — a session-level detector cannot "
        "do both.",
        [P("roll out the api hotfix to staging"),
         B('cd /Users/dev/ai_projects/acme-platform && helm upgrade api charts/api --set image.tag=2.2.1 --wait 2>&1 | tail -15', note="Deploy the new image to staging"),
         B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-973; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && kubectl rollout status deploy/api -n staging; echo "artifacts in $SP"', note="Wait for the rollout to finish"),
         B('echo "=== ./scripts/smoke.sh ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && ./scripts/smoke.sh staging\necho "=== exit $? ==="', note="Smoke-test staging after deploy"),
         P("why is the nightly job slower lately?"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git log --since=14.days --oneline\necho "=== exit $? ==="', note="See what landed in the last fortnight"), B('echo "=== cat ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && cat .github/workflows/nightly.yml\necho "=== exit $? ==="', note="Read the nightly workflow definition"),
         B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-325; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && tail -200 logs/nightly.log; echo "artifacts in $SP"', note="Read the tail of the nightly log")],
        episodes=1, methods=1, tags=["boundary", "multi", "mixed"]),
]


# Cases for the three capabilities the corpus could not see. Each was added
# after a feature shipped with nothing here able to score it, which is its own
# finding: `merge`, recurrence and the split all measured zero on a 17-case
# corpus that scored 17 of 17.
CASES += [
    Case(
        "deploy-then-status-email", "programming",
        "One request, two procedures from different worlds, and no marker "
        "between them. The unambiguous version of `case C` — nobody calls a "
        "helm rollout and a status email one procedure.",
        [P("ship the api hotfix and then draft my weekly update"),
         B('cd /Users/dev/ai_projects/acme-platform && helm upgrade api charts/api --set image.tag=2.2.1 --wait 2>&1 | tail -15', note="Deploy the new image to staging"),
         B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-973; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && kubectl rollout status deploy/api -n staging; echo "artifacts in $SP"', note="Wait for the rollout to finish"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git log --author=me --since=7.days --oneline\necho "=== exit $? ==="', note="Collect my commits for the update"),
         W("/tmp/weekly-update.md"),
         M("Gmail__create_draft", to="ludwig@example.com")],
        # Two procedures, so two candidates. Code banks one — there is no
        # marker to cut at — and `draft` splits it afterwards. Recorded as a
        # segmentation miss on purpose: the gap is real and the fix is
        # downstream, so hiding it here would flatter the segmenter.
        episodes=2, methods=2, tags=["multi", "no-marker", "needs-split"]),

    Case(
        "the-same-release-twice", "programming",
        "Two sessions, one procedure. Occurrences count sessions, so this is "
        "the only shape that can reach the threshold at all — and it had never "
        "been tested.",
        [P("cut the 2.4 release"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git checkout main\necho "=== exit $? ==="', note="Start from a clean main"), B('cd /Users/dev/ai_projects/acme-platform && git pull --ff-only 2>&1 | tail -15', note="Fast-forward to origin"), B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Verify green before tagging"),
         B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm version 2.4.0\necho "=== exit $? ==="', note="Bump the package version"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && git tag -s v2.4.0 -m rel\necho "=== exit $? ==="', note="Sign the release tag"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git push --follow-tags\necho "=== exit $? ==="', note="Publish the tag to origin")],
        follow=[P("cut the 2.5 release"),
                B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git checkout main\necho "=== exit $? ==="', note="Start from a clean main"), B('cd /Users/dev/ai_projects/acme-platform && git pull --ff-only 2>&1 | tail -15', note="Fast-forward to origin"), B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Verify green before tagging"),
                B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm version 2.5.0\necho "=== exit $? ==="', note="Bump the package version"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && git tag -s v2.5.0 -m rel\necho "=== exit $? ==="', note="Sign the release tag"),
                B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git push --follow-tags\necho "=== exit $? ==="', note="Publish the tag to origin")],
        episodes=1, methods=1, occurrences=2, tags=["recurrence"]),

    Case(
        "the-same-release-different-runner", "programming",
        "The same release with one step served by a different tool. Lexical "
        "similarity scores this 0.786 against a 0.85 threshold — the worst "
        "place to land — so it banks twice and needs `merge` to become one.",
        [P("cut the 2.4 release"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git checkout main\necho "=== exit $? ==="', note="Start from a clean main"), B('cd /Users/dev/ai_projects/acme-platform && git pull --ff-only 2>&1 | tail -15', note="Fast-forward to origin"), B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Verify green before tagging"),
         B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm version 2.4.0\necho "=== exit $? ==="', note="Bump the package version"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && git tag -s v2.4.0 -m rel\necho "=== exit $? ==="', note="Sign the release tag"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git push --follow-tags\necho "=== exit $? ==="', note="Publish the tag to origin")],
        follow=[P("cut the 2.5 release"),
                B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git checkout main\necho "=== exit $? ==="', note="Start from a clean main"), B('cd /Users/dev/ai_projects/acme-platform && git pull --ff-only 2>&1 | tail -15', note="Fast-forward to origin"), B('cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && pytest -q', note="Verify green before tagging"),
                B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm version 2.5.0\necho "=== exit $? ==="', note="Bump the package version"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && git tag -s v2.5.0 -m rel\necho "=== exit $? ==="', note="Sign the release tag"),
                B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git push --follow-tags\necho "=== exit $? ==="', note="Publish the tag to origin")],
        # One procedure done twice, so one entry at x2. Lexical similarity
        # banks two at x1 and `merge` folds them. Recorded as the miss it is.
        episodes=1, methods=1, occurrences=2, tags=["near-miss", "needs-merge"]),

    Case(
        "the-same-release-two-steps-different", "programming",
        "The same release where the test runner *and* the fetch differ. Scores "
        "0.531 lexically — under `merge`'s 0.70 near-miss floor, so the "
        "embedding that separates it at 0.925 is never asked. This is the shape "
        "that leaves three sightings of one procedure sitting at x1 each, and "
        "it is invisible to every band the live command can afford.",
        [P("cut the 2.4 release"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git checkout main\necho "=== exit $? ==="', note="Start from a clean main"), B('cd /Users/dev/ai_projects/acme-platform && git pull --ff-only 2>&1 | tail -15', note="Fast-forward to origin"), B('SP=/private/tmp/build-501/-Users-dev-ai-projects-acme-platform/cache/run-493; cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm test; echo "artifacts in $SP"', note="Verify green before tagging"),
         B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm version 2.4.0\necho "=== exit $? ==="', note="Bump the package version"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && git tag -s v2.4.0 -m rel\necho "=== exit $? ==="', note="Sign the release tag"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git push --follow-tags\necho "=== exit $? ==="', note="Publish the tag to origin")],
        follow=[P("cut the 2.5 release"),
                B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git checkout main\necho "=== exit $? ==="', note="Start from a clean main"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform && git fetch --all 2>&1 | head -30\necho "=== exit $? ==="', note="Fetch every remote ref"), B('cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && pytest -q', note="Verify green before tagging"),
                B('echo "=== npm ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && npm version 2.5.0\necho "=== exit $? ==="', note="Bump the package version"), B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && git tag -s v2.5.0 -m rel\necho "=== exit $? ==="', note="Sign the release tag"),
                B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git push --follow-tags\necho "=== exit $? ==="', note="Publish the tag to origin")],
        episodes=1, methods=1, occurrences=2,
        tags=["near-miss", "needs-merge", "below-live-floor"]),

    Case(
        "abandoned-then-done-another-way", "programming",
        "A first approach abandoned mid-way, then a different one that worked. "
        "The failed attempt is not a procedure and the session is not empty.",
        [P("get the staging certs renewed"),
         B('cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && certbot renew --dry-run', failed=True, note="Try renewing the certificate"),
         B('echo "=== cat ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && cat /etc/letsencrypt/renewal/staging.conf\necho "=== exit $? ==="', note="Read the renewal config"),
         B('echo "=== acme.sh ===" \ncd /Users/dev/ai_projects/acme-platform/services/api && acme.sh --renew -d staging.example.com\necho "=== exit $? ==="', note="Renew the certificate with acme.sh"),
         B('cd /Users/dev/ai_projects/acme-platform && kubectl create secret tls staging-tls --cert=fullchain.pem --key=privkey.pem --dry-run=client -o yaml | kubectl apply -f - 2>&1 | tail -15', note="Install the new certificate"),
         B('cd /Users/dev/ai_projects/acme-platform/backend/acme_agent && curl -sI https://staging.example.com', note="Confirm the new certificate is served")],
        episodes=1, methods=1, tags=["failure-then-fix", "no-commit"]),

    Case(
        "two-chores-then-nothing", "productivity",
        "Two real chores, then a spell of reading that concluded nothing. The "
        "reading must not attach itself to the second chore.",
        [P("do the weekly update"),
         B('echo "=== git ===" \ncd /Users/dev/ai_projects/acme-platform/backend/acme_agent && git log --since=7.days --oneline\necho "=== exit $? ==="', note="See what landed in the last week"), W("/tmp/weekly.md"),
         M("Gmail__create_draft", to="ludwig@example.com"),
         P("now file the March expenses"),
         M("Drive__search_files", query="receipt March"),
         W("/tmp/expenses.csv"),
         M("Sheets__append_rows", spreadsheet="Expenses 2026"),
         P("what was the pricing decision again?"),
         M("Slack__search_messages", query="pricing"),
         R("/tmp/pricing.md")],
        episodes=2, methods=2, tags=["multi", "mcp", "trailing-noise"]),
]


def by_kind(kind=None):
    return [c for c in CASES if kind is None or c.kind == kind]
