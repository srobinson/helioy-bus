#!/usr/bin/env bash
# bus-prune.sh — Proactive registry cleanup triggered by tmux hooks
#
# Removes agents whose tmux panes no longer exist and cleans stale PID files.
# Called by tmux after-kill-pane and window-unlinked hooks (installed by
# bus-register.sh on SessionStart). Also safe to run manually.
#
# Self-contained: uses direct sqlite3 via Python, no repo imports needed.
# Runs in the tmux server environment (not a pane env), so only $HOME
# and standard vars are available.

set -euo pipefail

BUS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}"
DB_PATH="$BUS_DIR/registry.db"
PIDS_DIR="$BUS_DIR/pids"

# TMUX_BIN is injected by the hook installer (bus-register.sh) because
# tmux's run-shell has a minimal PATH that may not include /opt/homebrew/bin.
# Fall back to PATH lookup for manual invocations.
TMUX_BIN="${TMUX_BIN:-$(command -v tmux 2>/dev/null || true)}"
[[ -n "$TMUX_BIN" ]] || exit 0

[[ -f "$DB_PATH" ]] || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

# Prune agents whose tmux panes no longer exist.
# Uses parameterized queries (no string interpolation) for safety.
_HELIOY_DB="$DB_PATH" _TMUX_BIN="$TMUX_BIN" python3 - <<'PYEOF' 2>/dev/null || true
import sqlite3, subprocess, os

db_path = os.environ["_HELIOY_DB"]
tmux_bin = os.environ["_TMUX_BIN"]
conn = sqlite3.connect(db_path, timeout=5)
conn.execute("PRAGMA journal_mode=WAL")

rows = conn.execute(
    "SELECT agent_id, tmux_target, pid FROM agents"
).fetchall()

dead = []
for agent_id, tmux_target, pid in rows:
    if tmux_target:
        try:
            r = subprocess.run(
                [tmux_bin, "list-panes", "-t", tmux_target],
                capture_output=True, timeout=3,
            )
            if r.returncode != 0:
                dead.append(agent_id)
        except Exception:
            dead.append(agent_id)
    elif pid:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            dead.append(agent_id)

if dead:
    placeholders = ",".join("?" * len(dead))
    conn.execute(f"DELETE FROM agents WHERE agent_id IN ({placeholders})", dead)
    conn.commit()

conn.close()
PYEOF

# Prune PID files for processes that no longer exist.
if [[ -d "$PIDS_DIR" ]]; then
    for pid_file in "$PIDS_DIR"/*; do
        [[ -f "$pid_file" ]] || continue
        pid_num="${pid_file##*/}"
        [[ "$pid_num" =~ ^[0-9]+$ ]] || continue
        if ! kill -0 "$pid_num" 2>/dev/null; then
            rm -f "$pid_file"
        fi
    done
fi
