#!/usr/bin/env bash
# check-mail.sh — PreToolUse hook for helioy-bus
#
# Fires on every matched tool use. Drains the agent's inbox and injects
# pending messages into Claude's context via additionalContext.
#
# Hook output on messages present:
#   {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "..."}}
#
# Exit 0 always — never block tool use.
#
# Matcher (in ~/.claude/settings.json):
#   TodoWrite|ToolSearch|WebFetch|WebSearch|Agent|Read|Write|Edit|Glob|Bash

set -euo pipefail

INBOX_BASE="${HELIOY_BUS_INBOX:-$HOME/.helioy/bus/inbox}"
PIDS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}/pids"

# Resolve agent_id from PID file written at SessionStart
PID_FILE="$PIDS_DIR/$PPID"
if [[ -f "$PID_FILE" ]]; then
    AGENT_ID="$(cat "$PID_FILE")"
elif [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
    AGENT_ID="$(basename "$CLAUDE_PROJECT_DIR")"
else
    AGENT_ID="$(basename "${PWD:-unknown}")"
fi

MAILBOX="$INBOX_BASE/$AGENT_ID"

# No inbox? Nothing to do.
if [[ ! -d "$MAILBOX" ]]; then
    exit 0
fi

# Collect unread message files (*.json, sorted lexicographically = arrival order)
mapfile -t MSG_FILES < <(find "$MAILBOX" -maxdepth 1 -name "*.json" -type f | sort)

if [[ ${#MSG_FILES[@]} -eq 0 ]]; then
    exit 0
fi

# Build notification — senders only, do NOT drain. get_messages does that.
SENDERS=""
COUNT=0

for f in "${MSG_FILES[@]}"; do
    if command -v jq &>/dev/null; then
        FROM=$(jq -r '.from // "unknown"' "$f" 2>/dev/null || echo "unknown")
    else
        FROM="unknown"
    fi
    SENDERS="${SENDERS:+$SENDERS, }${FROM}"
    COUNT=$((COUNT + 1))
done

if [[ $COUNT -eq 0 ]]; then
    exit 0
fi

CONTEXT="[helioy-bus] ${COUNT} pending message(s) for '${AGENT_ID}' from: ${SENDERS} — call get_messages to read."

# Emit hook response with additionalContext.
# The || exit 0 guard ensures a python3 failure never blocks tool use.
python3 -c "
import json, sys
ctx = sys.stdin.read()
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': 'PreToolUse',
        'additionalContext': ctx,
    }
}))
" <<< "$CONTEXT" || exit 0
