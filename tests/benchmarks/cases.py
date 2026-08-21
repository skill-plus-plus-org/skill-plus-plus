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


def B(command, failed=False):     # a shell command
    return ("Bash", command, failed)


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
         B("git checkout main"), B("git pull --ff-only"), B("npm test"),
         B("npm version 2.4.0"), B("git tag -s v2.4.0 -m 'Release 2.4.0'"),
         B("git push --follow-tags")],
        episodes=1, methods=1, tags=["marker", "recurring-shape"]),

    Case(
        "rotate-a-credential", "programming",
        "Quarterly, per credential. Rare but high value — the case frequency "
        "alone would never surface.",
        [P("rotate the staging database password"),
         B("openssl rand -base64 32"),
         B("kubectl create secret generic db-staging --from-literal=pw=<v> "
           "--dry-run=client -o yaml | kubectl apply -f -"),
         B("kubectl rollout restart deploy/api -n staging"),
         B("kubectl rollout status deploy/api -n staging"),
         B("psql -h staging -c 'select 1'")],
        episodes=1, methods=1, tags=["no-commit", "rare"]),

    Case(
        "onboard-a-repository", "programming",
        "Done per repo, per machine, per new joiner.",
        [P("get this repo running locally"),
         B("git clone git@github.com:acme/api.git"), B("cp .env.example .env"),
         B("npm ci"), B("docker compose up -d postgres"),
         B("npm run migrate"), B("npm test")],
        episodes=1, methods=1, tags=["no-commit"]),

    Case(
        "migration-with-a-lock", "programming",
        "The workaround is the knowledge: scale to zero, then migrate. Worth "
        "keeping even though it started as one failure.",
        [P("the staging migration is stuck, get it green"),
         B("npm run migrate", failed=True),
         B("kubectl scale deploy/api --replicas=0 -n staging"),
         B("npm run migrate"),
         B("kubectl scale deploy/api --replicas=3 -n staging"),
         B("npm test")],
        episodes=1, methods=1, tags=["failure-then-fix"]),

    Case(
        "explore-then-one-line-fix", "programming",
        "Eight greps and a one-line change. Real work, done once — banked, "
        "trimmed, and ranked low.",
        [P("the export endpoint 500s intermittently, find out why"),
         B("grep -rn export src/"), B("grep -rn timeout src/api/"),
         B("cat src/api/export.py"), B("grep -rn pool src/db/"),
         B("git log --oneline -20 src/api/export.py"),
         E("src/api/export.py"), B("npm test"), B("git commit -am 'raise export timeout'")],
        episodes=1, methods=0, tags=["exploration", "one-off"]),

    Case(
        "investigation-that-goes-nowhere", "programming",
        "Six reads and a shrug. Nothing was done, so nothing is a procedure.",
        [P("the nightly job got slower this week, any idea why"),
         B("git log --since=7.days --oneline"), B("cat .github/workflows/nightly.yml"),
         B("grep -rn timeout .github/"), B("ls -la logs/"),
         B("tail -100 logs/nightly.log")],
        episodes=0, methods=0, tags=["nothing-here"]),

    Case(
        "two-tasks-one-sitting", "programming",
        "A release and an unrelated CI bump. Two procedures, not one session.",
        [P("cut the 2.4 release tag"),
         B("npm test"), B("git tag -s v2.4.0 -m rel"), B("git push --follow-tags"),
         P("now bump CI to node 22"),
         E(".github/workflows/ci.yml"), B("npm test"),
         B("git commit -am 'ci: node 22'"), B("git push")],
        episodes=2, methods=2, tags=["boundary"]),

    Case(
        "hotfix-one-specific-bug", "programming",
        "A null check for one crash. Nobody follows these steps again.",
        [P("users report a crash on empty carts"),
         B("grep -rn 'cart.items' src/"), E("src/cart.py"),
         B("pytest tests/test_cart.py"), B("git commit -am 'fix: guard empty cart'")],
        episodes=1, methods=0, tags=["one-off"]),

    # --------------------------------------------------------------- productivity
    Case(
        "weekly-status-email", "productivity",
        "Every Friday, same shape, different week. No shell marker ever fires.",
        [P("draft my weekly update for Ludwig"),
         B("git log --author=jane.doe --since=7.days --oneline"),
         M("Gmail__search_messages", query="from:ludwig newer_than:7d"),
         W("/tmp/weekly-update.md"),
         M("Gmail__create_draft", to="ludwig@example.com", subject="Weekly update")],
        episodes=1, methods=1, tags=["mcp", "no-commit", "recurring-shape"]),

    Case(
        "expense-report", "productivity",
        "Monthly, same steps, different receipts.",
        [P("do my expenses for March"),
         M("Drive__search_files", query="receipt March"),
         B("ls ~/Documents/receipts/2026-03/"),
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
        "Read four documents, decided nothing, wrote nothing.",
        [P("catch me up on the pricing discussion"),
         M("Drive__search_files", query="pricing"),
         R("/tmp/pricing-v1.md"), R("/tmp/pricing-v2.md"),
         M("Slack__search_messages", query="pricing")],
        episodes=0, methods=0, tags=["nothing-here", "mcp"]),
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
         B("npm test"), B("git tag -s v2.4.0 -m rel"), B("git push --follow-tags"),
         P("now bump lodash to 4.17.21"),
         E("package.json"), B("npm ci"), B("npm test"),
         B("git commit -am 'chore: bump lodash'"),
         P("and the login redirect is broken on staging"),
         B("grep -rn redirect src/auth/"), E("src/auth/login.py"),
         B("pytest tests/test_auth.py"), B("git commit -am 'fix: login redirect'")],
        episodes=3, methods=1, tags=["boundary", "multi"]),

    Case(
        "two-chores-one-sitting", "productivity",
        "Friday: the update, then the expenses. Unrelated, both recurring.",
        [P("draft my weekly update"),
         B("git log --author=jane.doe --since=7.days --oneline"),
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
         B("helm upgrade api charts/api --set image.tag=2.2.1 --wait"),
         B("kubectl rollout status deploy/api -n staging"),
         B("./scripts/smoke.sh staging"),
         P("why is the nightly job slower lately?"),
         B("git log --since=14.days --oneline"), B("cat .github/workflows/nightly.yml"),
         B("tail -200 logs/nightly.log")],
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
         B("helm upgrade api charts/api --set image.tag=2.2.1 --wait"),
         B("kubectl rollout status deploy/api -n staging"),
         B("git log --author=me --since=7.days --oneline"),
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
         B("git checkout main"), B("git pull --ff-only"), B("npm test"),
         B("npm version 2.4.0"), B("git tag -s v2.4.0 -m rel"),
         B("git push --follow-tags")],
        follow=[P("cut the 2.5 release"),
                B("git checkout main"), B("git pull --ff-only"), B("npm test"),
                B("npm version 2.5.0"), B("git tag -s v2.5.0 -m rel"),
                B("git push --follow-tags")],
        episodes=1, methods=1, occurrences=2, tags=["recurrence"]),

    Case(
        "the-same-release-different-runner", "programming",
        "The same release with one step served by a different tool. Lexical "
        "similarity scores this 0.786 against a 0.85 threshold — the worst "
        "place to land — so it banks twice and needs `merge` to become one.",
        [P("cut the 2.4 release"),
         B("git checkout main"), B("git pull --ff-only"), B("npm test"),
         B("npm version 2.4.0"), B("git tag -s v2.4.0 -m rel"),
         B("git push --follow-tags")],
        follow=[P("cut the 2.5 release"),
                B("git checkout main"), B("git pull --ff-only"), B("pytest -q"),
                B("npm version 2.5.0"), B("git tag -s v2.5.0 -m rel"),
                B("git push --follow-tags")],
        # One procedure done twice, so one entry at x2. Lexical similarity
        # banks two at x1 and `merge` folds them. Recorded as the miss it is.
        episodes=1, methods=1, occurrences=2, tags=["near-miss", "needs-merge"]),

    Case(
        "abandoned-then-done-another-way", "programming",
        "A first approach abandoned mid-way, then a different one that worked. "
        "The failed attempt is not a procedure and the session is not empty.",
        [P("get the staging certs renewed"),
         B("certbot renew --dry-run", failed=True),
         B("cat /etc/letsencrypt/renewal/staging.conf"),
         B("acme.sh --renew -d staging.example.com"),
         B("kubectl create secret tls staging-tls --cert=fullchain.pem "
           "--key=privkey.pem --dry-run=client -o yaml | kubectl apply -f -"),
         B("curl -sI https://staging.example.com")],
        episodes=1, methods=1, tags=["failure-then-fix", "no-commit"]),

    Case(
        "two-chores-then-nothing", "productivity",
        "Two real chores, then a spell of reading that concluded nothing. The "
        "reading must not attach itself to the second chore.",
        [P("do the weekly update"),
         B("git log --since=7.days --oneline"), W("/tmp/weekly.md"),
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
