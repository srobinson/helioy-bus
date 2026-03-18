#!/usr/bin/env bash
# warroom.sh — Dual-mode agent spawner for the helioy ecosystem.
#
# Usage:
#   warroom.sh                              # repo-mode: one agent per helioy repo
#   warroom.sh <name> "type1 type2 ..."     # role-mode: named window of specialists
#   warroom.sh kill <name>                  # kill one named window
#   warroom.sh kill all                     # kill all warroom windows
#   warroom.sh status                       # list all warroom agents by window
#
# Examples:
#   warroom.sh design "brand-guardian ui-designer visual-storyteller"
#   warroom.sh engineering "senior-developer frontend-engineer"
#   warroom.sh review "clinical-reviewer code-reviewer"
#
# Each named window is idempotent: re-running the same name kills the old
# window and creates a fresh one.
#
# Pane title format: {repo}:{agent_type}:{session}:{window}.{pane}
# This is the source of truth for agent identity resolution in bus hooks.

set -euo pipefail

if [[ -z "${TMUX:-}" ]]; then
    echo "error: must be run inside a tmux session" >&2
    exit 1
fi

BASE="${HELIOY_BASE:-$HOME/Dev/LLM/DEV/helioy}"
CLAUDE="claude --verbose --dangerously-skip-permissions --model opus --effort max"

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
    target="${2:-}"
    if [[ -z "$target" ]]; then
        echo "usage: warroom.sh kill <name|all>" >&2
        exit 1
    fi
    if [[ "$target" == "all" ]]; then
        # Kill repo-mode window
        kill_window "warroom"
        # Kill all role-mode windows by scanning for panes with warroom identity titles
        session=$(tmux display-message -p '#{session_name}')
        tmux list-windows -t "$session" -F '#{window_name}' 2>/dev/null | while read -r wname; do
            # Check if any pane in this window has the repo:type:session:w.p title format
            has_warroom_pane=$(tmux list-panes -t "${session}:${wname}" \
                -F '#{pane_title}' 2>/dev/null \
                | grep -cE '^[^:]+:[^:]+:[^:]+:[0-9]+\.[0-9]+$' || true)
            if [[ "$has_warroom_pane" -gt 0 ]]; then
                kill_window "$wname"
            fi
        done
    else
        kill_window "$target"
    fi
    exit 0
fi

# ── Status ────────────────────────────────────────────────────────────────────

if [[ "${1:-}" == "status" ]]; then
    session=$(tmux display-message -p '#{session_name}')
    found=0

    tmux list-windows -t "$session" -F '#{window_index} #{window_name}' 2>/dev/null | while read -r widx wname; do
        panes=()
        while IFS= read -r line; do
            panes+=("$line")
        done < <(tmux list-panes -t "${session}:${widx}" \
            -F '#{pane_index} #{pane_title} #{pane_pid}' 2>/dev/null)

        # Filter to panes with warroom identity titles
        warroom_panes=()
        for p in "${panes[@]}"; do
            title=$(echo "$p" | awk '{print $2}')
            if [[ "$title" =~ ^[^:]+:[^:]+:[^:]+:[0-9]+\.[0-9]+$ ]]; then
                warroom_panes+=("$p")
            fi
        done

        if [[ ${#warroom_panes[@]} -eq 0 ]]; then
            continue
        fi

        found=1
        echo "── $wname (window $widx) ──"
        for p in "${warroom_panes[@]}"; do
            pane_idx=$(echo "$p" | awk '{print $1}')
            title=$(echo "$p" | awk '{print $2}')
            pid=$(echo "$p" | awk '{print $3}')
            # Parse repo and agent_type from title
            repo=$(echo "$title" | cut -d: -f1)
            agent_type=$(echo "$title" | cut -d: -f2)
            printf "  pane %s  %-20s  %-25s  pid %s\n" "$pane_idx" "$repo" "$agent_type" "$pid"
        done
        echo ""
    done

    if [[ "$found" -eq 0 ]]; then
        echo "no warroom agents running"
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

    # Get session:window_index.pane_index
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
    for (( i=1; i < ${#dirs[@]}; i++ )); do
        pane_id=$(tmux split-window -t "$WINDOW" -c "${dirs[$i]}" -P -F '#{pane_id}')
        setup_pane "$pane_id" "${names[$i]}" "general"
        tmux send-keys -t "$pane_id" "$CLAUDE" Enter
        tmux select-layout -t "$WINDOW" tiled
    done

    tmux select-layout -t "$WINDOW" tiled
    echo "warroom ready: ${#dirs[@]} agents (${names[*]})"

else

    # ── Role-mode ─────────────────────────────────────────────────────────────
    # warroom.sh <window-name> "type1 type2 ..."
    # One pane per agent type, repo = basename($PWD).

    WINDOW="$1"
    shift

    if [[ $# -eq 0 ]]; then
        echo "usage: warroom.sh <window-name> \"type1 type2 ...\"" >&2
        exit 1
    fi

    IFS=' ' read -r -a agent_types <<< "$1"

    if [[ ${#agent_types[@]} -eq 0 ]]; then
        echo "error: no agent types specified" >&2
        exit 1
    fi

    repo="$(basename "$PWD")"
    cwd="$PWD"

    # Idempotent: kill existing window with this name before creating
    tmux kill-window -t "$WINDOW" 2>/dev/null || true

    # Create window with first agent type
    first_pane=$(tmux new-window -n "$WINDOW" -c "$cwd" -P -F '#{pane_id}')
    setup_pane "$first_pane" "$repo" "${agent_types[0]}"
    lock_window_titles "$WINDOW"
    tmux send-keys -t "$first_pane" "$CLAUDE --agent ${agent_types[0]}" Enter

    # Split for remaining agent types
    for (( i=1; i < ${#agent_types[@]}; i++ )); do
        pane_id=$(tmux split-window -t "$WINDOW" -c "$cwd" -P -F '#{pane_id}')
        setup_pane "$pane_id" "$repo" "${agent_types[$i]}"
        tmux send-keys -t "$pane_id" "$CLAUDE --agent ${agent_types[$i]}" Enter
        tmux select-layout -t "$WINDOW" tiled
    done

    tmux select-layout -t "$WINDOW" tiled
    echo "$WINDOW ready: ${#agent_types[@]} agents (${agent_types[*]}) in $repo"

fi
