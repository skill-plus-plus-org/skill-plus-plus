#!/usr/bin/env bash
# End-to-end walkthrough against a scratch ledger. Touches nothing real.
#
#   ./examples/demo.sh
#
# Simulates three sessions of the same deploy workflow — including a leaked
# token, a failed-then-retried command, and a target that changes between runs
# — then drives the review surface over the resulting candidate.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

SKILLPP="python3 $ROOT/bin/skillpp --root $SCRATCH/ledger"

hook() { echo "$2" | $SKILLPP hook --event "$1" >/dev/null; }

session() {
  local sid="$1" target="$2" intent="$3"
  hook UserPromptSubmit "{\"session_id\":\"$sid\",\"cwd\":\"/proj/api\",\"prompt\":\"$intent\"}"
  hook PostToolUse "{\"session_id\":\"$sid\",\"cwd\":\"/proj/api\",\"tool_name\":\"Bash\",
    \"tool_input\":{\"command\":\"npm run build\"},\"tool_response\":{\"exit_code\":0}}"
  # A real leaked credential in the trace — it must never reach the ledger.
  hook PostToolUse "{\"session_id\":\"$sid\",\"cwd\":\"/proj/api\",\"tool_name\":\"Bash\",
    \"tool_input\":{\"command\":\"export DEPLOY_TOKEN=ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8\"},
    \"tool_response\":{\"exit_code\":0}}"
  # Fails, then succeeds with an extra flag: the trace has the fix, not the why.
  hook PostToolUse "{\"session_id\":\"$sid\",\"cwd\":\"/proj/api\",\"tool_name\":\"Bash\",
    \"tool_input\":{\"command\":\"terraform apply\"},\"tool_response\":{\"exit_code\":1,\"is_error\":true}}"
  hook PostToolUse "{\"session_id\":\"$sid\",\"cwd\":\"/proj/api\",\"tool_name\":\"Bash\",
    \"tool_input\":{\"command\":\"terraform apply -lock=false\"},\"tool_response\":{\"exit_code\":0}}"
  hook PostToolUse "{\"session_id\":\"$sid\",\"cwd\":\"/proj/api\",\"tool_name\":\"Bash\",
    \"tool_input\":{\"command\":\"./scripts/deploy.sh $target\"},\"tool_response\":{\"exit_code\":0}}"
  hook SessionEnd "{\"session_id\":\"$sid\"}"
}

banner() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

banner "1. Three sessions captured (deploy target differs on the third)"
session s1 staging "deploy the api to staging"
session s2 staging "ship the api build"
session s3 prod    "deploy the api to prod"
$SKILLPP stats

banner "2. Secrets never reached the ledger"
if grep -rq "ghp_A1b2C3d4" "$SCRATCH/ledger/ledger/" 2>/dev/null; then
  echo "FAIL: token found in ledger"; exit 1
fi
echo "OK — no raw token on disk; redaction placeholders instead:"
grep -rho "\[REDACTED:[a-z-]*\]" "$SCRATCH/ledger/ledger/" | sort -u | sed 's/^/  /'

banner "3. Candidates ready for review (threshold: 3 occurrences)"
$SKILLPP review

ID="$($SKILLPP review --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"

banner "4. The proposal — effects first, then evidence, then questions"
$SKILLPP show "$ID"

banner "5. The ledger is searchable, not just a suggestion queue"
$SKILLPP search deploy prod

banner "6. Generated scaffold (the agent edits this into the real skill)"
$SKILLPP scaffold "$ID" --name deploy-api --description "Deploy the API service." \
  --answers '{"when_to_use":"After a green build on main, when shipping the API."}'

banner "7. Dependency check at pull time"
$SKILLPP check --id "$ID" || true

banner "8. Lifecycle inventory"
mkdir -p "$SCRATCH/skills/deploy-api"
$SKILLPP scaffold "$ID" --name deploy-api --out "$SCRATCH/skills/deploy-api/SKILL.md" >/dev/null
$SKILLPP promote "$ID" --skill-path "$SCRATCH/skills/deploy-api/SKILL.md"
$SKILLPP lifecycle --skills-dir "$SCRATCH/skills" --project-root "$SCRATCH" -v
$SKILLPP tier deploy-api cold --skills-dir "$SCRATCH/skills"
$SKILLPP lifecycle --skills-dir "$SCRATCH/skills" --project-root "$SCRATCH"

banner "Done — scratch ledger discarded"
