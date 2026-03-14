#!/usr/bin/env bash
# lib/resolve-identity.sh — Shared agent identity resolution for helioy-bus hooks
#
# Source this file and call resolve_agent_id() to populate:
#   HELIOY_AGENT_ID    — full agent_id (pane-title derived or basename fallback)
#   HELIOY_AGENT_TYPE  — specialist role, e.g. "general", "backend-engineer"
#   HELIOY_AGENT_REPO  — repository/project name (basename of working directory)
#
# Identity format: {repo}:{agent_type}:{session}:{window}.{pane}
# Examples:
#   Pane-title (warroom/crew):  fmm:general:7:2.1          (unnamed session)
#   Pane-title (named session): fmm:general:helioy:2.1     (named session)
#   Pane-title (role-mode):     helioy-bus:backend-engineer:7:3.1
#   Fallback (ad-hoc, no tmux): myproject
#   Fallback (ad-hoc, tmux):    myproject:general:7:2.1

# Validation regex: repo:type[:subtype]:session:window.pane
# session_name may be a number (unnamed sessions) or an alphanumeric string
# (named sessions like "work" or "helioy"). window.pane are always numeric.
# agent_type may contain colons for namespaced types (e.g. voltagent-lang:rust-engineer).
_IDENTITY_PATTERN='^[a-zA-Z0-9_-]+:[a-zA-Z0-9_:-]+:[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$'

resolve_agent_id() {
    local title=""
    local tmux_target=""

    # Step 1: Try to read pane title when inside a tmux session.
    if [[ -n "${TMUX_PANE:-}" && -n "${TMUX:-}" ]]; then
        title=$(tmux display-message -p -t "$TMUX_PANE" '#{pane_title}' 2>/dev/null || true)
        # Override tmux target from env if set (warroom / crew may inject this)
        if [[ -n "${HELIOY_BUS_TMUX:-}" ]]; then
            tmux_target="$HELIOY_BUS_TMUX"
        else
            tmux_target=$(tmux display-message -p -t "$TMUX_PANE" \
                '#{session_name}:#{window_index}.#{pane_index}' 2>/dev/null || true)
        fi
    fi

    # Step 2: If the pane title matches the canonical identity pattern, use it
    # as the source of truth for agent_id and agent_type.
    if [[ -n "$title" ]] && printf '%s' "$title" | grep -qE "$_IDENTITY_PATTERN"; then
        HELIOY_AGENT_ID="$title"
        # Parse from both ends: repo is first segment, session:window.pane
        # is the last two segments, agent_type is everything in between.
        HELIOY_AGENT_REPO="${title%%:*}"
        # Strip trailing :session:window.pane (last two colon-segments)
        local _without_wp="${title%:*}"       # drop :window.pane
        local _without_swp="${_without_wp%:*}" # drop :session
        # agent_type = everything between repo: and :session
        HELIOY_AGENT_TYPE="${_without_swp#*:}"
        export HELIOY_AGENT_ID HELIOY_AGENT_TYPE HELIOY_AGENT_REPO
        return 0
    fi

    # Step 3: Fallback — derive from CLAUDE_PROJECT_DIR or PWD.
    local repo
    if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
        repo="$(basename "$CLAUDE_PROJECT_DIR")"
    else
        repo="$(basename "${PWD:-unknown}")"
    fi

    HELIOY_AGENT_REPO="$repo"
    # HELIOY_BUS_AGENT_TYPE overrides the default "general" in fallback mode only.
    HELIOY_AGENT_TYPE="${HELIOY_BUS_AGENT_TYPE:-general}"

    if [[ -n "$tmux_target" ]]; then
        HELIOY_AGENT_ID="${repo}:${HELIOY_AGENT_TYPE}:${tmux_target}"
    else
        HELIOY_AGENT_ID="$repo"
    fi

    export HELIOY_AGENT_ID HELIOY_AGENT_TYPE HELIOY_AGENT_REPO
}
