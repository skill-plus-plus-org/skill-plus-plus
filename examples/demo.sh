#!/usr/bin/env bash
# End-to-end walkthrough against a scratch ledger. Touches nothing real.
#
#   ./examples/demo.sh
#
# Simulates three sessions of the same deploy workflow — including a leaked
# token, a failed-then-retried command, and a target that changes between runs
# — then drives the review surface over the resulting candidate.
#
# Needs Ollama with `nomic-embed-text`, which is what decides the three runs are
# one procedure. Each session has a single prompt, so there is no gap for the
# boundary judge to ask about, and naming is off, so the larger model is never
# loaded.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRATCH="$(mktemp -d)"
trap 'rm -rf "$SCRATCH"' EXIT

export SKILL_PLUS_PLUS_NAME=0
SKILL_PLUS_PLUS="python3 $ROOT/bin/skill-plus-plus --root $SCRATCH/ledger"

# Said up front: without the embedding model every session is held rather than
# banked, and the walkthrough below would have nothing to show.
python3 - "${SKILL_PLUS_PLUS_OLLAMA:-http://127.0.0.1:11434}" "${SKILL_PLUS_PLUS_EMBED_MODEL:-nomic-embed-text}" <<'EOF'
import json, sys, urllib.request
url, model = sys.argv[1], sys.argv[2]
try:
    with urllib.request.urlopen(f"{url}/api/tags", timeout=5) as resp:
        names = [m.get("name", "") for m in json.load(resp).get("models", [])]
except Exception as exc:
    sys.exit(f"Ollama is not reachable at {url} ({exc}).\n"
             f"Start it, then pull the model:  ollama pull {model}")
if not any(n == model or n.startswith(model + ":") for n in names):
    sys.exit(f"Ollama is running, but {model} is not pulled:  ollama pull {model}")
EOF

hook() { echo "$2" | $SKILL_PLUS_PLUS hook --event "$1" >/dev/null; }

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
  # What the SessionEnd hook starts in the background, run in the foreground
  # here so the next step sees what it banked.
  $SKILL_PLUS_PLUS fold-session "$sid"
}

banner() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

banner "1. Three sessions captured (deploy target differs on the third)"
session s1 staging "deploy the api to staging"
session s2 staging "ship the api build"
session s3 prod    "deploy the api to prod"
$SKILL_PLUS_PLUS stats

banner "2. Secrets never reached the ledger"
if grep -rq "ghp_A1b2C3d4" "$SCRATCH/ledger/ledger/" 2>/dev/null; then
  echo "FAIL: token found in ledger"; exit 1
fi
echo "OK — no raw token on disk; redaction placeholders instead:"
grep -rho "\[REDACTED:[a-z-]*\]" "$SCRATCH/ledger/ledger/" | sort -u | sed 's/^/  /' || echo "  (none)"

banner "3. Candidates ready for review (threshold: 3 occurrences)"
$SKILL_PLUS_PLUS review

ID="$($SKILL_PLUS_PLUS review --json | python3 -c 'import json,sys
ready = json.load(sys.stdin)
if not ready:
    sys.exit("No candidate reached 3 occurrences: the three runs were not matched as one procedure.")
print(ready[0]["id"])')"

banner "4. The proposal — effects first, then evidence, then questions"
$SKILL_PLUS_PLUS show "$ID"

banner "5. The ledger is searchable, not just a suggestion queue"
$SKILL_PLUS_PLUS search deploy prod

banner "6. Generated scaffold (the agent edits this into the real skill)"
$SKILL_PLUS_PLUS scaffold "$ID" --name deploy-api --description "Deploy the API service." \
  --answers '{"when_to_use":"After a green build on main, when shipping the API."}'

banner "7. Dependency check at pull time"
$SKILL_PLUS_PLUS check --id "$ID" || true

banner "8. Lifecycle inventory"
mkdir -p "$SCRATCH/skills/deploy-api"
$SKILL_PLUS_PLUS scaffold "$ID" --name deploy-api --out "$SCRATCH/skills/deploy-api/SKILL.md" >/dev/null
$SKILL_PLUS_PLUS promote "$ID" --skill-path "$SCRATCH/skills/deploy-api/SKILL.md"
$SKILL_PLUS_PLUS lifecycle --skills-dir "$SCRATCH/skills" --project-root "$SCRATCH" -v
$SKILL_PLUS_PLUS tier deploy-api cold --skills-dir "$SCRATCH/skills"
$SKILL_PLUS_PLUS lifecycle --skills-dir "$SCRATCH/skills" --project-root "$SCRATCH"

banner "Done — scratch ledger discarded"
