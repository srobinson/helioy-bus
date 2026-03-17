#!/usr/bin/env bash
# token-watcher.sh — SessionStart hook for helioy-bus token tracking
#
# Spawns a background process that watches the agent's JSONL session file
# for assistant turns and writes token usage to registry.db.
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

# Derive JSONL path from project dir
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"
ENCODED_DIR="${PROJECT_DIR//\//-}"
JSONL_DIR="$HOME/.claude/projects/${ENCODED_DIR}"

# Try explicit session ID first, fall back to most recently modified JSONL
SESSION_ID="${HELIOY_SESSION_ID:-${CLAUDE_SESSION_ID:-}}"
if [[ -n "$SESSION_ID" ]]; then
    JSONL_FILE="$JSONL_DIR/${SESSION_ID}.jsonl"
else
    # Find the most recently modified JSONL in the project dir.
    # At SessionStart, the current session's file is the newest.
    JSONL_FILE=$(ls -t "$JSONL_DIR"/*.jsonl 2>/dev/null | head -1)
    if [[ -z "$JSONL_FILE" ]]; then
        # No JSONL files yet. Give Claude a moment to create one, then retry.
        sleep 2
        JSONL_FILE=$(ls -t "$JSONL_DIR"/*.jsonl 2>/dev/null | head -1)
    fi
    if [[ -z "$JSONL_FILE" ]]; then
        echo "{}"
        exit 0
    fi
fi

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

# Capture Claude's PID for orphan detection. In the hook context, PPID
# is the Claude Code process. Store it before entering the subshell so
# the background process has a stable reference.
CLAUDE_PID="$PPID"

# Spawn the background watcher.
# Phase 1: Process existing JSONL content for initial catch-up.
# Phase 2: tail -F for ongoing updates.
(
    # Disable errexit inside the watcher. Individual command failures
    # (jq parse errors, sqlite3 lock contention) should not kill the process.
    set +e

    TOTAL_OUTPUT=0

    # --- Helper: process one JSONL line and update the DB ---
    process_line() {
        local line="$1"
        local usage
        usage=$(printf '%s\n' "$line" | jq -c 'select(.type == "assistant") | .message.usage // empty' 2>/dev/null) || return 0
        [[ -z "$usage" ]] && return 0

        local input cache_creation cache_read output turn_input
        input=$(printf '%s' "$usage" | jq -r '.input_tokens // 0')
        cache_creation=$(printf '%s' "$usage" | jq -r '.cache_creation_input_tokens // 0')
        cache_read=$(printf '%s' "$usage" | jq -r '.cache_read_input_tokens // 0')
        output=$(printf '%s' "$usage" | jq -r '.output_tokens // 0')

        turn_input=$((input + cache_creation + cache_read))
        TOTAL_OUTPUT=$((TOTAL_OUTPUT + output))

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
    }

    # Phase 1: catch-up on existing content.
    # Process all existing lines to get current token state.
    if [[ -f "$JSONL_FILE" ]]; then
        while IFS= read -r line; do
            process_line "$line"
        done < "$JSONL_FILE"
    fi

    # Record the file size after catch-up so tail starts from here.
    # This avoids double-counting lines we already processed.
    if [[ -f "$JSONL_FILE" ]]; then
        EXISTING_LINES=$(wc -l < "$JSONL_FILE" 2>/dev/null || echo 0)
    else
        EXISTING_LINES=0
    fi

    # Phase 2: tail for new lines.
    # Uses tail -F (follow by name) which retries if the file doesn't exist yet.
    # +N starts from line N (1-indexed), so +$((EXISTING_LINES+1)) skips what we processed.
    tail -n +"$((EXISTING_LINES + 1))" -F "$JSONL_FILE" 2>/dev/null | while IFS= read -r line; do
        # Orphan cleanup: exit if Claude is gone.
        if ! kill -0 "$CLAUDE_PID" 2>/dev/null; then
            rm -f "$WATCHER_PID_FILE"
            break
        fi
        process_line "$line"
    done
) &>/dev/null &

echo $! > "$WATCHER_PID_FILE"
disown

echo "{}"
