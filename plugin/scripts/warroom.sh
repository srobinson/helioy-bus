#!/usr/bin/env bash
# warroom.sh — Dual-mode agent spawner for the helioy ecosystem.
#
# Usage:
#   warroom.sh                        # repo-mode: one agent per helioy repo
#   warroom.sh "type1 type2 ..."      # role-mode: named specialist agents in $PWD
#   warroom.sh kill                   # kill warroom window
#   warroom.sh kill crew              # kill crew window
#   warroom.sh kill all               # kill both windows
#
# Pane title format: {repo}:{agent_type}:{session}:{window}.{pane}
# This is the source of truth for agent identity resolution in bus hooks.

set -euo pipefail

if [[ -z "${TMUX:-}" ]]; then
    echo "error: must be run inside a tmux session" >&2
    exit 1
fi

BASE="${HELIOY_BASE:-$HOME/Dev/LLM/DEV/helioy}"
CLAUDE="claude --verbose --dangerously-skip-permissions"

# Helioy repos for repo-mode. Format: "name" or "name:path"
REPOS=(
    attention-matters
    fmm
    mdcontext
    nancyr
    helioy-plugins
    "nancy:$HOME/Dev/LLM/DEV/TMP/nancy"
)

# ── Kill ──────────────────────────────────────────────────────────────────────

kill_window() {
    local name="$1"
    tmux kill-window -t "$name" 2>/dev/null && echo "killed $name" || echo "no $name window"
}

if [[ "${1:-}" == "kill" ]]; then
    target="${2:-warroom}"
    if [[ "$target" == "all" ]]; then
        kill_window "warroom"
        kill_window "crew"
    else
        kill_window "$target"
    fi
    exit 0
fi

# ── Pane setup ────────────────────────────────────────────────────────────────
#
# Call AFTER split-window/new-window to set the canonical pane title.
# The title must be set BEFORE send-keys so the identity is stable when
# Claude's SessionStart hook fires and reads the pane title.
#
# Args:
#   $1  pane_id    tmux pane ID, e.g. %7 (from -P -F '#{pane_id}')
#   $2  repo       repository name
#   $3  agent_type specialist role, e.g. "general" or "backend-engineer"

setup_pane() {
    local pane_id="$1"
    local repo="$2"
    local agent_type="$3"

    # Get session:window_index.pane_index — cannot be known before the split.
    local tmux_target
    tmux_target=$(tmux display-message -p -t "$pane_id" \
        '#{session_name}:#{window_index}.#{pane_index}')

    # Canonical identity title
    tmux select-pane -t "$pane_id" -T "${repo}:${agent_type}:${tmux_target}"
}

lock_window_titles() {
    local window_name="$1"
    # Prevent Claude Code from overriding the pane titles we set above.
    tmux set-option -wt "$window_name" allow-rename off 2>/dev/null || true
    tmux set-option -wt "$window_name" allow-set-title off 2>/dev/null || true
}

# ── Mode dispatch ─────────────────────────────────────────────────────────────

if [[ $# -eq 0 ]]; then

    # ── Repo-mode ─────────────────────────────────────────────────────────────
    # One pane per helioy repo, window named "warroom", agent_type = "general".

    WINDOW="warroom"

    dirs=()
    names=()
    for entry in "${REPOS[@]}"; do
        if [[ "$entry" == *:* ]]; then
            name="${entry%%:*}"
            dir="${entry#*:}"
        else
            name="$entry"
            dir="$BASE/$name"
        fi
        if [[ ! -d "$dir" ]]; then
            echo "skipping $name: $dir not found"
            continue
        fi
        dirs+=("$dir")
        names+=("$name")
    done

    if [[ ${#dirs[@]} -eq 0 ]]; then
        echo "error: no repos found under $BASE" >&2
        exit 1
    fi

    # Create window with first repo
    first_pane=$(tmux new-window -n "$WINDOW" -c "${dirs[0]}" -P -F '#{pane_id}')
    setup_pane "$first_pane" "${names[0]}" "general"
    lock_window_titles "$WINDOW"
    tmux send-keys -t "$first_pane" "$CLAUDE" Enter

    # Split for remaining repos
    for i in $(seq 1 $(( ${#dirs[@]} - 1 ))); do
        pane_id=$(tmux split-window -t "$WINDOW" -c "${dirs[$i]}" -P -F '#{pane_id}')
        setup_pane "$pane_id" "${names[$i]}" "general"
        tmux send-keys -t "$pane_id" "$CLAUDE" Enter
        tmux select-layout -t "$WINDOW" tiled
    done

    tmux select-layout -t "$WINDOW" tiled
    echo "warroom ready: ${#dirs[@]} agents (${names[*]})"

else

    # ── Role-mode ─────────────────────────────────────────────────────────────
    # One pane per agent type, window named "crew", repo = basename($PWD).

    WINDOW="crew"

    IFS=' ' read -r -a agent_types <<< "$1"

    if [[ ${#agent_types[@]} -eq 0 ]]; then
        echo "error: no agent types specified" >&2
        exit 1
    fi

    repo="$(basename "$PWD")"
    cwd="$PWD"

    # Create window with first agent type
    first_pane=$(tmux new-window -n "$WINDOW" -c "$cwd" -P -F '#{pane_id}')
    setup_pane "$first_pane" "$repo" "${agent_types[0]}"
    lock_window_titles "$WINDOW"
    tmux send-keys -t "$first_pane" "$CLAUDE --agent helioy-tools:${agent_types[0]}" Enter

    # Split for remaining agent types
    for i in $(seq 1 $(( ${#agent_types[@]} - 1 ))); do
        pane_id=$(tmux split-window -t "$WINDOW" -c "$cwd" -P -F '#{pane_id}')
        setup_pane "$pane_id" "$repo" "${agent_types[$i]}"
        tmux send-keys -t "$pane_id" "$CLAUDE --agent helioy-tools:${agent_types[$i]}" Enter
        tmux select-layout -t "$WINDOW" tiled
    done

    tmux select-layout -t "$WINDOW" tiled
    echo "crew ready: ${#agent_types[@]} agents (${agent_types[*]}) in $repo"

fi
