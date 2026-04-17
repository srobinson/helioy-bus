#!/usr/bin/env bash
# check-mail.sh: hook context surfacing for helioy-bus
#
# Fires on every matched tool use and prompt submit. Does not drain the
# inbox. It only summarizes unread mail into additionalContext so the
# agent can call get_messages explicitly.
#
# Hook output on messages present:
#   {"hookSpecificOutput": {"hookEventName": "PreToolUse|UserPromptSubmit", "additionalContext": "..."}}
#
# Exit 0 always, never block tool use.
#
# Matcher (for the PreToolUse hook in ~/.claude/settings.json):
#   TodoWrite|ToolSearch|WebFetch|WebSearch|Agent|Read|Write|Edit|Glob|Bash

set -euo pipefail

INBOX_BASE="${HELIOY_BUS_INBOX:-$HOME/.helioy/bus/inbox}"
PIDS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}/pids"

# Prefer PID file written at SessionStart (fast path, avoids tmux call on every tool use).
# Fall back to shared identity resolution when no PID file is present.
PID_FILE="$PIDS_DIR/$PPID"
if [[ -f "$PID_FILE" ]]; then
    AGENT_ID="$(cat "$PID_FILE")"
else
    HOOKS_LIB="$(dirname "$0")/lib/resolve-identity.sh"
    # shellcheck source=lib/resolve-identity.sh
    source "$HOOKS_LIB"
    resolve_agent_id
    AGENT_ID="$HELIOY_AGENT_ID"
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

# Build notification: senders only, do not drain. get_messages does that.
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

CONTEXT="[helioy-bus] ${COUNT} pending message(s) for '${AGENT_ID}' from: ${SENDERS}. Call get_messages to read."

# Read hook input to detect event type. PreToolUse input contains "tool_name",
# UserPromptSubmit input contains "prompt". Default to PreToolUse.
HOOK_INPUT=$(cat /dev/stdin 2>/dev/null || true)
if printf '%s' "$HOOK_INPUT" | grep -q '"prompt"' 2>/dev/null; then
    EVENT_NAME="UserPromptSubmit"
else
    EVENT_NAME="PreToolUse"
fi

# Emit hook response with additionalContext.
# The || exit 0 guard ensures a python3 failure never blocks tool use.
python3 -c "
import json, sys
ctx, event = sys.argv[1], sys.argv[2]
print(json.dumps({
    'hookSpecificOutput': {
        'hookEventName': event,
        'additionalContext': ctx,
    }
}))
" "$CONTEXT" "$EVENT_NAME" || exit 0
