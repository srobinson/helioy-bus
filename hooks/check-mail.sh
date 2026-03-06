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

# Agent ID: basename of CLAUDE_PROJECT_DIR (matches registration convention)
if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
    AGENT_ID="$(basename "$CLAUDE_PROJECT_DIR")"
else
    AGENT_ID="$(basename "${PWD:-unknown}")"
fi

MAILBOX="$INBOX_BASE/$AGENT_ID"
ARCHIVE="$MAILBOX/archive"

# No inbox? Nothing to do.
if [[ ! -d "$MAILBOX" ]]; then
    exit 0
fi

# Collect unread message files (*.json, sorted lexicographically = arrival order)
mapfile -t MSG_FILES < <(find "$MAILBOX" -maxdepth 1 -name "*.json" -type f | sort)

if [[ ${#MSG_FILES[@]} -eq 0 ]]; then
    exit 0
fi

mkdir -p "$ARCHIVE"

# Build context block
LINES=""
COUNT=0

for f in "${MSG_FILES[@]}"; do
    if command -v jq &>/dev/null; then
        FROM=$(jq -r '.from // "unknown"' "$f" 2>/dev/null || echo "unknown")
        CONTENT=$(jq -r '.content // ""' "$f" 2>/dev/null || echo "")
        SENT_AT=$(jq -r '.sent_at // ""' "$f" 2>/dev/null || echo "")
        # Project: derive from 'from' agent's name (same as agent_id)
        PROJECT="$FROM"
    else
        FROM="unknown"
        CONTENT="$(cat "$f")"
        SENT_AT=""
        PROJECT="unknown"
    fi

    if [[ -n "$CONTENT" ]]; then
        if [[ -n "$SENT_AT" ]]; then
            LINES="${LINES}[helioy-bus] Message from ${FROM} (${PROJECT}) at ${SENT_AT}:
${CONTENT}

"
        else
            LINES="${LINES}[helioy-bus] Message from ${FROM} (${PROJECT}):
${CONTENT}

"
        fi
        COUNT=$((COUNT + 1))
    fi

    # Archive the message after reading (move, not delete)
    mv "$f" "$ARCHIVE/" 2>/dev/null || true
done

if [[ $COUNT -eq 0 ]]; then
    exit 0
fi

HEADER="[helioy-bus] ${COUNT} pending message(s) for '${AGENT_ID}':"
CONTEXT="${HEADER}

${LINES}"

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
