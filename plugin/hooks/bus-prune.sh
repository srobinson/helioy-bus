#!/usr/bin/env bash
# bus-prune.sh: Proactive registry cleanup triggered by tmux hooks
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

# Prune agents whose tmux panes no longer exist, and refresh drifted
# tmux_target values while we are here: this hook fires the instant a
# kill re-indexes the surviving windows, which is exactly when stored
# session:window.pane addresses go stale. Liveness and identity key on
# the stable pane_id (%N); the address is derived from one list-panes
# snapshot. Semantics mirror server/services/reconciliation.py
# (sync_pane_addresses + prune_dead_agents) — duplicated deliberately
# because this runs in the tmux server env where the repo venv (and a
# modern python) may be unavailable, so it must stay stdlib-3.9-safe
# and self-contained.
# Uses parameterized queries (no string interpolation) for safety.
_HELIOY_DB="$DB_PATH" _TMUX_BIN="$TMUX_BIN" python3 - <<'PYEOF' 2>/dev/null || true
import sqlite3, subprocess, os

db_path = os.environ["_HELIOY_DB"]
tmux_bin = os.environ["_TMUX_BIN"]

# One-call pane snapshot: {pane_id: current session:window.pane}.
snapshot = {}
snapshot_ok = False
try:
    r = subprocess.run(
        [tmux_bin, "list-panes", "-a", "-F",
         "#{pane_id} #{session_name}:#{window_index}.#{pane_index}"],
        capture_output=True, timeout=3,
    )
    if r.returncode == 0:
        snapshot_ok = True
        for line in r.stdout.decode().splitlines():
            pane_id, _, target = line.strip().partition(" ")
            if pane_id and target:
                snapshot[pane_id] = target
except Exception:
    pass

conn = sqlite3.connect(db_path, timeout=5)
conn.execute("PRAGMA journal_mode=WAL")

rows = conn.execute(
    "SELECT agent_id, tmux_target, pane_id, pid FROM agents"
).fetchall()

live_targets = set(snapshot.values())
dead = []
for agent_id, tmux_target, pane_id, pid in rows:
    if pane_id:
        # A failed snapshot means "cannot verify", never "all dead".
        if not snapshot_ok:
            continue
        current = snapshot.get(pane_id)
        if current is None:
            dead.append(agent_id)
        elif current != tmux_target:
            conn.execute(
                "UPDATE agents SET tmux_target = ? WHERE agent_id = ?",
                (current, agent_id),
            )
    elif tmux_target:
        # Legacy rows without pane_id keep address-based liveness.
        if snapshot_ok and tmux_target not in live_targets:
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

# Mark warrooms killed when their tmux window no longer exists.
# Uses parameterized queries (no string interpolation) for safety.
_HELIOY_DB="$DB_PATH" _TMUX_BIN="$TMUX_BIN" python3 - <<'PYEOF' 2>/dev/null || true
import sqlite3, subprocess, os

db_path = os.environ["_HELIOY_DB"]
tmux_bin = os.environ["_TMUX_BIN"]
conn = sqlite3.connect(db_path, timeout=5)
conn.execute("PRAGMA journal_mode=WAL")

rows = conn.execute(
    "SELECT warroom_id, tmux_session, tmux_window FROM warrooms WHERE status = 'active'"
).fetchall()

dead = []
for warroom_id, tmux_session, tmux_window in rows:
    try:
        r = subprocess.run(
            [tmux_bin, "list-panes", "-t", f"{tmux_session}:{tmux_window}"],
            capture_output=True, timeout=3,
        )
        if r.returncode != 0:
            dead.append(warroom_id)
    except Exception:
        dead.append(warroom_id)

if dead:
    placeholders = ",".join("?" * len(dead))
    conn.execute(
        f"UPDATE warrooms SET status = 'killed' "
        f"WHERE status = 'active' AND warroom_id IN ({placeholders})",
        dead,
    )
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
