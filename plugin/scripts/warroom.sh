#!/usr/bin/env bash
# warroom.sh — thin wrapper over helioy-warroom-cli.
#
# Usage:
#   warroom.sh                              # repo-mode: one agent per helioy repo
#   warroom.sh <name> "type1 type2 ..."     # role-mode: named window of specialists
#   warroom.sh kill <name|all>              # kill warroom(s)
#   warroom.sh status                       # list all warroom agents
#
# Delegates to the helioy-warroom-cli Python entry point.
# Resolves the project root via symlink-safe BASH_SOURCE so this works
# when the script is symlinked from the plugin cache.

_script="${BASH_SOURCE[0]}"
while [[ -L "$_script" ]]; do
    _dir="$(cd "$(dirname "$_script")" && pwd)"
    _target="$(readlink "$_script")"
    [[ "$_target" != /* ]] && _target="$_dir/$_target"
    _script="$_target"
done
_PROJECT_ROOT="$(cd "$(dirname "$_script")/../.." && pwd)"

exec "$_PROJECT_ROOT/.venv/bin/helioy-warroom-cli" "$@"
