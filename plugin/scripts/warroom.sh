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
# Install with: uv sync (in the helioy-bus project directory).

exec helioy-warroom-cli "$@"
