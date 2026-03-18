#!/usr/bin/env bash
# bus-register.sh — SessionStart hook for helioy-bus
#
# Registers this Claude Code instance directly into the bus SQLite registry.
# Uses direct DB writes to avoid MCP subprocess overhead in lifecycle hooks.
# Gracefully no-ops if Python or the bus dir is unavailable.
#
# Configured in /Users/alphab/Dev/LLM/DEV/helioy/helioy-plugins/plugins/helioy-bus/hooks/hooks.json as a SessionStart hook.

set -euo pipefail

BUS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}"
DB_PATH="$BUS_DIR/registry.db"
INBOX_BASE="$BUS_DIR/inbox"

# Resolve identity via shared lib (pane-title-first, then basename fallback).
# Exports: HELIOY_AGENT_ID, HELIOY_AGENT_TYPE, HELIOY_AGENT_REPO
HOOKS_LIB="$(dirname "$0")/lib/resolve-identity.sh"
# shellcheck source=lib/resolve-identity.sh
source "$HOOKS_LIB"
resolve_agent_id

AGENT_ID="$HELIOY_AGENT_ID"
AGENT_TYPE="$HELIOY_AGENT_TYPE"

# Derive TMUX_TARGET for the registry record (used for nudges).
TMUX_TARGET=""
if [[ -n "${TMUX_PANE:-}" && -n "${TMUX:-}" ]]; then
    TMUX_TARGET="${HELIOY_BUS_TMUX:-$(tmux display-message -p -t "$TMUX_PANE" \
        '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || echo "")}"
fi

# Working directory for this session
if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
    PWD_EFFECTIVE="$CLAUDE_PROJECT_DIR"
else
    PWD_EFFECTIVE="${PWD:-}"
fi

# Session ID: prefer stdin JSON (always available in hooks), fall back to env.
STDIN_JSON=$(cat)
SESSION_ID=$(echo "$STDIN_JSON" | jq -r '.session_id // empty' 2>/dev/null || true)
SESSION_ID="${SESSION_ID:-${HELIOY_SESSION_ID:-${CLAUDE_SESSION_ID:-}}}"

# Write PID → agent_id mapping so hooks and server tools can self-identify
PIDS_DIR="$BUS_DIR/pids"
mkdir -p "$PIDS_DIR"
echo "$AGENT_ID" > "$PIDS_DIR/$PPID"

# Write directly to SQLite (no MCP server needed).
# Values are passed via environment variables to avoid shell injection when
# paths contain quotes or other special characters.
_HELIOY_BUS_DIR="$BUS_DIR" \
_HELIOY_INBOX_BASE="$INBOX_BASE" \
_HELIOY_AGENT_ID="$AGENT_ID" \
_HELIOY_PWD="$PWD_EFFECTIVE" \
_HELIOY_TMUX="$TMUX_TARGET" \
_HELIOY_SESSION_ID="$SESSION_ID" \
_HELIOY_AGENT_TYPE="$AGENT_TYPE" \
python3 - <<PYEOF || true
import sqlite3, os
from datetime import datetime, timezone
from pathlib import Path

bus_dir = Path(os.environ["_HELIOY_BUS_DIR"])
bus_dir.mkdir(parents=True, exist_ok=True)

db_path = bus_dir / "registry.db"
inbox = Path(os.environ["_HELIOY_INBOX_BASE"]) / os.environ["_HELIOY_AGENT_ID"]
inbox.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(str(db_path), timeout=5)
conn.executescript("""
    PRAGMA journal_mode=WAL;
    PRAGMA synchronous=NORMAL;
    CREATE TABLE IF NOT EXISTS agents (
        agent_id      TEXT PRIMARY KEY,
        cwd           TEXT NOT NULL,
        tmux_target   TEXT NOT NULL DEFAULT '',
        pid           INTEGER,
        session_id    TEXT NOT NULL DEFAULT '',
        agent_type    TEXT NOT NULL DEFAULT 'general',
        registered_at TEXT NOT NULL,
        last_seen     TEXT NOT NULL
    );
""")

# Add session_id column if upgrading from older schema
try:
    conn.execute("ALTER TABLE agents ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
except Exception:
    pass  # column already exists

# Add agent_type column if upgrading from older schema
try:
    conn.execute("ALTER TABLE agents ADD COLUMN agent_type TEXT NOT NULL DEFAULT 'general'")
except Exception:
    pass  # column already exists

now = datetime.now(timezone.utc).isoformat()
pid = os.getpid()

conn.execute(
    """
    INSERT OR REPLACE INTO agents
        (agent_id, cwd, tmux_target, pid, session_id, agent_type, registered_at, last_seen)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """,
    (
        os.environ["_HELIOY_AGENT_ID"],
        os.environ["_HELIOY_PWD"],
        os.environ["_HELIOY_TMUX"],
        pid,
        os.environ.get("_HELIOY_SESSION_ID", ""),
        os.environ.get("_HELIOY_AGENT_TYPE", "general"),
        now, now,
    ),
)
conn.commit()
conn.close()
PYEOF

# Emit empty JSON (hooks require valid JSON or no output)
echo "{}"
