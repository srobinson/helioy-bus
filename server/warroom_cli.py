"""CLI entry point for helioy-warroom.

Implements the same interface as warroom.sh:
  warroom-cli                              # repo-mode: one agent per helioy repo
  warroom-cli <name> "type1 type2 ..."    # role-mode: named window of specialists
  warroom-cli kill <name|all>              # kill warroom(s)
  warroom-cli status                       # list all warroom agents

Calls Python functions directly — no MCP round-trip.
"""

from __future__ import annotations

import os
import sys


def _check_tmux() -> None:
    if not os.environ.get("TMUX"):
        print("error: must be run inside a tmux session", file=sys.stderr)
        sys.exit(1)


def _print_status(statuses: list[dict]) -> None:
    if not statuses:
        print("no warroom agents running")
        return
    for wr in statuses:
        print(f"── {wr['warroom_id']} (window {wr['tmux_window']}) ──")
        for m in wr.get("members", []):
            alive = "alive" if m.get("pane_alive") else "dead"
            registered = "registered" if m.get("registered") else "pending"
            print(f"  {m['tmux_target']}  {m['agent_type']}  [{alive}] [{registered}]")
        print()


def main() -> None:
    args = sys.argv[1:]

    if not args:
        # Repo-mode: spawn one general agent per helioy repo.
        _check_tmux()
        from server.warroom_server import warroom_spawn_repos

        result = warroom_spawn_repos()
        if "error" in result:
            print(f"error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        members = result.get("members", [])
        repos = [os.path.basename(m.get("tmux_target", "").rsplit(":", 1)[0] or "?")
                 for m in members]
        print(f"warroom ready: {len(members)} agents ({' '.join(repos)})")
        return

    if args[0] == "kill":
        targets = args[1:]
        if not targets:
            print("usage: warroom kill <name|all>", file=sys.stderr)
            sys.exit(1)
        from server.warroom_server import warroom_kill

        if targets[0] == "all":
            result = warroom_kill(kill_all=True)
        else:
            result = warroom_kill(name=targets[0])
        if "error" in result:
            print(f"error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        killed = result.get("killed", [])
        if killed:
            for k in killed:
                print(f"killed {k}")
        else:
            print("no warroom windows found")
        return

    if args[0] == "status":
        from server.warroom_server import warroom_status

        statuses = warroom_status()
        _print_status(statuses)
        return

    # Role-mode: warroom <name> "type1 type2 ..."
    if len(args) < 2:
        print('usage: warroom <name> "type1 type2 ..."', file=sys.stderr)
        sys.exit(1)

    _check_tmux()
    name = args[0]
    agent_types = args[1].split()

    from server.warroom_server import warroom_spawn

    result = warroom_spawn(name=name, agents=agent_types)
    if "error" in result:
        print(f"error: {result['error']}", file=sys.stderr)
        for detail in result.get("details", []):
            suggestions = detail.get("suggestions", [])
            suffix = f" (suggestions: {', '.join(suggestions)})" if suggestions else ""
            print(f"  unknown: {detail['agent']}{suffix}", file=sys.stderr)
        sys.exit(1)

    members = result.get("members", [])
    repo = os.path.basename(os.getcwd())
    print(f"{name} ready: {len(members)} agents ({' '.join(agent_types)}) in {repo}")


if __name__ == "__main__":
    main()
