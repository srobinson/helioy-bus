#!/usr/bin/env bash
# bus-register.sh — SessionStart hook for helioy-bus
#
# Registers this Claude Code instance directly into the bus SQLite registry.
# Uses direct DB writes to avoid MCP subprocess overhead in lifecycle hooks.
# Gracefully no-ops if Python or the bus dir is unavailable.
#
# Configured in ~/.claude/settings.json as a SessionStart hook.

set -euo pipefail

BUS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}"
DB_PATH="$BUS_DIR/registry.db"
INBOX_BASE="$BUS_DIR/inbox"

# Agent ID: basename of CLAUDE_PROJECT_DIR
if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
    AGENT_ID="$(basename "$CLAUDE_PROJECT_DIR")"
    PWD_EFFECTIVE="$CLAUDE_PROJECT_DIR"
else
    AGENT_ID="$(basename "${PWD:-unknown}")"
    PWD_EFFECTIVE="${PWD:-}"
fi

# Auto-detect tmux pane target
TMUX_TARGET=""
if [[ -n "${TMUX_PANE:-}" && -n "${TMUX:-}" ]]; then
    # TMUX_PANE is set by tmux (e.g. %0, %1) — convert to session:window.pane
    SESSION=$(tmux display-message -p '#{session_name}' 2>/dev/null || echo "")
    WINDOW=$(tmux display-message -p '#{window_index}' 2>/dev/null || echo "0")
    PANE=$(tmux display-message -p '#{pane_index}' 2>/dev/null || echo "0")
    if [[ -n "$SESSION" ]]; then
        TMUX_TARGET="${SESSION}:${WINDOW}.${PANE}"
    fi
elif [[ -n "${TMUX:-}" ]]; then
    TARGET=$(tmux display-message -p '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || echo "")
    TMUX_TARGET="$TARGET"
fi

# Override from environment if provided
TMUX_TARGET="${HELIOY_BUS_TMUX:-$TMUX_TARGET}"

# Write directly to SQLite (no MCP server needed).
# Values are passed via environment variables to avoid shell injection when
# paths contain quotes or other special characters.
_HELIOY_BUS_DIR="$BUS_DIR" \
_HELIOY_INBOX_BASE="$INBOX_BASE" \
_HELIOY_AGENT_ID="$AGENT_ID" \
_HELIOY_PWD="$PWD_EFFECTIVE" \
_HELIOY_TMUX="$TMUX_TARGET" \
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
        registered_at TEXT NOT NULL,
        last_seen     TEXT NOT NULL
    );
""")

now = datetime.now(timezone.utc).isoformat()
pid = os.getpid()

conn.execute(
    """
    INSERT OR REPLACE INTO agents
        (agent_id, cwd, tmux_target, pid, registered_at, last_seen)
    VALUES (?, ?, ?, ?, ?, ?)
    """,
    (
        os.environ["_HELIOY_AGENT_ID"],
        os.environ["_HELIOY_PWD"],
        os.environ["_HELIOY_TMUX"],
        pid, now, now,
    ),
)
conn.commit()
conn.close()
PYEOF

# Emit empty JSON (hooks require valid JSON or no output)
echo "{}"
