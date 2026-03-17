#!/usr/bin/env bash
# token-capture.sh — PreToolUse hook for helioy-bus token tracking
#
# Captures the token count from the tmux status bar on every tool call.
# The Claude Code status line always contains a "(\\d+) tokens" pattern.
# Extracts it via tmux capture-pane and writes to registry.db.
#
# Target: under 50ms total (tmux capture + grep + sqlite3).

set -euo pipefail

# Bail early if not in tmux
[[ -z "${TMUX_PANE:-}" ]] && exit 0

BUS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}"
DB_PATH="$BUS_DIR/registry.db"
PIDS_DIR="$BUS_DIR/pids"

# Fast path: resolve agent_id from PID file (avoids tmux call)
PID_FILE="$PIDS_DIR/$PPID"
if [[ -f "$PID_FILE" ]]; then
    AGENT_ID="$(cat "$PID_FILE")"
else
    exit 0
fi

# Capture the last 5 lines of the pane (status bar is at the bottom)
CAPTURED=$(tmux capture-pane -t "$TMUX_PANE" -p -S -5 2>/dev/null) || exit 0

# Extract token count: pattern is "<digits> tokens"
TOKENS=$(printf '%s\n' "$CAPTURED" | grep -oE '[0-9]+ tokens' | tail -1 | grep -oE '[0-9]+') || exit 0

[[ -z "$TOKENS" ]] && exit 0

# Write to registry.db with concurrency-safe timeout
sqlite3 -cmd ".timeout 3000" "$DB_PATH" "
    UPDATE agents
    SET token_usage = json_object('tokens', $TOKENS, 'updated', datetime('now')),
        last_seen = datetime('now')
    WHERE agent_id = '${AGENT_ID//\'/\'\'}';
" 2>/dev/null || true

exit 0
