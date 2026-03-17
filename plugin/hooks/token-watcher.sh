#!/usr/bin/env bash
# token-watcher.sh — SessionStart hook for helioy-bus token tracking
#
# Spawns a background `tail -F` process that watches the agent's JSONL
# session file for assistant turns and writes token usage to registry.db.
#
# JSONL path is deterministic:
#   ~/.claude/projects/-${PROJECT_DIR//\//-}/${SESSION_ID}.jsonl
#
# The watcher updates the agents.token_usage JSON column directly via sqlite3.
# PID is stored at ~/.helioy/bus/pids/${AGENT_ID}.token_watcher for cleanup.

set -euo pipefail

BUS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}"
DB_PATH="$BUS_DIR/registry.db"
PIDS_DIR="$BUS_DIR/pids"
CONTEXT_LIMIT="${TOKEN_CONTEXT_LIMIT:-200000}"

# Resolve identity (same as bus-register.sh)
HOOKS_LIB="$(dirname "$0")/lib/resolve-identity.sh"
# shellcheck source=lib/resolve-identity.sh
source "$HOOKS_LIB"
resolve_agent_id

AGENT_ID="$HELIOY_AGENT_ID"

# Derive JSONL path from project dir + session ID
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
ENCODED_DIR="-${PROJECT_DIR//\//-}"
SESSION_ID="${HELIOY_SESSION_ID:-}"

# Cannot watch without a session ID (ad-hoc sessions without claude-wrapper)
if [[ -z "$SESSION_ID" ]]; then
    echo "{}"
    exit 0
fi

JSONL_FILE="$HOME/.claude/projects/${ENCODED_DIR}/${SESSION_ID}.jsonl"

# Kill any existing watcher for this agent (handles agent restart in same pane)
WATCHER_PID_FILE="$PIDS_DIR/${AGENT_ID}.token_watcher"
if [[ -f "$WATCHER_PID_FILE" ]]; then
    OLD_PID=$(cat "$WATCHER_PID_FILE" 2>/dev/null || true)
    if [[ -n "$OLD_PID" ]] && kill -0 "$OLD_PID" 2>/dev/null; then
        kill "$OLD_PID" 2>/dev/null || true
    fi
    rm -f "$WATCHER_PID_FILE"
fi

mkdir -p "$PIDS_DIR"

# Escape agent_id for safe use in SQL (single quotes doubled)
SAFE_AGENT_ID="${AGENT_ID//\'/\'\'}"

# Spawn the background watcher.
# Uses tail -F (follow by name) which retries until the file appears.
# The while loop exits when tail is killed (by stop hook or orphan check).
(
    TOTAL_OUTPUT=0
    HOOK_PPID=$PPID

    tail -n 0 -F "$JSONL_FILE" 2>/dev/null | while IFS= read -r line; do
        # Orphan cleanup: exit if the parent process (claude) is gone
        if ! kill -0 "$HOOK_PPID" 2>/dev/null; then
            rm -f "$WATCHER_PID_FILE"
            break
        fi

        # Filter for assistant turns with usage data
        usage=$(echo "$line" | jq -c 'select(.type == "assistant") | .message.usage // empty' 2>/dev/null) || continue
        [[ -z "$usage" ]] && continue

        input=$(echo "$usage" | jq -r '.input_tokens // 0')
        cache_creation=$(echo "$usage" | jq -r '.cache_creation_input_tokens // 0')
        cache_read=$(echo "$usage" | jq -r '.cache_read_input_tokens // 0')
        output=$(echo "$usage" | jq -r '.output_tokens // 0')

        turn_input=$((input + cache_creation + cache_read))
        TOTAL_OUTPUT=$((TOTAL_OUTPUT + output))

        # Update registry.db directly (high-water mark for input, accumulate output)
        sqlite3 "$DB_PATH" "
            UPDATE agents SET token_usage = json_object(
                'total_input', MAX(COALESCE(json_extract(token_usage, '\$.total_input'), 0), $turn_input),
                'total_output', $TOTAL_OUTPUT,
                'limit', $CONTEXT_LIMIT,
                'percent', ROUND(CAST(MAX(COALESCE(json_extract(token_usage, '\$.total_input'), 0), $turn_input) AS REAL) / $CONTEXT_LIMIT * 100, 1),
                'updated', datetime('now')
            ), last_seen = datetime('now')
            WHERE agent_id = '$SAFE_AGENT_ID';
        " 2>/dev/null || true
    done
) &>/dev/null &

echo $! > "$WATCHER_PID_FILE"
disown

echo "{}"
