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

# ── Test 6: Claude Code TUI decoration stripping ─────────────────────────────
echo ""
echo "--- TUI decoration stripping ---"

# Claude Code prefixes pane titles with status icons like "✳ ", "⠐ ", etc.
strip_decorations() {
    printf '%s' "$1" | sed 's/^[^a-zA-Z0-9_-]* *//'
}

assert_eq "strip ✳ prefix"  "$(strip_decorations '✳ voltagent-lang:rust-engineer')" "voltagent-lang:rust-engineer"
assert_eq "strip ⠐ prefix"  "$(strip_decorations '⠐ Claude Code')"                  "Claude Code"
assert_eq "strip ⠒ prefix"  "$(strip_decorations '⠒ backend-engineer')"              "backend-engineer"
assert_eq "no-op clean title" "$(strip_decorations 'fmm:general:7:2.1')"             "fmm:general:7:2.1"
assert_eq "strip emoji prefix" "$(strip_decorations '🔵 rust-engineer')"              "rust-engineer"

# ── Test 7: Bare agent type recognition (Step 2.5) ───────────────────────────
echo ""
echo "--- Bare agent type recognition ---"

BARE_PATTERN='^[a-zA-Z][a-zA-Z0-9_:-]*[a-zA-Z0-9]$'

for bare in \
    "backend-engineer" \
    "voltagent-lang:rust-engineer" \
    "voltagent-qa-sec:architect-reviewer" \
    "general"
do
    if printf '%s' "$bare" | grep -qE "$BARE_PATTERN"; then
        ok "bare type recognized: $bare"
    else
        fail "bare type should match" "$bare" "matches bare pattern"
    fi
done

# These should NOT match as bare agent types
for nonbare in \
    "Claude Code" \
    "fmm:general:7:2.1" \
    "" \
    "1invalid"
do
    if ! printf '%s' "$nonbare" | grep -qE "$BARE_PATTERN" 2>/dev/null; then
        ok "non-bare rejected: '$nonbare'"
    else
        fail "non-bare should NOT match" "$nonbare" "does not match bare pattern"
    fi
done

# ── Test 8: Symlink-safe HELIOY_BUS_ROOT resolution ──────────────────────────
echo ""
echo "--- Symlink-safe repo root resolution (relative symlink install) ---"

_SYMLINK_TMP=$(mktemp -d)
_symlink_cleanup() { rm -rf "$_SYMLINK_TMP"; }
trap _symlink_cleanup EXIT

# Build a fake repo layout: real/plugin/hooks/bus-register.sh
mkdir -p "$_SYMLINK_TMP/real/plugin/hooks"

# Write a minimal probe script that uses the exact resolution logic from
# bus-register.sh and prints the derived HELIOY_BUS_ROOT.
cat > "$_SYMLINK_TMP/real/plugin/hooks/bus-register.sh" <<'PROBE'
#!/usr/bin/env bash
_script="${BASH_SOURCE[0]}"
while [[ -L "$_script" ]]; do
    _dir="$(cd "$(dirname "$_script")" && pwd)"
    _target="$(readlink "$_script")"
    [[ "$_target" != /* ]] && _target="$_dir/$_target"
    _script="$_target"
done
printf '%s' "$(cd "$(dirname "$_script")/../.." && pwd)"
PROBE
chmod +x "$_SYMLINK_TMP/real/plugin/hooks/bus-register.sh"

# Create a relative symlink: link_dir/bus-register.sh -> ../real/plugin/hooks/bus-register.sh
mkdir -p "$_SYMLINK_TMP/link_dir"
(cd "$_SYMLINK_TMP/link_dir" && ln -sf "../real/plugin/hooks/bus-register.sh" bus-register.sh)

# Run the probe via the symlink and verify the resolved root
_got=$(bash "$_SYMLINK_TMP/link_dir/bus-register.sh")
_expected="$_SYMLINK_TMP/real"
assert_eq "relative symlink resolves to correct repo root" "$_got" "$_expected"

# ── Summary -------------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
echo ""

[[ $FAIL -eq 0 ]]
