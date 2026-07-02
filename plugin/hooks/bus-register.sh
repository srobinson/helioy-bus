#!/usr/bin/env bash
# bus-register.sh: SessionStart hook for helioy-bus
#
# Registers this runtime instance directly into the bus SQLite registry.
# Uses direct DB writes to avoid MCP subprocess overhead in lifecycle hooks.
# Gracefully no-ops if Python or the bus dir is unavailable.
#
# Configured in /Users/alphab/Dev/LLM/DEV/helioy/helioy-plugins/plugins/helioy-bus/hooks/hooks.json as a SessionStart hook.

set -euo pipefail

BUS_DIR="${HELIOY_BUS_DIR:-$HOME/.helioy/bus}"
INBOX_BASE="$BUS_DIR/inbox"

# Resolve identity via shared lib (pane-title-first, then basename fallback).
# Exports: HELIOY_AGENT_ID, HELIOY_AGENT_TYPE, HELIOY_AGENT_REPO
HOOKS_LIB="$(dirname "$0")/lib/resolve-identity.sh"
# shellcheck source=lib/resolve-identity.sh
source "$HOOKS_LIB"

# Read the hook payload once, up front. Both runtime inference and session_id
# extraction need it, and the runtime must be known BEFORE identity resolution:
# codex names its pane after the cwd, which the Claude `--agent` branch would
# otherwise mistake for an agent type. resolve_runtime honors an explicit
# HELIOY_RUNTIME (runtime-launch.sh / warroom) before inferring from the payload.
# Claude's hook runner can leave stdin open on some startup paths. A plain
# `cat` would then block SessionStart forever, so read only immediately
# available payload bytes and fall back to an empty object.
STDIN_JSON=""
_stdin_ch=""
if IFS= read -r -n 1 -t 1 _stdin_ch; then
    STDIN_JSON="$_stdin_ch"
    while IFS= read -r -n 1 -t 1 _stdin_ch; do
        STDIN_JSON+="$_stdin_ch"
    done
fi
unset _stdin_ch
STDIN_JSON="${STDIN_JSON:-{}}"
HELIOY_RUNTIME="$(resolve_runtime "$STDIN_JSON")"
export HELIOY_RUNTIME

resolve_agent_id

AGENT_ID="$HELIOY_AGENT_ID"
AGENT_TYPE="$HELIOY_AGENT_TYPE"
RUNTIME="$HELIOY_RUNTIME"

# Derive TMUX_TARGET for the registry record (used for nudges).
TMUX_TARGET=""
if [[ -n "${TMUX_PANE:-}" && -n "${TMUX:-}" ]]; then
    TMUX_TARGET="${HELIOY_BUS_TMUX:-$(tmux display-message -p -t "$TMUX_PANE" \
        '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || echo "")}"
fi

# Working directory for this session. Shared resolver (resolve-identity.sh):
# CLAUDE_PROJECT_DIR, then the codex launch-cwd pin HELIOY_BUS_CWD, then PWD.
PWD_EFFECTIVE="$(_identity_project_dir)"

# Session ID: prefer stdin JSON (always available in hooks), fall back to env.
SESSION_ID=$(echo "$STDIN_JSON" | jq -r '.session_id // empty' 2>/dev/null || true)
SESSION_ID="${SESSION_ID:-${HELIOY_SESSION_ID:-${CLAUDE_SESSION_ID:-}}}"

# Write PID → agent_id mapping so hooks and server tools can self-identify
PIDS_DIR="$BUS_DIR/pids"
mkdir -p "$PIDS_DIR"
echo "$AGENT_ID" > "$PIDS_DIR/$PPID"

# Derive the helioy-bus repo root so server._db is importable.
# BASH_SOURCE resolution follows symlinks to the real script location.
# CLAUDE_PLUGIN_ROOT points to the plugin cache (not the repo), so we ignore it.
# HELIOY_BUS_PYTHON_PATH is an explicit override that always wins.
_script="${BASH_SOURCE[0]}"
while [[ -L "$_script" ]]; do
    _dir="$(cd "$(dirname "$_script")" && pwd)"
    _target="$(readlink "$_script")"
    [[ "$_target" != /* ]] && _target="$_dir/$_target"
    _script="$_target"
done
_BASH_SOURCE_ROOT="$(cd "$(dirname "$_script")/../.." && pwd)"
unset _script _dir _target

# Priority: explicit override > BASH_SOURCE (always correct for absolute hook paths).
# CLAUDE_PLUGIN_ROOT points to the plugin cache, not the repo, so never use it.
if [[ -n "${HELIOY_BUS_PYTHON_PATH:-}" ]]; then
    HELIOY_BUS_ROOT="$HELIOY_BUS_PYTHON_PATH"
else
    HELIOY_BUS_ROOT="$_BASH_SOURCE_ROOT"
fi
unset _BASH_SOURCE_ROOT

# Write directly to SQLite via _db.py (single source of truth for schema).
# All values passed through environment variables, never interpolated
# into Python source, to prevent injection when paths contain special chars.
LOG_DIR="$BUS_DIR/logs"
mkdir -p "$LOG_DIR"
PY_STDERR=$(mktemp)
PY_TIMEOUT_SECONDS="${HELIOY_BUS_REGISTER_TIMEOUT_SECONDS:-3}"
if ! [[ "$PY_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || [[ "$PY_TIMEOUT_SECONDS" -lt 1 ]]; then
    PY_TIMEOUT_SECONDS=3
fi

PYTHON_BIN="${HELIOY_BUS_PYTHON:-}"
if [[ -z "$PYTHON_BIN" && -x "$HELIOY_BUS_ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$HELIOY_BUS_ROOT/.venv/bin/python"
elif [[ -z "$PYTHON_BIN" && -x /usr/bin/python3 ]]; then
    PYTHON_BIN="/usr/bin/python3"
elif [[ -z "$PYTHON_BIN" ]]; then
    PYTHON_BIN="$(command -v python3 || true)"
fi

REGISTER_SCRIPT="$HELIOY_BUS_ROOT/plugin/hooks/lib/register_agent.py"

_run_registration() {
    _HELIOY_BUS_DIR="$BUS_DIR" \
    _HELIOY_INBOX_BASE="$INBOX_BASE" \
    _HELIOY_AGENT_ID="$AGENT_ID" \
    _HELIOY_PWD="$PWD_EFFECTIVE" \
    _HELIOY_TMUX="$TMUX_TARGET" \
    _HELIOY_PANE_ID="${TMUX_PANE:-}" \
    _HELIOY_SESSION_ID="$SESSION_ID" \
    _HELIOY_AGENT_TYPE="$AGENT_TYPE" \
    _HELIOY_RUNTIME="$RUNTIME" \
    _HELIOY_PID="$PPID" \
    HELIOY_BUS_ROOT="$HELIOY_BUS_ROOT" \
    "$PYTHON_BIN" "$REGISTER_SCRIPT" 2>"$PY_STDERR"
}

set +e
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
    printf 'python3 unavailable for bus-register\n' > "$PY_STDERR"
    PY_EXIT=127
elif [[ ! -f "$REGISTER_SCRIPT" ]]; then
    printf 'register helper missing: %s\n' "$REGISTER_SCRIPT" > "$PY_STDERR"
    PY_EXIT=127
else
    _run_registration &
    PY_PID=$!
    PY_EXIT=0
    _deadline=$((SECONDS + PY_TIMEOUT_SECONDS))
    while kill -0 "$PY_PID" 2>/dev/null; do
        if [[ "$SECONDS" -ge "$_deadline" ]]; then
            kill "$PY_PID" 2>/dev/null || true
            sleep 0.2
            kill -9 "$PY_PID" 2>/dev/null || true
            wait "$PY_PID" 2>/dev/null || true
            printf 'registration timed out after %ss\n' "$PY_TIMEOUT_SECONDS" > "$PY_STDERR"
            PY_EXIT=124
            break
        fi
        sleep 0.1
    done
    if [[ $PY_EXIT -eq 0 ]]; then
        wait "$PY_PID"
        PY_EXIT=$?
    fi
fi

if [[ $PY_EXIT -ne 0 ]]; then
    printf '[%s] bus-register FAIL agent_id=%s exit=%d\nstderr: %s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" "$AGENT_ID" "$PY_EXIT" \
        "$(cat "$PY_STDERR")" >> "$LOG_DIR/hook-errors.log"
fi
rm -f "$PY_STDERR"
set -e

# Prune stale PID files for processes that no longer exist.
# Runs on every SessionStart to prevent unbounded growth.
for pid_file in "$PIDS_DIR"/*; do
    [[ -f "$pid_file" ]] || continue
    pid_num="${pid_file##*/}"
    # Skip non-numeric filenames (e.g. .token_watcher artifacts)
    [[ "$pid_num" =~ ^[0-9]+$ ]] || continue
    # Skip our own entry
    [[ "$pid_num" == "$PPID" ]] && continue
    # Remove if the process no longer exists
    if ! kill -0 "$pid_num" 2>/dev/null; then
        rm -f "$pid_file"
    fi
done

# Install tmux hooks for proactive registry cleanup on kill-pane/kill-window.
# Uses indexed array slots [99] to avoid clobbering user hooks.
# Idempotent: re-setting the same index overwrites the previous value.
# Passes TMUX_BIN explicitly because run-shell has a minimal PATH.
if [[ -n "${TMUX:-}" ]]; then
    PRUNE_SCRIPT="$HELIOY_BUS_ROOT/plugin/hooks/bus-prune.sh"
    TMUX_BIN="$(command -v tmux)"
    tmux set-hook -g 'after-kill-pane[99]' \
        "run-shell \"TMUX_BIN=$TMUX_BIN $PRUNE_SCRIPT\"" 2>/dev/null || true
    tmux set-hook -g 'window-unlinked[99]' \
        "run-shell \"TMUX_BIN=$TMUX_BIN $PRUNE_SCRIPT\"" 2>/dev/null || true
fi

# Emit empty JSON (hooks require valid JSON or no output)
echo "{}"
