#!/usr/bin/env bash
# token-watcher-stop.sh — SessionEnd hook for helioy-bus token tracking
#
# Kills the background token watcher process spawned by token-watcher.sh.
# PID is read from ~/.helioy/bus/pids/${AGENT_ID}.token_watcher.

set -euo pipefail

BUS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}"
PIDS_DIR="$BUS_DIR/pids"

# Resolve agent identity
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

# Kill the watcher process if running
WATCHER_PID_FILE="$PIDS_DIR/${AGENT_ID}.token_watcher"
if [[ -f "$WATCHER_PID_FILE" ]]; then
    WATCHER_PID=$(cat "$WATCHER_PID_FILE" 2>/dev/null || true)
    if [[ -n "$WATCHER_PID" ]] && kill -0 "$WATCHER_PID" 2>/dev/null; then
        # Kill the watcher and its child processes (tail)
        kill -- -"$WATCHER_PID" 2>/dev/null || kill "$WATCHER_PID" 2>/dev/null || true
    fi
    rm -f "$WATCHER_PID_FILE"
fi

echo "{}"
