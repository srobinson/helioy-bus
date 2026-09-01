#!/usr/bin/env bash
# bus-unregister.sh: SessionEnd hook for helioy-bus
#
# Removes this runtime instance from the bus registry on session end.
# Uses direct DB writes after Codex or Claude emits SessionEnd.
# Gracefully no-ops if the registry does not exist.

set -euo pipefail

BUS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}"
DB_PATH="$BUS_DIR/registry.db"
PIDS_DIR="$BUS_DIR/pids"

# /clear and compaction end the *conversation*, not the runtime process:
# the same claude keeps running in the same pane and SessionStart re-fires
# immediately. Unregistering here would delete the row that the restart's
# identity-continuity lookup needs, forcing it to mint a fresh id from the
# CURRENT tmux address — which after window re-indexing can be another
# agent's birth id (identity takeover, reproduced live 2026-07-09). Keep
# the registration and the PID file; only true session ends unregister.
# Bounded stdin read: Claude's hook runner can leave stdin open (see
# bus-register.sh), so never block on a plain `cat`.
STDIN_JSON=""
_stdin_ch=""
if IFS= read -r -n 1 -t 1 _stdin_ch; then
    STDIN_JSON="$_stdin_ch"
    while IFS= read -r -n 1 -t 1 _stdin_ch; do
        STDIN_JSON+="$_stdin_ch"
    done
fi
unset _stdin_ch
REASON=$(echo "${STDIN_JSON:-{}}" | jq -r '.reason // empty' 2>/dev/null || true)
SESSION_ID=$(echo "${STDIN_JSON:-{}}" | jq -r '.session_id // empty' 2>/dev/null || true)
if [[ "$REASON" == "clear" || "$REASON" == "compact" ]]; then
    echo "{}"
    exit 0
fi

# Prefer the PID file written at SessionStart. When it is missing, the runtime
# session id is the stable cleanup key and survives hook cwd changes. Identity
# derivation is the final fallback for runtimes that provide neither.
PID_FILE="$PIDS_DIR/$PPID"
AGENT_ID=""
if [[ -f "$PID_FILE" ]]; then
    AGENT_ID="$(cat "$PID_FILE")"
    rm -f "$PID_FILE"
elif [[ -z "$SESSION_ID" ]]; then
    HOOKS_LIB="$(dirname "$0")/lib/resolve-identity.sh"
    # shellcheck source=lib/resolve-identity.sh
    source "$HOOKS_LIB"
    resolve_agent_id
    AGENT_ID="$HELIOY_AGENT_ID"
fi

# Only act if the DB exists
if [[ ! -f "$DB_PATH" ]]; then
    exit 0
fi

# Values passed via environment variables to avoid shell injection when
# paths contain quotes or other special characters.
LOG_DIR="$BUS_DIR/logs"
mkdir -p "$LOG_DIR"

_py_stderr=$(
_HELIOY_DB_PATH="$DB_PATH" \
_HELIOY_AGENT_ID="$AGENT_ID" \
_HELIOY_SESSION_ID="$SESSION_ID" \
python3 - <<'PYEOF' 2>&1
import sqlite3, os
from pathlib import Path

db_path = Path(os.environ["_HELIOY_DB_PATH"])
if not db_path.exists():
    exit(0)

conn = sqlite3.connect(str(db_path), timeout=5)
conn.execute("PRAGMA journal_mode=WAL;")
agent_id = os.environ["_HELIOY_AGENT_ID"]
session_id = os.environ["_HELIOY_SESSION_ID"]
if agent_id:
    conn.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
elif session_id:
    conn.execute("DELETE FROM agents WHERE session_id = ?", (session_id,))
conn.commit()
conn.close()
PYEOF
) || {
    printf '[%s] bus-unregister FAIL agent_id=%s\nstderr: %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" "$AGENT_ID" "$_py_stderr" \
        >> "$LOG_DIR/hook-errors.log"
}

echo "{}"
