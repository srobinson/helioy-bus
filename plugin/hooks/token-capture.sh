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

# Write to registry.db via parameterized Python (avoids SQL injection via AGENT_ID)
_HELIOY_DB_PATH="$DB_PATH" \
_HELIOY_AGENT_ID="$AGENT_ID" \
_HELIOY_TOKENS="$TOKENS" \
python3 -c "
import sqlite3, os, json
from datetime import datetime, timezone
now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
usage = json.dumps({'tokens': int(os.environ['_HELIOY_TOKENS']), 'updated': now})
conn = sqlite3.connect(os.environ['_HELIOY_DB_PATH'], timeout=3)
conn.execute(
    'UPDATE agents SET token_usage = ?, last_seen = ? WHERE agent_id = ?',
    (usage, now, os.environ['_HELIOY_AGENT_ID']),
)
conn.commit()
conn.close()
" 2>/dev/null || true

exit 0
