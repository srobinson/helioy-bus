#!/usr/bin/env bash
# lib/resolve-identity.sh: Shared agent identity resolution for helioy-bus hooks
#
# Source this file and call resolve_agent_id() to populate:
#   HELIOY_AGENT_ID:    full agent_id (pane-title derived or basename fallback)
#   HELIOY_AGENT_TYPE:  specialist role, e.g. "general", "backend-engineer"
#   HELIOY_AGENT_REPO:  repository/project name (basename of working directory)
#
# Canonical identity shape (see server/_identity.py for Python-side contract):
#   With tmux:    {repo}:{agent_type}:{session}:{window}.{pane}
#   Without tmux: {repo}:{agent_type}
# Examples:
#   Pane-title (warroom/crew):  fmm:general:7:2.1          (unnamed session)
#   Pane-title (named session): fmm:general:helioy:2.1     (named session)
#   Pane-title (role-mode):     helioy-bus:backend-engineer:7:3.1
#   Fallback (ad-hoc, no tmux): myproject:general
#   Fallback (ad-hoc, tmux):    myproject:general:7:2.1

# Validation regex: repo:type[:subtype]:session:window.pane
# session_name may be a number (unnamed sessions) or an alphanumeric string
# (named sessions like "work" or "helioy"). window.pane are always numeric.
# agent_type may contain colons for namespaced types (e.g. voltagent-lang:rust-engineer).
_IDENTITY_PATTERN='^[a-zA-Z0-9_-]+:[a-zA-Z0-9_:-]+:[a-zA-Z0-9_-]+:[0-9]+\.[0-9]+$'

# Repo name = basename of the project dir (CLAUDE_PROJECT_DIR preferred, PWD else).
_identity_repo() {
    if [[ -n "${CLAUDE_PROJECT_DIR:-}" ]]; then
        basename "$CLAUDE_PROJECT_DIR"
    else
        basename "${PWD:-unknown}"
    fi
}

# Determine the runtime ("claude" | "codex") for this hook invocation.
# Precedence:
#   1. explicit HELIOY_RUNTIME (set by codex-launch.sh / warroom spawn) wins;
#   2. otherwise infer from the SessionStart payload's transcript_path, which
#      lives under the runtime's home dir (~/.codex vs ~/.claude) and, for
#      codex, carries a "rollout-" session filename;
#   3. default to "claude".
# Arg 1: the raw hook stdin JSON (may be empty). Echoes the resolved runtime.
resolve_runtime() {
    if [[ -n "${HELIOY_RUNTIME:-}" ]]; then
        printf '%s' "$HELIOY_RUNTIME"
        return 0
    fi
    case "${1:-}" in
        */.codex/*|*rollout-*) printf 'codex' ;;
        */.claude/*)           printf 'claude' ;;
        *)                     printf 'claude' ;;
    esac
}

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

    # Strip Claude Code's TUI decorations from the pane title.
    # Claude prefixes titles with status icons like "✳ ", "⠐ ", "⠒ ", etc.
    if [[ -n "$title" ]]; then
        title=$(printf '%s' "$title" | sed 's/^[^a-zA-Z0-9_-]* *//')
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

    # Step 2.5: The pane title may contain a bare agent type set by
    # `claude --agent <type>` (e.g. "voltagent-lang:rust-engineer" or
    # "backend-engineer"). Recognize it and construct the full identity.
    # A bare agent type contains only alphanumerics, hyphens, underscores,
    # and colons, but does NOT end with a window.pane suffix.
    #
    # This is a Claude-only convention. Codex has no `--agent` analog and
    # names its pane after the cwd, so a codex title here is the repo name,
    # not a role; honoring it would mint agent_type=<repo>. Gate on runtime
    # so codex falls through to the canonical general fallback (Step 3).
    _BARE_AGENT_TYPE_PATTERN='^[a-zA-Z][a-zA-Z0-9_:-]*[a-zA-Z0-9]$'
    if [[ "${HELIOY_RUNTIME:-claude}" == "claude" ]] \
        && [[ -n "$title" ]] \
        && [[ "$title" != "Claude Code" ]] \
        && printf '%s' "$title" | grep -qE "$_BARE_AGENT_TYPE_PATTERN" \
        && ! printf '%s' "$title" | grep -qE '[0-9]+\.[0-9]+$'; then

        local repo
        repo="$(_identity_repo)"

        HELIOY_AGENT_REPO="$repo"
        HELIOY_AGENT_TYPE="$title"

        if [[ -n "$tmux_target" ]]; then
            HELIOY_AGENT_ID="${repo}:${title}:${tmux_target}"
        else
            HELIOY_AGENT_ID="${repo}:${title}"
        fi

        export HELIOY_AGENT_ID HELIOY_AGENT_TYPE HELIOY_AGENT_REPO
        return 0
    fi

    # Step 2.75: Parse --agent from parent process command line.
    # When `claude --agent voltagent-lang:rust-engineer` is run manually,
    # the pane title isn't set yet at SessionStart time. The process args
    # are the only reliable source of the agent type in this case. Like
    # Step 2.5 this is Claude-specific (`--agent` is a Claude flag), so it
    # only runs for the claude runtime.
    local _cli_agent_type=""
    if [[ "${HELIOY_RUNTIME:-claude}" == "claude" ]]; then
        local _parent_args
        _parent_args=$(ps -p "$PPID" -o args= 2>/dev/null || true)
        if [[ -n "$_parent_args" ]]; then
            # Extract value after --agent (handles both --agent=VAL and --agent VAL)
            _cli_agent_type=$(printf '%s' "$_parent_args" \
                | grep -oE -- '--agent[= ][^ ]+' \
                | head -1 \
                | sed 's/^--agent[= ]//' || true)
        fi
    fi

    if [[ -n "$_cli_agent_type" ]] \
        && printf '%s' "$_cli_agent_type" | grep -qE "$_BARE_AGENT_TYPE_PATTERN"; then

        local repo
        repo="$(_identity_repo)"

        HELIOY_AGENT_REPO="$repo"
        HELIOY_AGENT_TYPE="$_cli_agent_type"

        if [[ -n "$tmux_target" ]]; then
            HELIOY_AGENT_ID="${repo}:${_cli_agent_type}:${tmux_target}"
        else
            HELIOY_AGENT_ID="${repo}:${_cli_agent_type}"
        fi

        export HELIOY_AGENT_ID HELIOY_AGENT_TYPE HELIOY_AGENT_REPO
        return 0
    fi

    # Step 3: Fallback. Derive from CLAUDE_PROJECT_DIR or PWD.
    local repo
    repo="$(_identity_repo)"

    HELIOY_AGENT_REPO="$repo"
    # HELIOY_BUS_AGENT_TYPE overrides the default "general" in fallback mode only.
    HELIOY_AGENT_TYPE="${HELIOY_BUS_AGENT_TYPE:-general}"

    # Canonical 2-segment form when no tmux target. Never bare basename:
    # that legacy shape diverged from register_agent() and _self_agent_id().
    if [[ -n "$tmux_target" ]]; then
        HELIOY_AGENT_ID="${repo}:${HELIOY_AGENT_TYPE}:${tmux_target}"
    else
        HELIOY_AGENT_ID="${repo}:${HELIOY_AGENT_TYPE}"
    fi

    export HELIOY_AGENT_ID HELIOY_AGENT_TYPE HELIOY_AGENT_REPO
}
