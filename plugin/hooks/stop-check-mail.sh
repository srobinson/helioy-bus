#!/usr/bin/env bash
# stop-check-mail.sh — Stop hook for helioy-bus
#
# Fires when Claude finishes a response and is about to go idle.
# If the agent has unread messages, blocks the stop and injects the
# messages as the reason, keeping Claude active to process them.
#
# Hook output on messages present:
#   {"decision": "block", "reason": "..."}
#   Exit code 2 (block)
#
# When no messages:
#   Exit code 0 (allow stop)

set -euo pipefail

INBOX_BASE="${HELIOY_BUS_INBOX:-$HOME/.helioy/bus/inbox}"
PIDS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}/pids"

# Resolve agent identity (same as check-mail.sh)
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

# No inbox or no messages? Allow stop.
if [[ ! -d "$MAILBOX" ]]; then
    exit 0
fi

mapfile -t MSG_FILES < <(find "$MAILBOX" -maxdepth 1 -name "*.json" -type f | sort)

if [[ ${#MSG_FILES[@]} -eq 0 ]]; then
    exit 0
fi

# Build notification with sender info
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

REASON="[helioy-bus] You have ${COUNT} unread message(s) from: ${SENDERS}. Use the mail skill to read and respond."

# Block stop, keep Claude active to process mail.
python3 -c "
import json, sys
reason = sys.stdin.read()
print(json.dumps({
    'decision': 'block',
    'reason': reason,
}))
" <<< "$REASON" || exit 0

exit 2
