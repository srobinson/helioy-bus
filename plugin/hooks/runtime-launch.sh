#!/usr/bin/env bash
# runtime-launch.sh: pane launcher for runtimes without lifecycle hooks.
#
# Usage: runtime-launch.sh <runtime_id> <pid_env_name> <command> [args...]
#
# Stands in for SessionStart/SessionEnd hooks on runtimes that do not run
# them (codex has no hook system; grok discovers Claude plugin hooks but
# does not execute them): exports the runtime self-PID env, runs the shared
# register hook, installs an EXIT trap for unregister, then foregrounds the
# runtime command. The wrapper stays alive as the runtime's parent so the
# trap fires deterministically on exit or signal. Do not `exec` the final
# command: exec replaces the shell and drops the trap.
#
# Emitted as the launch command by wrapper-kind runtime adapters
# (server/runtimes/codex.py, server/runtimes/grok.py).

set -euo pipefail

if [[ $# -lt 3 ]]; then
    echo "usage: runtime-launch.sh <runtime_id> <pid_env_name> <command> [args...]" >&2
    exit 64
fi

RUNTIME_ID="$1"
PID_ENV_NAME="$2"
shift 2

HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOK_SHELL="${HELIOY_BUS_HOOK_SHELL:-/bin/bash}"

# The wrapper is the live process; _self_agent_id() looks up
# pids/<pid_env value>. Keying on $$ matches the register hook's $PPID when
# it runs as our direct child subprocess.
export "$PID_ENV_NAME=$$"
export HELIOY_RUNTIME="$RUNTIME_ID"
# Pin the launch cwd (the warroom/repo dir). Some runtimes re-run hooks
# chdir'd elsewhere (codex's `memories` feature re-runs bus-register from
# ~/.codex/memories); without this pin the agent would re-register with that
# directory's basename as its agent_id prefix. bus-register.sh prefers
# HELIOY_BUS_CWD.
export HELIOY_BUS_CWD="$PWD"

cleanup() {
    "$HOOK_SHELL" "$HOOKS_DIR/bus-unregister.sh" || true
}
# HUP matters: tmux kill-window/kill-session deliver SIGHUP, and bash
# skips the EXIT trap on an untrapped fatal signal, so without it every
# warroom teardown leaks the agent registration. Installed BEFORE the
# register hook so a kill that lands mid-registration still unregisters
# (unregister of an absent row is a no-op).
trap cleanup EXIT INT TERM HUP

# Reuse the runtime-neutral register hook. It reads stdin JSON for
# session_id; wrapper-kind runtimes have no session id at wrapper-start
# time, so pass {}.
"$HOOK_SHELL" "$HOOKS_DIR/bus-register.sh" <<< '{}'

"$@"
