#!/usr/bin/env bash
# test_resolve_identity.sh -- Unit tests for plugin/hooks/lib/resolve-identity.sh
#
# Tests the identity resolution logic without requiring a live tmux session.
# Run with: bash tests/test_resolve_identity.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LIB="$SCRIPT_DIR/plugin/hooks/lib/resolve-identity.sh"

PASS=0
FAIL=0

ok() {
    echo "  PASS: $1"
    PASS=$((PASS + 1))
}

fail() {
    echo "  FAIL: $1"
    echo "        got:      $2"
    echo "        expected: $3"
    FAIL=$((FAIL + 1))
}

assert_eq() {
    if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1" "$2" "$3"; fi
}

# Run resolve_agent_id in a subshell.
# Usage: run_resolve WORKDIR=/path [KEY=VAL ...]
# Prints: agent_id|agent_type|agent_repo
run_resolve() {
    local workdir="$SCRIPT_DIR"
    local exports=()
    for arg in "$@"; do
        if [[ "$arg" == WORKDIR=* ]]; then
            workdir="${arg#WORKDIR=}"
        else
            exports+=("$arg")
        fi
    done
    (
        cd "$workdir"
        for pair in "${exports[@]}"; do
            export "${pair?}"
        done
        # Ensure no tmux inheritance from caller
        unset TMUX TMUX_PANE 2>/dev/null || true
        source "$LIB"
        resolve_agent_id
        printf '%s|%s|%s' "$HELIOY_AGENT_ID" "$HELIOY_AGENT_TYPE" "$HELIOY_AGENT_REPO"
    )
}

parse_result() {
    # parse_result <result> <field: id|type|repo>
    local result="$1"
    local field="$2"
    local agent_id="${result%%|*}"
    local rest="${result#*|}"
    local agent_type="${rest%%|*}"
    local agent_repo="${rest##*|}"
    case "$field" in
        id)   printf '%s' "$agent_id" ;;
        type) printf '%s' "$agent_type" ;;
        repo) printf '%s' "$agent_repo" ;;
    esac
}

echo ""
echo "=== resolve-identity.sh tests ==="

# Set up temp directories
mkdir -p /tmp/helioy-test-myproject
mkdir -p /tmp/helioy-test-myrepo

# ── Test 1: No tmux, no CLAUDE_PROJECT_DIR -- fallback to PWD basename ----------
echo ""
echo "--- Fallback: no tmux, no CLAUDE_PROJECT_DIR ---"

result=$(run_resolve "WORKDIR=/tmp/helioy-test-myproject")
assert_eq "agent_id is basename(PWD)"   "$(parse_result "$result" id)"   "helioy-test-myproject"
assert_eq "agent_type defaults to general" "$(parse_result "$result" type)" "general"
assert_eq "agent_repo is basename(PWD)" "$(parse_result "$result" repo)" "helioy-test-myproject"

# ── Test 2: No tmux, with CLAUDE_PROJECT_DIR ----------------------------------
echo ""
echo "--- Fallback: no tmux, with CLAUDE_PROJECT_DIR ---"

result=$(run_resolve "WORKDIR=/tmp" "CLAUDE_PROJECT_DIR=/tmp/helioy-bus-test")
assert_eq "agent_id uses CLAUDE_PROJECT_DIR"   "$(parse_result "$result" id)"   "helioy-bus-test"
assert_eq "agent_type defaults to general"     "$(parse_result "$result" type)" "general"
assert_eq "agent_repo uses CLAUDE_PROJECT_DIR" "$(parse_result "$result" repo)" "helioy-bus-test"

# ── Test 3: No tmux, HELIOY_BUS_AGENT_TYPE override ---------------------------
echo ""
echo "--- Fallback with HELIOY_BUS_AGENT_TYPE override ---"

result=$(run_resolve "WORKDIR=/tmp/helioy-test-myrepo" "HELIOY_BUS_AGENT_TYPE=backend-engineer")
assert_eq "agent_type from HELIOY_BUS_AGENT_TYPE" "$(parse_result "$result" type)" "backend-engineer"
assert_eq "agent_id is basename(PWD)"             "$(parse_result "$result" id)"   "helioy-test-myrepo"

# ── Test 4: Identity pattern validation regex ----------------------------------
echo ""
echo "--- Identity pattern validation ---"

# Must stay in sync with _IDENTITY_PATTERN in resolve-identity.sh
PATTERN='^[a-zA-Z0-9_-]+:[a-zA-Z0-9_:-]+:[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$'

for title in \
    "fmm:general:7:2.1" \
    "helioy-bus:backend-engineer:7:3.1" \
    "my_repo:frontend-engineer:10:0.0" \
    "repo:general:1:1.1" \
    "fmm:general:helioy:2.1" \
    "helioy-bus:general:work:1.0" \
    "myrepo:backend-engineer:my-session:3.2" \
    "helioy:voltagent-lang:rust-engineer:5:2.1" \
    "helioy:voltagent-qa-sec:architect-reviewer:5:2.4"
do
    if printf '%s' "$title" | grep -qE "$PATTERN"; then
        ok "valid title matches: $title"
    else
        fail "valid title should match" "$title" "matches pattern"
    fi
done

for title in \
    "fmm" \
    "fmm:general" \
    "fmm:general:7" \
    "fmm:general:7:abc" \
    "repo with spaces:general:7:2.1" \
    ""
do
    if ! printf '%s' "$title" | grep -qE "$PATTERN"; then
        ok "invalid title rejected: '$title'"
    else
        fail "invalid title should NOT match" "$title" "does not match pattern"
    fi
done

# ── Test 5: Pane-title extraction when title matches format -------------------
echo ""
echo "--- Pane-title extraction ---"

# Simulate the parsing logic from resolve_agent_id.
# Parse from both ends: repo from left, session:window.pane from right,
# agent_type is everything between.
parse_title() {
    local t="$1"
    local _repo="${t%%:*}"
    local _without_wp="${t%:*}"
    local _without_swp="${_without_wp%:*}"
    local _type="${_without_swp#*:}"
    printf '%s|%s' "$_repo" "$_type"
}

# Simple agent type
parsed=$(parse_title "helioy-bus:backend-engineer:7:3.1")
assert_eq "repo extracted from title" "${parsed%%|*}" "helioy-bus"
assert_eq "type extracted from title" "${parsed#*|}"  "backend-engineer"

parsed=$(parse_title "fmm:general:10:2.3")
assert_eq "repo extracted (fmm)"     "${parsed%%|*}" "fmm"
assert_eq "type extracted (general)" "${parsed#*|}"   "general"

# Namespaced agent type (colon in type)
parsed=$(parse_title "helioy:voltagent-lang:rust-engineer:5:2.1")
assert_eq "repo extracted (namespaced)" "${parsed%%|*}" "helioy"
assert_eq "type extracted (namespaced)" "${parsed#*|}"  "voltagent-lang:rust-engineer"

parsed=$(parse_title "helioy:voltagent-qa-sec:architect-reviewer:5:2.4")
assert_eq "repo extracted (qa-sec)"     "${parsed%%|*}" "helioy"
assert_eq "type extracted (qa-sec)"     "${parsed#*|}"  "voltagent-qa-sec:architect-reviewer"

# Named session with namespaced type
parsed=$(parse_title "myrepo:voltagent-lang:python-pro:helioy:1.0")
assert_eq "repo extracted (named session)" "${parsed%%|*}" "myrepo"
assert_eq "type extracted (named session)" "${parsed#*|}"  "voltagent-lang:python-pro"

# ── Summary -------------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
echo ""

[[ $FAIL -eq 0 ]]
