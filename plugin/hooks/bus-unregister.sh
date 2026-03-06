#!/usr/bin/env bash
# bus-unregister.sh — SessionEnd hook for helioy-bus
#
# Removes this Claude Code instance from the bus registry on session end.
# Uses direct DB writes — Claude is no longer active when SessionEnd fires.
# Gracefully no-ops if the registry does not exist.
#
# Configured in ~/.claude/settings.json as a SessionEnd hook.

set -euo pipefail

BUS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}"
DB_PATH="$BUS_DIR/registry.db"

# Agent ID: basename of CLAUDE_PROJECT_DIR
if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
    AGENT_ID="$(basename "$CLAUDE_PROJECT_DIR")"
else
    AGENT_ID="$(basename "${PWD:-unknown}")"
fi

# Only act if the DB exists
if [[ ! -f "$DB_PATH" ]]; then
    exit 0
fi

# Values passed via environment variables to avoid shell injection when
# paths contain quotes or other special characters.
_HELIOY_DB_PATH="$DB_PATH" \
_HELIOY_AGENT_ID="$AGENT_ID" \
python3 - <<PYEOF || true
import sqlite3, os
from pathlib import Path

db_path = Path(os.environ["_HELIOY_DB_PATH"])
if not db_path.exists():
    exit(0)

conn = sqlite3.connect(str(db_path), timeout=5)
conn.execute("PRAGMA journal_mode=WAL;")
conn.execute("DELETE FROM agents WHERE agent_id = ?", (os.environ["_HELIOY_AGENT_ID"],))
conn.commit()
conn.close()
PYEOF

echo "{}"
