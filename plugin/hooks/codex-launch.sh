#!/usr/bin/env bash
# codex-launch.sh: Codex pane launcher that registers on the helioy-bus.
#
# Codex has no SessionStart/SessionEnd hook mechanism, so this wrapper
# stands in for it: exports the runtime self-PID env, runs the shared
# register hook, installs an EXIT trap for unregister, then foregrounds
# codex. The wrapper stays alive as codex's parent so the trap fires
# deterministically on exit or signal. Do not use `exec codex`: exec
# replaces the shell and drops the trap.
#
# Emitted as the launch command by the Codex runtime adapter
# (server/runtimes/codex.py::CodexRuntimeAdapter.build_launch_command).

set -euo pipefail

HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The wrapper is the live process; _self_agent_id() looks up
# pids/<HELIOY_BUS_CODEX_PID>. Keying on $$ matches the register hook's
# $PPID when it runs as our direct child subprocess.
export HELIOY_BUS_CODEX_PID=$$

# Reuse the runtime-neutral register hook. It reads stdin JSON for
# session_id; codex has no session id at wrapper-start time, so pass {}.
bash "$HOOKS_DIR/bus-register.sh" <<< '{}'

cleanup() {
    bash "$HOOKS_DIR/bus-unregister.sh" || true
}
trap cleanup EXIT INT TERM

codex --dangerously-bypass-approvals-and-sandbox
