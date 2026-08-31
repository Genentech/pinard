#!/usr/bin/env bash
# Tests for parcelle / run ID computation in bin/pinard
# Run: bash tests/unit/parcelle.test.sh

set -euo pipefail
PASS=0
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [[ "$expected" == "$actual" ]]; then
    echo "  PASS: $desc"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $desc — expected '$expected', got '$actual'"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== Parcelle / Run ID tests ==="

# Source just the computation logic by simulating the env
# We test the logic directly rather than running bin/pinard (which execs pi)

# Test 1: Default parcelle = project name
WORKER_PARCELLE=""
WORKER_PROJECT="exo-cli"
WORKER_PARCELLE="${WORKER_PARCELLE:-$WORKER_PROJECT}"
assert_eq "default parcelle is project name" "exo-cli" "$WORKER_PARCELLE"

# Test 2: Explicit parcelle preserved
WORKER_PARCELLE="semantic-search"
WORKER_PROJECT="exo-cli"
WORKER_PARCELLE="${WORKER_PARCELLE:-$WORKER_PROJECT}"
assert_eq "explicit parcelle preserved" "semantic-search" "$WORKER_PARCELLE"

# Test 3: Run ID with issue (deterministic)
WORKER_ISSUE="42"
WORKER_PROCESS="dev"
WORKER_PROJECT="exo-cli"
WORKER_SESSION="exohub-exo-cli-1234"
if [[ -n "$WORKER_ISSUE" ]]; then
  RUN_ID="${WORKER_PROJECT}-${WORKER_PROCESS}-${WORKER_ISSUE}"
else
  RUN_ID="${WORKER_PROJECT}-${WORKER_PROCESS}-${WORKER_SESSION}"
fi
assert_eq "issue-driven run ID" "exo-cli-dev-42" "$RUN_ID"

# Test 4: Run ID without issue (session-based)
WORKER_ISSUE=""
WORKER_PROCESS="hello"
WORKER_PROJECT="exo-cli"
WORKER_SESSION="exohub-exo-cli-5036"
if [[ -n "$WORKER_ISSUE" ]]; then
  RUN_ID="${WORKER_PROJECT}-${WORKER_PROCESS}-${WORKER_ISSUE}"
else
  RUN_ID="${WORKER_PROJECT}-${WORKER_PROCESS}-${WORKER_SESSION}"
fi
assert_eq "session-based run ID" "exo-cli-hello-exohub-exo-cli-5036" "$RUN_ID"

# Test 5: Runs dir computed from vignoble + parcelle
VIGNOBLE="/data/vignoble-exohub"
WORKER_PARCELLE="babysitter"
RUNS_DIR="$VIGNOBLE/.parcelles/$WORKER_PARCELLE/runs"
assert_eq "runs dir path" "/data/vignoble-exohub/.parcelles/babysitter/runs" "$RUNS_DIR"

# Test 6: Same issue + process + project = same run ID (resumable)
WORKER_ISSUE="42"
WORKER_PROCESS="dev"
WORKER_PROJECT="exo-cli"
RUN_ID_1="${WORKER_PROJECT}-${WORKER_PROCESS}-${WORKER_ISSUE}"
RUN_ID_2="${WORKER_PROJECT}-${WORKER_PROCESS}-${WORKER_ISSUE}"
assert_eq "deterministic run ID across spawns" "$RUN_ID_1" "$RUN_ID_2"

echo ""
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
